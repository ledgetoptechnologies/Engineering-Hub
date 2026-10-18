"""Linux ASGI lifecycle ownership with simulated receipts, not host acceptance."""
import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.events import EventBroker
from engineering_hub.pilot_ownership import PilotOwner, PilotOwnershipError
from engineering_hub.pilot_service import (
    REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS, OwnedPilotService, create_owned_pilot_app,
)
from engineering_hub.container_pilot import ContainerPilotTarget
from engineering_hub.repository import HubError
from tests.test_hub_container_pilot import FakeClient, IMAGE, pilot_manifest, reviewed_task


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux ownership lifecycle')
PILOT = '/api/hub/v1/container-pilot/runs'
CODEX = {'Authorization': 'Bearer fixture-codex'}
CLAUDE = {'Authorization': 'Bearer fixture-claude'}


class Feed:
    def __init__(self, *, fail_start=False):
        self.broker = EventBroker()
        self.fail_start = fail_start
        self.stopped = False

    @property
    def available(self):
        return self.broker.available

    async def start(self):
        if self.fail_start:
            raise RuntimeError('private feed startup detail')
        self.broker.activate(0)

    async def stop(self):
        self.stopped = True
        self.broker.fail()


class Client(FakeClient):
    def __init__(self, *, blocked=False, fail_close=False):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.closed = self.cancelled = False
        self.fail_close = fail_close
        self.inspect_entered = asyncio.Event()
        self.inspect_release = asyncio.Event()
        self.inspect_release.set()

    async def submit_command(self, command):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().submit_command(command)

    async def get_command(self, command):
        self.inspect_entered.set()
        try:
            await self.inspect_release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().get_command(command)

    async def aclose(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError('private credential in client exception')


async def settle(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 3)


@pytest.fixture
def rig(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    settings = Settings(private / 'hub.sqlite',
                        AuthSettings('fixture-hermes', 'fixture-codex', claude_token='fixture-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    owner = PilotOwner(settings.database, allow_ephemeral_test_storage=True)
    target = ContainerPilotTarget.from_manifest(pilot_manifest())
    def build(*, blocked=False, fail_close=False, fail_start=False):
        client, feed = Client(blocked=blocked, fail_close=fail_close), Feed(fail_start=fail_start)
        app = create_owned_pilot_app(settings, client, [target], owner=owner, event_feed=feed,
                                     supervisor_stop_grace_seconds=1200)
        return SimpleNamespace(app=app, client=client, feed=feed, owner=owner,
                               settings=settings, target=target, service=app.state.owned_pilot_service)
    yield build
    owner.close()


def assert_exclusive(state):
    state.owner.assert_active(state.settings.database)
    with pytest.raises(PilotOwnershipError):
        PilotOwner(state.settings.database, allow_ephemeral_test_storage=True)


def assert_released(state):
    with PilotOwner(state.settings.database, allow_ephemeral_test_storage=True):
        pass


def run_for(state):
    task = reviewed_task(state.service.repo)
    return state.service.workflow.create_run('codex', task['id'], task['revision'], IMAGE, state.target.target_id)


async def test_constructor_holds_owner_and_lifespan_starts_actual_runtime(rig):
    state = rig()
    assert_exclusive(state)
    assert REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS == 1120
    assert state.service.status == {
        'state': 'not_started',
        'required_supervisor_stop_grace_seconds': 1120,
        'configured_supervisor_stop_grace_seconds': 1200,
        'supervisor_configuration_verified': False,
        'configuration_source': 'operator_assertion',
        'tasks': state.service.tasks.status,
    }
    assert state.service.tasks.status['state'] == 'not_started'
    async with state.app.router.lifespan_context(state.app):
        await settle(lambda: state.app.state.staging_runtime.last_result is not None)
        assert state.app.state.staging_runtime.status['running']
        assert state.feed.broker.subscriber_count == 1
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app), base_url='https://hub.test') as http:
            assert (await http.get('/health')).status_code == 200
            response = await http.get('/api/hub/v1/autonomy', headers=CODEX)
            assert response.status_code == 200
            lifecycle = response.json()['pilot_lifecycle']
            assert lifecycle['state'] == 'active'
            assert lifecycle['required_supervisor_stop_grace_seconds'] == 1120
            assert lifecycle['configured_supervisor_stop_grace_seconds'] == 1200
            assert lifecycle['supervisor_configuration_verified'] is False
            assert lifecycle['configuration_source'] == 'operator_assertion'
            assert lifecycle['tasks']['state'] == 'active'
    assert state.client.closed and state.feed.stopped
    assert state.service.status['state'] == 'stopped'
    assert not state.app.state.staging_runtime.status['running']
    assert_released(state)


@pytest.mark.parametrize('factory', [OwnedPilotService, create_owned_pilot_app])
@pytest.mark.parametrize('grace', ['omitted', None, True, False, 0, -1, 60, 1119, 1120.0, '1200', float('nan')])
async def test_invalid_stop_grace_rejected_before_owner_transfer(rig, monkeypatch, factory, grace):
    state = rig()
    rejected_client = Client()
    kwargs = {} if grace == 'omitted' else {'supervisor_stop_grace_seconds': grace}
    def unexpected_close():
        pytest.fail('Rejected grace closed the existing service owner')
    with monkeypatch.context() as patch:
        patch.setattr(state.owner, 'close', unexpected_close)
        with pytest.raises(ValueError, match='^owned_pilot_supervisor_stop_grace_required$'):
            factory(state.settings, rejected_client, [None], owner=state.owner, **kwargs)
    assert_exclusive(state)
    assert not rejected_client.closed and not state.client.closed
    # The accepted first service still has an ordinary, usable lifespan.
    async with state.app.router.lifespan_context(state.app):
        assert state.service.status['state'] == 'active'
    await rejected_client.aclose()
    assert_released(state)


async def test_short_observer_deadline_does_not_release_running_attempt(rig):
    state = rig(blocked=True)
    await state.feed.start()
    await state.service.start(state.feed)
    run = run_for(state)
    accepted = asyncio.create_task(state.service.advance(run['id']))
    await state.client.entered.wait()
    shutdown = asyncio.create_task(state.service.stop(None, state.feed))
    try:
        await settle(lambda: state.service.tasks.status['state'] == 'stopping')
        # Model an external observer's too-short deadline, not real elapsed
        # command time and not an actual supervisor SIGKILL/reboot proof.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(shutdown), timeout=0)
        assert not shutdown.done() and not accepted.done()
        assert_exclusive(state)
        assert state.service.status['state'] == 'stopping'
        assert state.service.status['supervisor_configuration_verified'] is False
        with state.service.repo._connection() as db:
            assert db.execute('SELECT 1 FROM hub_container_pilot_advances WHERE run_id=?',
                              (run['id'],)).fetchone() is not None
        assert not state.client.closed and not state.client.cancelled
    finally:
        state.client.release.set()
        result = await accepted
        await shutdown
    assert result['status'] == 'succeeded'
    assert state.service.status['state'] == 'stopped'
    assert_released(state)


@pytest.mark.parametrize('invalid_targets', [False, True])
async def test_duplicate_capability_transfer_never_closes_accepted_service(rig, monkeypatch, invalid_targets):
    state = rig(blocked=True)
    second_client = Client()
    async with state.app.router.lifespan_context(state.app):
        await settle(lambda: state.app.state.staging_runtime.last_result is not None)
        run = run_for(state)
        accepted = asyncio.create_task(state.service.advance(run['id']))
        try:
            await state.client.entered.wait()
            # Neither repository construction nor a later malformed manifest
            # may be reached by a rejected second transfer. Its constructor's
            # failure cleanup would otherwise close the first service's owner.
            def unexpected_repository(*args, **kwargs):
                pytest.fail('Duplicate transfer touched repository construction')
            with monkeypatch.context() as patch:
                patch.setattr('engineering_hub.pilot_service.HubRepository', unexpected_repository)
                with pytest.raises(ValueError, match='^owned_pilot_capability_already_transferred$'):
                    create_owned_pilot_app(state.settings, second_client,
                                           [None] if invalid_targets else [state.target],
                                           owner=state.owner, event_feed=Feed(),
                                           supervisor_stop_grace_seconds=REQUIRED_SUPERVISOR_STOP_GRACE_SECONDS)
            assert_exclusive(state)
            state.service.assert_active()
            assert state.service.tasks.status['inflight'] == 1
            assert not state.client.closed and not second_client.closed
        finally:
            state.client.release.set()
            result = await accepted
            await second_client.aclose()  # Rejected clients stay caller-owned.
        assert result['status'] == 'succeeded'
        assert len(state.client.submissions) == 2
    assert_released(state)


@pytest.mark.parametrize('cancel_shutdown', [False, True])
async def test_http_detach_and_shutdown_keep_owner_until_accepted_work_finishes(rig, cancel_shutdown):
    state = rig(blocked=True)
    started, stop = asyncio.Event(), asyncio.Event()
    async def lifecycle():
        async with state.app.router.lifespan_context(state.app):
            started.set()
            await stop.wait()
    lifetime = asyncio.create_task(lifecycle())
    await started.wait()
    await settle(lambda: state.app.state.staging_runtime.last_result is not None)
    run = run_for(state)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app), base_url='https://hub.test') as http:
        request = asyncio.create_task(http.post(f"{PILOT}/{run['id']}/dispatch", headers=CODEX))
        await state.client.entered.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert state.service.tasks.status['inflight'] == 1
        stop.set()
        await settle(lambda: state.service.tasks.status['state'] == 'stopping')
        if cancel_shutdown:
            for _ in range(3):
                lifetime.cancel()
                await asyncio.sleep(0)
                assert not lifetime.done()
        assert_exclusive(state)
        assert not state.client.closed and not state.client.cancelled
        state.client.release.set()
        if cancel_shutdown:
            with pytest.raises(asyncio.CancelledError):
                await lifetime
        else:
            await lifetime
    assert state.client.closed and not state.client.cancelled
    assert state.service.repo.get_run(run['id'])['status'] == 'succeeded'
    assert len(state.client.submissions) == 2
    assert_released(state)


