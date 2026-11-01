import hashlib,io,os,tarfile,time
import pytest
from scripts.hub_local_operator_seed import seed,verify
from scripts.hub_local_test_launcher import source_tree_digest

def inputs(tmp_path,name='a/b/file',kind=tarfile.REGTYPE,compressed=False):
    archive=tmp_path/'source.tar'; client=tmp_path/'client'; client.write_bytes(b'client')
    with tarfile.open(archive,'w:gz' if compressed else 'w') as t:
        item=tarfile.TarInfo(name); item.type=kind; item.size=1 if kind==tarfile.REGTYPE else 0
        t.addfile(item,io.BytesIO(b'x') if item.size else None)
    roots=[tmp_path/x for x in ('source','scratch','journal')]
    for p in roots: p.mkdir()
    digest=lambda p:'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()
    return archive,client,roots,digest(archive),digest(client)

def run(values,**kw):
    archive,client,roots,ah,ch=values
    return seed(str(archive),str(client),*(str(x) for x in roots),archive_sha256=kw.get('ah',ah),client_sha256=ch,deadline_monotonic=kw.get('deadline',time.monotonic()+10))

def test_seed_exact_content_modes_and_cli(tmp_path):
    v=inputs(tmp_path); result=run(v); roots=v[2]
    assert result=={'archive_sha256':v[3],'client_sha256':v[4],
                    'source_digest':source_tree_digest(str(roots[0]/'checkout'),deadline_monotonic=time.monotonic()+10)}
    assert (roots[0]/'checkout/a/b/file').read_bytes()==b'x'
    assert (roots[0]/'docker').read_bytes()==b'client'
    for p in (roots[0]/'checkout/a',roots[0]/'checkout/a/b'): assert os.stat(p).st_mode&0o777==0o555
    for p in roots[1:]: assert (os.stat(p).st_uid,os.stat(p).st_gid,os.stat(p).st_mode&0o777)==(10001,10001,0o700)
    checked=verify(*(str(p) for p in roots),client_sha256=v[4],deadline_monotonic=time.monotonic()+10)
    assert checked['source_digest']==result['source_digest'] and checked['client_sha256']==v[4]
    for invalid in (float('nan'),True):
        with pytest.raises(ValueError): verify(*(str(p) for p in roots),client_sha256=v[4],deadline_monotonic=invalid)
    os.chmod(roots[0]/'docker',0o644)
    with pytest.raises(ValueError): verify(*(str(p) for p in roots),client_sha256=v[4],deadline_monotonic=time.monotonic()+10)

def test_bad_hash_or_alias_has_no_effect(tmp_path):
    v=inputs(tmp_path)
    with pytest.raises(ValueError): run(v,ah='sha256:'+'0'*64)
    assert all(not list(p.iterdir()) for p in v[2])
    with pytest.raises(ValueError): seed(str(v[0]),str(v[1]),str(v[2][0]),str(v[2][0]),str(v[2][2]),archive_sha256=v[3],client_sha256=v[4],deadline_monotonic=time.monotonic()+10)
    assert all(not list(p.iterdir()) for p in v[2])

@pytest.mark.parametrize('name,kind,compressed',[('../escape',tarfile.REGTYPE,False),('link',tarfile.SYMTYPE,False),('a',tarfile.REGTYPE,True)])
def test_unsafe_archive_denied(tmp_path,name,kind,compressed):
    v=inputs(tmp_path,name,kind,compressed)
    with pytest.raises((ValueError,tarfile.ReadError)): run(v)
    assert not (tmp_path/'escape').exists()

def test_expired_deadline_does_not_create_checkout(tmp_path):
    v=inputs(tmp_path)
    with pytest.raises(TimeoutError): run(v,deadline=time.monotonic()-1)
    assert all(not list(p.iterdir()) for p in v[2])

def test_deadline_expiring_before_final_chown_denies_chown(tmp_path,monkeypatch):
    import scripts.hub_local_operator_seed as module
    v=inputs(tmp_path); now=[0.0]; calls=[]; original=os.fchmod
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    def chmod(fd,mode):
        original(fd,mode)
        if mode==0o700: now[0]=11.0
    monkeypatch.setattr(module.os,'fchmod',chmod)
    monkeypatch.setattr(module.os,'fchown',lambda *args:calls.append(args))
    with pytest.raises(TimeoutError): run(v,deadline=10.0)
    assert calls==[]
