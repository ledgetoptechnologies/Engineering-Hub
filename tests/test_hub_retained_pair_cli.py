"""Root-owned synthetic entrypoint orchestration; no daemon or host changes."""
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
pytest.importorskip('fcntl')
from hub_container_executor import retained_pair_cli as cli


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


@pytest.mark.skipif(os.geteuid()!=0, reason='root-owned disposable test files')
@pytest.mark.parametrize('failure', ['none', 'start', 'preexisting', 'stop_during_owner', 'stop_during_coordinator', 'expired_report', 'broken_status'])
def test_entrypoint_retains_single_owner_and_holds_uncertainty(tmp_path, monkeypatch, failure):
    root=tmp_path/'private';root.mkdir(mode=0o700)
    paths={name:str(root/name) for name in ('executor','key','journal','held','handoff','pair','config','session',
                                             'admission','coordinator','coordinator-key','coordinator-journal',
                                             'coordinator-held','broker')}
    for name in ('config','session','admission'):
        Path(paths[name]).write_bytes(name.encode());os.chmod(paths[name],0o600)
    handoff={'schema':'engineering-hub.retained-pair-handoff.v1','pair_policy':paths['pair'],
        'config_file':paths['config'],'session_file':paths['session'],'admission_file':paths['admission'],
        'coordinator_enrollment':paths['coordinator'],'coordinator_key':paths['coordinator-key'],
        'coordinator_journal':paths['coordinator-journal'],'held_ranges':paths['coordinator-held'],
        'broker_enrollment':paths['broker']}
    if failure=='preexisting':
        Path(paths['handoff']).write_bytes(canonical(handoff));os.chmod(paths['handoff'],0o600)
    server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(root/'listener'));server.listen(1)
    listener_fd=server.detach()
    setup={'schema':'engineering-hub.retained-pair-operator.v1','executor_enrollment':paths['executor'],
        'executor_key':paths['key'],'executor_journal':paths['journal'],'held_ranges':paths['held'],
        'handoff_file':paths['handoff'],'broker_listener_fd':listener_fd,'report_wait_seconds':30,
        'run_wait_seconds':30,'executor_stop_timeout_ms':5000,'broker_stop_seconds':5}
    setup_path=root/'operator';setup_path.write_bytes(canonical(setup));setup_path.chmod(0o600)
    events=[]; holder={}; opened=[]
    class Parked(BaseException): pass
    class Supervisor:
        def __init__(self, **_):
            events.append('construct-executor');self.profile=SimpleNamespace(role=None,executor_endpoint='protected-uds-v1',gate_ledger_channel='pair-outcome-listener-v1')
            self._launch_id='launch';self._container_id='container';self._consumer=SimpleNamespace(boot='boot',pid=123,start=456)
            self._approval_expires_at_ms=1;self._mutex=threading.Lock()
        def launch(self): events.append('launch')
        def wait_for_receipt_request(self, **_):return True
        def serve_one_receipt(self):
            events.append('receipt');Path(paths['handoff']).write_bytes(canonical(handoff));os.chmod(paths['handoff'],0o600)
            holder['signals'].ready=True
        def wait_for_stop_with_observations(self, **kw): events.append('observations');return kw['stop_requested']()
        def _hold(self):events.append('executor-held')
        def close(self):events.append('executor-close')
    class Owner:
        def __init__(self, **kw):
            assert kw['config_raw']==b'config' and kw['session_raw']==b'session' and kw['admission_raw']==b'admission'
            events.append('owner');self._policy={'pair_id':'synthetic-pair'}
            self._coordinator=SimpleNamespace(_coordinator_stop_grace=1120,_coordinator_admission=SimpleNamespace(timeout_ms=2000))
            if failure=='stop_during_owner':holder['signals'].stop=True
        def create_coordinator(self, **_):
            events.append('create-coordinator')
            if failure=='stop_during_coordinator':holder['signals'].stop=True
        def start(self):
            events.append('start')
            if failure in ('start','broken_status'):raise RuntimeError('synthetic')
        def wait(self, **kw):holder['signals'].stop=True;events.append('wait');return kw['stop_requested']()
        def stop(self, **_):events.append('stop')
        def close(self):events.append('close')
        def _hold(self):events.append('owner-held')
    def install(state):holder['signals']=state;return []
    original_parent=cli._root_parent
    def parent(*args,**kwargs):
        fd=original_parent(*args,**kwargs);opened.append(fd);return fd
    def open_broker(_):
        fd=os.open('/dev/null',os.O_RDONLY);opened.append(fd);return fd
    monkeypatch.setattr(cli,'_root_parent',parent);monkeypatch.setattr(cli,'_broker_fd',open_broker)
    monkeypatch.setattr(cli,'_host_available',lambda:True);monkeypatch.setattr(cli,'_install_signals',install)
    monkeypatch.setattr(cli,'LaunchSupervisor',Supervisor);monkeypatch.setattr(cli,'RetainedOAuthPair',Owner)
    monkeypatch.setattr(cli,'_park_held',lambda: (_ for _ in ()).throw(Parked()))
    if failure=='expired_report':
        clock=[100.0];original_root_file=cli._root_file
        monkeypatch.setattr(cli.time,'monotonic',lambda:clock[0])
        def delayed(path,maximum):
            result=original_root_file(path,maximum)
            if str(path)==paths['session']:clock[0]=200.0
            return result
        monkeypatch.setattr(cli,'_root_file',delayed)
    if failure=='broken_status':
        original_emit=cli._emit
        def emit(state,**fields):
            if state=='held':raise BrokenPipeError()
            return original_emit(state,**fields)
        monkeypatch.setattr(cli,'_emit',emit)
    try:
        if failure in ('start','stop_during_owner','stop_during_coordinator','expired_report','broken_status'):
            with pytest.raises(Parked):cli.main(['--operator-file',str(setup_path),'--execute'])
            assert events[-1]==('executor-held' if failure=='expired_report' else 'owner-held')
            assert 'close' not in events and 'stop' not in events
            if failure in ('start','broken_status'):assert events[-2]=='start'
            else: assert 'start' not in events
        else:
            result=cli.main(['--operator-file',str(setup_path),'--execute'])
            if failure=='preexisting':
                assert result==2 and events==[]
            else:
                assert result==0 and events==['construct-executor','launch','receipt','observations','owner',
                    'create-coordinator','start','wait','stop','close']
    finally:
        # Test-only cleanup of descriptors intentionally retained by the held path.
        for fd in [listener_fd,*opened]:
            try:os.close(fd)
            except OSError:pass


