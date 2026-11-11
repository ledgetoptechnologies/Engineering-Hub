"""Real Linux disk/owner/crash evidence, not Docker effects or health proof."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Host executor package requires actual Linux kernel ownership")

from hub_container_executor.operation_journal import (
    SQLiteOperationJournal, OperationJournalConflict, OperationJournalError, OperationBinding,
)

LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Actual private Linux journal ownership")
CID = "a" * 64


def binding(*, name="hub-pilot-demo-0123456789ab-candidate", target=None, mac=None):
    return OperationBinding(str(uuid4()), 123, 456, name, target,
                            mac or "hmac-sha256:" + "a" * 64, 1)


@LINUX
def test_opt_in_migration_keeps_legacy_pending_and_bound_ids_unique(path):
    legacy, bound = str(uuid4()), str(uuid4())
    value = binding()
    with journal(path) as store:
        store.begin(legacy, "create_candidate")
        with pytest.raises(OperationJournalError):
            store.begin_bound(bound, "create_candidate", value)
        store.enable_bindings()
        assert store.inspect(legacy).state == "pending"
        assert store.inspect_binding(legacy) is None
        with pytest.raises(OperationJournalError):
            store.begin(str(uuid4()), "create_candidate")
        store.begin_bound(bound, "create_candidate", value)
        assert store.inspect(bound).state == "pending"
        recorded = store.inspect_binding(bound)
        assert recorded == replace(value, owner_epoch=store._owner.epoch)
        with pytest.raises(OperationJournalConflict):
            store.begin_bound(str(uuid4()), "create_candidate", value)
        with pytest.raises(OperationJournalConflict):
            store.begin_bound(bound, "create_candidate", value)
    with journal(path, create=False) as store:
        assert store.inspect(legacy).state == "pending"
        assert store.inspect_binding(bound) == recorded
        assert recorded.owner_epoch != store._owner.epoch


@LINUX
def test_bound_target_validation_and_no_plaintext_binding(path):
    with journal(path) as store:
        store.enable_bindings()
        identity = str(uuid4())
        value = binding(name=None, target=CID)
        store.begin_bound(identity, "start", value)
        assert store.inspect_binding(identity) == replace(value, owner_epoch=store._owner.epoch)
        with pytest.raises(OperationJournalError):
            store.begin_bound(str(uuid4()), "start", replace(value, owner_epoch=str(uuid4())))
        for invalid in (binding(name=None, target=None), binding(name=None, target=CID, mac="secret-token"),
                        binding(name="../foreign")):
            with pytest.raises(OperationJournalError):
                store.begin_bound(str(uuid4()), "start", invalid)
        assert b"secret-token" not in path.read_bytes()


@LINUX
def test_interrupted_migration_rolls_back_both_schema_and_version(path, monkeypatch):
    with journal(path) as store:
        original = __import__("hub_container_executor.operation_journal", fromlist=["_schema"])
        real = original._schema
        def reject_v2(db, **kwargs):
            if db.execute("PRAGMA user_version").fetchone() == (2,):
                raise OperationJournalError("injected migration failure")
            return real(db, **kwargs)
        monkeypatch.setattr(original, "_schema", reject_v2)
        with pytest.raises(OperationJournalError):
            store.enable_bindings()
        monkeypatch.setattr(original, "_schema", real)
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone() == (1,)
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name='private_operation_binding'").fetchone() is None


@LINUX
def test_binding_insert_failure_rolls_back_intent(path, monkeypatch):
    identity = str(uuid4())
    with journal(path) as store:
        store.enable_bindings()
        original = store._connection
        @contextmanager
        def rejecting(*args, **kwargs):
            with original(*args, **kwargs) as db:
                db.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_INSERT and table == "private_operation_binding"
                    else sqlite3.SQLITE_OK)
                yield db
        monkeypatch.setattr(store, "_connection", rejecting)
        with pytest.raises(OperationJournalError):
            store.begin_bound(identity, "create_candidate", binding())
        monkeypatch.setattr(store, "_connection", original)
        assert store.inspect(identity) is None
        assert store.inspect_binding(identity) is None


@LINUX
def test_orphan_binding_rejected_before_owner_reopen(path):
    with journal(path) as store:
        store.enable_bindings()
    with sqlite3.connect(path) as db:
        value = binding()
        db.execute("INSERT INTO private_operation_binding VALUES (?,?,?,?,?,?,?,?,?)",
                   (str(uuid4()), value.boot_id, value.daemon_pid, value.daemon_start,
                    value.resource_name, value.target_id, value.request_mac, value.fence, str(uuid4())))
    with pytest.raises(OperationJournalError):
        journal(path, create=False)


@LINUX
def test_unknown_owner_epoch_rejected_before_owner_reopen(path):
    identity = str(uuid4())
    with journal(path) as store:
        store.enable_bindings()
        store.begin_bound(identity, "create_candidate", binding())
    with sqlite3.connect(path) as db:
        owner_before = db.execute("SELECT * FROM hub_pilot_owner").fetchall()
        db.execute("UPDATE private_operation_binding SET owner_epoch=? WHERE operation_id=?",
                   (str(uuid4()), identity))
    with pytest.raises(OperationJournalError):
        journal(path, create=False)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM hub_pilot_owner").fetchall() == owner_before


@LINUX
def test_bound_pending_survives_writer_crash_without_replay(path):
    identity = str(uuid4())
    boot_id = str(uuid4())
    script = """
