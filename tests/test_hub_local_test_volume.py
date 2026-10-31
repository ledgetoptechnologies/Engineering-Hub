import importlib.util
from pathlib import Path
import sys
import uuid
import json
import pytest

spec = importlib.util.spec_from_file_location('hub_local_test_volume', Path(__file__).parents[1] / 'scripts' / 'hub_local_test_volume.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

TASK = 'f27cc5ed-c03f-4397-90ff-02c6e94a0b91'
DAEMON = 'daemon-1234567890'
NAME = 'hub-local-test-' + TASK

class Docker:
    def __init__(self): self.calls=[]; self.fault=None; self.existing=False
    def __call__(self, argv):
        self.calls.append(argv)
        if argv[3] == 'info': return '/var/lib/docker' if 'DockerRootDir' in argv[-1] else DAEMON
        if argv[3:5] == ['volume','ls']: return NAME if self.existing else ''
        if argv[3:5] == ['volume','create']:
            if self.fault == 'create': raise TimeoutError
            return NAME
        if argv[3:5] == ['volume','inspect']:
            d={'Name':NAME,'Driver':'local','Scope':'local','Labels':{m.LABEL:TASK},'Mountpoint':'/var/lib/docker/volumes/'+NAME+'/_data','Options':{}}
            if self.fault == 'inspect': d['Labels']={}
            return json.dumps([d])
        raise AssertionError(argv)

def test_create_exact_owned_volume(tmp_path):
    d=Docker(); v=m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=d)
    assert v.name == NAME and v.mountpoint.endswith(NAME+'/_data')
    assert all(c[:3]==['/usr/bin/docker','--host','unix:///var/run/docker.sock'] for c in d.calls)


def test_tmpfs_volume_pins_options_and_precreate_intent(tmp_path):
    d=Docker()
    def run(argv):
        result=d(argv)
        if argv[3:5]==['volume','inspect']:
            value=json.loads(result); value[0]['Options']=dict(m.TMPFS_OPTIONS); return json.dumps(value)
        return result
    volume=m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,storage='tmpfs-512m-v1',run=run)
    create=next(call for call in d.calls if call[3:5]==['volume','create'])
    assert volume.options==m.TMPFS_OPTIONS and create[-1]==NAME
    assert [create[i+1] for i,value in enumerate(create) if value=='--opt']==[k+'='+v for k,v in m.TMPFS_OPTIONS]
    intent=json.loads((tmp_path/(TASK+'.intent')).read_text())
    assert intent['storage']=='tmpfs-512m-v1'


def test_tmpfs_wrong_daemon_options_are_held_not_removed(tmp_path):
    d=Docker()
    with pytest.raises(m.VolumeOutcomeUnknown):
        m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,storage='tmpfs-512m-v1',run=d)
    assert not any('rm' in call for call in d.calls)
    assert (tmp_path/(TASK+'.intent')).is_file()

def test_existing_never_adopted(tmp_path):
    d=Docker(); d.existing=True
    with pytest.raises(m.VolumeError): m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=d)
    assert not any('create' in x for x in d.calls)

@pytest.mark.parametrize('fault',['create','inspect'])
def test_ambiguous_effect_is_held_without_removal(fault,tmp_path):
    d=Docker(); d.fault=fault
    with pytest.raises(m.VolumeOutcomeUnknown): m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=d)
    assert not any('rm' in x for x in d.calls)

def test_mismatch_and_bad_endpoint_reject_before_create(tmp_path):
    d=Docker()
    with pytest.raises(m.VolumeError): m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id='wrong-daemon-id',run=d)
    with pytest.raises(m.VolumeError): m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,endpoint='tcp://remote',run=d)
    assert not any('create' in x for x in d.calls)



def test_rejects_mountpoint_not_under_pinned_daemon_root(tmp_path):
    d=Docker()
    def bad(args):
        result=d(args)
        if args[3:5] == ['volume','inspect']:
            obj=json.loads(result); obj[0]['Mountpoint']='/etc'; return json.dumps(obj)
        return result
    with pytest.raises(m.VolumeOutcomeUnknown): m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=bad)
    assert not any('rm' in x for x in d.calls)

def test_daemon_change_after_create_is_unknown(tmp_path):
    d=Docker(); count=0
    def changed(args):
        nonlocal count
        if args[3] == 'info' and 'ID' in args[-1]:
            count += 1
            if count == 2: return 'other-daemon'
        return d(args)
    with pytest.raises(m.VolumeOutcomeUnknown): m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=changed)


def test_intent_survives_unknown_and_blocks_retry(tmp_path):
    d=Docker(); d.fault='create'
    with pytest.raises(m.VolumeOutcomeUnknown): m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=d)
    before=len([x for x in d.calls if 'create' in x]); d.fault=None
    with pytest.raises(m.VolumeError): m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=d)
    assert len([x for x in d.calls if 'create' in x]) == before
    assert (tmp_path / (TASK + '.intent')).is_file()

def test_bounded_subprocess_rejects_oversized_output():
    import sys
    with pytest.raises(ValueError):
        m._call([sys.executable, '-c', 'print("x" * 70000)'])

def test_lost_inspection_is_unknown(tmp_path):
    d=Docker()
    def lost(args):
        if args[3:5] == ['volume','inspect']: raise TimeoutError
        return d(args)
    with pytest.raises(m.VolumeOutcomeUnknown):
        m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=lost)
    assert (tmp_path / (TASK + '.intent')).exists()

def test_symlinked_journal_ancestor_denies(tmp_path):
    actual=tmp_path/'actual'; actual.mkdir(mode=0o700)
    alias=tmp_path/'alias'; alias.symlink_to(actual, target_is_directory=True)
    d=Docker()
    with pytest.raises(m.VolumeError):
        m.create_volume(journal_dir=str(alias),task_id=TASK,daemon_id=DAEMON,run=d)
    assert not any('create' in x for x in d.calls)

