"""Export deterministic synthetic staging wire examples from the actual API.

This uses a temporary SQLite ledger, synthetic authentication and an inert
controller. It never connects to a worker or creates an execution job. Only the
resource module's UUID namespace is patched; stdlib UUID and unrelated modules
remain unchanged. Run from the source tree, not a separately installed package.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'src'
sys.path.insert(0, str(SOURCE))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from engineering_hub.projects import ProjectRepository
from engineering_hub.repository import HubError, HubRepository
from engineering_hub.staging_api import attach_staging_routes
from engineering_hub import staging_resources


FIXTURE = ROOT / 'hub-ui/src/fixtures/staging-wire-v1.json'
NOW = 1800000000
PROJECT = 'engineering-hub'
NAMESPACE = uuid.UUID('6b018091-c999-451a-84d4-063f3ac94e2d')
JOB_ID = str(uuid.uuid5(NAMESPACE, 'synthetic-job'))


def fixture_document():
    """Return real JSON response bodies, with no credentials or client content."""
    if Path(staging_resources.__file__).resolve().parent != SOURCE / 'engineering_hub':
        raise RuntimeError('Use this worktree source for the wire fixture')
    original_uuid4 = uuid.uuid4
    counter = iter(range(1, 100))
    local_uuid = SimpleNamespace(UUID=uuid.UUID,
        uuid4=lambda: uuid.uuid5(NAMESPACE, 'synthetic-' + str(next(counter))))
    with ExitStack() as stack:
        temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix='eh-wire-fixture-'))
        stack.enter_context(patch.object(staging_resources, 'uuid', local_uuid))
        # Aging is explicit fixture time; no direct row edits are used.
        now = [NOW - 15 * 86400]
        repo = HubRepository(Path(temporary) / 'hub.sqlite3')
        ProjectRepository(repo)
        control = object()
        store = staging_resources.StagingResourceStore(repo,
            admin_actors=frozenset({'human:1'}), control_principal=control,
            clock=lambda: now[0])
        store.set_policy('human:1', PROJECT, 0, {
            actor: ['read', 'retain', 'cleanup', 'preview'] for actor in ('human:1', 'codex')})
        job = store.bind_job(control, JOB_ID, project=PROJECT,
                              created_by='codex', source_commit='a' * 40)
        build = store.register_resource(control, JOB_ID, 'build', topic='Synthetic wire example')
        store.mark_resource_ready(control, build['id'], build['revision'],
                                  artifact_digest='sha256:' + 'b' * 64, size_bytes=2048)
        preview = store.register_resource(control, JOB_ID, 'preview', topic='Synthetic wire example')
        preview = store.mark_resource_ready(control, preview['id'], preview['revision'],
            artifact_digest='sha256:' + 'c' * 64, size_bytes=None,
            preview_path='/staging/previews/' + preview['id'])
        store.update_job(control, JOB_ID, job['revision'], terminal=True)
        now[0] = NOW
        preview = store.inspect_resource(control, preview['id'])
        preview = store.set_keep('human:1', preview['id'], 'pinned',
            reason='Synthetic example retained for review', expected_revision=preview['revision'])
        store.set_protection(control, preview['id'], preview['revision'],
            [{'reason': 'decision_evidence', 'ref': 'synthetic-wire-review'}])

        class Controller:
            resources = store

            async def status(self, actor):
                return {'configured': True, 'running': False, 'reachable': False, 'profiles': {},
                        'can_reconcile': actor == 'human:1', 'held_jobs': [{
                            'id': JOB_ID, 'project': PROJECT, 'recipe': 'static',
                            'state': 'held', 'phase': 'held', 'source_commit': 'a' * 40}]}

            async def reconcile_for(self, actor, job_id, *, admission_guard=None):
                if actor != 'human:1':
                    raise HubError('staging_admin_required', 403)
                binding = store.inspect_job(control, job_id)
                ids = {item['kind']: item['id'] for item in (build, preview)}
                for identity in ids.values():
                    if store.inspect_resource(control, identity)['job_id'] != binding['id']:
                        raise RuntimeError('Synthetic resource binding drift')
                # Inert held outcome: this route does not contact or restart a worker.
                return {'job': {'id': binding['id'], 'state': 'held', 'project': binding['project'],
                                'actor': binding['created_by'], 'environment': 'staging'},
                        'phase': 'held', 'resource_ids': ids,
                        'source_id': str(uuid.uuid5(NAMESPACE, 'synthetic-source'))}

        app = FastAPI()

        @app.exception_handler(HubError)
        async def hub_error(request, error):
            return JSONResponse({'code': error.code}, status_code=error.status)

        def human(request, mutation=False):
            # Explicit in-process synthetic identity; no cookie, bearer or host.
            if request.headers.get('x-synthetic-actor') != 'human:1':
                raise HubError('unauthorized', 401)
            if mutation and request.headers.get('x-synthetic-mutation') != 'verified':
                raise HubError('csrf_required', 403)
            return 'human:1'

        # Synthetic fixture identity has no cookie-backed session. Its guard is
        # deliberately inert; production supplies the same-ledger session guard.
        attach_staging_routes(app, Controller(), human, human,
                              human_admission=lambda request, principal: lambda db: None)
        headers = {'X-Synthetic-Actor': 'human:1', 'X-Synthetic-Mutation': 'verified'}
        with TestClient(app) as client:
            status = client.get('/api/hub/v1/staging/status', headers=headers)
            resources = client.get('/api/hub/v1/staging/resources',
                params={'project': PROJECT}, headers=headers)
            cleanup = client.post('/api/hub/v1/staging/cleanup/plan',
                json={'project': PROJECT}, headers=headers)
            reconcile = client.post('/api/hub/v1/staging/jobs/' + JOB_ID + '/reconcile',
                json={}, headers=headers)
        for response in (status, resources, cleanup, reconcile):
            response.raise_for_status()
        if uuid.uuid4 is not original_uuid4:
            raise RuntimeError('Global UUID namespace changed')
        return {'status': status.json(), 'resources': resources.json(),
                'cleanup_plan': cleanup.json(), 'reconcile': reconcile.json()}


def encoded_fixture():
    return json.dumps(fixture_document(), sort_keys=True, indent=2, ensure_ascii=True) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--write', action='store_true', help='Write the tracked synthetic fixture')
    mode.add_argument('--check', action='store_true', help='Fail if the tracked fixture differs')
    args = parser.parse_args()
    expected = encoded_fixture()
    if args.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(expected, encoding='utf-8', newline='\n')
        print('Wrote hub-ui/src/fixtures/staging-wire-v1.json')
        return 0
    if not FIXTURE.is_file() or FIXTURE.read_bytes() != expected.encode():
        print('Staging wire fixture differs; run scripts/export_staging_wire.py --write', file=sys.stderr)
        return 1
    print('Staging wire fixture matches actual API responses')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
