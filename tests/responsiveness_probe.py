"""Fresh-interpreter strict latency scenarios; not a runtime or NAS benchmark.

Normal GC stays enabled. No pre-measurement collection or warmup, pytest runner,
or fixture unwrapping. Post-cleanup collection audits deferred resource failures.
The synchronous-observer negative control exists only in this test-support module.
"""
import argparse
import asyncio
from contextlib import contextmanager
import gc
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import warnings

LIMIT = .25
PREFIX = 'EH_RESPONSIVENESS_REPORT '
MODULES = ('tests.responsiveness_probe', 'tests.test_hub_queued_pilot',
    'tests.test_hub_queued_pilot_async', 'tests.test_hub_trusted_queued_session',
    'engineering_hub.app', 'engineering_hub.startup_reconciliation',
    'engineering_hub.trusted_queued_session', 'engineering_hub.work_queue', 'agent_message_bus.app')


@contextmanager
def strict_resource_audit(negative=None):
    """Strict warnings/unraisables through cleanup; never pre-measurement GC."""
    unraisables = []
    previous = sys.unraisablehook
    def record(value):
        unraisables.append(type(value.exc_value).__name__)
    with warnings.catch_warnings():
        warnings.simplefilter('error',ResourceWarning)
        sys.unraisablehook = record
        try:
            yield
            if negative == 'warning':
                warnings.warn('Synthetic resource warning negative control',ResourceWarning)
            elif negative == 'unraisable':
                class BrokenCleanup:
                    def __del__(self):
                        raise RuntimeError('Synthetic unraisable negative control')
                broken = BrokenCleanup()
                del broken
        finally:
            # Only AFTER measurement and owned cleanup: surface destructor
            # failures, never change the heap to make a latency sample pass.
            try:
                gc.collect()
                assert not unraisables, ('unraisable_cleanup_failure',unraisables)
            finally:
                sys.unraisablehook = previous


def source_provenance():
    root = Path(__file__).resolve().parent.parent
    result = {}
    for name in MODULES:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        relative = (name.replace('.', '/') + '.py')
        expected = root / (relative if name.startswith('tests.') else 'src/' + relative)
        assert path == expected
        result[name] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def report(scenario, action, negative, latencies, **facts):
    assert gc.isenabled() and len(latencies) >= 5
    accepted = max(latencies) < LIMIT
    return dict(schema='engineering-hub.isolated-responsiveness.v1', scenario=scenario,
        action=action, negative_control=negative, gc_enabled=True, warmup=False,
        latency_limit_seconds=LIMIT, samples=latencies, sample_count=len(latencies),
        max_seconds=max(latencies), latency_accepted=accepted,
        rejection=None if accepted else 'latency_limit_exceeded', functional_verified=True,
        source=source_provenance(), **facts)