def test_journal_fsync_failure_never_creates(tmp_path,monkeypatch):
    d=Docker()
    def fail(fd): raise OSError('fsync failed')
    monkeypatch.setattr(m.os,'fsync',fail)
    with pytest.raises(m.VolumeError):
        m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=d)
    assert not any('create' in x for x in d.calls)
    assert (tmp_path / (TASK + '.intent')).exists()

def test_changed_named_directory_denies_create(tmp_path,monkeypatch):
    d=Docker(); original=m._check_path; calls=0
    def changed(handles):
        nonlocal calls
        calls+=1
        if calls==3: raise ValueError('changed')
        return original(handles)
    monkeypatch.setattr(m,'_check_path',changed)
    with pytest.raises(m.VolumeOutcomeUnknown):
        m.create_volume(journal_dir=str(tmp_path),task_id=TASK,daemon_id=DAEMON,run=d)
    assert not any('create' in x for x in d.calls)

@pytest.mark.parametrize('prefix',['//','/tmp/../'])
def test_ambiguous_journal_paths_denied(prefix,tmp_path):
    d=Docker(); path=prefix + str(tmp_path).lstrip('/')
    with pytest.raises(m.VolumeError): m.create_volume(journal_dir=path,task_id=TASK,daemon_id=DAEMON,run=d)
    assert not any('create' in x for x in d.calls)

def test_close_handles_attempts_all(monkeypatch):
    seen=[]
    def close(fd):
        seen.append(fd)
        if fd==2: raise OSError
    monkeypatch.setattr(m.os,'close',close)
    with pytest.raises(OSError): m._close_handles([(1,None,None),(2,None,None),(3,None,None)])
    assert seen==[3,2,1]

def test_sealed_volume_remove_once(tmp_path,monkeypatch):
    import threading
    from scripts.hub_local_test_engine_broker import CleanedBrokerSnapshot
    snap=CleanedBrokerSnapshot(TASK,DAEMON,NAME,'/var/lib/docker/volumes/'+NAME+'/_data','a'*64,())
    t=threading.Thread(target=lambda:None); t.start(); t.join()
    d=Docker()
    def run(args):
        if args[3:5]==['ps','--all']: return ''
        if args[3:5]==['volume','rm']: d.calls.append(args); return NAME
        return d(args)
    v=m.OwnedVolume(NAME,TASK,DAEMON,snap.volume_mount)
    m.remove_sealed_volume(volume=v,snapshot=snap,server_thread=t,journal_dir=str(tmp_path),run=run)
    assert (tmp_path/(TASK+'.delete-intent')).exists()
    with pytest.raises(m.VolumeError): m.remove_sealed_volume(volume=v,snapshot=snap,server_thread=t,journal_dir=str(tmp_path),run=run)
    assert len([x for x in d.calls if 'rm' in x])==1

def test_present_owned_container_blocks_removal(tmp_path,monkeypatch):
    import threading
    from scripts.hub_local_test_engine_broker import CleanedBrokerSnapshot
    snap=CleanedBrokerSnapshot(TASK,DAEMON,NAME,'/var/lib/docker/volumes/'+NAME+'/_data','a'*64,(('b'*64,'owned'),))
    t=threading.Thread(target=lambda:None); t.start(); t.join(); d=Docker()
    def run(args): return 'b'*64 if args[3:5]==['ps','--all'] else d(args)
    with pytest.raises(m.VolumeError): m.remove_sealed_volume(volume=m.OwnedVolume(NAME,TASK,DAEMON,snap.volume_mount),snapshot=snap,server_thread=t,journal_dir=str(tmp_path),run=run)
    assert not (tmp_path/(TASK+'.delete-intent')).exists()

def test_lost_volume_remove_is_unknown_and_not_replayed(tmp_path,monkeypatch):
    import threading
    from scripts.hub_local_test_engine_broker import CleanedBrokerSnapshot
    snap=CleanedBrokerSnapshot(TASK,DAEMON,NAME,'/var/lib/docker/volumes/'+NAME+'/_data','a'*64,())
    t=threading.Thread(target=lambda:None); t.start(); t.join(); d=Docker(); attempts=[]
    def run(args):
        if args[3:5]==['ps','--all']: return ''
        if args[3:5]==['volume','rm']: attempts.append(1); raise TimeoutError
        return d(args)
    v=m.OwnedVolume(NAME,TASK,DAEMON,snap.volume_mount)
    with pytest.raises(m.VolumeOutcomeUnknown): m.remove_sealed_volume(volume=v,snapshot=snap,server_thread=t,journal_dir=str(tmp_path),run=run)
    with pytest.raises(m.VolumeError): m.remove_sealed_volume(volume=v,snapshot=snap,server_thread=t,journal_dir=str(tmp_path),run=run)
    assert len(attempts)==1

def test_sealed_but_not_cleaned_snapshot_denied(tmp_path):
    import threading
    from scripts.hub_local_test_engine_broker import ClosedBrokerSnapshot
    snap=ClosedBrokerSnapshot(TASK,DAEMON,NAME,'/var/lib/docker/volumes/'+NAME+'/_data','a'*64,())
    t=threading.Thread(target=lambda:None); t.start(); t.join(); d=Docker()
    with pytest.raises(m.VolumeError): m.remove_sealed_volume(volume=m.OwnedVolume(NAME,TASK,DAEMON,snap.volume_mount),snapshot=snap,server_thread=t,journal_dir=str(tmp_path),run=d)
    assert not d.calls
