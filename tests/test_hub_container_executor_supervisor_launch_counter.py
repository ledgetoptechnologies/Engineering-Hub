"""Durable monotonic per-executor dispatch counter for the supervisor route."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import tempfile
import threading
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from hub_container_executor import supervisor_route as route_module
from hub_container_executor.supervisor_route import (
    DISPATCH_HEADER, MAX_DISPATCH_COUNTER, DispatchCounterGate, DispatchGateConfig, DispatchRefused,
    PeerMismatch, RouteEpoch, SupervisorRouteDisabled, SupervisorRouteError, SupervisorRouteRefused,
    SupervisorRouteRejected, UnixSocketBackend, binding_digest, binding_for, dispatch_header,
    parse_dispatch_header,
)
from tests.test_hub_container_executor_supervisor_route import (  # noqa: F401  (world is a fixture)
    BOOT, LAUNCH, LINUX, TOKEN, Backend, Crash, _own_pin, canonical, command, echo, world,
)

EXECUTOR = "a" * 64  # container_id of the world fixture's pin.


class DispatchBackend(Backend):
    """Fake transport that carries the dispatch header (like UnixSocketBackend)."""
    carries_dispatch = True

    def __init__(self, pin, gate=None):
        super().__init__(pin)
        self.dispatches, self.gate, self.before_send = [], gate, None

    def forward(self, *, dispatch, **kwargs):
        self.dispatches.append(dispatch)
        if self.before_send is not None:
            raise self.before_send  # Fails after the counter is recorded, before any byte.
        if self.gate is not None:
            self.gate.accept(dispatch, body=kwargs["body"])  # Receiver checks before acting.
        return super().forward(**kwargs)


def rows(path):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        intents = dict(db.execute("SELECT command_id, dispatch_counter FROM supervisor_route_intent"))
        states = dict(db.execute("SELECT command_id, state FROM supervisor_route_intent"))
        counters = dict(db.execute("SELECT executor_id, counter FROM supervisor_route_dispatch"))
    return intents, states, counters


def gate_config(path, create=True, **overrides):
    values = dict(enabled=True, journal_path=str(path), create_journal=create, executor_id=EXECUTOR,
                  allow_ephemeral_test_storage=True)
    values.update(overrides)
    return DispatchGateConfig(**values)


@pytest.fixture
def gate_path(tmp_path):
    private = tmp_path / "executor"
    private.mkdir(mode=0o700)
    return private / "dispatch.sqlite"


def header(counter, body=b"{}", executor=EXECUTOR):
    return dispatch_header(executor, counter, body)


@LINUX
def test_counter_recorded_before_send_and_strictly_increasing(world):
    backend = DispatchBackend(world["pin"])
    route = world["make"](backend=backend)
    sent = [command(world["epoch"]) for _ in range(3)]
    for cmd in sent:
        assert route.submit(cmd).state == "completed"
    intents, _, counters = rows(world["path"])
    assert [intents[c.command_id] for c in sent] == [1, 2, 3] and counters == {EXECUTOR: 3}
    parsed = [parse_dispatch_header(d) for d in backend.dispatches]
    assert parsed == [(EXECUTOR, n, hashlib.sha256(c.body).hexdigest()) for n, c in zip((1, 2, 3), sent)]
    assert route.submit(sent[0]).replayed and len(backend.dispatches) == 3  # Replay never re-sends.
    route.close()
    restarted = world["make"](create=False, backend=backend)  # Durable across restart.
    assert restarted.submit(command(world["epoch"])).state == "completed"
    assert parse_dispatch_header(backend.dispatches[-1])[1] == 4
    restarted.close()


@LINUX
@pytest.mark.parametrize("failure", ["crash_before_send", "peer_mismatch", "admit_committed_then_raised"])
def test_consumed_counter_never_reused_after_unsent_or_uncertain_attempt(world, monkeypatch, failure):
    backend = DispatchBackend(world["pin"])
    route = world["make"](backend=backend)
    lost = command(world["epoch"])
    if failure == "crash_before_send":
        backend.before_send = Crash()
        with pytest.raises(Crash):
            route.submit(lost)
        expected = ("held", "interrupted")
    elif failure == "peer_mismatch":
        backend.before_send = PeerMismatch()
        assert route.submit(lost).state == "refused"
        expected = ("refused", "backend_identity_mismatch")
    else:
        original = route._journal.admit

        def admit_then_fault(*args, **kwargs):
            original(*args, **kwargs)  # Counter + intent COMMITTED here.
            raise OSError()
        monkeypatch.setattr(route._journal, "admit", admit_then_fault)
        result = route.submit(lost)
        assert (result.state, result.error) == ("held", "admission_uncertain")
        monkeypatch.undo()
        expected = ("held", "interrupted")
    backend.before_send = None
    inspected = route.inspect(lost.command_id)
    assert (inspected.state, inspected.error) == expected  # Held/refused exactly as without a counter.
    assert route.submit(lost).state == expected[0] and backend.calls == []  # Never sent.
    nxt = command(world["epoch"])
    assert route.submit(nxt).state == "completed"
    intents, _, counters = rows(world["path"])
    assert intents == {lost.command_id: 1, nxt.command_id: 2} and counters == {EXECUTOR: 2}
    assert parse_dispatch_header(backend.dispatches[-1])[1] == 2  # Higher; 1 is never sent again.
    route.close()


@LINUX
def test_rolled_back_admit_consumes_nothing(world, monkeypatch):
    route = world["make"](backend=DispatchBackend(world["pin"]))
    original = route._journal._connection

    def writable_fails(*, readonly=False):
        if not readonly:
            raise OSError()
        return original(readonly=True)
    monkeypatch.setattr(route._journal, "_connection", writable_fails)
    with pytest.raises(SupervisorRouteRefused):
        route.submit(command(world["epoch"]))
    monkeypatch.undo()
    assert rows(world["path"]) == ({}, {}, {})  # Nothing durable, nothing sent.
    assert route.submit(command(world["epoch"])).state == "completed"
    assert rows(world["path"])[2] == {EXECUTOR: 1}
    route.close()


@LINUX
def test_concurrent_admissions_get_distinct_counters(world):
    route = world["make"]()
    journal, results, errors = route._journal, [], []
    barrier = threading.Barrier(8)

    def admit():
        cmd = command(world["epoch"])
        binding = binding_for(cmd, world["pin"])
        barrier.wait(10)
        for _ in range(50):  # Retry only the bounded-busy refusal; nothing else.
            try:
                results.append(journal.admit(cmd.command_id, binding_digest(binding), binding,
                                             world["epoch"], secrets.token_hex(16)))
                return
            except route_module.RouteJournalError:
                continue
        errors.append(cmd.command_id)
    threads = [threading.Thread(target=admit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == [] and sorted(results) == [("new", n) for n in range(1, 9)]
    assert rows(world["path"])[2] == {EXECUTOR: 8}
    route.close()


def _raw(path):
    return sqlite3.connect(Path(path).as_uri() + "?mode=rw", uri=True, isolation_level=None)


@LINUX
@pytest.mark.parametrize("table", ["supervisor_route_dispatch", "supervisor_route_dispatch_accepted"])
def test_triggers_refuse_decrement_delete_and_replace(world, gate_path, table):
    if table == "supervisor_route_dispatch":
        route = world["make"]()
        assert route.submit(command(world["epoch"])).state == "completed"
        assert route.submit(command(world["epoch"])).state == "completed"
        path, store = world["path"], route
    else:
        store = DispatchCounterGate(config=gate_config(gate_path))
        assert store.accept(header(2), body=b"{}") == 2
        path = gate_path
    db = _raw(path)
    try:
        for sql, args in ((f"UPDATE {table} SET counter=1", ()),
                          (f"UPDATE {table} SET counter=2", ()),
                          (f"UPDATE {table} SET counter=-5", ()),
                          (f"UPDATE {table} SET executor_id=?, counter=99", ("b" * 64,)),
                          (f"DELETE FROM {table}", ()),
                          (f"INSERT OR REPLACE INTO {table} VALUES (?, 1)", (EXECUTOR,)),
                          (f"INSERT INTO {table} VALUES (?, 1)", (EXECUTOR,)),
                          (f"INSERT INTO {table} VALUES (?, ?)", ("c" * 64, 2**63)),
                          (f"INSERT INTO {table} VALUES (?, 'x')", ("c" * 64,)),
                          (f"INSERT INTO {table} VALUES (?, 0)", ("c" * 64,))):
            with pytest.raises((sqlite3.IntegrityError, OverflowError)):
                db.execute(sql, args)
        assert db.execute(f"SELECT executor_id, counter FROM {table}").fetchall() == [(EXECUTOR, 2)]
        db.execute(f"DROP TRIGGER {table}_no_delete")  # Tampering with the invariant...
    finally:
        db.close()
    store.close()
    with pytest.raises(SupervisorRouteError):  # ...makes the store refuse to open (exact schema).
        if table == "supervisor_route_dispatch":
            world["make"](create=False)
        else:
            DispatchCounterGate(config=gate_config(gate_path, create=False))


@LINUX
def test_exhausted_counter_rejected_without_forward(world):
    route = world["make"]()
    assert route.submit(command(world["epoch"])).state == "completed"
    db = _raw(world["path"])
    db.execute("UPDATE supervisor_route_dispatch SET counter=?", (MAX_DISPATCH_COUNTER,))  # Increase: allowed.
    db.close()
    cmd = command(world["epoch"])
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.submit(cmd)
    assert raised.value.reason == "dispatch_counter_exhausted"
    assert route.inspect(cmd.command_id) is None and len(world["backend"].calls) == 1
    route.close()


def test_gate_disabled_by_default(gate_path):
    assert DispatchGateConfig().enabled is False
    for config in (DispatchGateConfig(), DispatchGateConfig(journal_path=str(gate_path), executor_id=EXECUTOR),
                   gate_config(gate_path, enabled=1), None):
        with pytest.raises(SupervisorRouteDisabled):
            DispatchCounterGate(config=config)
    for bad in ("A" * 64, "a" * 63, None):
        with pytest.raises(SupervisorRouteError):
            DispatchCounterGate(config=gate_config(gate_path, executor_id=bad))
    assert not gate_path.exists()


@LINUX
def test_gate_refuses_replay_equal_and_lower_allows_gaps_and_survives_restart(gate_path):
    gate = DispatchCounterGate(config=gate_config(gate_path))
    assert gate.last_accepted() is None
    assert gate.accept(header(1), body=b"{}") == 1
    for replay in (1, 1):
        with pytest.raises(DispatchRefused) as raised:
            gate.accept(header(replay), body=b"{}")  # Replay of an accepted counter.
        assert raised.value.reason == "dispatch_replay"
    assert gate.accept(header(5), body=b"{}") == 5  # Gap: 2..4 consumed but never sent.
    for stale in (5, 4, 2):  # Equal and lower (reordered) are refused, not deduplicated.
        with pytest.raises(DispatchRefused) as raised:
            gate.accept(header(stale), body=b"{}")
        assert raised.value.reason == "dispatch_replay"
    assert gate.last_accepted() == 5
    gate.close()
    reopened = DispatchCounterGate(config=gate_config(gate_path, create=False))
    with pytest.raises(DispatchRefused):
        reopened.accept(header(5), body=b"{}")
    assert reopened.accept(header(6), body=b"{}") == 6
    reopened.close()


@LINUX
def test_gate_refuses_bad_missing_and_overflowing_headers_without_recording(gate_path):
    gate = DispatchCounterGate(config=gate_config(gate_path))
    assert gate.accept(header(3), body=b"{}") == 3
    digest = hashlib.sha256(b"{}").hexdigest()
    prefix = f"v1;executor={EXECUTOR};counter="
    invalid = [None, "", b"v1", 7, prefix + f"0;body={digest}", prefix + f"-4;body={digest}",
               prefix + f"04;body={digest}", prefix + f"4.0;body={digest}", prefix + f"abc;body={digest}",
               prefix + f"{2**63};body={digest}", prefix + f"{10**19};body={digest}",
               prefix + f"{10**30};body={digest}", prefix + f" 4;body={digest}", prefix + f"4;body={digest} ",
               prefix + f"4;body={digest};x=1", prefix + f"4;body={digest.upper()}", prefix + "4",
               f"v2;executor={EXECUTOR};counter=4;body={digest}", prefix + f"٤;body={digest}",
               prefix + f"4;body={digest}\n"]
    for value in invalid:
        with pytest.raises(DispatchRefused) as raised:
            gate.accept(value, body=b"{}")
        assert raised.value.reason == "dispatch_invalid", value
    with pytest.raises(DispatchRefused) as raised:
        gate.accept(header(4, executor="b" * 64), body=b"{}")
    assert raised.value.reason == "dispatch_wrong_executor"
    for body in (b"[]", "{}", None):
        with pytest.raises(DispatchRefused) as raised:
            gate.accept(header(4), body=body)
        assert raised.value.reason == "dispatch_body_mismatch"
    assert gate.last_accepted() == 3  # Nothing refused was recorded.
    assert gate.accept(header(MAX_DISPATCH_COUNTER), body=b"{}") == MAX_DISPATCH_COUNTER
    with pytest.raises(DispatchRefused):
        gate.accept(header(MAX_DISPATCH_COUNTER), body=b"{}")
    gate.close()


@LINUX
def test_gate_concurrent_duplicates_accept_exactly_once(gate_path):
    gate = DispatchCounterGate(config=gate_config(gate_path))
    outcomes, barrier = [], threading.Barrier(8)

    def accept():
        barrier.wait(10)
        try:
            outcomes.append(gate.accept(header(9), body=b"{}"))
        except DispatchRefused as error:
            outcomes.append(error.reason)
    threads = [threading.Thread(target=accept) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(outcomes, key=str) == [9] + ["dispatch_replay"] * 7
    gate.close()


@LINUX
def test_route_to_gate_end_to_end_replayed_header_refused(world, gate_path):
    gate = DispatchCounterGate(config=gate_config(gate_path))
    backend = DispatchBackend(world["pin"], gate=gate)
    route = world["make"](backend=backend)
    first = command(world["epoch"])
    assert route.submit(first).state == "completed"
    backend.before_send = Crash()  # Counter 2 consumed, never delivered.
    with pytest.raises(Crash):
        route.submit(command(world["epoch"]))
    backend.before_send = None
    third = command(world["epoch"])
    assert route.submit(third).state == "completed" and gate.last_accepted() == 3  # Gap accepted.
    captured = backend.dispatches[0]
    with pytest.raises(DispatchRefused):  # A captured, already-accepted header replayed verbatim.
        gate.accept(captured, body=first.body)
    route.close()
    gate.close()


@LINUX
def test_unix_socket_backend_sends_dispatch_header_and_refuses_missing(world):
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "capture.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(2)
    listener.settimeout(0.1)
    heads, accepted, stop = [], [], threading.Event()

    def serve():
        while not stop.is_set():
            try:
                peer, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            accepted.append(1)
            with peer:
                peer.settimeout(10)
                data = b""
                while b"\r\n\r\n" not in data:
                    data += peer.recv(65536)
                head, body = data.split(b"\r\n\r\n", 1)
                length = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")][0]
                             .split(b":")[1])
                while len(body) < length:
                    body += peer.recv(65536)
                heads.append(head)
                raw = canonical(echo(json.loads(body)))
                peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(raw) + raw)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        pin = _own_pin()
        world["epochs"].value = (world["epoch"], pin)
        backend = UnixSocketBackend(path, bearer_token=TOKEN)
        route = world["make"](backend=backend)
        cmd = command(world["epoch"])
        assert route.submit(cmd).state == "completed"
        lines = [l.decode() for l in heads[0].split(b"\r\n")]
        sent = [l.split(": ", 1)[1] for l in lines if l.lower().startswith(DISPATCH_HEADER.lower() + ":")]
        assert sent == [dispatch_header(pin.container_id, 1, cmd.body)]
        route.close()
        for bad in (None, "v1;counter=1"):  # Missing/malformed: refused BEFORE connecting.
            with pytest.raises(SupervisorRouteError):
                backend.forward(method="POST", path=cmd.path, body=cmd.body, timeout_ms=500, pin=pin,
                                dispatch=bad)
        assert len(accepted) == 1
    finally:
        stop.set()
        thread.join(10)
        listener.close()
        os.unlink(path)
        os.rmdir(directory)