async def test_feed_start_failure_closes_client_and_owner(rig):
    state = rig(fail_start=True)
    with pytest.raises(RuntimeError, match='private feed startup detail'):
        async with state.app.router.lifespan_context(state.app):
            pytest.fail('Failed startup must not yield')
    assert state.client.closed and state.feed.stopped
    assert state.service.tasks.status['state'] == 'stopped'
    assert_released(state)


async def test_client_close_failure_is_sanitized_and_releases_owner(rig):
    state = rig(fail_close=True)
    with pytest.raises(HubError, match='^owned_pilot_shutdown_failed$') as error:
        async with state.app.router.lifespan_context(state.app):
            await settle(lambda: state.app.state.staging_runtime.last_result is not None)
            run = run_for(state)
            assert (await state.service.advance(run['id']))['status'] == 'succeeded'
    assert error.value.status == 503
    assert state.client.closed and state.feed.stopped
    assert_released(state)


async def test_health_fails_closed_after_owner_lost(rig):
    state = rig()
    async with state.app.router.lifespan_context(state.app):
        await settle(lambda: state.app.state.staging_runtime.last_result is not None)
        state.owner.close()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app), base_url='https://hub.test') as http:
            response = await http.get('/health')
            assert response.status_code == 503
            assert response.json() == {'detail': {'code': 'owned_pilot_unavailable'}}
    assert state.client.closed


