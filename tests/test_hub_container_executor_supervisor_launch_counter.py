"""Durable monotonic per-executor dispatch counter and effect fence for the supervisor route."""
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from hub_container_executor import supervisor_route as route_module
from hub_container_executor.supervisor_route import (
    DISPATCH_HEADER, MAX_DISPATCH_COUNTER, BackendPin, DispatchCounterGate, DispatchGateConfig,
    DispatchRefused, PeerMismatch, RouteEpoch, SupervisorRouteDisabled, SupervisorRouteError,
    SupervisorRouteRefused, SupervisorRouteRejected, UnixSocketBackend, SUBMIT_PATH, binding_digest,
    binding_for, dispatch_claim, dispatch_header, parse_dispatch_header,
)
from tests.test_hub_container_executor_supervisor_route import (  # noqa: F401  (world is a fixture)
    BOOT, LAUNCH, LINUX, TOKEN, Backend, Crash, _own_pin, canonical, command, echo, world,
)

EXECUTOR = "a" * 64  # container_id of the world fixture's pin.
EPOCH = RouteEpoch(LAUNCH, 5, BOOT)  # Equals the world fixture's epoch.
PIN = BackendPin(EXECUTOR, 98765, 2**40, 10001, 10001, "engine:pinned", 500, 123)


def signed_off(counter, cmd=None, **claim_changes):
    """(header, cmd) for ``cmd`` (fresh by default); the claim is UNSIGNED."""
    cmd = cmd or command(EPOCH)
    claim = {**dispatch_claim(binding_for(cmd, PIN), counter), **claim_changes}
    return dispatch_header(claim), cmd


class Effects:
    def __init__(self):
        self.done = []

    def __call__(self, label):
        return lambda: self.done.append(label) or label


def run(gate, header, cmd, effect):
    return gate.run(header, method=cmd.method, path=cmd.path, body=cmd.body, effect=effect)


class DispatchBackend(Backend):
    """Fake transport carrying the dispatch header (like UnixSocketBackend);
    with a gate it runs the 'executor effect' through the gate."""
    carries_dispatch = True

    def __init__(self, pin, gate=None):
        super().__init__(pin)
        self.dispatches, self.gate, self.before_send = [], gate, None

    def forward(self, *, dispatch, **kwargs):
        self.dispatches.append(dispatch)
        if self.before_send is not None:
            raise self.before_send  # Fails after the counter is recorded, before any byte.
        if self.gate is None:
            return super().forward(**kwargs)
        return self.gate.run(dispatch, method=kwargs["method"], path=kwargs["path"], body=kwargs["body"],
                             effect=lambda: super(DispatchBackend, self).forward(**kwargs))


def rows(path):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        intents = dict(db.execute("SELECT command_id, dispatch_counter FROM supervisor_route_intent"))
        counters = dict(db.execute("SELECT executor_id, counter FROM supervisor_route_dispatch"))
    return intents, counters


def outcomes(path):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT counter, outcome FROM supervisor_route_dispatch_outcome "
                          "ORDER BY counter").fetchall()


def evidence_rows(path):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT counter, evidence FROM supervisor_route_dispatch_outcome "
                          "ORDER BY counter").fetchall()


QUIESCED = "operator: executor pid 4242 confirmed exited; no engine jobs for command"


def gate_config(path, create=True, **overrides):
    values = dict(enabled=True, journal_path=str(path), create_journal=create, executor_id=EXECUTOR,
                  launch_epoch=EPOCH, allow_ephemeral_test_storage=True)
    values.update(overrides)
    return DispatchGateConfig(**values)


@pytest.fixture
def gate_path(tmp_path):
    private = tmp_path / "executor"
    private.mkdir(mode=0o700)
    return private / "dispatch.sqlite"


@pytest.fixture
def gate(gate_path):
    store = DispatchCounterGate(config=gate_config(gate_path))
    yield store
    store.close()


