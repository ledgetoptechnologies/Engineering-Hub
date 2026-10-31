"""One retained, root-enrolled executor/coordinator/broker lifecycle.

This composition does not enable supervisor roles or establish host custody.
The caller transfers the executor, listener and broker enrollment to this owner;
the coordinator is created only after the pair journal is reserved. The caller
must retain the owner on uncertainty. No restart or adoption is attempted.
"""
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from uuid import UUID

from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins
from engineering_hub.trusted_queued_session import parse_enrollment, SCHEMA_V3, MAX_REPORT_AGE_MS
from .github_broker_owner import OwnedBrokerProcess
from .github_broker_service import read_enrollment
from .launch_supervisor import LaunchSupervisor, _Journal, _path, _root_file, _strict


class RetainedPairError(RuntimeError):
    def __init__(self): super().__init__('retained local pair unavailable; preserve evidence')


def _require(value):
    if not value: raise RetainedPairError()


class RetainedOAuthPair:
    """Single-use owner; failures hold all remaining processes and evidence."""
    def __init__(self, *, executor, pair_policy, config_raw, session_raw,
                 admission_raw, broker_enrollment_fd, broker_listener):
        self._pid=os.getpid();self._thread=threading.get_ident();self._state='constructing'
        self._executor=executor;self._coordinator=None;self._broker=OwnedBrokerProcess()
        self._listener=broker_listener;self._broker_fd=broker_enrollment_fd
        self._stop_observer=threading.Event();self._observer_failed=threading.Event();self._observer=None
        self._journal=None;self._start_attempted=False;self._stop_attempted=False;self._construction_attempted=False
        self._last_wall=None;self._last_mono=None;self._fresh_deadline=None
        self._config=config_raw;self._session=session_raw;self._admission=admission_raw
        try:
            _require(type(executor) is LaunchSupervisor and executor._pid==self._pid
                     and executor.profile.role is None and executor.profile.executor_endpoint is not None
                     and executor.profile.gate_ledger_channel=='pair-outcome-listener-v1')
            self._policy_path=_path(pair_policy);raw,self._policy_identity=_root_file(self._policy_path,16384)
            policy=_strict(raw,16384)
            fields={'schema','pair_id','journal_file','manifest_digest','executor_launch_id','coordinator_profile_digest',
                    'session_digest','admission_digest','broker_enrollment_digest','broker_runtime_policy','broker_runtime_digest',
                    'coordinator_supervisor_digest'}
            _require(type(policy) is dict and set(policy)==fields and policy['schema']=='engineering-hub.retained-oauth-pair.v2')
            _require(type(policy['pair_id']) is str and str(UUID(policy['pair_id']))==policy['pair_id'] and UUID(policy['pair_id']).version==4)
            _require(all(type(policy[k]) is str and re.fullmatch(r'sha256:[0-9a-f]{64}',policy[k]) for k in
                         ('manifest_digest','coordinator_profile_digest','session_digest','admission_digest','broker_enrollment_digest','broker_runtime_digest','coordinator_supervisor_digest')))
            _require(type(policy['executor_launch_id']) is str and str(UUID(policy['executor_launch_id']))==policy['executor_launch_id'])
            self._policy=policy; self._runtime_path=_path(policy['broker_runtime_policy']);journal_path=_path(policy['journal_file'])
            _require(len({self._policy_path,self._runtime_path,journal_path})==3
                     and hashlib.sha256(session_raw).hexdigest()==policy['session_digest'][7:]
                     and hashlib.sha256(admission_raw).hexdigest()==policy['admission_digest'][7:])
            runtime,self._runtime_identity=_root_file(self._runtime_path,16384)
            _require('sha256:'+hashlib.sha256(runtime).hexdigest()==policy['broker_runtime_digest'])
            broker=read_enrollment(broker_enrollment_fd)
            broker_raw=json.dumps(broker,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
            _require('sha256:'+hashlib.sha256(broker_raw).hexdigest()==policy['broker_enrollment_digest'])
            self._validate_executor()
            self._pins()
            self._journal=_Journal(str(journal_path),create=True)
            self._journal.reserve(policy['pair_id'],{'policy_sha256':hashlib.sha256(raw).hexdigest(),
                'executor_launch_id':policy['executor_launch_id'],'manifest_digest':policy['manifest_digest']})
            self._state='awaiting_coordinator'
        except BaseException:
            if self._journal is not None:self._journal.close()
            raise RetainedPairError() from None

    def _authorize_coordinator_construction(self, supervisor, *, enrollment_raw, enrollment_digest):
        """One owner-bound check called before coordinator journal/Engine setup."""
        _require(self._state=='authorizing' and self._construction_attempted and self._coordinator is None
                 and type(supervisor) is LaunchSupervisor and supervisor is not self._executor
                 and enrollment_digest==self._policy['coordinator_supervisor_digest']
                 and 'sha256:'+hashlib.sha256(enrollment_raw).hexdigest()==enrollment_digest)
        self._pins();self._validate_executor()
        _require(supervisor._pid==self._pid and supervisor.profile.role=='supervised-local-coordinator-v3'
                 and supervisor.profile.digest()==self._policy['coordinator_profile_digest']
                 and supervisor._admission_raw_digest==self._policy['admission_digest']
                 and supervisor._daemon_pin==self._executor._daemon_pin
                 and supervisor._launch_id is None and not supervisor._closed and not supervisor._held)
        from .configuration_commitment import supervised_local_coordinator_commitment
        supervised_local_coordinator_commitment(supervisor.profile,self._config,self._session,self._admission,
            root_raw_digest=self._policy['admission_digest'],independent_release=supervisor.release)
        self._coordinator=supervisor
        self._bind_endpoint();self._pins();self._validate_executor()

    def create_coordinator(self, *, enrollment_file, signing_key_file, journal_file, held_ranges_file):
        """Attempt the exact root-enrolled supervisor constructor once."""
        _require(self._state=='awaiting_coordinator' and not self._construction_attempted)
        self._pins();self._construction_attempted=True;self._state='authorizing'
        try:
            self._record('coordinator_construction_reserved')
            coordinator=LaunchSupervisor(enrollment_file=enrollment_file,signing_key_file=signing_key_file,
                journal_file=journal_file,held_ranges_file=held_ranges_file,create_journal=True,retained_pair=self)
            _require(coordinator is self._coordinator and not coordinator._closed and not coordinator._held)
            self._pins();self._validate_executor();self._record('prepared');self._state='prepared'
        except BaseException:
            self._hold();raise RetainedPairError() from None

    def _pins(self):
        _require(self._pid==os.getpid() and self._thread==threading.get_ident())
        _require(_root_file(self._policy_path,16384)[1]==self._policy_identity
                 and _root_file(self._runtime_path,16384)[1]==self._runtime_identity)

    def _validate_executor(self):
        supervisor=self._executor;session=parse_enrollment(self._session);policy=self._policy
        _require(session.schema==SCHEMA_V3 and session.manifest_digest==policy['manifest_digest']
                 and session.launch.launch_id==policy['executor_launch_id'])
        _require(supervisor._mutex.acquire(timeout=5))
        try:
            supervisor._guard()
            _require(supervisor._receipt_served and not supervisor._issuance_open and supervisor._consumer is not None)
            supervisor._validate_live(time.monotonic()+5)
            release=CurrentExecutorReleasePins(**{name:getattr(supervisor.release,name) for name in CurrentExecutorReleasePins.__dataclass_fields__})
            launch=CurrentLaunchPins(supervisor._launch_id,supervisor._consumer.boot,supervisor._consumer.pid,supervisor._consumer.start,
                supervisor._container_id,supervisor._daemon_pin.daemon_id,supervisor._daemon_pin.pid,
                supervisor._daemon_pin.start_ticks,supervisor._approval_expires_at_ms)
            _require(release==session.release and launch==session.launch)
            self._fresh(session)
        finally:supervisor._mutex.release()

    def _fresh(self,session):
        wall=time.time();mono=time.monotonic()
        _require(type(wall) in (int,float) and math.isfinite(wall) and wall>0
                 and type(mono) in (int,float) and math.isfinite(mono) and mono>=0)
        now=int(wall*1000)
        _require(0<=now-session.observed_at_ms<=MAX_REPORT_AGE_MS and now<session.expires_at_ms
                 and now<session.launch.approval_expires_at_ms and mono<self._executor._expires)
        if self._last_wall is None:
            remaining=min(session.expires_at_ms,session.observed_at_ms+MAX_REPORT_AGE_MS,
                          session.launch.approval_expires_at_ms)-now
            _require(remaining>0)
            self._fresh_deadline=min(self._executor._expires,mono+remaining/1000)
        else:
            _require(now>=self._last_wall and mono>=self._last_mono and mono<self._fresh_deadline)
        self._last_wall=now;self._last_mono=mono

    def _check_authority(self):
        """Recheck live pair authority at an admitted coordinator effect."""
        _require(self._state=='starting' and self._observer is not None and self._observer.is_alive()
                 and not self._observer_failed.is_set() and self._broker_alive())
        self._pins();self._validate_executor();self._bind_endpoint()
        self._executor.attest_executor_endpoint(timeout_ms=5000)
        self._pins();self._validate_executor()
        _require(not self._observer_failed.is_set() and self._broker_alive())
        self._fresh(parse_enrollment(self._session))

    def _bind_endpoint(self):
        from .installed_coordinator import parse_configuration
        from .installed_runner import parse_configuration as parse_runner, MAX_CONFIG_BYTES
        from .configuration_commitment import configuration_commitment
        executor=[m for m in self._executor.profile.mounts if m.role=='executor_endpoint_directory']
        coordinator=[m for m in self._coordinator.profile.mounts if m.role=='local_executor_directory']
        _require(len(executor)==len(coordinator)==1)
        left,right=executor[0],coordinator[0]
        config=parse_configuration(self._config)
        _require((left.source,left.uid,left.gid)==(right.source,right.uid,right.gid)
                 and self._executor._executor_endpoint_name==os.path.basename(config.executor_socket))
        mount=self._executor._mounts['config'];fd=os.open(mount.source,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
        try:
            before=os.fstat(fd);named=os.lstat(mount.source)
            _require(stat.S_ISREG(before.st_mode) and before.st_uid==mount.uid and before.st_gid==mount.gid
                     and stat.S_IMODE(before.st_mode)==mount.mode and before.st_nlink==1 and 0<before.st_size<=MAX_CONFIG_BYTES
                     and (before.st_dev,before.st_ino)==(named.st_dev,named.st_ino)==self._executor._source_pins['config'])
            raw=bytearray()
            while len(raw)<=MAX_CONFIG_BYTES:
                part=os.read(fd,min(65536,MAX_CONFIG_BYTES+1-len(raw)))
                if not part:break
                raw.extend(part)
            raw=bytes(raw);after=os.fstat(fd);again=os.lstat(mount.source)
            _require(len(raw)==before.st_size and (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                     ==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)
                     and (before.st_dev,before.st_ino)==(again.st_dev,again.st_ino)
                     and configuration_commitment(raw).raw_digest==self._executor._configuration_raw_digest)
            runner=parse_runner(raw)
            runner_digests=[digest for slot,version,digest in runner.resources['secret_bindings'] if (slot,version)==runner.bearer_slot]
            coordinator_digests=[digest for slot,version,digest in config.bindings if (slot,version)==config.roles['executor']]
            _require(len(runner_digests)==len(coordinator_digests)==1 and runner_digests[0]==coordinator_digests[0]
                     and runner.manifest.digest()==config.manifest.digest()==self._policy['manifest_digest'])
        finally:os.close(fd)

    def _broker_alive(self):
        process=self._broker._process
        return process is not None and process.poll() is None

    def _hold(self):
        self._state='held';self._stop_observer.set()
        if self._observer is not None and self._observer.ident is not None:
            self._observer.join(timeout=10)
        try:self._record('held')
        except BaseException:pass

    def _record(self,state):
        self._pins();self._journal.state(self._policy['pair_id'],state,{'executor_launch_id':self._policy['executor_launch_id'],
            'manifest_digest':self._policy['manifest_digest'],'coordinator_launch_id':getattr(self._coordinator,'_launch_id',None),
            'broker_pid':self._broker.pid})

    def _observe(self):
        try:
            while not self._stop_observer.is_set():
                if self._executor.wait_for_stop_with_observations(timeout_ms=3_600_000,stop_requested=self._stop_observer.is_set): break
        except BaseException: self._observer_failed.set()

    def start(self):
        _require(self._state=='prepared' and not self._start_attempted);self._pins();self._start_attempted=True;self._state='starting'
        try:
            self._validate_executor();self._bind_endpoint();self._executor.attest_executor_endpoint(timeout_ms=5000)
            self._record('broker_reserved')
            self._observer=threading.Thread(target=self._observe,name='hub-pair-observations',daemon=False)
            self._observer.start()
            _require(not self._observer_failed.is_set())
            self._broker.start_for_coordinator(profile=self._coordinator.profile,config_raw=self._config,session_raw=self._session,
                admission_raw=self._admission,admission_raw_digest=self._policy['admission_digest'],
                independent_release=self._coordinator.release,runtime_policy=str(self._runtime_path),
                enrollment_fd=self._broker_fd,listener=self._listener,
                expected_enrollment_digest=self._policy['broker_enrollment_digest'])
            _require(not self._observer_failed.is_set() and self._broker_alive());self._validate_executor();self._bind_endpoint()
            self._executor.attest_executor_endpoint(timeout_ms=5000);self._record('coordinator_reserved')
            _require(getattr(self._coordinator,'_retained_pair',None) is self)
            self._coordinator.launch(retained_pair=self)
            _require(self._coordinator.wait_for_receipt_request(timeout_ms=60000,stop_requested=self._observer_failed.is_set)
                     and not self._observer_failed.is_set())
            self._coordinator.serve_one_receipt(retained_pair=self);_require(not self._observer_failed.is_set() and self._broker_alive())
            self._record('running');self._state='running'
        except BaseException:
            self._hold()
            raise RetainedPairError() from None

    def wait(self, *, timeout_ms, stop_requested):
        _require(self._state=='running');self._pins()
        try:
            requested=self._coordinator.wait_for_coordinator_stop(timeout_ms=timeout_ms,
                stop_requested=lambda: self._observer_failed.is_set() or not self._broker_alive() or stop_requested())
            _require(not self._observer_failed.is_set() and self._broker_alive())
            return requested
        except BaseException:
            self._hold();raise RetainedPairError() from None

    def stop(self, *, coordinator_timeout_ms, broker_timeout_seconds, executor_timeout_ms):
        _require(self._state=='running' and not self._stop_attempted);self._pins();self._stop_attempted=True
        try:
            _require(not self._observer_failed.is_set() and self._broker_alive());self._record('coordinator_stop_reserved')
            self._coordinator.graceful_stop_coordinator(timeout_ms=coordinator_timeout_ms)
            _require(not self._observer_failed.is_set() and self._broker_alive());self._record('broker_stop_reserved')
            self._broker.stop(timeout_seconds=broker_timeout_seconds)
            _require(not self._observer_failed.is_set());self._record('executor_seal_reserved')
            self._executor.seal_pair_drain(pair_id=self._policy['pair_id'],manifest_digest=self._policy['manifest_digest'],timeout_seconds=5)
            self._stop_observer.set();self._observer.join(timeout=10)
            _require(not self._observer.is_alive() and not self._observer_failed.is_set())
            self._record('executor_stop_reserved')
            outcome=self._executor.graceful_stop_sealed_executor(pair_id=self._policy['pair_id'],
                manifest_digest=self._policy['manifest_digest'],timeout_ms=executor_timeout_ms)
            self._record('stopped');self._state='stopped';return outcome
        except BaseException:
            self._hold()
            raise RetainedPairError() from None

    def close(self):
        _require(self._state=='stopped');self._pins()
        failed=False
        for resource in (self._coordinator,self._executor,self._journal):
            try:resource.close()
            except BaseException:failed=True
        self._state='held' if failed else 'closed'
        if failed:raise RetainedPairError()
