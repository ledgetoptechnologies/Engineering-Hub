"""Pure local lifecycle tests; mount and unmount syscalls are substituted."""
import ctypes
import os
import pytest

from scripts import hub_local_test_launcher as launcher
from scripts import hub_local_state_alias
from scripts.hub_local_test_volume import OwnedVolume, VolumeError, VolumeOutcomeUnknown

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux descriptor semantics')


class Call:
    def __init__(self, fn): self.fn = fn
    def __call__(self, *args): return self.fn(*args)


def setup_alias(tmp_path, monkeypatch, fail=None):
    parent = tmp_path / 'alias'; parent.mkdir(mode=0o700)
    source = tmp_path / 'source'; source.mkdir(mode=0o700)
    monkeypatch.setattr(launcher, '_STATE_ALIAS_PARENT', str(parent))
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    original_fstat = os.fstat
    parent_identity = (parent.stat().st_dev, parent.stat().st_ino)
    def parent_owned(fd):
        info = original_fstat(fd)
        if (info.st_dev, info.st_ino) == parent_identity:
            fields = list(info); fields[4] = fields[5] = 0
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(os, 'fstat', parent_owned)
    events, intents = [], set()
    def reserve(_journal, _owner, _daemon, _name, suffix, kind):
        events.append(('intent', kind))
        if suffix in intents: raise VolumeError('already reserved')
        intents.add(suffix)
        return kind
    monkeypatch.setattr(launcher, '_reserve_intent', reserve)
    monkeypatch.setattr(launcher, '_check_path', lambda h: events.append(('check', h)))
    monkeypatch.setattr(launcher, '_close_handles', lambda h: events.append(('journal-close', h)))
    class Guard:
        def __init__(self, *args, **kwargs): events.append(('guard-open',))
        def check(self): events.append(('guard-check',))
        def close(self): events.append(('guard-close',))
    monkeypatch.setattr(hub_local_state_alias, 'StateAlias', Guard)
    class Libc:
        def __init__(self):
            self.mount = Call(self.do_mount); self.umount2 = Call(self.do_umount)
        def do_mount(self, src, dst, fs, flags, data):
            events.append(('mount', flags))
            return int(fail == ('bind' if flags == 4096 else 'private'))
        def do_umount(self, path, flags):
            events.append(('unmount', flags))
            return int(fail == 'detach')
    monkeypatch.setattr(ctypes, 'CDLL', lambda *args, **kwargs: Libc())
    volume = OwnedVolume('volume', 'task', 'daemon', str(source))
    return parent, volume, events


def test_bind_and_detach_have_separate_intents_and_exact_cleanup(tmp_path, monkeypatch):
    parent, volume, events = setup_alias(tmp_path, monkeypatch)
    alias = launcher._OwnedStateAlias(volume, str(tmp_path))
    assert (parent / 'task').is_dir()
    assert events.index(('intent', 'alias-bind')) < events.index(('mount', 4096)) < events.index(('mount', 16384 | 262144))
    alias.detach()
    assert events.index(('intent', 'alias-detach')) < events.index(('unmount', 0))
    assert not (parent / 'task').exists()
    assert list(parent.iterdir()) == []
    with pytest.raises(VolumeError): alias.detach()


@pytest.mark.parametrize('failure', ['bind', 'private'])
def test_failed_bind_holds_placeholder_without_retry(tmp_path, monkeypatch, failure):
    parent, volume, events = setup_alias(tmp_path, monkeypatch, failure)
    with pytest.raises(VolumeOutcomeUnknown): launcher._OwnedStateAlias(volume, str(tmp_path))
    assert (parent / 'task').is_dir()
    assert not any(e[0] == 'unmount' for e in events)
    with pytest.raises(VolumeError): launcher._OwnedStateAlias(volume, str(tmp_path))
    assert sum(e == ('mount', 4096) for e in events) == 1


def test_failed_detach_holds_without_retry_or_removal(tmp_path, monkeypatch):
    parent, volume, events = setup_alias(tmp_path, monkeypatch, 'detach')
    alias = launcher._OwnedStateAlias(volume, str(tmp_path))
    try:
        with pytest.raises(VolumeOutcomeUnknown): alias.detach()
        assert (parent / 'task').is_dir()
        with pytest.raises(VolumeError): alias.detach()
        assert events.count(('unmount', 0)) == 1
    finally:
        if alias.parent is not None: os.close(alias.parent); alias.parent = None


def test_replaced_placeholder_is_not_removed(tmp_path, monkeypatch):
    parent, volume, events = setup_alias(tmp_path, monkeypatch)
    alias = launcher._OwnedStateAlias(volume, str(tmp_path))
    try:
        (parent / 'task').rename(parent / 'original')
        (parent / 'task').mkdir(mode=0o700)
        with pytest.raises(VolumeOutcomeUnknown): alias.detach()
        assert (parent / 'task').is_dir() and (parent / 'original').is_dir()
    finally:
        if alias.parent is not None: os.close(alias.parent); alias.parent = None
