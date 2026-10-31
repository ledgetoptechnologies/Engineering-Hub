import hashlib
import io
import os
import tarfile
import time

import pytest

from scripts.hub_local_operator_storage import prepare_source
from scripts.hub_local_test_launcher import source_tree_digest


def make_archive(path, name='pkg/a.py', kind=tarfile.REGTYPE):
    with tarfile.open(path,'w') as tar:
        item=tarfile.TarInfo(name); item.type=kind
        data=b'print(1)\n'; item.size=len(data) if kind==tarfile.REGTYPE else 0
        tar.addfile(item,io.BytesIO(data) if item.size else None)
    return 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_source_prepared_and_digest_matches_launcher(tmp_path):
    archive=tmp_path/'source.tar'; sha=make_archive(archive)
    path,digest=prepare_source(str(archive),str(tmp_path/'out'),archive_sha256=sha,deadline_monotonic=time.monotonic()+10)
    assert (tmp_path/'out/pkg/a.py').read_bytes()==b'print(1)\n'
    assert os.stat(tmp_path/'out/pkg/a.py').st_mode & 0o777==0o444
    assert digest==source_tree_digest(path,deadline_monotonic=time.monotonic()+10)
    with pytest.raises(FileExistsError): prepare_source(str(archive),path,archive_sha256=sha,deadline_monotonic=time.monotonic()+10)


@pytest.mark.parametrize('name,kind',[('../escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE),('.git/config',tarfile.REGTYPE)])
def test_unsafe_members_fail_without_escape(tmp_path,name,kind):
    archive=tmp_path/'source.tar'; sha=make_archive(archive,name,kind)
    with pytest.raises(ValueError): prepare_source(str(archive),str(tmp_path/'out'),archive_sha256=sha,deadline_monotonic=time.monotonic()+10)
    assert not (tmp_path/'escape').exists()


def test_bad_hash_creates_no_output(tmp_path):
    archive=tmp_path/'source.tar'; make_archive(archive)
    with pytest.raises(ValueError): prepare_source(str(archive),str(tmp_path/'out'),archive_sha256='sha256:'+'0'*64,deadline_monotonic=time.monotonic()+10)
    assert not (tmp_path/'out').exists()


def test_nested_directory_only_normalizes_all_ancestors(tmp_path):
    archive=tmp_path/'source.tar'; sha=make_archive(archive,'a/b',tarfile.DIRTYPE)
    prepare_source(str(archive),str(tmp_path/'out'),archive_sha256=sha,deadline_monotonic=time.monotonic()+10)
    assert os.stat(tmp_path/'out/a').st_mode & 0o777==0o555
    assert os.stat(tmp_path/'out/a/b').st_mode & 0o777==0o555


def test_compressed_archive_is_rejected(tmp_path):
    archive=tmp_path/'source.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        entry=tarfile.TarInfo('a'); entry.size=1; tar.addfile(entry,io.BytesIO(b'x'))
    sha='sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(tarfile.ReadError):
        prepare_source(str(archive),str(tmp_path/'out'),archive_sha256=sha,deadline_monotonic=time.monotonic()+10)
