"""Real container HTTP + pinned Hub workflow acceptance, fixture data only.

Run with this worktree and the exact Hub checkout's src/ on PYTHONPATH.
Fault cases alter tiny fixture state or discard an actual HTTP response; no
executor response is fabricated and no live credentials or applications enter.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

import httpx
import engineering_hub
from engineering_hub.executor_client import ExecutorClient
from engineering_hub.fixture_workflow import FixtureTarget, FixtureWorkflow
from engineering_hub.repository import HubRepository
from hub_executor.fixtures import (
    FIXTURE_TARGET, FIXTURE_CANDIDATE_DIGEST, FIXTURE_CANDIDATE_COMMIT,
    FIXTURE_ROLLBACK_DIGEST, FIXTURE_ROLLBACK_COMMIT,
)

ROOT = Path(__file__).resolve().parents[1]


class ActualHTTPFaultTransport(httpx.AsyncHTTPTransport):
    def __init__(self, mode, data):
        super().__init__(retries=0)
        self.mode, self.data, self.posts = mode, data, 0

    async def handle_async_request(self, request):
        if request.method == 'POST' and request.url.path == '/v1/commands':
            self.posts += 1
            command = json.loads(request.content)
            if self.mode == 'rollback' and command['action'] == 'smoke_fixture':
                # Deliberate corruption in the isolated scratch target only.
                path = self.data / 'targets/3d-viewer-staging/state.json'
                state = json.loads(path.read_text())
                state.update(active_artifact_digest=FIXTURE_ROLLBACK_DIGEST,
                             artifact_bytes='engineering hub fixture v1\n')
                replacement = path.with_name('injected-corruption.json')
                replacement.write_text(json.dumps(state))
                replacement.chmod(0o640)
                os.replace(replacement, path)
            response = await super().handle_async_request(request)
            if self.mode == 'unknown' and command['action'] == 'deploy_fixture':
                # Actual request reached executor and response headers arrived.
                # Discard that response to exercise unknown-outcome handling.
                await response.aclose()
                raise httpx.ReadTimeout('fixture response intentionally lost', request=request)
            return response
        return await super().handle_async_request(request)


async def exercise(mode, origin, token, data, restart, root):
    repo = HubRepository(root / 'hub.sqlite3', execution_principal='executor:fixture')
    task = repo.create_task('human:42', '3d-viewer', 'Docker fixture acceptance', 'fixture-only')
    task = repo.claim_task('codex', task['id'], task['revision'])
    task = repo.set_candidate('codex', task['id'], task['revision'], FIXTURE_CANDIDATE_COMMIT)
    task = repo.review_task('claude', task['id'], task['revision'], FIXTURE_CANDIDATE_COMMIT, 'approve')
    run = repo.create_run('codex', task['id'], task['revision'], FIXTURE_CANDIDATE_DIGEST, FIXTURE_TARGET)
    target = FixtureTarget(FIXTURE_TARGET, FIXTURE_CANDIDATE_DIGEST, FIXTURE_CANDIDATE_COMMIT,
                           FIXTURE_ROLLBACK_DIGEST, FIXTURE_ROLLBACK_COMMIT)
    transport = ActualHTTPFaultTransport(mode, data)
    async with ExecutorClient(origin, token, transport=transport) as service:
        workflow = FixtureWorkflow(repo, service, [target])
        assert (await service.health()).ok
        if mode == 'pause':
            assert await service.pause(True) is True
        if mode == 'success':
            # Actual Hub REST dispatch, ASGI only for Hub; executor is real
            # loopback HTTP into Docker, not MockTransport or a fake driver.
            from agent_message_bus.auth import AuthSettings
            from engineering_hub.app import Settings, create_app
            from engineering_hub.auth import AuthConfig
            settings = Settings(repo.path, AuthSettings('fixture-hermes', 'fixture-codex', claude_token='fixture-claude'),
                                AuthConfig('https://hub.test', beau_github_id=42))
            app = create_app(settings, repository=repo, fixture_workflow=workflow)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://hub.test') as api:
                    response = await api.post(f"/api/hub/v1/runs/{run['id']}/dispatch",
                                              headers={'Authorization': 'Bearer fixture-codex'})
                    assert response.status_code == 200
                    result = response.json()
        else:
            result = await workflow.advance(run['id'])
        expected = {'success': 'succeeded', 'rollback': 'rolled_back', 'unknown': 'held', 'pause': 'held'}[mode]
        assert result['status'] == expected
        posts = transport.posts
        assert posts == {'success': 2, 'rollback': 3, 'unknown': 1, 'pause': 1}[mode]
        restart()  # Real container restart, retaining only this fixture mount.
        assert (await service.health()).ok
        reopened = HubRepository(repo.path, execution_principal='executor:fixture')
        workflow = FixtureWorkflow(reopened, service, [target])
        assert (await workflow.advance(run['id']))['status'] == expected
        assert transport.posts == posts  # Restart never blindly posts again.
        if mode in {'success', 'unknown'}:
            observed = await workflow.inspect(run['id'], 'deploy_fixture')
            assert observed.state == 'succeeded'
            assert transport.posts == posts
        if mode != 'pause':
            actual = json.loads((data / 'targets/3d-viewer-staging/state.json').read_text())
            digest = FIXTURE_ROLLBACK_DIGEST if mode == 'rollback' else FIXTURE_CANDIDATE_DIGEST
            assert actual['active_artifact_digest'] == digest
        else:
            assert not (data / 'targets').exists()
        return {'scenario': mode, 'run_status': expected, 'command_posts': posts,
                'real_container_restart': True, 'no_restart_replay': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hub-commit', required=True)
    args = parser.parse_args()
    hub_root = Path(engineering_hub.__file__).resolve().parents[2]
    revision = subprocess.check_output(['git', '-C', str(hub_root), 'rev-parse', 'HEAD'], text=True).strip()
    assert revision == args.hub_commit, 'Hub imported source does not match requested exact commit'
    harness = runpy.run_path(str(ROOT / 'docker/hub-test-smoke.py'))
    results = []
    for mode in ('success', 'rollback', 'unknown', 'pause'):
        def scenario(origin, token, data, restart):
            with tempfile.TemporaryDirectory(prefix='hub-controller-fixture-', dir=os.environ['TMPDIR']) as temp:
                result = asyncio.run(exercise(mode, origin, token, data, restart, Path(temp)))
            results.append(result)
            return [f"real Hub/ExecutorClient/container HTTP {mode}: PASS"]
        checks = harness['run_smoke'](scenario=scenario)
        print(json.dumps({'scenario': mode, 'checks': checks}), flush=True)
    report = {'hub_commit': revision, 'transport': 'actual container HTTP; explicit fixture fault injection',
              'results': results, 'staging_activation': False, 'truenas_zfs_boot_acceptance': False}
    (ROOT / 'docker/hub-test-integration-results.json').write_text(json.dumps(report, indent=2) + '\n')
    print('All real container workflow scenarios passed', flush=True)


if __name__ == '__main__':
    main()
