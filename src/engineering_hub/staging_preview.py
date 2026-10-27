"""Explicit isolated preview capabilities; never a browser-session proxy.

The caller authenticates/CSRF-checks ticket issuance and owns the fixed artifact
reader. No worker URL, browser cookie, Access assertion or agent token is passed
to that reader. This module installs no route, listener, policy or background job.
Deployers MUST redact /p/<capability>/ paths in every HTTP/access log.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, Response

from .preview_protocol import _origin, _PATH, _MIME, _DIGEST
from .repository import HubError, HubRepository
from .staging_resources import StagingResourceStore


_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_BINDING = ('id', 'project', 'job_id', 'artifact_digest', 'revision')


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 253402300799:
        raise HubError('preview_unavailable', 403)
    return value


def _timestamp(value):
    try:
        if type(value) is not str or len(value) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return _number(parsed.timestamp())
    except (ValueError, OverflowError):
        raise HubError('preview_unavailable', 403) from None


def _resource_deadline(resource):
    deadline = _timestamp(resource.get('expires_at'))
    keep = resource.get('keep')
    if keep:
        if keep.get('mode') == 'pinned':
            return 253402300798.0
        if keep.get('mode') == 'until':
            deadline = max(deadline, _timestamp(keep.get('until')))
    return deadline


class PreviewBroker:
    def __init__(self, repo, resources, *, hub_origin, preview_origin,
                 session_alive, clock=time.time, ttl=300, max_file_bytes=8 * 1024**2):
        if (type(repo) is not HubRepository or type(resources) is not StagingResourceStore
                or resources.repo is not repo or not callable(session_alive) or not callable(clock)
                or type(ttl) is not int or not 1 <= ttl <= 300
                or type(max_file_bytes) is not int or not 1 <= max_file_bytes <= 8 * 1024**2):
            raise ValueError('Invalid preview configuration')
        self.hub_origin, self.preview_origin = _origin(hub_origin), _origin(preview_origin)
        if self.hub_origin == self.preview_origin:
            raise ValueError('Preview requires a different HTTPS origin')
        self.repo, self.resources = repo, resources
        self.session_alive, self.clock = session_alive, clock
        self.ttl, self.max_file_bytes = ttl, max_file_bytes
        with repo._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hub_staging_preview_tickets (token_hash TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def _now(self):
        return _number(self.clock())

    def _resource(self, actor, identity):
        resource, policy_revision = self.resources.preview_binding(actor, identity)
        if (type(resource) is not dict or resource.get('id') != identity
                or resource.get('kind') != 'preview' or resource.get('state') != 'ready'
                or type(resource.get('revision')) is not int or resource['revision'] < 1
                or type(policy_revision) is not int or policy_revision < 1
                or type(resource.get('project')) is not str or not resource['project']
                or type(resource.get('job_id')) is not str or not resource['job_id']
                or type(resource.get('artifact_digest')) is not str
                or not _DIGEST.fullmatch(resource['artifact_digest'])
                or any(p.get('reason') == 'active_job' for p in resource.get('protected', []))
                or _resource_deadline(resource) <= self._now()):
            raise HubError('preview_unavailable', 403)
        return resource, policy_revision

    def issue(self, actor, resource_id, session_hash, session_expires):
        """Trusted human+CSRF composition only; returned URL is a bearer secret."""
        self.repo._actor(actor, human=True)
        now = self._now()
        if (type(session_hash) is not str or not _HASH.fullmatch(session_hash)
                or _number(session_expires) <= now
                or self.session_alive(actor, session_hash) is not True):
            raise HubError('preview_unavailable', 403)
        resource, revision = self._resource(actor, resource_id)
        expires = min(now + self.ttl, session_expires, _resource_deadline(resource))
        token = secrets.token_urlsafe(32)
        binding = {key: resource[key] for key in _BINDING}
        binding.update(actor=actor, session_hash=session_hash, policy_revision=revision, expires=expires)
        with self.repo._transaction() as db:
            # Tickets are disposable capabilities, not an ever-growing audit
            # history. Retain current tickets and remove only expired hashes.
            db.execute("DELETE FROM hub_staging_preview_tickets WHERE json_extract(data,'$.expires')<=?", (now,))
            db.execute('INSERT INTO hub_staging_preview_tickets VALUES (?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), json.dumps(binding, sort_keys=True, allow_nan=False)))
        return {'resource_id': resource_id,
                'preview_url': self.preview_origin + '/p/' + token + '/index.html',
                'expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat()}

    def _resolve(self, token):
        if type(token) is not str or not _TOKEN.fullmatch(token):
            raise HubError('preview_unavailable', 403)
        with self.repo._connection() as db:
            row = db.execute('SELECT data FROM hub_staging_preview_tickets WHERE token_hash=?',
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise HubError('preview_unavailable', 403)
        binding = json.loads(row['data'])
        if (binding['expires'] <= self._now()
                or self.session_alive(binding['actor'], binding['session_hash']) is not True):
            raise HubError('preview_unavailable', 403)
        resource, revision = self._resource(binding['actor'], binding['id'])
        if revision != binding['policy_revision'] or any(binding[key] != resource[key] for key in _BINDING):
            raise HubError('preview_unavailable', 403)
        return resource

    def _headers(self, token):
        prefix = self.preview_origin + '/p/' + token + '/'
        csp = ('sandbox allow-scripts; default-src \'none\'; '
               f'script-src {prefix} \'unsafe-inline\'; style-src {prefix} \'unsafe-inline\'; '
               f'connect-src {prefix}; img-src {prefix} data: blob:; font-src {prefix} data:; '
               f'frame-ancestors {self.hub_origin}; '
               'object-src \'none\'; frame-src \'none\'; worker-src \'none\'; '
               'base-uri \'none\'; form-action \'none\'')
        return {'Content-Security-Policy': csp, 'Access-Control-Allow-Origin': '*',
                'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff'}

    def app(self, fetch_file):
        """Fixed async reader(resource_id, relative_path)->(bytes, media_type)."""
        if not callable(fetch_file):
            raise ValueError('Explicit preview artifact reader required')

        async def gateway(scope, receive, send):
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
                return
            if scope['type'] != 'http':
                raise RuntimeError('Preview gateway has no independent lifespan')
            try:
                headers = scope.get('headers', [])
                hosts = [value for name, value in headers if name.lower() == b'host']
                if hosts != [urlsplit(self.preview_origin).netloc.encode()]:
                    raise HubError('preview_unavailable', 403)
                path = scope.get('path', '')
                if (scope.get('method') not in ('GET', 'HEAD') or scope.get('query_string', b'')
                        or type(path) is not str or not path.isascii()
                        or scope.get('raw_path', path.encode()) != path.encode()):
                    raise HubError('preview_unavailable', 403)
                matched = re.fullmatch(r'/p/([A-Za-z0-9_-]{43})/(.+)', path)
                if matched is None:
                    raise HubError('preview_unavailable', 403)
                token, relative = matched.groups()
                if (not _PATH.fullmatch(relative) or any(part in ('', '.', '..') for part in relative.split('/'))
                        or relative.rsplit('.', 1)[-1].lower() not in _MIME):
                    raise HubError('preview_unavailable', 403)
                resource = self._resolve(token)
                async with asyncio.timeout(15):
                    content, media_type = await fetch_file(resource['id'], relative)
                # Revocation, expiry, cleanup or a new revision during worker I/O
                # cannot turn the already-open response into stale permission.
                self._resolve(token)
                expected = _MIME[relative.rsplit('.', 1)[-1].lower()]
                if (type(content) is not bytes or len(content) > self.max_file_bytes
                        or type(media_type) is not str or media_type not in expected):
                    raise HubError('preview_unavailable', 403)
                response = Response(content if scope['method'] == 'GET' else b'',
                                    media_type=expected[0], headers=self._headers(token))
                if scope['method'] == 'HEAD':
                    response.headers['content-length'] = str(len(content))
            except Exception:
                # No request URLs, tokens, worker details or credential values.
                response = JSONResponse({'detail': {'code': 'preview_unavailable'}}, status_code=404,
                    headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                             'X-Content-Type-Options': 'nosniff'})
            await response(scope, receive, send)

        return gateway
