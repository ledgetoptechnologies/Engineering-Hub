"""Optional, explicitly granted interactive reviews and private immutable assets.

This adapter runs no supplied code and forwards neither browser cookies nor
agent credentials. An absent/bad optional file leaves ordinary Hub work intact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import hashlib
from pathlib import Path
import re
import ssl
from uuid import UUID

import httpx
from fastapi import Request
from starlette.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .repository import HubError, HubRepository
from .lifecycle import finish_owned
from .staging_client import _origin, _read_file, _unique


ACTION = frozenset({'review_read', 'review_create', 'review_answer'})
TYPES = frozenset({'application/json', 'image/jpeg', 'image/png',
                  'model/gltf-binary', 'application/octet-stream'})
MAX_RANGE = 8 * 1024 * 1024


class _AssetResponse(StreamingResponse):
    """Own the acquired socket even when sending headers/body is cancelled."""
    def __init__(self, *args, upstream, **kwargs):
        super().__init__(*args, **kwargs)
        self.upstream = upstream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await finish_owned(self.upstream.aclose())


@dataclass
class InteractiveService:
    grants: dict = field(default_factory=dict, repr=False)
    assets: dict = field(default_factory=dict, repr=False)
    client: object = field(default=None, repr=False)
    state: str = 'not_configured'

    def pin_assets(self, repo):
        """UUIDs are permanently tied to metadata; removed entries revoke access.

        Retaining tiny fingerprints prevents a future config edit from silently
        replacing evidence attached to an existing review after a restart.
        """
        if not self.assets:
            return
        try:
            with repo._transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS hub_interactive_asset_bindings '
                           '(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)')
                for identity, metadata in self.assets.items():
                    fingerprint = hashlib.sha256(json.dumps(metadata, sort_keys=True,
                        separators=(',', ':')).encode()).hexdigest()
                    prior = db.execute('SELECT fingerprint FROM hub_interactive_asset_bindings WHERE id=?',
                                       (identity,)).fetchone()
                    if prior is not None and prior['fingerprint'] != fingerprint:
                        raise ValueError('asset_identity_reused')
                    db.execute('INSERT OR IGNORE INTO hub_interactive_asset_bindings VALUES(?,?)',
                               (identity, fingerprint))
        except Exception:
            self.assets = {}
            self.state = 'asset_binding_invalid'

    def authorize(self, actor, project, action):
        HubRepository._actor(actor)
        if action not in ACTION or action not in self.grants.get(project, {}).get(actor, []):
            raise HubError('interactive_forbidden', 403)
        return True

    def asset_authorize(self, actor, project, component_id, asset_id):
        # The repository checks the operation-specific grant before this binding.
        HubRepository._actor(actor)
        asset = self.assets.get(asset_id)
        return bool(asset and asset['project'] == project and asset['component_id'] == component_id)

    async def close(self):
        if self.client is not None:
            await self.client.aclose()


def compose_interactive(path, *, client_factory=httpx.AsyncClient, test_transport=None):
    """Read only operator-owned config; grants are explicit actor/project pairs."""
    service = InteractiveService()
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return service
    try:
        value = json.loads(_read_file(path, 1024 * 1024), object_pairs_hook=_unique)
        if (type(value) is not dict or value.get('schema') != 1
                or set(value) - {'schema', 'enabled', 'grants', 'assets', 'origin', 'ca_file', 'credential_file'}
                or type(value.get('enabled')) is not bool):
            raise ValueError()
        if not value['enabled']:
            service.state = 'disabled'
            return service
        grants = value.get('grants', {})
        if type(grants) is not dict or len(grants) > 1000:
            raise ValueError()
        for project, actors in grants.items():
            if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', project) or type(actors) is not dict or len(actors) > 100:
                raise ValueError()
            for actor, actions in actors.items():
                HubRepository._actor(actor)
                if type(actions) is not list or any(a not in ACTION for a in actions):
                    raise ValueError()
        assets = value.get('assets', {})
        if type(assets) is not dict or len(assets) > 10000:
            raise ValueError()
        for key, asset in assets.items():
            if str(UUID(key)) != key or type(asset) is not dict or set(asset) != {
                    'project', 'component_id', 'size', 'sha256', 'media_type'}:
                raise ValueError()
            if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', asset['project'])
                    or str(UUID(asset['component_id'])) != asset['component_id']
                    or type(asset['size']) is not int or not 0 < asset['size'] <= 2 * 1024**3
                    or not re.fullmatch(r'[a-f0-9]{64}', asset['sha256']) or asset['media_type'] not in TYPES):
                raise ValueError()
        if assets:
            origin = _origin(value['origin'])
            # Real asset transport requires TLS, even when its peer is private.
            if not origin.startswith('https://') and test_transport is None:
                raise ValueError()
            token = _read_file(Path(value['credential_file']), 4096, private=True,
                               synthetic_windows=test_transport is not None).decode('ascii').strip()
            if not 32 <= len(token) <= 4096 or any(not 33 <= ord(c) <= 126 for c in token):
                raise ValueError()
            tls = ssl.create_default_context(cadata=_read_file(Path(value['ca_file']), 128 * 1024).decode('ascii')) if origin.startswith('https://') else True
            service.client = client_factory(base_url=origin, headers={'Authorization': 'Bearer ' + token},
                verify=tls, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(15, connect=3),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2), transport=test_transport)
        service.grants, service.assets, service.state = grants, assets, 'configured'
    except Exception:
        service.state = 'invalid'
    return service


def byte_range(value, size):
    """Canonical single bounded range. Large data must use chunks."""
    if value is None:
        if size > MAX_RANGE:
            raise HubError('interactive_range_required', 416)
        return 0, size - 1, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
    if match is None or not any(match.groups()):
        raise HubError('interactive_invalid_range', 416)
    left, right = match.groups()
    if len(left) > 12 or len(right) > 12:
        raise HubError('interactive_invalid_range', 416)
    if left:
        start, end = int(left), min(int(right), size - 1) if right else size - 1
    else:
        suffix = int(right)
        if suffix == 0:
            raise HubError('interactive_invalid_range', 416)
        start, end = max(0, size - suffix), size - 1
    if start >= size or start > end or end - start + 1 > MAX_RANGE:
        raise HubError('interactive_invalid_range', 416)
    return start, end, True


def attach_interactive_assets(app, service, reviews, human):
    @app.head('/api/hub/v1/interactive-reviews/{review_id}/assets/{asset_id}', include_in_schema=False)
    @app.get('/api/hub/v1/interactive-reviews/{review_id}/assets/{asset_id}',
             response_class=Response,
             responses={status: {'description': description, 'content': {
                 media: {'schema': {'type': 'string', 'format': 'binary'}}
                 for media in sorted(TYPES)}} for status, description in
                 ((200, 'Complete asset'), (206, 'Partial asset'))})
    async def asset(review_id: str, asset_id: str, request: Request):
        def admitted():
            actor = human(request)
            review = reviews.get_review(actor, review_id)
            metadata = service.assets.get(asset_id)
            if (metadata is None or metadata['project'] != review['project']
                    or metadata['component_id'] != review['component_id']
                    or not any(item.get('asset_id') == asset_id for item in review['items'])):
                raise HubError('interactive_asset_not_found', 404)
            return metadata
        metadata = admitted()
        if service.client is None:
            raise HubError('interactive_assets_unavailable', 503)
        size = metadata['size']
        if request.method == 'HEAD':
            start, end, partial = 0, size - 1, False
        else:
            start, end, partial = byte_range(request.headers.get('range'), size)
        headers = {'Cache-Control': 'private, no-store', 'Accept-Ranges': 'bytes',
                   'ETag': '"sha256:' + metadata['sha256'] + '"', 'X-Content-Type-Options': 'nosniff',
                   'Content-Length': str(end - start + 1)}
        if partial:
            headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        if request.method == 'HEAD':
            return Response(headers=headers, media_type=metadata['media_type'])
        upstream = None
        try:
            # A direct Request bypasses client cookie/default-header merging.
            # Only the dedicated private service credential is admitted here.
            private_headers = {'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'}
            credential = service.client.headers.get('authorization')
            if credential:
                private_headers['Authorization'] = credential
            outgoing = httpx.Request('GET', service.client.base_url.join('/v1/assets/' + asset_id),
                                     headers=private_headers)
            upstream = await service.client.send(outgoing, stream=True, auth=None, follow_redirects=False)
            if (upstream.status_code != 206
                    or upstream.headers.get('content-range') != f'bytes {start}-{end}/{size}'
                    or upstream.headers.get('content-length') != str(end - start + 1)
                    or upstream.headers.get('etag') != headers['ETag']
                    or upstream.headers.get('content-type') != metadata['media_type']
                    or 'set-cookie' in upstream.headers
                    or upstream.headers.get('content-encoding') not in (None, 'identity')):
                raise ValueError()
            admitted()
        except BaseException as error:
            if upstream is not None:
                await upstream.aclose()
            if not isinstance(error, Exception):
                raise
            raise HubError('interactive_assets_unavailable', 503) from None

        async def body():
            remaining = end - start + 1
            try:
                async for chunk in upstream.aiter_raw(chunk_size=65536):
                    admitted()  # Logout, project revocation and replacement apply mid-stream.
                    if len(chunk) > remaining:
                        raise ValueError('invalid_asset_length')
                    remaining -= len(chunk)
                    yield chunk
                if remaining:
                    raise ValueError('invalid_asset_length')
            finally:
                await upstream.aclose()
        return _AssetResponse(body(), upstream=upstream, status_code=206 if partial else 200,
                                 headers=headers, media_type=metadata['media_type'],
                                 background=BackgroundTask(upstream.aclose))
