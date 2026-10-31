import pytest
from scripts import hub_local_windows_parent as parent
from scripts.hub_local_windows_storage import Held


def config(tmp_path):
    return {'daemon_id':'daemon-123456789','docker_root':'/var/lib/docker','base_dir':str(tmp_path),
        'helper_image_id':'sha256:'+'a'*64,'operator_image_id':'sha256:'+'b'*64,
        'archive':'archive','archive_sha256':'sha256:'+'c'*64,'client':'client',
        'daemon_paths':['C:\\p\\script','C:\\p\\archive','C:\\p\\client'],
        'timeout_seconds':60,'inner':{'helper_image_id':'sha256:'+'a'*64,'coordinator_image_id':'sha256:'+'d'*64,
        'initial_manifest':'manifest','candidate':{},'rollback':{},'source_digest':'sha256:'+'e'*64,
        'startup_wait_ms':0,'timeout_seconds':60}}


def test_parent_composes_original_task_and_holds_storage(tmp_path,monkeypatch):
    events=[]
    class Task:
        def __init__(self,**kw): self.task_id=kw['task_id']; self.journal_path=tmp_path/'journal'; events.append('task')
        def record(self,*args,**kw): events.append(args[0])
        def close(self): events.append('close')
    class Storage:
        source_mount='/docker/source'; scratch_mount='/docker/scratch'; journal_mount='/docker/journal'
    monkeypatch.setattr(parent,'WindowsTask',Task)
    monkeypatch.setattr(parent,'_preflight',lambda config:('script',[]))
    monkeypatch.setattr(parent,'_daemon_preflight',lambda *a:events.append('daemon-preflight'))
    monkeypatch.setattr(parent,'provision_storage',lambda task,**kw:(events.append('provision') or Storage()))
    monkeypatch.setattr(parent,'initialize',lambda task,storage,**kw:(events.append('initialize') or 'sha256:'+'e'*64))
    def operator(task,storage,**kw):
        request=kw['request']; assert request['schema']=='engineering-hub.local-launch-request.v2'
        assert request['source_root']=='/docker/source/checkout' and request['socket_group']==0
        events.append('operator'); return object()
    monkeypatch.setattr(parent,'run_operator',operator)
    monkeypatch.setattr(parent,'cleanup',lambda *args:(events.append('cleanup') or {'status':'cleaned_journal_retained','journal_volume':'journal-volume'}))
    result=parent.run(config(tmp_path))
    assert result['status']=='cleaned_journal_retained'
    assert events==['task','daemon-preflight','provision','initialize','operator','cleanup','close']


def test_wrong_source_digest_holds_before_operator(tmp_path,monkeypatch):
    events=[]
    class Task:
        def __init__(self,**kw): self.task_id=kw['task_id']; self.journal_path=tmp_path/'journal'
        def record(self,*args,**kw): events.append(args[0])
        def close(self): events.append('close')
    monkeypatch.setattr(parent,'WindowsTask',Task)
    monkeypatch.setattr(parent,'_preflight',lambda config:('script',[]))
    monkeypatch.setattr(parent,'_daemon_preflight',lambda *a:None)
    monkeypatch.setattr(parent,'provision_storage',lambda *a,**k:object())
    monkeypatch.setattr(parent,'initialize',lambda *a,**k:'sha256:'+'f'*64)
    monkeypatch.setattr(parent,'run_operator',lambda *a,**k:pytest.fail('operator invoked'))
    with pytest.raises(Held): parent.run(config(tmp_path))
    assert events==['parent-held','close']


def test_invalid_image_preflight_occurs_before_task_creation(tmp_path,monkeypatch):
    monkeypatch.setattr(parent,'WindowsTask',lambda **kw:pytest.fail('task created'))
    with pytest.raises(ValueError): parent.run(config(tmp_path))

def test_regular_request_file_rejects_duplicate_keys_before_run(tmp_path,monkeypatch):
    path=tmp_path/'request.json'; path.write_bytes(b'{"a":1,"a":2}')
    monkeypatch.setattr(parent,'run',lambda config:pytest.fail('request accepted'))
    assert parent.main(['--request-file',str(path)])==1
