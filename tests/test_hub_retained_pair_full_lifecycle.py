"""Real pair control flow with owned synthetic processes, no Docker or host custody.

Engine/kernel mount inspection, executor enrollment/custody, coordinator input
capture and broker enrollment binding are deliberately synthetic boundaries.
The owner, both supervisor lifecycle methods, journals, coordinator receipt and
drain, broker process, signed executor ledger channel and ledger are real.
"""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
pytest.importorskip('fcntl')
from cryptography.hazmat.primitives import serialization
from hub_container_executor import launch_supervisor as launch, retained_oauth_pair as pair
from hub_container_executor import configuration_commitment, github_broker_owner as broker_owner
from hub_container_executor import gate_ledger_channel as gate, ledger as ledger_module
from hub_container_executor.broker_seqpacket import PeerPin, _start
from hub_container_executor.runtime_identity import LaunchReceiptIdentityVerifier
from tests.test_executor_launch_profile import supervised_local_oauth_profile
from tests.test_hub_container_executor_launch_supervisor import synthetic_issuer, short_root, root_journal, v3_constructor, disjoint_policy, current_ready
from tests.test_hub_container_executor_runtime_identity import Issuer
from tests.test_hub_github_broker_service import enrollment, fd_for
from tests.test_hub_trusted_queued_session import v2_document, encode
from engineering_hub.trusted_queued_session import SCHEMA_V3


@pytest.mark.skipif(os.getuid()!=0 or os.environ.get('HUB_SYNTHETIC_PAIRED_TEST') != '1',
                    reason='Explicit disposable root QA container opt-in required')
