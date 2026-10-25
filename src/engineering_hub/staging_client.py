"""Explicit private staging connection; no discovery, retries or background work.

Configuration/status errors are deliberately opaque. Credentials are read only
when a client is explicitly constructed, never by optional config discovery.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import stat
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .repository import HubError, HubRepository


_NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_COMMIT = re.compile(r'[a-f0-9]{40}\Z')
_RELATIVE = re.compile(r'[A-Za-z0-9_@./-]{1,512}\Z')
_KINDS = frozenset({'workspace', 'build', 'test_run', 'preview'})
_FILE_TYPES = {
    'html': ('text/html',), 'htm': ('text/html',), 'js': ('text/javascript', 'application/javascript'),
    'mjs': ('text/javascript', 'application/javascript'), 'css': ('text/css',),
    'json': ('application/json',), 'txt': ('text/plain',), 'svg': ('image/svg+xml',),
    'png': ('image/png',), 'jpg': ('image/jpeg',), 'jpeg': ('image/jpeg',),
    'webp': ('image/webp',), 'gif': ('image/gif',), 'ico': ('image/x-icon', 'image/vnd.microsoft.icon'),
    'woff': ('font/woff',), 'woff2': ('font/woff2',), 'ttf': ('font/ttf',),
    'otf': ('font/otf',), 'wasm': ('application/wasm',),
}


class StagingClientError(HubError):
    def __init__(self, code='staging_connection_unavailable', status=503):
        super().__init__(code, status)


_NOT_ACCEPTED = {
    '/v1/jobs': {
        409: frozenset({'worker_busy_or_held', 'worker_storage_capacity', 'resource_conflict'}),
        403: frozenset({'project_not_allowed', 'recipe_not_allowed', 'peer_not_allowed'}),
        401: frozenset({'unauthorized'}),
    },
    '/v1/cleanup': {
        409: frozenset({'worker_busy', 'cleanup_revision_or_reference', 'resource_job_not_terminal'}),
        403: frozenset({'peer_not_allowed'}),
        401: frozenset({'unauthorized'}),
    },
}
_REJECTION_REASONS = frozenset(reason for statuses in _NOT_ACCEPTED.values()
                               for reasons in statuses.values() for reason in reasons)


class StagingNotAccepted(StagingClientError):
    """Known rejection of THIS POST, not proof an older operation never ran.

    Controllers must retain the original dispatch/ownership history. This
    subtype is admitted only on configured private HTTPS or explicit synthetic
    MockTransport, never a real plaintext loopback response. It permits no
    retry, reinterpretation of a lost acknowledgment, or
    replacing a local preflight-only ``not dispatched`` proof.
    """
    def __init__(self, reason):
        if type(reason) is not str or reason not in _REJECTION_REASONS:
            raise StagingClientError() from None
        self.reason = reason
        super().__init__('staging_worker_rejected', 409)


def _check(condition):
    if not condition:
        raise StagingClientError('staging_connection_invalid', 400)


def _opaque(method):
    @wraps(method)
    async def call(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except StagingClientError:
            raise
        except Exception:
            raise StagingClientError() from None
    return call


def _uuid(value):
    try:
        _check(type(value) is str and str(UUID(value)) == value)
    except (ValueError, TypeError, AttributeError):
        raise StagingClientError('staging_connection_invalid', 400) from None
    return value


def _relative(value):
    _check(type(value) is str and _RELATIVE.fullmatch(value) is not None
           and all(part not in ('', '.', '..') for part in value.split('/')))
    return value


def _origin(value):
    try:
        _check(type(value) is str and value.isascii())
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname)
        port = parsed.port
        _check('%' not in parsed.hostname and not parsed.username and not parsed.password and not parsed.path
               and not parsed.query and not parsed.fragment and parsed.netloc == parsed.netloc.lower()
               and value == parsed.scheme + '://' + parsed.netloc
               and (port is None or 1 <= port <= 65535)
               and (parsed.scheme == 'https' and address.is_private
                    and not address.is_unspecified and not address.is_multicast
                    or parsed.scheme == 'http' and str(address) in {'127.0.0.1', '::1'}))
    except (ValueError, TypeError, AttributeError):
        raise StagingClientError('staging_connection_invalid', 400) from None
    return value


def _path_reference(value, *, optional=False):
    if optional and value is None:
        return None
    _check(isinstance(value, (str, Path)) and str(value) and '\x00' not in str(value))
    path = Path(value)
    _check(path.is_absolute() and '..' not in path.parts)
    return path


@dataclass(frozen=True)
class StagingConnectionConfig:
    origin: str = field(repr=False)
    credential_file: Path | str = field(repr=False)
    ca_file: Path | str | None = field(default=None, repr=False)
    admin_actors: frozenset[str] = frozenset()
    preview_origin: str | None = None
    enabled: bool = False
    auto_run: bool = False
    projects: object = field(default_factory=dict, repr=False)
    schema: int = 1

    def __post_init__(self):
        _check(type(self.schema) is int and self.schema == 1
               and type(self.enabled) is bool and type(self.auto_run) is bool
               and (not self.auto_run or self.enabled)
               and type(self.admin_actors) is frozenset)
        _origin(self.origin)
        object.__setattr__(self, 'credential_file', _path_reference(self.credential_file))
        object.__setattr__(self, 'ca_file', _path_reference(self.ca_file, optional=True))
        _check(not self.origin.startswith('https://') or self.ca_file is not None)
        for actor in self.admin_actors:
            try:
                HubRepository._actor(actor, human=True)
            except HubError:
                raise StagingClientError('staging_connection_invalid', 400) from None
        if self.preview_origin is not None:
            from .staging_preview import _origin as preview_origin
            try:
                preview_origin(self.preview_origin)
            except ValueError:
                raise StagingClientError('staging_connection_invalid', 400) from None
        _check(type(self.projects) in (dict, MappingProxyType) and len(self.projects) <= 100)
        normalized = {}
        for name, value in self.projects.items():
            _check(type(name) is str and _NAME.fullmatch(name) is not None
                   and type(value) in (dict, MappingProxyType) and set(value) == {'recipes'}
                   and type(value['recipes']) in (list, tuple) and 0 < len(value['recipes']) <= 32
                   and all(type(recipe) is str and _NAME.fullmatch(recipe) for recipe in value['recipes'])
                   and len(set(value['recipes'])) == len(value['recipes']))
            normalized[name] = MappingProxyType({'recipes': tuple(value['recipes'])})
        object.__setattr__(self, 'projects', MappingProxyType(normalized))


def _read_file(path, maximum, *, private=False, synthetic_windows=False):
    """Read the checked descriptor, not a second path lookup after validation."""
    descriptor = None
    try:
        path = _path_reference(path)
        before = path.lstat()
        _check(stat.S_ISREG(before.st_mode) and before.st_size <= maximum)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        actual = os.fstat(descriptor)
        _check(stat.S_ISREG(actual.st_mode) and actual.st_size <= maximum
               and (actual.st_dev, actual.st_ino) == (before.st_dev, before.st_ino)
               and actual.st_nlink == 1)
        if os.name == 'posix':
            _check(actual.st_uid in {0, os.getuid()}
                   and not stat.S_IMODE(actual.st_mode) & (0o077 if private else 0o022))
        elif private:
            # Production cannot infer Windows ACL safety from POSIX mode bits.
            # MockTransport is an explicit synthetic-only testing boundary.
            _check(synthetic_windows)
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            data = stream.read(maximum + 1)
        _check(len(data) <= maximum)
        return data
    except (OSError, ValueError, TypeError):
        raise StagingClientError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate')
        value[key] = item
    return value


def load_optional_config(path):
    """Return (configuration-or-None, safe status); never read credentials."""
    try:
        target = _path_reference(path)
        if not target.exists() and not target.is_symlink():
            return None, {'state': 'not_configured', 'enabled': False}
        data = json.loads(_read_file(target, 65536), object_pairs_hook=_unique)
        allowed = {'schema', 'origin', 'credential_file', 'ca_file', 'admin_actors',
                   'preview_origin', 'enabled', 'auto_run', 'projects'}
        _check(type(data) is dict and not set(data) - allowed and data.get('schema') == 1
               and type(data.get('admin_actors', [])) is list)
        data['admin_actors'] = frozenset(data.get('admin_actors', []))
        config = StagingConnectionConfig(**data)
        return config, {'state': 'configured' if config.enabled else 'disabled', 'enabled': config.enabled}
    except Exception:
        return None, {'state': 'invalid', 'enabled': False}


def _shape(value, keys):
    _check(type(value) is dict and set(value) == set(keys))


def _key(value):
    _check(type(value) is str and 0 < len(value) <= 128
           and all(ord(char) >= 32 and ord(char) != 127 for char in value))


class StagingClient:
    MAX_RESPONSE_BYTES = 16 * 1024**2
    MAX_UPLOAD_BYTES = 12 * 1024**2

    def __init__(self, config, *, test_transport=None):
        _check(type(config) is StagingConnectionConfig and config.enabled)
        _check(test_transport is None or type(test_transport) is httpx.MockTransport)
        try:
            raw = _read_file(config.credential_file, 4096, private=True,
                             synthetic_windows=test_transport is not None)
            token = raw.decode('ascii').strip()
            _check(16 <= len(token) <= 4096 and all(33 <= ord(char) <= 126 for char in token))
            context = True
            if config.origin.startswith('https://'):
                certificate = _read_file(config.ca_file, 128 * 1024)
                context = ssl.create_default_context(cadata=certificate.decode('ascii'))
            self._token, self.config = token, config
            self._definitive_rejections = (config.origin.startswith('https://')
                                          or type(test_transport) is httpx.MockTransport)
            self._client = httpx.AsyncClient(transport=test_transport, verify=context,
                trust_env=False, follow_redirects=False, timeout=15,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
        except Exception:
            raise StagingClientError() from None

    @_opaque
    async def aclose(self):
        try:
            await self._client.aclose()
        finally:
            self._token = ''

    def _project(self, project, recipe=None):
        _check(type(project) is str and project in self.config.projects)
        if recipe is not None:
            _check(recipe in self.config.projects[project]['recipes'])

    @staticmethod
    def _source(payload):
        _check(type(payload['source_digest']) is str and _SHA.fullmatch(payload['source_digest']))
        if 'source_id' in payload:
            _uuid(payload['source_id'])
        if 'source_zip_base64' in payload:
            encoded = payload['source_zip_base64']
            _check(type(encoded) is str and len(encoded) <= ((8 * 1024**2 + 2) // 3) * 4)
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                raise StagingClientError('staging_connection_invalid', 400) from None
            _check(len(raw) <= 8 * 1024**2 and hashlib.sha256(raw).hexdigest() == payload['source_digest'])
            return len(raw)

    async def _request(self, method, path, payload=None, *, file_types=None, maximum=None):
        if self._client.is_closed:
            raise StagingClientError()
        response = None
        response_limit = self.MAX_RESPONSE_BYTES if maximum is None else maximum
        try:
            content = None if payload is None else json.dumps(payload, separators=(',', ':'), allow_nan=False).encode()
            _check(content is None or len(content) <= self.MAX_UPLOAD_BYTES)
            timeout = 30.0 if path == '/v1/sources' else 15.0
            headers = {'Authorization': 'Bearer ' + self._token, 'Accept-Encoding': 'identity',
                       'Accept': '*/*' if file_types else 'application/json'}
            if content is not None:
                headers['Content-Type'] = 'application/json'
            # No client cookies/default headers, redirects or external URL input.
            request = httpx.Request(method, self.config.origin + path, headers=headers, content=content,
                extensions={'timeout': dict(connect=15.0, read=timeout, write=timeout, pool=15.0)})
            async with asyncio.timeout(timeout):
                response = await self._client.send(request, auth=None, follow_redirects=False, stream=True)
                _check('set-cookie' not in response.headers and 'location' not in response.headers
                       and response.headers.get('content-encoding', 'identity') == 'identity'
                       and all(self._token not in value for value in response.headers.values()))
                media = response.headers.get('content-type', '')
                if file_types:
                    _check(media in file_types)
                else:
                    _check(media in {'application/json', 'application/json; charset=utf-8'})
                length = response.headers.get('content-length')
                _check(length is None or re.fullmatch(r'0|[1-9][0-9]{0,8}', length)
                       and int(length) <= response_limit)
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    _check(len(data) + len(chunk) <= response_limit)
                    data.extend(chunk)
                if length is not None:
                    _check(len(data) == int(length))
                _check(self._token.encode() not in data)
                if file_types:
                    _check(response.status_code == 200)
                    return bytes(data), media
                result = json.loads(data, object_pairs_hook=_unique)
                _check(type(result) is dict)
                if (self._definitive_rejections and method == 'POST'
                        and path in _NOT_ACCEPTED and set(result) == {'error'}):
                    reason = result['error']
                    allowed = _NOT_ACCEPTED[path].get(response.status_code, frozenset())
                    if type(reason) is str and reason in allowed:
                        raise StagingNotAccepted(reason)
                _check(response.status_code in ({200, 202} if path == '/v1/jobs' else {200}))
                return result
        except StagingNotAccepted:
            raise
        except Exception:
            raise StagingClientError() from None
        finally:
            if response is not None:
                await response.aclose()
            self._client.cookies.clear()

    @_opaque
    async def status(self):
        result = await self._request('GET', '/v1/status')
        _shape(result, {'schema', 'active_command_id', 'projects'})
        _check(result['schema'] == 'hub-staging.v1' and type(result['projects']) is dict
               and len(result['projects']) <= 100)
        if result['active_command_id'] is not None:
            _uuid(result['active_command_id'])
        for name, recipes in result['projects'].items():
            _check(type(name) is str and _NAME.fullmatch(name) and type(recipes) is list
                   and 0 < len(recipes) <= 32
                   and all(type(recipe) is str and _NAME.fullmatch(recipe) for recipe in recipes)
                   and len(set(recipes)) == len(recipes))
        return result

    @_opaque
    async def runtime(self):
        from .staging_runtime import validate_runtime_snapshot
        return validate_runtime_snapshot(await self._request('GET', '/v1/runtime', maximum=128 * 1024))

    @_opaque
    async def put_source(self, payload):
        _shape(payload, {'source_id', 'project', 'source_digest', 'source_zip_base64'})
        _uuid(payload['source_id']); self._project(payload['project'])
        size = self._source(payload)
        result = await self._request('POST', '/v1/sources', payload)
        _shape(result, {'source_id', 'source_digest', 'size_bytes', 'project'})
        _check(all(result[key] == payload[key] for key in ('source_id', 'source_digest', 'project'))
               and type(result['size_bytes']) is int and result['size_bytes'] == size)
        return result

    @staticmethod
    def _operation(result, *, job_id=None, command_id=None, cleanup=False):
        _shape(result, {'command_id', 'job_id', 'state', 'result'})
        _uuid(result['command_id'])
        if cleanup:
            _check(result['job_id'] is None)
        else:
            _uuid(result['job_id'])
        _check((job_id is None or result['job_id'] == job_id)
               and (command_id is None or result['command_id'] == command_id)
               and type(result['state']) is str
               and result['state'] in {'accepted', 'running', 'held', 'failed', 'succeeded'}
               and type(result['result']) is dict)
        details = result['result']
        _check(not set(details) - {'error', 'stages', 'preview_ready', 'source_commit',
               'source_commit_verification', 'source_digest', 'resource_ids', 'deleted_resource_ids'})
        if 'error' in details:
            _check(type(details['error']) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,127}', details['error']))
        if 'preview_ready' in details:
            _check(type(details['preview_ready']) is bool)
        if 'stages' in details:
            _shape(details['stages'], {'build', 'test'})
            for stage in details['stages'].values():
                _shape(stage, {'exit_code', 'log_bytes'})
                _check(type(stage['exit_code']) is int and -255 <= stage['exit_code'] <= 255
                       and type(stage['log_bytes']) is int and 0 <= stage['log_bytes'] <= 1024**2)
        if 'source_commit' in details:
            _check(type(details['source_commit']) is str and _COMMIT.fullmatch(details['source_commit']))
        if 'source_commit_verification' in details:
            _check(details['source_commit_verification'] == 'declared')
        if 'source_digest' in details:
            _check(type(details['source_digest']) is str and _SHA.fullmatch(details['source_digest']))
        if 'resource_ids' in details:
            _shape(details['resource_ids'], _KINDS)
            identifiers = [_uuid(item) for item in details['resource_ids'].values()]
            _check(len(set(identifiers)) == len(identifiers))
        if 'deleted_resource_ids' in details:
            _check(type(details['deleted_resource_ids']) is list and len(details['deleted_resource_ids']) <= 4)
            identifiers = [_uuid(item) for item in details['deleted_resource_ids']]
            _check(len(set(identifiers)) == len(identifiers))
        return result

    @_opaque
    async def submit(self, payload):
        required = {'job_id', 'command_id', 'idempotency_key', 'project', 'recipe',
                    'source_commit', 'source_digest', 'resource_ids'}
        _check(type(payload) is dict and set(payload) in (required | {'source_id'}, required | {'source_zip_base64'}))
        _uuid(payload['job_id']); _uuid(payload['command_id']); _key(payload['idempotency_key'])
        self._project(payload['project'], payload['recipe']); self._source(payload)
        _check(type(payload['source_commit']) is str and _COMMIT.fullmatch(payload['source_commit']))
        _shape(payload['resource_ids'], _KINDS)
        ids = [_uuid(value) for value in payload['resource_ids'].values()]
        _check(len(set(ids)) == len(ids))
        result = await self._request('POST', '/v1/jobs', payload)
        result = self._operation(result, job_id=payload['job_id'], command_id=payload['command_id'])
        _check(all(key not in result['result'] or result['result'][key] == payload[key]
                   for key in ('source_commit', 'source_digest', 'resource_ids')))
        return result

    @_opaque
    async def job(self, identity):
        identity = _uuid(identity)
        return self._operation(await self._request('GET', '/v1/jobs/' + identity), job_id=identity)

    @_opaque
    async def operation(self, identity):
        """Read the original cleanup receipt after an uncertain acknowledgment.

        This sends exactly one fixed-path GET, never a cleanup POST or a retry.
        The controller must still match the receipt's resource IDs to its
        durable original cleanup request before recording confirmed absence.
        """
        identity = _uuid(identity)
        result = self._operation(await self._request('GET', '/v1/operations/' + identity),
                                 command_id=identity, cleanup=True)
        _check(not set(result['result']) - {'error', 'deleted_resource_ids'})
        if result['state'] == 'succeeded':
            _check('error' not in result['result']
                   and 1 <= len(result['result'].get('deleted_resource_ids', [])) <= 4)
        return result

    @_opaque
    async def resource(self, identity):
        identity = _uuid(identity)
        result = await self._request('GET', '/v1/resources/' + identity + '/metadata')
        _shape(result, {'id', 'job_id', 'kind', 'revision', 'state', 'inode', 'manifest', 'bytes', 'refs'})
        _uuid(result['job_id'])
        _check(result['id'] == identity and type(result['kind']) is str and result['kind'] in _KINDS
               and type(result['state']) is str and result['state'] in {'building', 'ready', 'deleting', 'deleted', 'delete_failed'}
               and type(result['revision']) is int and result['revision'] >= 1
               and type(result['inode']) is int and result['inode'] > 0
               and type(result['bytes']) is int and 0 <= result['bytes'] <= 64 * 1024**2
               and type(result['manifest']) is dict and len(result['manifest']) <= 5000
               and type(result['refs']) is list and len(result['refs']) <= 100
               and all(type(ref) is str and 0 < len(ref) <= 256 for ref in result['refs']))
        total = 0
        for path, entry in result['manifest'].items():
            _relative(path); _shape(entry, {'bytes', 'sha256'})
            _check(type(entry['bytes']) is int and 0 <= entry['bytes'] <= 64 * 1024**2
                   and type(entry['sha256']) is str and _SHA.fullmatch(entry['sha256']))
            total += entry['bytes']
        _check(total == result['bytes'])
        return result

    @_opaque
    async def file(self, identity, path):
        identity, path = _uuid(identity), _relative(path)
        extension = path.rsplit('.', 1)[-1].lower()
        _check(extension in _FILE_TYPES)
        return await self._request('GET', '/v1/resources/' + identity + '/files/' + path,
                                   file_types=_FILE_TYPES[extension])

    @_opaque
    async def cleanup(self, payload):
        _shape(payload, {'command_id', 'idempotency_key', 'resources'})
        _uuid(payload['command_id']); _key(payload['idempotency_key'])
        _check(type(payload['resources']) is list and 1 <= len(payload['resources']) <= 4)
        identities = []
        for item in payload['resources']:
            _shape(item, {'resource_id', 'expected_revision'})
            identities.append(_uuid(item['resource_id']))
            _check(type(item['expected_revision']) is int and item['expected_revision'] >= 1)
        _check(len(set(identities)) == len(identities))
        result = await self._request('POST', '/v1/cleanup', payload)
        result = self._operation(result, command_id=payload['command_id'], cleanup=True)
        if result['state'] == 'succeeded':
            _check(set(result['result'].get('deleted_resource_ids', [])) == set(identities))
        return result