def _raw(path):
    return sqlite3.connect(Path(path).as_uri() + "?mode=rw", uri=True, isolation_level=None)


# ---------------------------------------------------------------- sender side

@LINUX
def test_counter_recorded_before_send_and_strictly_increasing(world):
    backend = DispatchBackend(world["pin"])
    route = world["make"](backend=backend)
    sent = [command(world["epoch"]) for _ in range(3)]
    for cmd in sent:
        assert route.submit(cmd).state == "completed"
    intents, counters = rows(world["path"])
    assert [intents[c.command_id] for c in sent] == [1, 2, 3] and counters == {EXECUTOR: 3}
    assert [parse_dispatch_header(d) for d in backend.dispatches] == [
        dispatch_claim(binding_for(c, world["pin"]), n) for n, c in zip((1, 2, 3), sent)]
    assert route.submit(sent[0]).replayed and len(backend.dispatches) == 3  # Replay never re-sends.
    route.close()
    restarted = world["make"](create=False, backend=backend)  # Durable across restart.
    assert restarted.submit(command(world["epoch"])).state == "completed"
    assert parse_dispatch_header(backend.dispatches[-1])["counter"] == 4
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
    intents, counters = rows(world["path"])
    assert intents == {lost.command_id: 1, nxt.command_id: 2} and counters == {EXECUTOR: 2}
    assert parse_dispatch_header(backend.dispatches[-1])["counter"] == 2  # Higher; 1 never sent again.
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
    assert rows(world["path"]) == ({}, {})  # Nothing durable, nothing sent.
    assert route.submit(command(world["epoch"])).state == "completed"
    assert rows(world["path"])[1] == {EXECUTOR: 1}
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
    assert rows(world["path"])[1] == {EXECUTOR: 8}
    route.close()


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


@LINUX
@pytest.mark.parametrize("table", ["supervisor_route_dispatch", "supervisor_route_dispatch_accepted",
                                   "supervisor_route_dispatch_started"])
def test_triggers_refuse_decrement_delete_and_replace(world, gate_path, table):
    if table == "supervisor_route_dispatch":
        route = world["make"]()
        assert route.submit(command(world["epoch"])).state == "completed"
        assert route.submit(command(world["epoch"])).state == "completed"
        path, store = world["path"], route
    else:
        store = DispatchCounterGate(config=gate_config(gate_path))
        assert run(store, *signed_off(2), Effects()("two")) == "two"
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
        if table != "supervisor_route_dispatch":
            for sql in ("UPDATE supervisor_route_dispatch_outcome SET outcome='raised'",
                        "DELETE FROM supervisor_route_dispatch_outcome"):
                with pytest.raises(sqlite3.IntegrityError):
                    db.execute(sql)
        db.execute(f"DROP TRIGGER {table}_no_delete")  # Tampering with the invariant...
    finally:
        db.close()
    store.close()
    with pytest.raises(SupervisorRouteError):  # ...makes the store refuse to open (exact schema).
        if table == "supervisor_route_dispatch":
            world["make"](create=False)
        else:
            DispatchCounterGate(config=gate_config(gate_path, create=False))


# ---------------------------------------------------------------- receiving side

def test_gate_disabled_by_default(gate_path):
    assert DispatchGateConfig().enabled is False
    for config in (DispatchGateConfig(), DispatchGateConfig(journal_path=str(gate_path), executor_id=EXECUTOR),
                   gate_config(gate_path, enabled=1), None):
        with pytest.raises(SupervisorRouteDisabled):
            DispatchCounterGate(config=config)
    for bad in (dict(executor_id="A" * 64), dict(executor_id=None), dict(launch_epoch=None),
                dict(launch_epoch=EPOCH.as_dict()), dict(effect_wait_ms=51), dict(effect_wait_ms=True)):
        with pytest.raises(SupervisorRouteError):
            DispatchCounterGate(config=gate_config(gate_path, **bad))
    assert not gate_path.exists()