@pytest.mark.parametrize('child_exit', [0, 7])
def test_retained_pair_real_control_flow_with_synthetic_owned_processes(synthetic_issuer, short_root, root_journal, monkeypatch, request, child_exit):
    executor, _, _ = synthetic_issuer
    current_ready(executor)
    executor._socket_path.chmod(0o660)
    executor._socket_parent = os.open(str(short_root), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    issuer_stat = executor._socket_path.lstat()
    executor._socket_identity = (issuer_stat.st_dev, issuer_stat.st_ino)
    sha = lambda raw: 'sha256:' + hashlib.sha256(raw).hexdigest()
    source_root = str(Path(__file__).resolve().parents[1])
    session_doc = v2_document(); session_doc.update(schema=SCHEMA_V3, operator_actor='human:123')
    session = encode(session_doc); admission = b'synthetic-admission'; config = b'synthetic-config'
    clock = SimpleNamespace(wall=time.time(), mono=time.monotonic())
    binding = (executor._launch_id, executor._consumer.boot, executor._consumer.pid,
               executor._consumer.start, executor._container_id)
    def executor_receipt(payload):
        payload.update(dict(zip(('launch_id','boot_id','authenticated_host_pid','process_start_ticks','container_id'), binding)))
    verifier = LaunchReceiptIdentityVerifier(enrollment=executor.release,
        issuer_transport=Issuer(executor.release, executor._signer, clock, mutate=executor_receipt),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    request.addfinalizer(verifier.close)
    assert verifier.launch_binding() == binding
    monkeypatch.setattr(ledger_module, '_mount_type_for', lambda *_: 'ext4')
    dbroot = short_root/'executor-ledger'; dbroot.mkdir(mode=0o700)
    manifest = 'sha256:'+'a'*64
    ledger = ledger_module.SQLiteLedger(str(dbroot/'ledger.sqlite'), manifest_digest=manifest,
        candidate_commit='b'*40, policy_revision=1)
    request.addfinalizer(ledger.close)
    ledger_listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    request.addfinalizer(ledger_listener.close)
    ledger_path = str(short_root/'ledger.sock'); ledger_listener.bind(ledger_path); ledger_listener.listen(4)
    ledger_worker = gate.OwnedGateLedgerService(listener=ledger_listener, ledger=ledger,
        supervisor_public_key=verifier.issuer_public_key(), launch_verifier=verifier,
        manifest_digest=manifest, hold_uncertain=lambda _: None, timeout_seconds=3)
    request.addfinalizer(ledger_worker.close)
    # Synthetic enrollment boundary; traffic itself uses pair-only signatures,
    # live kernel peer credentials, ledger transactions and outcome ACK.
    ledger_worker._pair_only = True; ledger_worker._require_sender_liveness = True
    ledger_worker.start(on_failure=lambda: None)
    endpoint = short_root/'endpoint'; endpoint.mkdir(mode=0o700)
    endpoint_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    request.addfinalizer(endpoint_listener.close)
    endpoint_path = endpoint/'executor.sock'; endpoint_listener.bind(str(endpoint_path)); endpoint_path.chmod(0o600)
    endpoint_listener.listen(16); endpoint_listener.settimeout(.1)
    endpoint_stop = threading.Event()
    def accept_endpoint():
        while not endpoint_stop.is_set():
            try:
                connection, _ = endpoint_listener.accept(); connection.close()
            except socket.timeout: pass
            except OSError: break
    endpoint_thread = threading.Thread(target=accept_endpoint); endpoint_thread.start()
    def finish_endpoint():
        endpoint_stop.set(); endpoint_listener.close(); endpoint_thread.join(timeout=5)
        assert not endpoint_thread.is_alive()
    request.addfinalizer(finish_endpoint)
    stat_dir, stat_socket = endpoint.stat(), endpoint_path.stat()
    executor.profile = SimpleNamespace(role=None, executor_endpoint='protected-uds-v1', gate_ledger_channel='pair-outcome-listener-v1')
    executor._mounts = {'executor_endpoint_directory':SimpleNamespace(source=str(endpoint),uid=0,gid=0)}
    executor._source_pins = {'executor_endpoint_directory':(stat_dir.st_dev,stat_dir.st_ino)}
    executor._executor_endpoint_name = 'executor.sock'; executor._executor_endpoint_identity=(stat_socket.st_dev,stat_socket.st_ino)
    @contextmanager
    def ledger_client(*, manifest_digest, timeout_seconds):
        channel=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET); channel.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
        peer=None
        try:
            channel.connect(ledger_path)
            peer=PeerPin(pid=os.getpid(),uid=os.getuid(),gid=os.getgid(),start_ticks=_start(os.getpid()))
            client=gate.SupervisorLedgerClient(channel=channel,peer=peer,signing_key=executor._signer,
                launch_binding=binding,manifest_digest=manifest_digest,timeout_seconds=timeout_seconds,pair_only=True)
            yield client
            peer.check(); client.confirm_pair_outcome()
        finally:
            if peer is not None: peer.close()
            channel.close()
    executor.open_gate_ledger_client=ledger_client
    drained=threading.Event(); drain_thread=[]; children=[]; owned=None; broker_fd=None
    real_select=launch.select.select
    def select_exit(read,write,error,timeout=None):
        if read==[executor._consumer.fd] and drained.wait(timeout): return (read,[],[])
        return real_select(read,write,error,timeout)
    monkeypatch.setattr(launch.select,'select',select_exit)
    class ExecutorEngine:
        def json(self,method,path,deadline,**kwargs):
            assert method=='POST' and path.endswith('/kill?signal=SIGTERM')
            def finish():
                try: ledger_worker.post_drain_exchange()
                finally: drained.set()
            thread=threading.Thread(target=finish); drain_thread.append(thread); thread.start()
        def close(self): pass
    executor._engine=ExecutorEngine()
    executor._inspect=lambda *a,**kw: {'Running':False,'Status':'exited','Pid':0,'ExitCode':0,'OOMKilled':False,'Error':''}
    monkeypatch.setattr(pair.RetainedOAuthPair,'_validate_executor',lambda self: None)
    monkeypatch.setattr(pair.RetainedOAuthPair,'_bind_endpoint',lambda self: None)
    monkeypatch.setattr(configuration_commitment,'supervised_local_coordinator_commitment',lambda *a,**kw: None)
    def capture(supervisor):
        supervisor._coordinator_admission=SimpleNamespace(config_digest=sha(config),session_digest=sha(session),timeout_ms=2000)
        supervisor._coordinator_stop_grace=1
    monkeypatch.setattr(launch.LaunchSupervisor,'_pin_trusted_local_inputs',capture)
    with tempfile.TemporaryDirectory(prefix='eh-pair-broker-',dir='/tmp') as broker_temp:
        broker_root=Path(broker_temp); broker_root.chmod(0o755)
        broker_listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        request.addfinalizer(broker_listener.close)
        broker_data=enrollment(broker_root); broker_path=broker_data['socket_path']
        broker_listener.bind(broker_path); os.chown(broker_path,12345,12346); os.chmod(broker_path,0o660); broker_listener.listen(4)
        opened,named=os.fstat(broker_listener.fileno()),os.lstat(broker_path)
        broker_data.update(listener_dev=opened.st_dev,listener_ino=opened.st_ino,path_dev=named.st_dev,path_ino=named.st_ino,
                           secret_digest=sha(b'synthetic'))
        broker_fd=fd_for(broker_root,broker_data)
        monkeypatch.setattr(broker_owner,'_runtime',lambda _: {'source_root':source_root,'python_path':sys.executable})
        monkeypatch.setattr(broker_owner,'_BOOT',"""import os,sys
root=sys.argv[1];gate=int(sys.argv[2]);assert os.read(gate,2)==b'G';os.close(gate);sys.path[:0]=[root,root+'/src']
import hub_container_executor.github_broker_service as s
class R:
 def __init__(self,*a,**k): pass
 def __enter__(self): return self
 def __exit__(self,*a): pass
 def resolve_test_secret(self,*a): return 'synthetic'
s.ImmutableTestSecretResolver=R
from hub_container_executor.github_broker_process import main
raise SystemExit(main(sys.argv[3:]))
""")
        # The actual broker child and drain are used; only protected config-to-
        # broker enrollment binding is synthetic in this fixture.
        monkeypatch.setattr(broker_owner.OwnedBrokerProcess,'start_for_coordinator',
            lambda self,**kw:self.start(runtime_policy=kw['runtime_policy'],enrollment_fd=kw['enrollment_fd'],listener=kw['listener']))
        try:
            with v3_constructor(short_root,root_journal) as (data,kwargs,requests):
                data['schema']='coordinator_launch_supervisor_v4'; data['admission_raw_digest']=sha(admission)
                data['profile']=supervised_local_oauth_profile();data['profile']['daemon_policy']=disjoint_policy()
                next(m for m in data['profile']['mounts'] if m['role']=='local_issuer_socket')['source']=data['socket_path']
                signed=launch.ExecutorLaunchProfile.model_validate(data['profile']);data['release']['launch_profile_digest']=signed.digest()
                raw=launch._canonical(data);Path(kwargs['enrollment_file']).write_bytes(raw)
                executor._daemon_pin=launch._DaemonPin(**data['daemon'],disjoint=True)
                runtime_path=short_root/'pair-runtime';runtime_path.write_bytes(b'synthetic-runtime');runtime_path.chmod(0o600)
                broker_raw=json.dumps(broker_data,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
                policy={'schema':'engineering-hub.retained-oauth-pair.v2','pair_id':str(uuid4()),
                    'journal_file':str(short_root/'pair.sqlite3'),'manifest_digest':manifest,'executor_launch_id':executor._launch_id,
                    'coordinator_profile_digest':signed.digest(),'session_digest':sha(session),'admission_digest':sha(admission),
                    'broker_enrollment_digest':sha(broker_raw),'broker_runtime_policy':str(runtime_path),
                    'broker_runtime_digest':sha(b'synthetic-runtime'),'coordinator_supervisor_digest':sha(raw)}
                policy_path=short_root/'pair-policy';policy_path.write_bytes(launch._canonical(policy));policy_path.chmod(0o600)
                owned=pair.RetainedOAuthPair(executor=executor,pair_policy=str(policy_path),config_raw=config,
                    session_raw=session,admission_raw=admission,broker_enrollment_fd=broker_fd,broker_listener=broker_listener)
                args={k:kwargs[k] for k in ('enrollment_file','signing_key_file','journal_file','held_ranges_file')}
                owned.create_coordinator(**args); coordinator=owned._coordinator
                # Fixture-created placeholder is task-owned and is replaced by
                # the real supervisor bind, not a host or shared socket.
                Path(data['socket_path']).unlink(); coordinator._consumer_uid=coordinator._consumer_gid=0
                coordinator._check_daemon_maps=lambda: None; coordinator._pin_sources=lambda: None
                coordinator._validate_live=lambda deadline,**_: (coordinator._consumer.check(),launch._remaining(deadline))
                child_code="""import json,signal,sys,time
root=sys.argv[1];sys.path[:0]=[root,root+'/src']
from hub_container_executor.runtime_identity import CoordinatorReleaseEnrollment,CoordinatorLaunchReceiptIdentityVerifier
from hub_container_executor.launch_transport import CoordinatorUnixLaunchReceiptTransport
from hub_container_executor.coordinator_drain import report_local_drain
from engineering_hub.container_pilot_manifest import Platform
d=json.loads(sys.argv[3]);d['public_key']=bytes.fromhex(d['public_key']);d['platform']=Platform.model_validate(d['platform']);r=CoordinatorReleaseEnrollment(**d)
stopped=[False];signal.signal(signal.SIGTERM,lambda *_:stopped.__setitem__(0,True))
t=CoordinatorUnixLaunchReceiptTransport(socket_path=sys.argv[2],expected_issuer_uid=0,expected_issuer_gid=0)
v=CoordinatorLaunchReceiptIdentityVerifier(enrollment=r,issuer_transport=t,max_receipt_lifetime_seconds=60,exchange_timeout_ms=2000)
v.implementation_commit();binding=v.launch_binding()
while not stopped[0]:time.sleep(.01)
report_local_drain(socket_path=sys.argv[2],issuer_uid=0,issuer_gid=0,release=r,binding=binding,owner_epoch=sys.argv[4],config_digest=sys.argv[5],session_digest=sys.argv[6],timeout_ms=2000)
v.close();t.close()
raise SystemExit(int(sys.argv[7]))
"""
                release=dict(data['release'])
                class CoordinatorEngine:
                    child=None
                    def image(self,image,deadline):
                        return {'Entrypoint':list(signed.entrypoint),'Cmd':list(signed.command),'Env':list(signed.environment)}
                    def json(self,method,path,deadline,**kw):
                        if path.startswith('/containers/create'): return 201,{'Id':'e'*64,'Warnings':[]}
                        if path.endswith('/start'):
                            self.child=subprocess.Popen([sys.executable,'-I','-B','-c',child_code,source_root,data['socket_path'],
                                json.dumps(release),str(uuid4()),sha(config),sha(session),str(child_exit)],stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env={'LANG':'C.UTF-8'})
                            children.append(self.child);return 204,None
                        if path.endswith('/kill?signal=SIGTERM'):
                            self.child.terminate();return 204,None
                        raise AssertionError(path)
                    def close(self): pass
                coordinator._engine.close(); coordinator._engine=CoordinatorEngine()
                def inspect(deadline,*,running=None):
                    child=coordinator._engine.child
                    if child is None: return {'Running':False,'Status':'created','Pid':0}
                    status=child.poll()
                    return {'Running':status is None,'Status':'running' if status is None else 'exited',
                        'Pid':child.pid if status is None else 0,'ExitCode':status,'OOMKilled':False,'Error':''}
                coordinator._inspect=inspect
                owned.start(); assert owned.wait(timeout_ms=1000,stop_requested=lambda:True)
                if child_exit:
                    with pytest.raises(pair.RetainedPairError):
                        owned.stop(coordinator_timeout_ms=9000,broker_timeout_seconds=5,executor_timeout_ms=9000)
                    assert owned._state=='held' and coordinator._engine.child.wait(timeout=3)==child_exit
                    assert owned._broker._process.poll() is None and not ledger.has_pair_drain_seal()
                else:
                    outcome=owned.stop(coordinator_timeout_ms=9000,broker_timeout_seconds=5,executor_timeout_ms=9000)
                    assert outcome['invalid_receipts']==0 and owned._state=='stopped'
                    assert coordinator._engine.child.wait(timeout=3)==0 and owned._broker._process.poll()==0
                    assert ledger.has_pair_drain_seal()
                    with coordinator._journal.transaction() as db:
                        assert db.execute('SELECT state FROM issuance').fetchone()==('sent',)
                        state, record = db.execute('SELECT state,data FROM launch').fetchone()
                        assert state == 'stopped' and json.loads(record)['local_drain']['owner_epoch']
                    with owned._journal.transaction() as db:
                        assert db.execute('SELECT state FROM launch').fetchone()==('stopped',)
                    owned.close(); assert owned._state=='closed' and not owned._observer.is_alive()
        finally:
            cleanup_errors=[]
            def attempt(action):
                try: action()
                except BaseException as error: cleanup_errors.append(error)
            if owned is not None:
                owned._stop_observer.set()
                if owned._observer is not None:
                    attempt(lambda: owned._observer.join(timeout=5))
                    if owned._observer.is_alive(): cleanup_errors.append(AssertionError('owned observer did not exit'))
                process=owned._broker._process
                if process is not None and process.poll() is None:
                    attempt(lambda: owned._broker.stop(timeout_seconds=5))
                    if process.poll() is None:
                        # This exact process was created by this disposable test.
                        attempt(process.terminate)
                    attempt(lambda: process.wait(timeout=5))
                if process is not None and process.poll() is None:
                    cleanup_errors.append(AssertionError('owned broker did not exit'))
                if process is not None:
                    attempt(lambda: owned._broker.wait(timeout_seconds=5))
            for child in children:
                if child.poll() is None: attempt(child.terminate)
                attempt(lambda child=child: child.wait(timeout=5))
                if child.poll() is None: cleanup_errors.append(AssertionError('owned coordinator did not exit'))
            for thread in drain_thread:
                attempt(lambda thread=thread: thread.join(timeout=5))
                if thread.is_alive(): cleanup_errors.append(AssertionError('owned ledger drain did not exit'))
            if owned is not None and owned._state != 'closed':
                if owned._coordinator is not None: attempt(owned._coordinator.close)
                attempt(owned._executor.close); attempt(owned._journal.close)
            if broker_fd is not None: attempt(lambda: os.close(broker_fd))
            attempt(broker_listener.close)
            endpoint_stop.set(); attempt(endpoint_listener.close); attempt(lambda: endpoint_thread.join(timeout=5))
            if endpoint_thread.is_alive(): cleanup_errors.append(AssertionError('owned endpoint thread did not exit'))
            attempt(ledger_worker.close); attempt(ledger.close); attempt(verifier.close)
            if cleanup_errors: raise AssertionError('synthetic pair cleanup failed') from cleanup_errors[0]
