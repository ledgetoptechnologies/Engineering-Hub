"""Real local resource files + synthetic fixed Docker/absence observations."""
import copy
import hashlib
import json
import os
import http.client
import ipaddress
import threading
from types import SimpleNamespace

import pytest

from hub_staging_service.core import Service, Rejected, recipe
from hub_staging_service.docker import DockerBackend, FrozenDiagnosticFailure
from tests.test_staging_service_docker import diagnostic_stage_probe, profile
from tests.test_staging_service_scratch import payload


def service_probe(tmp_path,monkeypatch,*,stage='build',mode='nonzero',exit_code=7,absence=True):
    host_uid=getattr(os,'getuid',lambda:1000)
    host_gid=getattr(os,'getgid',lambda:1000)
    backend,_,events=diagnostic_stage_probe(monkeypatch,tmp_path,mode=mode,exit_code=exit_code)
    # The reused CLI fixture simulates container UID1000 globally. Preserve the
    # actual host UID for Service's real private-directory ownership guard;
    # only the Docker component receives the synthetic container identity.
    monkeypatch.setattr(os,'getuid',host_uid,raising=False)
    monkeypatch.setattr(os,'getgid',host_gid,raising=False)
    import hub_staging_service.docker as docker
    monkeypatch.setattr(docker,'os',SimpleNamespace(**{**vars(os),'name':'posix','getuid':lambda:1000,'getgid':lambda:1000}))
    monkeypatch.setattr(backend,'_verify_daemon',lambda:'original-engine-A')
    def absent(name):
        events.append(('absent',name))
        if not absence:raise Rejected('execution_uncertain')
    monkeypatch.setattr(backend,'_container_absent',absent)
    monkeypatch.setattr(backend,'_volume_absent',lambda name:events.append(('volume-absent',name)))
    # The fixed recipe CLI and its freeze/export code are real; this synthetic
    # harness does not launch containers or claim actual Engine absence proof.
    def stages(paths,selected,command,**kwargs):
        if stage=='test':
            (paths['build']/'index.html').write_text('prior successful build, never published after failed test')
        return backend._stage_inner(paths,selected,command,stage,**kwargs)
    monkeypatch.setattr(backend,'_run_stages',stages)
    service=Service(tmp_path/'worker',{'demo':profile()},backend)
    return service,backend,events