@LINUX
def test_gate_refuses_replay_equal_and_lower_allows_gaps_and_survives_restart(gate_path):
    gate = DispatchCounterGate(config=gate_config(gate_path))
    effects = Effects()
    one = signed_off(1)
    assert run(gate, *one, effects(1)) == 1
    with pytest.raises(DispatchRefused) as raised:
        run(gate, *one, effects("replay"))  # Exact replay of an accepted header.
    assert raised.value.reason == "dispatch_replay"
    assert run(gate, *signed_off(5), effects(5)) == 5  # Gap: 2..4 consumed but never sent.
    for stale in (5, 4, 2):  # Equal and lower (reordered) are refused, not deduplicated.
        with pytest.raises(DispatchRefused) as raised:
            run(gate, *signed_off(stale), effects(stale))
        assert raised.value.reason == "dispatch_replay"
    assert effects.done == [1, 5]
    assert gate.inspect() == {"accepted": 5, "started": 5, "in_flight": None}
    assert outcomes(gate_path) == [(1, "completed"), (5, "completed")]
    gate.close()
    reopened = DispatchCounterGate(config=gate_config(gate_path, create=False))
    with pytest.raises(DispatchRefused):
        run(reopened, *signed_off(5), effects("again"))
    assert run(reopened, *signed_off(6), effects(6)) == 6 and effects.done == [1, 5, 6]
    reopened.close()


def _b64(raw):
    return "v1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@LINUX
def test_gate_refuses_bad_missing_and_overflowing_headers_without_recording(gate):
    effects = Effects()
    assert run(gate, *signed_off(3), effects(3)) == 3
    good, cmd = signed_off(4)
    claim = parse_dispatch_header(good)
    body = good[3:]
    invalid = [None, "", b"v1", 7, "v1.", "v2." + body, good + "=", good + "==", good + ".sig", " " + good,
               good + "\n", "v1." + body[:-1] + "+", "v1." + "A" * 3000,
               _b64(json.dumps(claim).encode()),  # Non-canonical JSON (spaces).
               _b64(canonical({**claim, "extra": 1})),
               _b64(canonical({k: v for k, v in claim.items() if k != "queue_fence"})),
               _b64(canonical({**claim, "protocol": "other"}))]
    for counter in (0, -4, 2**63, 10**30, "4", True, None):
        invalid.append(_b64(canonical({**claim, "counter": counter})))
    invalid.append(_b64(canonical(claim).replace(b'"counter":4', b'"counter":4.0')))
    invalid.append(_b64(canonical(claim).replace(b'"counter":4', b'"counter":04')))
    for value in invalid:
        with pytest.raises(DispatchRefused) as raised:
            run(gate, value, cmd, effects("invalid"))
        assert raised.value.reason == "dispatch_invalid", value
    assert gate.inspect()["accepted"] == 3 and effects.done == [3]  # Nothing refused was recorded or run.
    assert run(gate, signed_off(MAX_DISPATCH_COUNTER, cmd)[0], cmd, effects("max")) == "max"
    with pytest.raises(DispatchRefused):
        run(gate, *signed_off(MAX_DISPATCH_COUNTER), effects("max-again"))