import os,sys
from hub_container_executor.operation_journal import SQLiteOperationJournal, OperationBinding
s=SQLiteOperationJournal(sys.argv[1],create=True,allow_ephemeral_test_storage=True)
s.enable_bindings()
b=OperationBinding(sys.argv[3],123,456,'hub-pilot-demo-0123456789ab-candidate',None,'hmac-sha256:'+'a'*64,1)
s.begin_bound(sys.argv[2],'create_candidate',b)
os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path), identity, boot_id],
                            timeout=10, capture_output=True, text=True)
    assert result.returncode == 23
    with journal(path, create=False) as store:
        assert store.inspect(identity).state == "pending"
        assert store.inspect_binding(identity).boot_id == boot_id
        with pytest.raises(OperationJournalConflict):
            store.begin_bound(identity, "create_candidate", replace(store.inspect_binding(identity), owner_epoch=None))


@pytest.fixture
def path(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return private / "operations.sqlite"


def journal(path, *, create=True):
    return SQLiteOperationJournal(path, create=create, allow_ephemeral_test_storage=True)


def test_nonlinux_fails_closed_before_creation(path, monkeypatch):
    monkeypatch.setattr("hub_container_executor.operation_journal.sys.platform", "win32")
    with pytest.raises(OperationJournalError):
        journal(path)
    assert not path.exists()


@LINUX
@pytest.mark.parametrize("kind", ["create_candidate", "create_rollback", "start", "stop", "probe"])
def test_pending_and_completed_durable_reopen_and_no_replay(path, kind):
    identity = str(uuid4())
    with journal(path) as store:
        assert store.inspect(identity) is None
        store.begin(identity, kind)
        observed = store.inspect(identity)
        assert observed.state == "pending" and observed.terminal is False and observed.container_id is None
        with pytest.raises(OperationJournalConflict):
            store.begin(identity, kind)
    with journal(path, create=False) as store:
        assert store.inspect(identity) == observed  # Crash uncertainty stays pending.
        with pytest.raises(OperationJournalConflict):
            store.begin(identity, kind)
        store.completed(identity, kind, CID)
        completed = store.inspect(identity)
        assert completed.state == "completed" and completed.terminal is True and completed.container_id == CID
        store.completed(identity, kind, CID)  # Exact receipt replay alone is idempotent.
        with pytest.raises(OperationJournalConflict):
            store.completed(identity, kind, "b" * 64)
        with pytest.raises(OperationJournalConflict):
            store.begin(identity, kind)
    with journal(path, create=False) as store:
        assert store.inspect(identity) == completed
        with pytest.raises(OperationJournalConflict):
            store.begin(identity, kind)


@LINUX
def test_conflicting_kind_missing_intent_and_invalid_receipts_deny(path):
    identity = str(uuid4())
    with journal(path) as store:
        with pytest.raises(OperationJournalConflict):
            store.completed(identity, "start", CID)
        store.begin(identity, "start")
        for kind in ("stop", "probe"):
            with pytest.raises(OperationJournalConflict):
                store.begin(identity, kind)
            with pytest.raises(OperationJournalConflict):
                store.completed(identity, kind, CID)
        for value in (None, "", "A" * 64, "a" * 63, "sha256:" + CID, 123):
            with pytest.raises(OperationJournalError):
                store.completed(identity, "start", value)
        assert store.inspect(identity).state == "pending"


@LINUX
@pytest.mark.parametrize("identity", [None, 1, "", "not-uuid", "A" * 36,
                                      "12345678123456781234567812345678", "{12345678-1234-1234-1234-123456781234}"])
def test_noncanonical_ids_never_admitted(path, identity):
    with journal(path) as store:
        for operation in (lambda: store.begin(identity, "start"), lambda: store.inspect(identity),
                          lambda: store.completed(identity, "start", CID)):
            with pytest.raises(OperationJournalError):
                operation()


@LINUX
@pytest.mark.parametrize("kind", [None, "", "START", "terminal_failed", "remove", "shell", {}, "create_other"])
def test_unknown_kinds_never_admitted(path, kind):
    with journal(path) as store:
        with pytest.raises(OperationJournalError):
            store.begin(str(uuid4()), kind)


@LINUX
def test_explicit_create_reopen_and_single_owner(path):
    with pytest.raises(OperationJournalError):
        journal(path, create=False)
    assert not path.exists()
    store = journal(path)
    with pytest.raises(OperationJournalError):
        journal(path, create=False)
    with pytest.raises(OperationJournalError):
        journal(path, create=True)
    store.close()
    store.close()
    with pytest.raises(OperationJournalError):
        store.inspect(str(uuid4()))
    with journal(path, create=False):
        pass


@LINUX
def test_unrelated_owner_enrolled_hub_database_is_not_adopted_or_changed(path):
    from engineering_hub.pilot_ownership import PilotOwner
    with PilotOwner(path, allow_ephemeral_test_storage=True):
        pass
    before = path.read_bytes()
    with pytest.raises(OperationJournalError):
        journal(path, create=False)
    assert path.read_bytes() == before


@LINUX
@pytest.mark.parametrize("attack", ["permissions", "symlink", "hardlink", "wrong_owner", "missing_parent"])
def test_insecure_state_path_rejected(path, attack):
    if attack == "permissions":
        path.parent.chmod(0o755)
    elif attack == "symlink":
        target = path.parent / "target"
        target.write_text("not-a-journal")
        path.symlink_to(target)
    elif attack == "hardlink":
        with journal(path):
            pass
        os.link(path, path.with_name("linked.sqlite"))
    elif attack == "wrong_owner":
        if os.geteuid() != 0:
            pytest.skip("Changing file owner requires isolated test root")
        os.chown(path.parent, 12345, -1)
    else:
        path = path.parent / "missing" / "ops.sqlite"
    with pytest.raises(OperationJournalError):
        journal(path, create=attack not in {"symlink", "hardlink"})


@LINUX
@pytest.mark.parametrize("target", ["database", "lock", "parent", "sidecar"])
def test_live_path_replacement_or_unsafe_sidecar_fails_closed(path, target):
    store = journal(path)
    identity = str(uuid4())
    store.begin(identity, "start")
    try:
        if target == "database":
            replacement = path.with_name("replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            replacement.replace(path)
        elif target == "lock":
            lock = path.with_name(path.name + ".pilot-owner.lock")
            lock.rename(lock.with_name("old.lock"))
            lock.touch(mode=0o600)
        elif target == "parent":
            path.parent.rename(path.parent.with_name("old-private"))
            path.parent.mkdir(mode=0o700)
        else:
            path.with_name(path.name + "-journal").symlink_to(path)
        with pytest.raises(OperationJournalError):
            store.completed(identity, "start", CID)
        with pytest.raises(OperationJournalError):
            store.inspect(identity)
    finally:
        store.close()


@LINUX
@pytest.mark.parametrize("attack", ["corrupt", "schema", "row", "sidecar", "dbmode", "missinglock"])
def test_reopen_rejects_corruption_missing_or_unsafe_state(path, attack, monkeypatch):
    identity = str(uuid4())
    with journal(path) as store:
        store.begin(identity, "probe")
    if attack == "corrupt":
        path.write_bytes(b"not a sqlite file")
    elif attack in {"schema", "row"}:
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE private_operation_journal" if attack == "schema" else
                       "UPDATE private_operation_journal SET state='terminal_failed'")
    elif attack == "sidecar":
        path.with_name(path.name + "-journal").symlink_to(path)
    elif attack == "dbmode":
        path.chmod(0o644)
    else:
        lock = path.with_name(path.name + ".pilot-owner.lock")
        lock.unlink()
        before_bytes = path.read_bytes()
        def owner_and_journal_rows():
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                return (db.execute('SELECT * FROM hub_pilot_owner').fetchall(),
                        db.execute('SELECT * FROM hub_pilot_owner_epochs ORDER BY epoch').fetchall(),
                        db.execute('SELECT * FROM private_operation_journal ORDER BY operation_id').fetchall())
        before_rows = owner_and_journal_rows()
        real_open = os.open
        create_attempts = []
        def guarded_open(filename, flags, *args, **kwargs):
            if os.fspath(filename) in {lock.name, str(lock)} and flags & os.O_CREAT:
                create_attempts.append((os.fspath(filename), flags))
                raise OSError('Regression: attempted to recreate missing journal lock')
            return real_open(filename, flags, *args, **kwargs)
        monkeypatch.setattr(os, 'open', guarded_open)
    with pytest.raises(OperationJournalError):
        journal(path, create=False)
    if attack == 'missinglock':
        # A sanitized exception alone could be caused by our spy or a later
        # failure after changing ownership. Verify the missing inode is not
        # repaired and both enrollment history and pending operation persist.
        assert create_attempts == []
        assert not lock.exists() and not lock.is_symlink()
        assert path.read_bytes() == before_bytes
        assert owner_and_journal_rows() == before_rows


@LINUX
@pytest.mark.parametrize("complete", [False, True])
def test_process_crash_keeps_durable_pending_or_completed_without_retry(path, complete):
    identity = str(uuid4())
    script = """
import os,sys
from hub_container_executor.operation_journal import SQLiteOperationJournal
store=SQLiteOperationJournal(sys.argv[1],create=True,allow_ephemeral_test_storage=True)
store.begin(sys.argv[2],'start')
if sys.argv[3]=='yes': store.completed(sys.argv[2],'start','a'*64)
os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path), identity, "yes" if complete else "no"],
                            timeout=10, capture_output=True, text=True)
    assert result.returncode == 23
    with journal(path, create=False) as store:
        observed = store.inspect(identity)
        assert observed.state == ("completed" if complete else "pending")
        assert observed.terminal is complete
        with pytest.raises(OperationJournalConflict):
            store.begin(identity, "start")


@LINUX
def test_external_sqlite_lock_fails_with_bounded_busy_wait(path):
    with journal(path) as store:
        with sqlite3.connect(path, isolation_level=None) as other:
            other.execute("BEGIN EXCLUSIVE")
            start = time.monotonic()
            with pytest.raises(OperationJournalError):
                store.begin(str(uuid4()), "start")
            assert time.monotonic() - start < 1
            other.rollback()


@LINUX
def test_child_close_drops_inherited_fds_without_unlocking_parent_or_stranding_lock(path):
    import select
    from engineering_hub import pilot_ownership

    identity = str(uuid4())
    store = journal(path)
    store.begin(identity, "start")
    # Exercise this journal's own child-close defense, not just PilotOwner's
    # automatic at-fork hook. Keep real inherited lock FDs until child.close().
    pilot_ownership._OWNERS.discard(store._owner)
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        os.close(release_write)
        try:
            store.close()
            store.close()
            try:
                store.inspect(identity)
            except OperationJournalError:
                pass
            else:
                os._exit(3)
            os.write(ready_write, b"closed")
            os.read(release_read, 1)  # Remain alive across parent close/reopen.
            os._exit(0)
        except BaseException:
            os.write(ready_write, b"failed")
            os._exit(4)
    os.close(ready_write)
    os.close(release_read)
    try:
        assert select.select([ready_read], [], [], 3)[0], "Child close timed out"
        assert os.read(ready_read, 6) == b"closed"
        assert os.waitpid(child, os.WNOHANG) == (0, 0)
        assert store.inspect(identity).state == "pending"
        with pytest.raises(OperationJournalError):
            journal(path, create=False)  # Child must not LOCK_UN the parent.
        store.close()
        with journal(path, create=False) as reopened:
            assert reopened.inspect(identity).state == "pending"
            assert os.waitpid(child, os.WNOHANG) == (0, 0)
    finally:
        store.close()
        os.close(ready_read)
        try:
            os.write(release_write, b"x")
        except BrokenPipeError:
            pass
        os.close(release_write)
        reaped, status = os.waitpid(child, 0)
        assert reaped == child and os.waitstatus_to_exitcode(status) == 0