def test_schema_is_closed_and_dry_run_reads_no_files(monkeypatch, capsys):
    monkeypatch.setattr(cli,'_root_file',lambda *_:pytest.fail('dry run read filesystem'))
    assert cli.main(['--operator-file','/root/unused'])==0
    assert json.loads(capsys.readouterr().out)=={'state':'not_executed'}
    with pytest.raises(Exception):cli._startup(canonical({'schema':'engineering-hub.retained-pair-operator.v1'}))


def test_isolated_module_dry_run_has_no_filesystem_prerequisites():
    root=str(Path(__file__).resolve().parents[1])
    code=("import runpy,sys; root=sys.argv[1]; sys.path[:0]=[root,root+'/src']; "
          "sys.argv=['retained_pair_cli','--operator-file','/nonexistent/operator']; "
          "runpy.run_module('hub_container_executor.retained_pair_cli',run_name='__main__')")
    result=subprocess.run([sys.executable,'-I','-B','-c',code,root],stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={'LANG':'C.UTF-8'},timeout=5)
    assert result.returncode==0 and json.loads(result.stdout)=={'state':'not_executed'} and result.stderr==b''


@pytest.mark.skipif(os.geteuid()!=0, reason='root-owned disposable test files')
def test_broker_enrollment_requires_real_stable_root_owned_file(tmp_path):
    from tests.test_hub_github_broker_service import enrollment, fd_for
    root=tmp_path/'private';root.mkdir(mode=0o700)
    initial=fd_for(root,enrollment(root));os.close(initial)
    fd=cli._broker_fd(str(root/'enrollment'))
    os.close(fd)
    (root/'alias').symlink_to(root/'enrollment')
    with pytest.raises(Exception):cli._broker_fd(str(root/'alias'))