async def test_recovery_http_requires_original_run_creator(rig):
    state = rig()
    async with state.app.router.lifespan_context(state.app):
        await settle(lambda: state.app.state.staging_runtime.last_result is not None)
        run = run_for(state)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app), base_url='https://hub.test') as http:
            forbidden = await http.post(f"{PILOT}/{run['id']}/recover", headers=CLAUDE)
            assert forbidden.status_code == 403
            assert state.client.inspections == [] and state.service.tasks.status['inflight'] == 0
            permitted = await http.post(f"{PILOT}/{run['id']}/recover", headers=CODEX)
            assert permitted.status_code == 409  # No accepted attempt to inspect yet.
            assert permitted.json()['detail']['code'] == 'pilot_recovery_reservation_unverified'


async def test_recovery_survives_detached_http_request_without_post_replay(rig):
    state = rig()
    async with state.app.router.lifespan_context(state.app):
        await settle(lambda: state.app.state.staging_runtime.last_result is not None)
        run = run_for(state)
        state.client.outcomes['deploy_container'] = 'pending'
        assert (await state.service.advance(run['id']))['status'] == 'held'
        state.client.outcomes.clear()
        state.client.inspect_release.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.app), base_url='https://hub.test') as http:
            request = asyncio.create_task(http.post(f"{PILOT}/{run['id']}/recover", headers=CODEX))
            await state.client.inspect_entered.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert state.service.tasks.status['inflight'] == 1
            assert_exclusive(state)
            state.client.inspect_release.set()
            await settle(lambda: state.service.tasks.status['inflight'] == 0)
        assert not state.client.cancelled
        assert len(state.client.inspections) == 1 and len(state.client.submissions) == 1


async def test_default_app_has_no_owned_activation(tmp_path):
    settings = Settings(tmp_path / 'default.sqlite',
                        AuthSettings('fixture-hermes', 'fixture-codex', claude_token='fixture-claude'),
                        AuthConfig('https://hub.test', beau_github_id=42))
    feed = Feed()
    app = create_app(settings, event_feed=feed)
    assert app.state.owned_pilot_service is None and app.state.staging_runtime is None
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://hub.test') as http:
            response = await http.post(f'{PILOT}/00000000-0000-0000-0000-000000000000/recover', headers=CODEX)
            assert response.status_code == 423
            assert response.json()['detail']['code'] == 'owned_pilot_not_enabled'
    assert feed.stopped
