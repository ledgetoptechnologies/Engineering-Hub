"""Offline lifecycle ordering; effects and host admission are synthetic."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
pytest.importorskip('fcntl')
from hub_container_executor import retained_oauth_pair as pair
from hub_container_executor import configuration_commitment


@pytest.mark.parametrize('wall,mono',[(99.9,20.1),(100.1,21.1)])
def test_pair_freshness_cannot_revive_after_wall_regression_or_monotonic_expiry(monkeypatch,wall,mono):
    clock=SimpleNamespace(wall=100.0,mono=20.0)
    monkeypatch.setattr(pair.time,'time',lambda:clock.wall);monkeypatch.setattr(pair.time,'monotonic',lambda:clock.mono)
    instance=object.__new__(pair.RetainedOAuthPair)
    instance._executor=SimpleNamespace(_expires=1000.0)
    instance._last_wall=instance._last_mono=instance._fresh_deadline=None
    session=SimpleNamespace(observed_at_ms=99900,expires_at_ms=101000,
                            launch=SimpleNamespace(approval_expires_at_ms=102000))
    instance._fresh(session)
    clock.wall=wall;clock.mono=mono
    with pytest.raises(pair.RetainedPairError):instance._fresh(session)


def test_endpoint_binds_exact_pinned_runner_bearer_digest(tmp_path):
    from tests.test_hub_container_executor_installed_runner import configuration as runner_configuration,canonical
    from tests.test_hub_container_executor_installed_coordinator import github_configuration
    from engineering_hub.container_pilot_manifest import PilotManifest
    runner=runner_configuration();runner['executor_socket_path']='/run/hub-executor/host.sock'
    coordinator=github_configuration();coordinator['manifest']=runner['manifest']
    raw=canonical(runner);source=tmp_path/'runner.json';source.write_bytes(raw);source.chmod(0o600)
    info=source.stat();directory=str(tmp_path/'endpoint')
    endpoint=SimpleNamespace(role='executor_endpoint_directory',source=directory,uid=10,gid=20)
    remote=SimpleNamespace(role='local_executor_directory',source=directory,uid=10,gid=20)
    mount=SimpleNamespace(source=str(source),uid=os.getuid(),gid=os.getgid(),mode=0o600)
    executor=SimpleNamespace(profile=SimpleNamespace(mounts=(endpoint,)),_executor_endpoint_name='host.sock',
        _mounts={'config':mount},_source_pins={'config':(info.st_dev,info.st_ino)},
        _configuration_raw_digest='sha256:'+hashlib.sha256(raw).hexdigest())
    instance=object.__new__(pair.RetainedOAuthPair);instance._executor=executor
    instance._coordinator=SimpleNamespace(profile=SimpleNamespace(mounts=(remote,)))
    instance._config=canonical(coordinator)
    instance._policy={'manifest_digest':PilotManifest.from_bytes(canonical(runner['manifest'])).digest()}
    instance._bind_endpoint()
    coordinator['secret_bindings']['executor']['digest']='sha256:'+'6'*64
    instance._config=canonical(coordinator)
    with pytest.raises(pair.RetainedPairError):instance._bind_endpoint()


@pytest.mark.parametrize('failure',['none','broker_start','broker_stop','broker_exit','close'])
def test_pair_drains_in_order_and_holds_on_broker_failure(monkeypatch,failure):
    events=[];session=b'synthetic-session';admission=b'synthetic-admission';runtime=b'synthetic-runtime'
    alive=[True]
    sha=lambda raw:'sha256:'+hashlib.sha256(raw).hexdigest()
    enrollment={'synthetic':'broker'}
    broker_digest=sha(json.dumps(enrollment,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode())
    policy={'schema':'engineering-hub.retained-oauth-pair.v1','pair_id':str(uuid4()),'journal_file':'/journal',
        'manifest_digest':'sha256:'+'a'*64,'executor_launch_id':str(uuid4()),'coordinator_profile_digest':'sha256:'+'b'*64,
        'session_digest':sha(session),'admission_digest':sha(admission),'broker_enrollment_digest':broker_digest,
        'broker_runtime_policy':'/runtime','broker_runtime_digest':sha(runtime)}
    class Profile:
        def __init__(self,role):
            self.role=role;self.executor_endpoint=object();self.gate_ledger_channel='pair-outcome-listener-v1'
        def digest(self):return policy['coordinator_profile_digest']
    class Supervisor:
        def __init__(self,role):
            import os
            self._pid=os.getpid();self.profile=Profile(role);self._launch_id=None if role else policy['executor_launch_id']
            self._closed=False;self._held=False;self._admission_raw_digest=sha(admission);self.release=object();self._daemon_pin='same-daemon'
        def attest_executor_endpoint(self,**kwargs):events.append('endpoint-observed')
        def wait_for_stop_with_observations(self,*,stop_requested,**kwargs):
            while not stop_requested():time.sleep(.01)
            events.append('observation-drained');return True
        def launch(self,**kwargs):events.append('coordinator-launch');self._launch_id='coordinator-id'
        def wait_for_receipt_request(self,**kwargs):events.append('coordinator-ready');return True
        def serve_one_receipt(self,**kwargs):events.append('receipt')
        def wait_for_coordinator_stop(self,*,stop_requested,**kwargs):return stop_requested()
        def graceful_stop_coordinator(self,**kwargs):events.append('coordinator-drained')
        def seal_pair_drain(self,**kwargs):events.append('executor-sealed')
        def graceful_stop_sealed_executor(self,**kwargs):events.append('executor-drained');return {'state':'done'}
        def close(self):
            events.append('close')
            if failure=='close' and self.profile.role:raise OSError('synthetic close failure')
    class Broker:
        pid=123
        _process=SimpleNamespace(poll=lambda:None if alive[0] else 1)
        def start_for_coordinator(self,**kwargs):
            events.append('broker-start')
            if failure=='broker_start':raise RuntimeError()
        def stop(self,**kwargs):
            events.append('broker-stop')
            if failure=='broker_stop':raise RuntimeError()
    class Journal:
        def __init__(self,*a,**kw):pass
        def reserve(self,*a):events.append('reserved')
        def state(self,identity,state,data):events.append(state)
        def close(self):events.append('journal-close')
    monkeypatch.setattr(pair,'LaunchSupervisor',Supervisor);monkeypatch.setattr(pair,'OwnedBrokerProcess',Broker)
    monkeypatch.setattr(pair,'_Journal',Journal);monkeypatch.setattr(pair,'_path',lambda value:Path(value))
    monkeypatch.setattr(pair,'_root_file',lambda path,maximum: (runtime,('runtime',)) if str(path)=='/runtime' else (b'policy',('policy',)))
    monkeypatch.setattr(pair,'_strict',lambda *_:policy);monkeypatch.setattr(pair,'read_enrollment',lambda _:enrollment)
    monkeypatch.setattr(pair.RetainedOAuthPair,'_validate_executor',lambda self:events.append('executor-validated'))
    monkeypatch.setattr(pair.RetainedOAuthPair,'_bind_endpoint',lambda self:events.append('endpoint-bound'))
    monkeypatch.setattr(configuration_commitment,'supervised_local_coordinator_commitment',lambda *a,**kw:events.append('commitment'))
    executor=Supervisor(None);coordinator=Supervisor('supervised-local-coordinator-v3')
    owned=pair.RetainedOAuthPair(executor=executor,coordinator=coordinator,pair_policy='/policy',config_raw=b'cfg',
        session_raw=session,admission_raw=admission,broker_enrollment_fd=8,broker_listener=object())
    try:
        if failure=='broker_start':
            with pytest.raises(pair.RetainedPairError):owned.start()
            assert owned._state=='held' and 'coordinator-launch' not in events and not owned._observer.is_alive()
            return
        owned.start();assert owned.wait(timeout_ms=10,stop_requested=lambda:True)
        if failure=='broker_exit':
            alive[0]=False
            with pytest.raises(pair.RetainedPairError):owned.wait(timeout_ms=10,stop_requested=lambda:False)
            assert owned._state=='held' and not owned._observer.is_alive() and 'executor-sealed' not in events
            return
        if failure=='broker_stop':
            with pytest.raises(pair.RetainedPairError):owned.stop(coordinator_timeout_ms=2000,broker_timeout_seconds=2,executor_timeout_ms=2000)
            assert owned._state=='held' and not owned._observer.is_alive() and 'executor-sealed' not in events and 'executor-drained' not in events
            with pytest.raises(pair.RetainedPairError):owned.close()
        else:
            assert owned.stop(coordinator_timeout_ms=2000,broker_timeout_seconds=2,executor_timeout_ms=2000)=={'state':'done'}
            assert events.index('broker-start')<events.index('coordinator-launch')<events.index('coordinator-drained')
            assert events.index('coordinator-drained')<events.index('broker-stop')<events.index('executor-sealed')
            assert events.index('executor-sealed')<events.index('observation-drained')<events.index('executor-drained')
            if failure=='close':
                with pytest.raises(pair.RetainedPairError):owned.close()
                assert owned._state=='held'
            else:owned.close()
            assert events[-3:]==['close','close','journal-close']
    finally:
        owned._stop_observer.set()
        if owned._observer is not None and owned._observer.ident is not None:owned._observer.join(timeout=2)
        assert owned._observer is None or not owned._observer.is_alive()