async def unified(root, patches, negative):
    import httpx
    from agent_message_bus.app import create_app as create_bus
    from engineering_hub.app import Settings, create_app
    from engineering_hub.auth import AuthConfig
    from tests.test_hub_queued_pilot import queued_pilot_rig, submit
    from tests.test_hub_queued_pilot_async import async_pilot_case, accept
    from tests.test_hub_unified_mcp import AUTH, BASE, headers, result, security
    with queued_pilot_rig(root) as rig, async_pilot_case(rig) as case:
        patches.setenv('AGENT_BUS_ALLOWED_HOSTS', '127.0.0.1:*')
        bus = create_bus(db_path=root/'messages.sqlite3', audit_path=root/'audit.jsonl',
            file_root=root/'files', auth=AUTH, push_events_operational=False,
            transport_security_settings=security())
        core = create_app(Settings(Path(case.repo.path), AUTH, AuthConfig('https://hub.test',
            human_auth_mode='cloudflare_access', cloudflare_issuer='https://fixture.cloudflareaccess.com',
            cloudflare_audience='fixture', cloudflare_allowed_emails=('beau@example.test',)),
            agent_allowed_hosts=('127.0.0.1:8000',)), repository=case.repo,
            work_queue=case.queue, bus_app=bus)
        case.observation.delay = .3 if negative else 1.0
        await accept(case)
        submit(case)
        loop = asyncio.get_running_loop()
        inline_calls = []
        original = loop.run_in_executor
        if negative:
            def synchronous_observer(executor, function, *args):
                if executor is None and function is case.gate._observer:
                    interval = dict(thread=threading.get_ident(), start=time.monotonic())
                    inline_calls.append(interval)
                    future = loop.create_future()
                    try:
                        future.set_result(function(*args))
                    except BaseException as error:
                        future.set_exception(error)
                    finally:
                        interval['end'] = time.monotonic()
                    return future
                return original(executor, function, *args)
            patches.setattr(loop, 'run_in_executor', synchronous_observer)
        async with core.router.lifespan_context(core):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core), base_url=BASE) as client:
                session = None
                running = None
                latencies = []
                requests = []
                try:
                    initialized = await client.post('/mcp', headers=headers(), json={'jsonrpc':'2.0','id':1,
                        'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},
                            'clientInfo':{'name':'isolated-latency-check','version':'1'}}})
                    result(initialized)
                    session = initialized.headers['Mcp-Session-Id']
                    notified = await client.post('/mcp', headers=headers(session=session), json={
                        'jsonrpc':'2.0','method':'notifications/initialized'})
                    assert notified.status_code == 202
                    running = asyncio.create_task(case.bridge.run_once())
                    # The negative control can finish all synchronous phases in
                    # the first request. Keep five samples without discarding
                    # that blocked sample; positive scenario retains its loop.
                    while not running.done() or negative and len(latencies) < 5:
                        started = time.monotonic()
                        response = await client.post('/mcp', headers=headers(session=session),
                            json={'jsonrpc':'2.0','id':2,'method':'ping'})
                        assert result(response) == {}
                        overview = await client.get('/api/hub/v1/overview', headers=headers())
                        assert overview.status_code == 200
                        ended = time.monotonic()
                        latencies.append(ended-started)
                        requests.append(dict(start=started,end=ended))
                        await asyncio.sleep(.005)
                    assert (await running)['state'] == 'succeeded'
                    assert len(case.client.submissions) == 2
                    assert case.repo.list_runs()[0]['status'] == 'succeeded'
                    if negative:
                        assert inline_calls and all(value['thread'] == threading.get_ident() for value in inline_calls)
                        assert any(request['start'] <= call['start'] < call['end'] <= request['end']
                            for request in requests for call in inline_calls)
                    else:
                        assert all(value != threading.get_ident() for value in case.observation.threads)
                finally:
                    try:
                        if running is not None:
                            await running  # Drain original accepted work, never abandon it.
                    finally:
                        if session is not None:
                            assert (await client.delete('/mcp', headers=headers(session=session))).status_code == 200
        return report('unified', None, negative, latencies,
            observation_calls=case.observation.calls, inline_observations=len(inline_calls),
            inline_observation_intervals=inline_calls, measured_request_intervals=requests,
            inline_observation_overlaps_request=any(
                request['start'] <= call['start'] < call['end'] <= request['end']
                for request in requests for call in inline_calls),
            session_deleted=True, lifespan_drained=True, workflow_state='succeeded')