@LINUX
def test_gate_refuses_header_moved_to_another_request(gate):
    effects = Effects()
    header, cmd = signed_off(1)
    other = command(EPOCH, fence=cmd.queue_fence)
    moved = [  # (header, method, path, body): the header was made for a different request.
        (header, cmd.method, cmd.path, other.body),
        (header, "GET", cmd.path, cmd.body),
        (header, cmd.method, SUBMIT_PATH + "/x", cmd.body),
        (signed_off(1, cmd, command_id=str(uuid4()))[0], cmd.method, cmd.path, cmd.body),
        (signed_off(1, cmd, queue_fence=cmd.queue_fence + 1)[0], cmd.method, cmd.path, cmd.body),
        (signed_off(1, cmd, path=SUBMIT_PATH + "/x")[0], cmd.method, SUBMIT_PATH + "/x", cmd.body),
        (signed_off(1, cmd, body_sha256="0" * 64)[0], cmd.method, cmd.path, cmd.body),
    ]
    for value, method, path, body in moved:
        with pytest.raises(DispatchRefused) as raised:
            gate.run(value, method=method, path=path, body=body, effect=effects("moved"))
        assert raised.value.reason == "dispatch_request_mismatch"
    with pytest.raises(DispatchRefused) as raised:
        run(gate, signed_off(1, cmd, executor_id="b" * 64)[0], cmd, effects("executor"))
    assert raised.value.reason == "dispatch_wrong_executor"
    for epoch in (RouteEpoch(LAUNCH, 6, BOOT), RouteEpoch(str(uuid4()), 5, BOOT),
                  RouteEpoch(LAUNCH, 5, str(uuid4()))):
        with pytest.raises(DispatchRefused) as raised:
            run(gate, signed_off(1, cmd, epoch=epoch.as_dict())[0], cmd, effects("epoch"))
        assert raised.value.reason == "dispatch_wrong_epoch"
    assert effects.done == [] and gate.inspect()["accepted"] is None


@LINUX
def test_stale_accepted_counter_cannot_act_after_newer_one(gate, monkeypatch):
    """Barrier: 1 is accepted then pauses before its effect; 2 is accepted and
    acts; 1 resumes and is refused with no effect."""
    effects = Effects()
    original = gate._fenced
    paused, resume = threading.Event(), threading.Event()

    def pause_before_effect(counter, effect):
        if counter == 1:
            paused.set()
            assert resume.wait(10)
        return original(counter, effect)
    monkeypatch.setattr(gate, "_fenced", pause_before_effect)
    first, second = signed_off(1), signed_off(2)
    outcome = {}

    def stale():
        try:
            outcome["value"] = run(gate, *first, effects(1))
        except DispatchRefused as error:
            outcome["refused"] = error.reason
        except Exception as error:
            outcome["error"] = type(error).__name__
    worker = threading.Thread(target=stale)
    worker.start()
    try:
        assert paused.wait(10)
        assert gate.inspect() == {"accepted": 1, "started": None, "in_flight": None}  # 1 durably accepted.
        assert run(gate, *second, effects(2)) == 2
    finally:
        resume.set()
        worker.join(10)
    assert outcome == {"refused": "dispatch_superseded"}
    assert effects.done == [2]  # The stale counter never acted.
    assert gate.inspect() == {"accepted": 2, "started": 2, "in_flight": None}
    assert outcomes(gate.path) == [(2, "completed")]


@LINUX
def test_effect_lock_refuses_concurrent_run_and_order_is_preserved(gate):
    entered, release = threading.Event(), threading.Event()
    done = []

    def slow():
        entered.set()
        assert release.wait(10)
        done.append(1)
    first, second = signed_off(1), signed_off(2)
    worker = threading.Thread(target=lambda: run(gate, *first, slow))
    worker.start()
    try:
        assert entered.wait(10)
        with pytest.raises(DispatchRefused) as raised:
            run(gate, *second, lambda: done.append(2))  # 1's effect is running.
        assert raised.value.reason == "dispatch_busy"
        assert gate.inspect() == {"accepted": 2, "started": 1, "in_flight": 1}
    finally:
        release.set()
        worker.join(10)
    with pytest.raises(DispatchRefused):  # Consumed while busy: never acts later.
        run(gate, *second, lambda: done.append(2))
    assert done == [1] and outcomes(gate.path) == [(1, "completed")]
    assert run(gate, *signed_off(3), lambda: done.append(3)) is None and done == [1, 3]


