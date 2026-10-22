"""Pure mount metadata and pre-open repository admission; no real mount changes."""
from pathlib import PurePosixPath

import pytest

from engineering_hub import image_state_boundary as boundary
from engineering_hub import repository


BASE='10 1 0:42 / / rw,relatime - overlay overlay rw\n'
PARENT='20 10 0:42 /volume/_data /var/lib/hub rw,relatime - ext4 /dev/example rw\n'
SELECTIVE=('21 10 0:42 /volume/_data/settings.json /var/lib/hub/settings.json ro - ext4 /dev/example rw\n'
    '22 10 0:42 /volume/_data/agent-message-bus /var/lib/hub/agent-message-bus rw - ext4 /dev/example rw\n'
    '23 10 0:42 /volume/_data/private /var/lib/hub-private rw - ext4 /dev/example rw\n')


@pytest.mark.parametrize('text',[BASE,BASE+SELECTIVE,
    BASE+'21 10 0:42 /volume/settings.json /var/lib/hub/settings.json ro - ext4 /dev/example rw\n',
    BASE+'21 10 0:42 /volume /var/lib/hub-other rw - ext4 /dev/example rw\n'])
def test_nested_file_and_similar_prefix_mounts_are_not_the_parent(text):
    with pytest.raises(boundary.ImageStateBoundaryError,match='hub_image_state_mount_required'):
        boundary._require_exact_mount(text)


def test_exact_parent_bind_is_accepted_even_on_same_device_as_root():
    boundary._require_exact_mount(BASE+PARENT)
    boundary._require_exact_mount(BASE+PARENT+SELECTIVE)
    # A literal '-' is also a legitimate filesystem source after the separator.
    boundary._require_exact_mount(BASE+PARENT.replace('/dev/example','-'))


@pytest.mark.parametrize('text',['',None,'broken',BASE+PARENT+'malformed\n',BASE+PARENT+PARENT,
    BASE+PARENT.replace('20 10','010 10'),BASE+PARENT.replace('0:42','0:bad'),
    BASE+PARENT.replace('/volume/_data',r'/volume\999bad'),
    BASE+PARENT.replace('/var/lib/hub','/var/lib/../lib/hub'),
    BASE+PARENT.replace(' - ext4',' - extra ext4'),BASE+PARENT.replace('/var/lib/hub','//var/lib/hub')])
def test_malformed_or_ambiguous_mount_metadata_fails_closed(text):
    with pytest.raises(boundary.ImageStateBoundaryError): boundary._require_exact_mount(text)


def fake_linux(monkeypatch):
    monkeypatch.setattr(boundary.sys,'platform','linux')
    monkeypatch.setattr(boundary.os.path,'abspath',lambda value:str(value))
    monkeypatch.setattr(boundary.Path,'resolve',lambda self,**kwargs:PurePosixPath(str(self)))


@pytest.mark.parametrize('metadata',[BASE,BASE+SELECTIVE,'malformed'])
def test_repository_denies_before_mkdir_or_any_sqlite_open(monkeypatch,metadata):
    fake_linux(monkeypatch)
    monkeypatch.setattr(boundary,'_read_mountinfo',lambda:metadata)
    monkeypatch.setattr(repository.Path,'mkdir',lambda *a,**k:pytest.fail('Repository created a directory'))
    monkeypatch.setattr(repository.sqlite3,'connect',lambda *a,**k:pytest.fail('Repository opened SQLite'))
    with pytest.raises(repository.HubError,match='hub_image_state_mount_required') as error:
        repository.HubRepository('/var/lib/hub/hub.sqlite3')
    assert error.value.code=='hub_image_state_mount_required'


@pytest.mark.parametrize('path',['/var/lib/hub-private/hub.sqlite3','/var/lib/hub-other/hub.sqlite3',
    '/tmp/source-tests/hub.sqlite3'])
def test_other_roots_do_not_read_mountinfo(monkeypatch,path):
    fake_linux(monkeypatch)
    monkeypatch.setattr(boundary,'_read_mountinfo',lambda:pytest.fail('Unrelated namespace inspected'))
    boundary.require_image_state_mount(path)


def test_alias_into_fixed_image_namespace_is_guarded(monkeypatch):
    fake_linux(monkeypatch)
    monkeypatch.setattr(boundary.Path,'resolve',lambda self,**kw:PurePosixPath('/var/lib/hub/hub.sqlite3'))
    monkeypatch.setattr(boundary,'_read_mountinfo',lambda:BASE)
    with pytest.raises(boundary.ImageStateBoundaryError): boundary.require_image_state_mount('/alias/hub.sqlite3')


def test_fixed_namespace_accepted_after_real_exact_mount_metadata(monkeypatch):
    fake_linux(monkeypatch)
    monkeypatch.setattr(boundary,'_read_mountinfo',lambda:BASE+PARENT)
    boundary.require_image_state_mount('/var/lib/hub/nested/hub.sqlite3')


def test_unavailable_mount_metadata_refuses_fixed_namespace(monkeypatch):
    fake_linux(monkeypatch)
    def unavailable(): raise OSError('synthetic private error')
    monkeypatch.setattr(boundary,'_read_mountinfo',unavailable)
    with pytest.raises(boundary.ImageStateBoundaryError) as error:
        boundary.require_image_state_mount('/var/lib/hub/hub.sqlite3')
    assert str(error.value)=='hub_image_state_mount_required'


def test_non_linux_source_paths_unchanged(monkeypatch):
    monkeypatch.setattr(boundary.sys,'platform','win32')
    monkeypatch.setattr(boundary,'_read_mountinfo',lambda:pytest.fail('Linux metadata on source platform'))
    boundary.require_image_state_mount('C:/local/hub.sqlite3')