async def health(root, patches, action):
    import httpx
    from tests.test_hub_trusted_queued_session import synthetic_dependencies, composed
    dependencies = synthetic_dependencies(patches)
    with composed(root, dependencies) as state:
        actor = state.composition._enrollment.operator_actor
        before = dependencies.events.count('observe')
        snapshot = state.broker.snapshot(actor=actor)
        assert dependencies.events.count('observe') == before
        observer_type = type(dependencies.observers[0])
        original = observer_type.observe_verified
        entered, release = threading.Event(), threading.Event()
        def slow_observation(observer):
            entered.set()
            if not release.wait(2):
                raise TimeoutError('Synthetic observation was not drained')
            return original(observer)
        patches.setattr(observer_type, 'observe_verified', slow_observation)
        operation = asyncio.create_task(state.broker.decide_async(actor=actor,
            request_id=snapshot['request_id'], expected_revision=snapshot['revision'], action=action))
        latencies = []
        try:
            if action == 'accept':
                async with asyncio.timeout(1):
                    while not entered.is_set():
                        await asyncio.sleep(.001)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=state.core),
                    base_url='https://hub.example') as client:
                for _ in range(5):
                    started = time.monotonic()
                    response = await asyncio.wait_for(client.get('/health'), timeout=LIMIT)
                    latencies.append(time.monotonic()-started)
                    assert response.status_code == 200
            if action == 'accept':
                assert not operation.done() and state.client.submissions == []
        finally:
            release.set()
            await operation
        calls = dependencies.events.count('observe')-before
        assert calls == (1 if action == 'accept' else 0)
        assert state.client.submissions == []
        return report('health', action, False, latencies, observation_calls=calls,
                      operation_drained=True, no_command_posts=True)


def run_isolated_probe(scenario, *, action=None, negative_control=False):
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root/'src'),str(root))),
               PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, '-B', '-m', 'tests.responsiveness_probe', scenario]
    if action is not None:
        command += ['--action', action]
    if negative_control:
        command += ['--negative-control']
    child = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, (child.returncode, child.stdout[-4000:], child.stderr[-4000:])
    lines = [line[len(PREFIX):] for line in child.stdout.splitlines() if line.startswith(PREFIX)]
    assert len(lines) == 1
    value = json.loads(lines[0])
    return validate_report(value,scenario=scenario,action=action,negative_control=negative_control,
                           expected_source=source_provenance())


def validate_report(value, *, scenario, action, negative_control, expected_source):
    assert value['schema'] == 'engineering-hub.isolated-responsiveness.v1'
    assert value['source'] == expected_source
    assert value['scenario'] == scenario and value['action'] == action
    assert value['negative_control'] is negative_control
    assert value['gc_enabled'] is True and value['warmup'] is False
    assert value['functional_verified'] is True and value['latency_limit_seconds'] == LIMIT
    assert value['resource_audit_passed'] is True
    samples = value['samples']
    assert isinstance(samples,list) and len(samples) >= 5 and value['sample_count'] == len(samples)
    assert all(type(sample) in (int,float) and math.isfinite(sample) and sample >= 0 for sample in samples)
    assert value['max_seconds'] == max(samples)
    assert value['latency_accepted'] is (max(samples) < LIMIT)
    if negative_control:
        assert value['rejection'] == 'latency_limit_exceeded' and not value['latency_accepted']
        assert value['inline_observation_overlaps_request'] is True
        assert value['workflow_state'] == 'succeeded'
        assert value['session_deleted'] is True and value['lifespan_drained'] is True
    else:
        assert value['rejection'] is None and value['latency_accepted']
    return value


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('scenario',choices=('unified','health'))
    parser.add_argument('--action',choices=('accept','decline'))
    parser.add_argument('--negative-control',action='store_true')
    parser.add_argument('--resource-negative-control',choices=('warning','unraisable'))
    options=parser.parse_args(argv)
    assert sys.platform == 'linux' and gc.isenabled()
    assert (options.scenario == 'health') == (options.action is not None)
    assert not options.negative_control or options.scenario == 'unified'
    import pytest
    with strict_resource_audit(options.resource_negative_control):
        with tempfile.TemporaryDirectory(prefix='eh-responsiveness-') as directory:
            with pytest.MonkeyPatch.context() as patches:
                with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
                    value=runner.run(unified(Path(directory),patches,options.negative_control)
                        if options.scenario=='unified' else health(Path(directory),patches,options.action))
    value['resource_audit_passed'] = True
    print(PREFIX+json.dumps(value,sort_keys=True),flush=True)
    return 0 if value['latency_accepted'] is not options.negative_control else 3


if __name__ == '__main__':
    raise SystemExit(main())