@LINUX
def test_gate_concurrent_duplicates_act_exactly_once(gate):
    effects = Effects()
    header, cmd = signed_off(9)
    results, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait(10)
        try:
            results.append(run(gate, header, cmd, effects(9)))
        except DispatchRefused as error:
            results.append(error.reason)
    threads = [threading.Thread(target=go) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(results, key=str) == [9] + ["dispatch_replay"] * 7 and effects.done == [9]


@LINUX
@pytest.mark.parametrize("fault", [RuntimeError, KeyboardInterrupt])
def test_raising_effect_keeps_marker_fail_closed_until_quiescence_release(gate, fault):
    one = signed_off(1)

    def boom():
        raise fault("effect failed part-way")  # May have partially happened.
    with pytest.raises(fault):
        run(gate, *one, boom)
    assert outcomes(gate.path) == [] and gate.inspect() == {"accepted": 1, "started": 1, "in_flight": 1}
    for header, cmd in (one, signed_off(2)):
        with pytest.raises(DispatchRefused):
            run(gate, header, cmd, lambda: "ran")
    gate.release_unresolved_after_quiescence(1, evidence=QUIESCED)
    assert evidence_rows(gate.path) == [(1, QUIESCED)]
    with pytest.raises(DispatchRefused):
        run(gate, *one, lambda: "ran")  # Never rerun.
    assert run(gate, *signed_off(3), lambda: "ok") == "ok"


@LINUX
def test_async_effects_refused_and_awaitable_result_keeps_marker(gate):
    import asyncio
    calls = []

    async def coroutine_effect():
        calls.append("ran")
    with pytest.raises(SupervisorRouteError):
        run(gate, *signed_off(1), coroutine_effect)  # Refused before anything is recorded.
    assert gate.inspect() == {"accepted": None, "started": None, "in_flight": None}
    with pytest.raises(DispatchRefused) as raised:
        run(gate, *signed_off(2), lambda: coroutine_effect())  # Hidden behind a sync wrapper.
    assert raised.value.reason == "dispatch_effect_async"
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(DispatchRefused) as raised:
            run(gate, *signed_off(3), lambda: loop.create_future())  # A future may be live work.
        assert raised.value.reason == "dispatch_in_flight_unresolved"  # 2's marker is kept.
    finally:
        loop.close()
    assert calls == [] and gate.inspect()["in_flight"] == 2 and outcomes(gate.path) == []


@LINUX
def test_quiescence_release_requires_evidence_and_is_refused_mid_effect(gate):
    entered, release = threading.Event(), threading.Event()
    observed = {}

    def slow():
        entered.set()
        assert release.wait(10)
    worker = threading.Thread(target=lambda: run(gate, *signed_off(1), slow))
    worker.start()
    try:
        assert entered.wait(10)
        with pytest.raises(DispatchRefused) as raised:
            gate.release_unresolved_after_quiescence(1, evidence=QUIESCED)  # Live effect: not quiescent.
        observed["release"] = raised.value.reason
        with pytest.raises(route_module.SupervisorRouteInFlight):
            gate.close()  # Ownership is not released mid-effect.
        assert gate.inspect()["in_flight"] == 1  # Store still owned and open.
    finally:
        release.set()
        worker.join(10)
    assert observed == {"release": "dispatch_busy"} and outcomes(gate.path) == [(1, "completed")]

    def boom():
        raise RuntimeError()
    with pytest.raises(RuntimeError):
        run(gate, *signed_off(2), boom)
    for bad in (None, "", " " * 0, "x" * 1025, "caf\u00e9", "line\nbreak", b"bytes"):
        with pytest.raises(SupervisorRouteError):
            gate.release_unresolved_after_quiescence(2, evidence=bad)
    with pytest.raises(TypeError):
        gate.release_unresolved_after_quiescence(2)  # Evidence is a required argument.
    assert gate.inspect()["in_flight"] == 2
    gate.release_unresolved_after_quiescence(2, evidence=QUIESCED)
    assert outcomes(gate.path) == [(1, "completed"), (2, "released")]


@LINUX
def test_unrecorded_completion_fails_closed_until_operator_release(gate, monkeypatch):
    effects = Effects()

    def finish_fails(*args):
        raise route_module.RouteJournalError("disk")
    monkeypatch.setattr(gate, "_finish", finish_fails)
    with pytest.raises(route_module.RouteJournalError):
        run(gate, *signed_off(1), effects(1))
    monkeypatch.undo()
    assert effects.done == [1] and gate.inspect()["in_flight"] == 1
    with pytest.raises(DispatchRefused) as raised:
        run(gate, *signed_off(2), effects(2))
    assert raised.value.reason == "dispatch_in_flight_unresolved" and effects.done == [1]
    with pytest.raises(route_module.RouteJournalError):
        gate.release_unresolved_after_quiescence(7, evidence=QUIESCED)  # Only the exact in-flight one.
    gate.release_unresolved_after_quiescence(1, evidence=QUIESCED)
    assert outcomes(gate.path) == [(1, "released")]
    with pytest.raises(DispatchRefused):  # 2 was consumed while unresolved; never acts.
        run(gate, *signed_off(2), effects(2))
    assert run(gate, *signed_off(3), effects(3)) == 3 and effects.done == [1, 3]


_DIES_IN_EFFECT = r'''
import os, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, os.path.join(sys.argv[1], 'src'))
from hub_container_executor.supervisor_route import (DispatchCounterGate, DispatchGateConfig, RouteEpoch,
                                                       SUBMIT_PATH)
gate = DispatchCounterGate(config=DispatchGateConfig(
    enabled=True, journal_path=sys.argv[2], create_journal=False, executor_id='a' * 64,
    launch_epoch=RouteEpoch(sys.argv[3], 5, sys.argv[4]), allow_ephemeral_test_storage=True))
def die():
    os._exit(17)  # Process dies mid-effect: no completion, no cleanup.
gate.run(sys.argv[5], method='POST', path=SUBMIT_PATH, body=sys.argv[6].encode(), effect=die)
'''


@LINUX
def test_process_dead_mid_effect_fails_closed_and_stale_never_runs(gate_path):
    DispatchCounterGate(config=gate_config(gate_path)).close()
    header, cmd = signed_off(1)
    result = subprocess.run([sys.executable, "-c", _DIES_IN_EFFECT, str(Path(__file__).parents[1]),
                             str(gate_path), LAUNCH, BOOT, header, cmd.body.decode()],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 17, result.stderr
    gate = DispatchCounterGate(config=gate_config(gate_path, create=False))  # Dead owner's lock is gone.
    effects = Effects()
    assert gate.inspect() == {"accepted": 1, "started": 1, "in_flight": 1}
    with pytest.raises(DispatchRefused) as raised:
        run(gate, header, cmd, effects("stale"))
    assert raised.value.reason == "dispatch_replay"
    with pytest.raises(DispatchRefused) as raised:
        run(gate, *signed_off(2), effects(2))
    assert raised.value.reason == "dispatch_in_flight_unresolved"  # Loud, not silent.
    assert effects.done == []
    gate.release_unresolved_after_quiescence(1, evidence=QUIESCED)  # Operator: process is gone.
    with pytest.raises(DispatchRefused):
        run(gate, header, cmd, effects("stale"))  # The dead run's counter never runs again.
    assert run(gate, *signed_off(3), effects(3)) == 3 and effects.done == [3]
    assert outcomes(gate_path) == [(1, "released"), (3, "completed")]
    gate.close()


_HOLDS_EFFECT = r'''
import os, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, os.path.join(sys.argv[1], 'src'))
from hub_container_executor.supervisor_route import (DispatchCounterGate, DispatchGateConfig, RouteEpoch,
                                                       SUBMIT_PATH, SupervisorRouteInFlight)
gate = DispatchCounterGate(config=DispatchGateConfig(
    enabled=True, journal_path=sys.argv[2], create_journal=False, executor_id='a' * 64,
    launch_epoch=RouteEpoch(sys.argv[3], 5, sys.argv[4]), allow_ephemeral_test_storage=True))
def effect():
    print('started', flush=True)
    try:
        gate.close()  # Attempt to drop the inter-process owner lock mid-effect.
        print('close_ok', flush=True)
    except SupervisorRouteInFlight:
        print('close_refused', flush=True)
    sys.stdin.readline()  # Barrier: the effect stays active until the parent says so.
    return 'done'
try:
    print('result', gate.run(sys.argv[5], method='POST', path=SUBMIT_PATH, body=sys.argv[6].encode(),
                             effect=effect), flush=True)
except Exception as error:
    print('error', type(error).__name__, flush=True)
gate.close()
print('closed', flush=True)
'''


@LINUX
def test_second_process_cannot_take_over_while_first_effect_is_active(gate_path):
    DispatchCounterGate(config=gate_config(gate_path)).close()
    header, cmd = signed_off(1)
    a = subprocess.Popen([sys.executable, "-c", _HOLDS_EFFECT, str(Path(__file__).parents[1]), str(gate_path),
                          LAUNCH, BOOT, header, cmd.body.decode()],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    b_ran = []
    try:
        assert a.stdout.readline().strip() == "started"
        close_attempt = a.stdout.readline().strip()
        # Process B, while A's effect is active: open + operator release + higher counter.
        try:
            b = DispatchCounterGate(config=gate_config(gate_path, create=False))
        except SupervisorRouteError:
            b = None
        if b is not None:
            try:
                b.release_unresolved_after_quiescence(1, evidence=QUIESCED)
                run(b, *signed_off(2), lambda: b_ran.append(2))
            finally:
                b.close()
        assert close_attempt == "close_refused"
        assert b is None and b_ran == []  # B could not even open the store.
    finally:
        a.stdin.write("\n")
        a.stdin.flush()
        out, err = a.communicate(timeout=30)
    assert out.split() == ["result", "done", "closed"], err
    b = DispatchCounterGate(config=gate_config(gate_path, create=False))  # Only now, after A drained.
    assert b.inspect() == {"accepted": 1, "started": 1, "in_flight": None}
    assert outcomes(gate_path) == [(1, "completed")]
    assert run(b, *signed_off(2), lambda: "two") == "two"
    b.close()


@LINUX
def test_route_to_gate_end_to_end(world, gate_path):
    gate = DispatchCounterGate(config=gate_config(gate_path))
    backend = DispatchBackend(world["pin"], gate=gate)
    route = world["make"](backend=backend)
    first = command(world["epoch"])
    assert route.submit(first).state == "completed"
    backend.before_send = Crash()  # Counter 2 consumed, never delivered.
    with pytest.raises(Crash):
        route.submit(command(world["epoch"]))
    backend.before_send = None
    assert route.submit(command(world["epoch"])).state == "completed"
    assert gate.inspect() == {"accepted": 3, "started": 3, "in_flight": None}  # Gap accepted.
    with pytest.raises(DispatchRefused):  # A captured, already-accepted header replayed verbatim.
        run(gate, backend.dispatches[0], first, lambda: None)
    assert len(backend.calls) == 2
    route.close()
    gate.close()


@LINUX
def test_unix_socket_backend_sends_bound_header_and_refuses_missing_or_moved(world):
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
        assert sent == [dispatch_header(dispatch_claim(binding_for(cmd, pin), 1))]
        route.close()
        other = command(world["epoch"])
        for bad in (None, "v1;counter=1", sent[0]):  # Missing, malformed, or made for another body.
            with pytest.raises(SupervisorRouteError):
                backend.forward(method="POST", path=other.path, body=other.body, timeout_ms=500, pin=pin,
                                dispatch=bad)
        assert len(accepted) == 1  # Refused before connecting: zero bytes.
    finally:
        stop.set()
        thread.join(10)
        listener.close()
        os.unlink(path)
        os.rmdir(directory)
