"""Authenticated attachment reads of registered failed-job diagnostics.

Installs no route/session/grant. The app must pass its existing authenticated
human actor and a trusted live-session check. Never serves source HTML inline,
creates preview tickets, chooses a worker origin or performs execution effects.
"""
import hashlib
import json

from starlette.responses import Response

from .repository import HubError
from .staging_client import StagingClient, _relative
from .staging_resources import StagingResourceStore


MAX_DOWNLOAD_BYTES = 8 * 1024**2
_BINDING = ('id', 'project', 'job_id', 'revision', 'artifact_digest', 'size_bytes')


def _require(value):
    if not value:
        raise HubError('staging_diagnostic_unavailable', 403)


def _session(resources, client, actor, alive):
    _require(type(resources) is StagingResourceStore and type(client) is StagingClient and callable(alive))
    resources.repo._actor(actor, human=True)
    _require(alive() is True)


def _repeat(resources, actor, initial, revision, alive):
    _require(alive() is True)
    current, current_revision = resources.diagnostic_binding(actor, initial['id'])
    _require(current_revision == revision and all(current[key] == initial[key] for key in _BINDING))


async def _manifest(resources, client, actor, identity, alive):
    _session(resources, client, actor, alive)
    resource, revision = resources.diagnostic_binding(actor, identity)
    fact = await client.resource(identity)
    digest = hashlib.sha256(json.dumps(fact['manifest'], sort_keys=True, separators=(',', ':'),
                                       allow_nan=False).encode()).hexdigest()
    _require(fact['id'] == identity and fact['kind'] == 'test_run' and fact['state'] == 'ready'
             and fact['job_id'] == resource['job_id'] and fact['bytes'] == resource['size_bytes']
             and bool(fact['manifest']) and resource['artifact_digest'] == 'sha256:' + digest)
    _repeat(resources, actor, resource, revision, alive)
    return resource, revision, fact, digest


async def diagnostic_manifest(resources, client, actor, resource_id, *, session_alive):
    """Relative registered file names and hashes, not filesystem paths/URLs."""
    resource, _, fact, digest = await _manifest(resources, client, actor, resource_id, session_alive)
    return {'resource_id': resource['id'], 'job_id': resource['job_id'], 'revision': resource['revision'],
            'manifest_sha256': digest, 'bytes': fact['bytes'], 'files': fact['manifest']}


async def diagnostic_download(resources, client, actor, resource_id, relative_path, *, session_alive):
    name = _relative(relative_path)
    resource, revision, fact, digest = await _manifest(resources, client, actor, resource_id, session_alive)
    entry = fact['manifest'].get(name)
    _require(type(entry) is dict and type(entry.get('bytes')) is int and 0 <= entry['bytes'] <= MAX_DOWNLOAD_BYTES)
    data, media = await client.diagnostic_file(resource_id, name, digest)
    _require(type(data) is bytes and media == 'application/octet-stream' and len(data) == entry['bytes']
             and hashlib.sha256(data).hexdigest() == entry['sha256'])
    _repeat(resources, actor, resource, revision, session_alive)
    filename = name.rsplit('/', 1)[-1]
    if len(filename) > 128:
        filename = 'diagnostic-' + resource_id + '.bin'
    return Response(data, media_type='application/octet-stream', headers={
        'Content-Disposition': 'attachment; filename="' + filename + '"',
        'Content-Security-Policy': "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store, private',
        'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer'})