@pytest.mark.parametrize('stage',['build','test'])
def test_failed_recipe_registers_only_frozen_test_run_and_exact_manifest(tmp_path,monkeypatch,stage):
    service,backend,events=service_probe(tmp_path,monkeypatch,stage=stage)
    try:
        job=payload();service.submit(job);service.wait()
        result=service.status(job['job_id'])
        assert result['state']=='failed' and result['result']['error']=='stage_'+stage+'_failed'
        fact=service.metadata(job['resource_ids']['test_run'])
        diagnostic=result['result']['diagnostic']
        assert fact['state']=='ready' and stage+'/failure.json' in fact['manifest']
        digest=hashlib.sha256(json.dumps(fact['manifest'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        assert diagnostic['manifest_sha256']==digest and diagnostic['resource_id']==fact['id']
        assert diagnostic['scope']=='original_engine_owned_writers_exporters_volumes_absent'
        for kind in ('build','preview','workspace'):
            assert service.metadata(job['resource_ids'][kind])['state']=='building'
        assert any(e[0]=='volume-absent' for e in events if isinstance(e,tuple))
        if os.name=='posix':
            assert service.diagnostic_file(fact['id'],digest,stage+'/failure.json')==b'{"fixture":"failed"}'
    finally:service.close()


@pytest.mark.parametrize('mode,exit_code,absence',[
    ('timeout',7,True),('signal',7,True),('not_paused',7,True),('pause_failure',7,True),
    ('export_uncertain',7,True),('unpaused_after_export',7,True),('nonzero',137,True),
    ('nonzero',7,False)])
def test_unknown_or_unfrozen_failure_is_held_and_never_registered(tmp_path,monkeypatch,mode,exit_code,absence):
    service,_,_=service_probe(tmp_path,monkeypatch,mode=mode,exit_code=exit_code,absence=absence)
    try:
        job=payload();service.submit(job);service.wait()
        result=service.status(job['job_id'])
        assert result['state']=='held' and 'diagnostic' not in result['result']
        assert service.metadata(job['resource_ids']['test_run'])['state']=='building'
        for kind in ('build','preview'):
            assert service.metadata(job['resource_ids'][kind])['state']=='building'
    finally:service.close()


def test_log_only_failure_requires_the_same_explicit_absence_proof(tmp_path,monkeypatch):
    service,backend,_=service_probe(tmp_path,monkeypatch)
    monkeypatch.setattr(backend,'_export',lambda *args,**kwargs:None)
    try:
        job=payload();service.submit(job);service.wait()
        fact=service.metadata(job['resource_ids']['test_run'])
        assert service.status(job['job_id'])['state']=='failed' and set(fact['manifest'])=={'build.log'}
    finally:service.close()


@pytest.mark.skipif(os.name!='posix',reason='Descriptor-only worker download')
@pytest.mark.parametrize('tamper',['bytes','symlink','hardlink','manifest','held','wrong-sha','traversal','large'])
def test_registered_download_rejects_changed_or_unsafe_artifact(tmp_path,monkeypatch,tamper):
    service,_,_=service_probe(tmp_path,monkeypatch)
    try:
        job=payload();service.submit(job);service.wait();identity=job['resource_ids']['test_run']
        fact=service.metadata(identity);digest=service.status(job['job_id'])['result']['diagnostic']['manifest_sha256']
        name='build/failure.json';path=service.resources/identity/'build'/'failure.json'
        if tamper=='bytes':path.write_text('different')
        elif tamper=='symlink':path.unlink();path.symlink_to(service.root/'journal.sqlite3')
        elif tamper=='hardlink':os.link(path,path.with_name('second'))
        elif tamper=='manifest':
            service.db.execute('UPDATE resources SET manifest=? WHERE id=?',('{}',identity));service.db.commit()
        elif tamper=='held':
            service.db.execute("UPDATE operations SET state='held' WHERE job_id=?",(job['job_id'],));service.db.commit()
        elif tamper=='wrong-sha':digest='b'*64
        elif tamper=='traversal':name='../journal.sqlite3'
        else:
            fact['manifest'][name]['bytes']=8*1024**2+1
            raw=json.dumps(fact['manifest'],sort_keys=True,separators=(',',':'))
            digest=hashlib.sha256(raw.encode()).hexdigest()
            result=service.status(job['job_id'])['result'];result['diagnostic']['manifest_sha256']=digest
            service.db.execute('UPDATE resources SET manifest=? WHERE id=?',(raw,identity))
            service.db.execute('UPDATE operations SET result=? WHERE job_id=?',(json.dumps(result),job['job_id']));service.db.commit()
        with pytest.raises((Rejected,OSError)):
            service.diagnostic_file(identity,digest,name)
    finally:service.close()


def test_non_scratch_engine_retarget_before_cleanup_cannot_issue_diagnostic_receipt(tmp_path,monkeypatch):
    service,backend,_=service_probe(tmp_path,monkeypatch)
    original=backend._run_stages
    def changed(*args,**kwargs):
        try:return original(*args,**kwargs)
        finally:monkeypatch.setattr(backend,'_verify_daemon',lambda:'foreign-engine-B')
    monkeypatch.setattr(backend,'_run_stages',changed)
    try:
        job=payload();service.submit(job);service.wait()
        assert service.status(job['job_id'])['state']=='held'
        assert service.metadata(job['resource_ids']['test_run'])['state']=='building'
    finally:service.close()


@pytest.mark.skipif(os.name!='posix',reason='Actual descriptor-only worker download')
def test_private_worker_get_downloads_registered_bytes_with_manifest_binding(tmp_path,monkeypatch):
    from hub_staging_service.__main__ import BoundedServer, handler
    service,_,_=service_probe(tmp_path,monkeypatch)
    server=None;thread=None
    try:
        job=payload();service.submit(job);service.wait();identity=job['resource_ids']['test_run']
        digest=service.status(job['job_id'])['result']['diagnostic']['manifest_sha256']
        token='synthetic-private-diagnostic-token'
        server=BoundedServer(('127.0.0.1',0),handler(service,token,{ipaddress.ip_address('127.0.0.1')}))
        thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=0.01));thread.start()
        def get(path,authorized=True):
            connection=http.client.HTTPConnection(*server.server_address,timeout=3)
            try:
                connection.request('GET',path,headers={'Authorization':'Bearer '+token} if authorized else {})
                response=connection.getresponse();return response.status,response.read(),dict(response.getheaders())
            finally:connection.close()
        route='/v1/resources/'+identity+'/diagnostics/'+digest+'/build/failure.json'
        assert get(route,False)[0]==401
        code,data,headers=get(route)
        assert code==200 and data==b'{"fixture":"failed"}' and headers['Content-Type']=='application/octet-stream'
        assert headers['X-Content-Type-Options']=='nosniff'
        assert get(route.replace(digest,'b'*64))[0]==409
        assert get('/v1/resources/'+identity+'/files/build/failure.json')[0]==409
    finally:
        if server is not None:server.shutdown();server.server_close()
        if thread is not None:thread.join(timeout=3);assert not thread.is_alive()
        service.close()
