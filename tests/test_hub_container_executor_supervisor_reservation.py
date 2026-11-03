"""Two-phase reserve/dispatch for the disabled-by-default supervisor route."""
import base64
import json
from pathlib import Path
import secrets
import sqlite3
import threading
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from hub_container_executor import supervisor_route as route_module
from hub_container_executor.supervisor_route import (
    RESERVE_AUDIENCE, RESERVE_DOMAIN, RESERVE_PROTOCOL, SIGNATURE_DOMAIN, DispatchCounterGate,
    ReservationReceiptVerifier, ReservationSigner, ReservationUncertain, RouteEpoch, RouteReservation,
    SupervisorRouteBusy, SupervisorRouteError, SupervisorRouteRejected, binding_digest, binding_for,
    parse_dispatch_header, reservation_expectation,
)
from tests.test_hub_container_executor_supervisor_route import (  # noqa: F401  (world is a fixture)
    BOOT, LAUNCH, LINUX, Crash, canonical, command, world,
)
from tests.test_hub_container_executor_supervisor_launch_counter import (
    DispatchBackend, _raw, gate_config,
)


class FixtureSigner:
    """TEST-ONLY reservation signer over a throwaway key."""

    def __init__(self, key_id="test-reservation-key"):
        self.key = Ed25519PrivateKey.generate()
        self.key_id = key_id
        self.public_key = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, message):
        return self.key.sign(message)


@pytest.fixture
def two(world):
    world["signer"] = FixtureSigner()
    world["verifier"] = ReservationReceiptVerifier(public_key=world["signer"].public_key,
                                                   key_id=world["signer"].key_id)
    world["sender"] = DispatchBackend(world["pin"])
    return world


def make(two, **overrides):
    overrides.setdefault("backend", two["sender"])
    return two["make"](reservation_signer=two["signer"], **overrides)


def forge(payload, *, signer, domain=RESERVE_DOMAIN):
    signature = signer.key.sign(domain + canonical(payload))
    return canonical({"payload": payload, "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode()})


def payload_of(receipt):
    return json.loads(receipt)["payload"]


def tables(path):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        return (db.execute("SELECT command_id, counter, state FROM supervisor_route_reservation "
                           "ORDER BY counter").fetchall(),
                db.execute("SELECT command_id, state, dispatch_counter FROM supervisor_route_intent "
                           "ORDER BY dispatch_counter").fetchall())


# ---------------------------------------------------------------- defaults

@LINUX
def test_default_signer_refuses_reserve_and_dispatch_without_effect(two):
    route = two["make"](backend=two["sender"])  # No reservation signer: the default refuses.
    cmd = command(two["epoch"])
    with pytest.raises(SupervisorRouteError) as raised:
        route.reserve(cmd)
    assert "not configured" in str(raised.value)
    with pytest.raises(SupervisorRouteError):
        route.dispatch(b"{}", cmd)
    with pytest.raises(SupervisorRouteError):
        ReservationSigner().sign(b"anything")
    assert tables(two["path"]) == ([], []) and two["sender"].calls == []
    route.close()
    for bad in (ReservationSigner(), object(), FixtureSigner(key_id="bad id")):
        with pytest.raises(SupervisorRouteError):
            two["make"](create=False, reservation_signer=bad)


# ---------------------------------------------------------------- reserve

@LINUX
def test_reserve_sends_zero_bytes_signs_binding_and_is_idempotent_across_restart(two):
    route = make(two)
    cmd = command(two["epoch"])
    first = route.reserve(cmd)
    assert type(first) is RouteReservation and (first.counter, first.replayed) == (1, False)
    assert two["sender"].calls == [] and two["sender"].dispatches == []  # Nothing sent.
    assert route.inspect(cmd.command_id) is None  # No intent: not admitted for sending.
    assert route.inspect_reservation(cmd.command_id) == ("reserved", 1, first.receipt)
    payload = two["verifier"].verify(first.receipt, expected=reservation_expectation(cmd, two["pin"]))
    assert set(payload) == {"protocol", "audience", "key_id", "command_id", "body_sha256", "method", "path",
                            "pin", "epoch", "fence", "counter", "reserved_at_ms"}
    assert (payload["protocol"], payload["audience"], payload["key_id"]) == (
        RESERVE_PROTOCOL, RESERVE_AUDIENCE, "test-reservation-key")
    assert payload["pin"] == two["pin"].as_dict() and payload["epoch"] == two["epoch"].as_dict()
    assert payload["fence"] == cmd.queue_fence and payload["counter"] == 1
    again = route.reserve(cmd)
    assert again.receipt == first.receipt and again.replayed and again.counter == 1  # Same bytes.
    assert route.reserve(command(two["epoch"])).counter == 2
    route.close()
    restarted = make(two, create=False)
    repeat = restarted.reserve(cmd)
    assert repeat.receipt == first.receipt and repeat.replayed
    assert restarted.dispatch(first.receipt, cmd).state == "completed"  # Reserved before the restart.
    assert parse_dispatch_header(two["sender"].dispatches[-1])["counter"] == 1
    restarted.close()


@LINUX
def test_conflicting_binding_and_mixed_paths_refused(two):
    route = make(two)
    cmd = command(two["epoch"])
    route.reserve(cmd)
    for variant in (dict(fence=cmd.queue_fence + 1), dict(run_id=str(uuid4()))):
        with pytest.raises(SupervisorRouteRejected) as raised:
            route.reserve(command(two["epoch"], command_id=cmd.command_id, **variant))
        assert raised.value.reason == "binding_conflict"
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.submit(cmd)  # One-step send of a reserved command.
    assert raised.value.reason == "reserved_command"
    sent = command(two["epoch"])
    assert route.submit(sent).state == "completed"
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.reserve(sent)
    assert raised.value.reason == "already_admitted"
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.reserve(command(RouteEpoch(LAUNCH, 4, BOOT)))  # Stale epoch never reserves.
    assert raised.value.reason == "stale_epoch"
    assert tables(two["path"])[0] == [(cmd.command_id, 1, "reserved")]
    assert len(two["sender"].calls) == 1
    route.close()


@LINUX
def test_signer_failure_consumes_nothing(two):
    class Broken(FixtureSigner):
        def sign(self, message):
            return b"\x00" * 64  # Wrong signature: self-verification fails.
    route = two["make"](backend=two["sender"], reservation_signer=Broken())
    with pytest.raises(ReservationUncertain):
        route.reserve(command(two["epoch"]))
    assert tables(two["path"]) == ([], [])
    route.close()
    route = make(two, create=False)
    assert route.reserve(command(two["epoch"])).counter == 1  # Nothing was consumed.
    route.close()


# ---------------------------------------------------------------- Hub-side verifier

@LINUX
def test_receipt_verifier_default_refuses_and_checks_exact_bytes_key_and_binding(two):
    route = make(two)
    cmd = command(two["epoch"])
    receipt = route.reserve(cmd).receipt
    expected = reservation_expectation(cmd, two["pin"])
    verifier = two["verifier"]
    assert verifier.verify(receipt, expected=expected)["counter"] == 1
    with pytest.raises(SupervisorRouteError):
        ReservationReceiptVerifier().verify(receipt, expected=expected)  # Default refuses.
    with pytest.raises(SupervisorRouteError):
        ReservationReceiptVerifier(public_key=two["signer"].public_key).verify(receipt, expected=expected)
    other = FixtureSigner()
    payload = payload_of(receipt)
    bad_receipts = [
        receipt + b" ", receipt[:-1], json.dumps(json.loads(receipt)).encode(),  # Non-canonical bytes.
        canonical({**json.loads(receipt), "payload": {**payload, "counter": 7}}),  # Tampered, old signature.
        forge(payload, signer=other),  # Wrong key.
        forge(payload, signer=two["signer"], domain=SIGNATURE_DOMAIN),  # Route-record domain.
        forge({**payload, "audience": "other"}, signer=two["signer"]),
        forge({**payload, "extra": 1}, signer=two["signer"]),
        forge({**payload, "counter": 0}, signer=two["signer"]),
        forge({**payload, "counter": True}, signer=two["signer"]),
    ]
    for bad in bad_receipts:
        with pytest.raises(SupervisorRouteError):
            verifier.verify(bad, expected=expected)
    with pytest.raises(SupervisorRouteError):
        ReservationReceiptVerifier(public_key=other.public_key, key_id=two["signer"].key_id).verify(
            receipt, expected=expected)
    with pytest.raises(SupervisorRouteError):
        ReservationReceiptVerifier(public_key=two["signer"].public_key, key_id="other-key").verify(
            receipt, expected=expected)
    other_pin = route_module.BackendPin("b" * 64, 4321, 98765, 10001, 10001, "engine:pinned", 500, 123)
    for wrong in (reservation_expectation(command(two["epoch"]), two["pin"]),
                  reservation_expectation(command(two["epoch"], command_id=cmd.command_id, fence=8), two["pin"]),
                  reservation_expectation(cmd, other_pin),
                  {**expected, "epoch": RouteEpoch(LAUNCH, 6, BOOT).as_dict()},
                  {**expected, "fence": True}, {k: v for k, v in expected.items() if k != "path"}):
        with pytest.raises(SupervisorRouteError):
            verifier.verify(receipt, expected=wrong)
    route.close()


# ---------------------------------------------------------------- dispatch

@LINUX
def test_dispatch_consumes_once_and_sends_with_reserved_counter(two):
    route = make(two)
    early, late = command(two["epoch"]), command(two["epoch"])
    early_r, late_r = route.reserve(early), route.reserve(late)
    result = route.dispatch(late_r.receipt, late)
    assert result.state == "completed" and len(two["sender"].calls) == 1
    assert parse_dispatch_header(two["sender"].dispatches[-1])["counter"] == late_r.counter == 2
    assert route.inspect_reservation(late.command_id)[0] == "dispatched"
    again = route.dispatch(late_r.receipt, late)
    assert (again.state, again.error) == ("held", "already_dispatched")
    assert route.inspect(late.command_id).state == "completed" and len(two["sender"].calls) == 1
    assert route.dispatch(early_r.receipt, early).state == "completed"  # Its own reserved counter.
    assert parse_dispatch_header(two["sender"].dispatches[-1])["counter"] == 1
    assert tables(two["path"]) == ([(early.command_id, 1, "dispatched"), (late.command_id, 2, "dispatched")],
                                   [(early.command_id, "completed", 1), (late.command_id, "completed", 2)])
    route.close()


@LINUX
def test_concurrent_dispatches_send_exactly_once(two):
    route = make(two, timeout_ms=2000)
    cmd = command(two["epoch"])
    receipt = route.reserve(cmd).receipt
    results, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait(10)
        results.append(route.dispatch(receipt, cmd).state)
    threads = [threading.Thread(target=go) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(two["sender"].calls) == 1 and results.count("completed") == 1
    assert set(results) <= {"completed", "busy", "held"}
    # Journal level, bypassing the route mutex: exactly one consume.
    other = command(two["epoch"])
    other_receipt = route.reserve(other).receipt
    binding = binding_for(other, two["pin"])
    outcomes, barrier = [], threading.Barrier(8)

    def consume():
        barrier.wait(10)
        for _ in range(50):
            try:
                route._journal.dispatch_reserved(other.command_id, binding_digest(binding), binding,
                                                 other_receipt, 2, two["epoch"], secrets.token_hex(16))
                outcomes.append("consumed")
                return
            except SupervisorRouteRejected as error:
                outcomes.append(error.reason)
                return
            except route_module.RouteJournalError:
                continue
    threads = [threading.Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(outcomes) == ["already_dispatched"] * 7 + ["consumed"]
    route.close()


@LINUX
def test_forged_or_mismatched_receipt_held_with_zero_bytes(two):
    route = make(two)
    cmd, other = command(two["epoch"]), command(two["epoch"])
    receipt, other_receipt = route.reserve(cmd).receipt, route.reserve(other).receipt
    payload = payload_of(receipt)
    never_reserved = command(two["epoch"])
    forged_unreserved = forge({**payload, **reservation_expectation(never_reserved, two["pin"]), "counter": 9},
                              signer=two["signer"])  # Valid signature, but no reservation row.
    resigned = forge({**payload, "reserved_at_ms": payload["reserved_at_ms"] + 1}, signer=two["signer"])
    cases = [
        (other_receipt, cmd, "receipt_invalid"),  # Another command's receipt.
        (forge(payload, signer=FixtureSigner()), cmd, "receipt_invalid"),  # Wrong key.
        (receipt.replace(b'"counter":1', b'"counter":2'), cmd, "receipt_invalid"),  # Tampered.
        (b"not a receipt", cmd, "receipt_invalid"),
        (receipt, command(two["epoch"], command_id=cmd.command_id, fence=8), "receipt_invalid"),  # Other bytes.
        (forged_unreserved, never_reserved, "reservation_missing"),
        (resigned, cmd, "reservation_mismatch"),  # Not the exact stored receipt bytes.
    ]
    for value, target, reason in cases:
        result = route.dispatch(value, target)
        assert (result.state, result.error, result.record) == ("held", reason, None), reason
    two["epochs"].value = (RouteEpoch(str(uuid4()), 6, BOOT), two["pin"])
    assert route.dispatch(receipt, cmd).error == "epoch_changed"  # Current epoch moved on.
    two["epochs"].value = (two["epoch"], two["pin"])
    assert two["sender"].calls == [] and tables(two["path"])[1] == []  # Zero bytes, nothing consumed.
    assert route.inspect_reservation(cmd.command_id)[0] == "reserved"
    assert route.dispatch(receipt, cmd).state == "completed"  # The genuine one still works once.
    route.close()


@LINUX
def test_crash_between_dispatched_mark_and_send_stays_held(two, monkeypatch):
    route = make(two)
    crashed, faulted = command(two["epoch"]), command(two["epoch"])
    crashed_r, faulted_r = route.reserve(crashed), route.reserve(faulted)
    two["sender"].before_send = Crash()  # Dispatched + intent committed; dies before any byte.
    with pytest.raises(Crash):
        route.dispatch(crashed_r.receipt, crashed)
    two["sender"].before_send = None
    original = route._journal.dispatch_reserved

    def commit_then_fault(*args, **kwargs):
        original(*args, **kwargs)  # Dispatched mark + intent COMMITTED here.
        raise OSError()
    monkeypatch.setattr(route._journal, "dispatch_reserved", commit_then_fault)
    result = route.dispatch(faulted_r.receipt, faulted)
    assert (result.state, result.error) == ("held", "admission_uncertain")
    monkeypatch.undo()
    route.close()
    restarted = make(two, create=False)
    for cmd, receipt, error in ((crashed, crashed_r.receipt, "interrupted"),
                                (faulted, faulted_r.receipt, "interrupted")):
        assert restarted.inspect_reservation(cmd.command_id)[0] == "dispatched"
        held = restarted.inspect(cmd.command_id)
        assert (held.state, held.error) == ("held", error)
        assert restarted.dispatch(receipt, cmd).error == "already_dispatched"  # Never resent.
    assert two["sender"].calls == []
    assert restarted.reserve(command(two["epoch"])).counter == 3  # Never reused.
    restarted.close()


@LINUX
def test_abandoned_reservation_never_dispatches_and_counter_stays_consumed(two):
    route = make(two)
    cmd = command(two["epoch"])
    receipt = route.reserve(cmd).receipt
    assert route.abandon_reservation(cmd.command_id) is True
    assert route.abandon_reservation(cmd.command_id) is False
    assert route.dispatch(receipt, cmd).error == "reservation_abandoned"
    assert route.abandon_reservation(str(uuid4())) is False
    nxt = command(two["epoch"])
    assert route.reserve(nxt).counter == 2  # Gap: 1 consumed, never sent.
    assert route.dispatch(route.inspect_reservation(nxt.command_id)[2], nxt).state == "completed"
    assert route.abandon_reservation(nxt.command_id) is False  # Dispatched is final.
    assert len(two["sender"].calls) == 1
    route.close()


@LINUX
def test_out_of_order_dispatch_is_refused_by_the_executor_gate(two, tmp_path):
    exec_dir = tmp_path / "executor"
    exec_dir.mkdir(mode=0o700)
    gate = DispatchCounterGate(config=gate_config(exec_dir / "gate.sqlite"))
    two["sender"].gate = gate
    route = make(two)
    first, second = command(two["epoch"]), command(two["epoch"])
    first_r, second_r = route.reserve(first), route.reserve(second)
    assert route.dispatch(second_r.receipt, second).state == "completed"
    late = route.dispatch(first_r.receipt, first)  # Lower counter after a higher one acted.
    assert late.state == "held" and late.record is None
    assert gate.inspect() == {"accepted": 2, "started": 2, "in_flight": None}
    assert len(two["sender"].calls) == 1  # The executor never acted on the stale counter.
    route.close()
    gate.close()


@LINUX
def test_reservation_triggers_refuse_tamper(two):
    route = make(two)
    one, two_cmd = command(two["epoch"]), command(two["epoch"])
    route.reserve(one)
    route.dispatch(route.reserve(two_cmd).receipt, two_cmd)
    db = _raw(two["path"])
    t = "supervisor_route_reservation"
    try:
        for sql, args in ((f"UPDATE {t} SET state='reserved' WHERE command_id=?", (two_cmd.command_id,)),
                          (f"UPDATE {t} SET state='abandoned' WHERE command_id=?", (two_cmd.command_id,)),
                          (f"UPDATE {t} SET counter=5 WHERE command_id=?", (one.command_id,)),
                          (f"UPDATE {t} SET receipt='x' WHERE command_id=?", (one.command_id,)),
                          (f"UPDATE {t} SET binding_digest=? WHERE command_id=?", ("0" * 64, one.command_id)),
                          (f"UPDATE {t} SET state='dispatched', counter=9 WHERE command_id=?", (one.command_id,)),
                          (f"DELETE FROM {t}", ()),
                          (f"INSERT OR REPLACE INTO {t} SELECT command_id, executor_id, binding_digest, binding, "
                           f"counter, state, receipt FROM {t} WHERE command_id=?", (one.command_id,)),
                          (f"INSERT INTO {t} VALUES (?, ?, ?, '{{}}', 1, 'reserved', 'r')",
                           (str(uuid4()), "a" * 64, "0" * 64)),  # Lower counter for the executor.
                          (f"INSERT INTO {t} VALUES (?, ?, ?, '{{}}', 99, 'dispatched', 'r')",
                           (str(uuid4()), "a" * 64, "0" * 64))):  # Must start reserved.
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, args)
        rows =db.execute(f"SELECT command_id, counter, state FROM {t} ORDER BY counter").fetchall()
        assert rows == [(one.command_id, 1, "reserved"), (two_cmd.command_id, 2, "dispatched")]
        db.execute("DROP TRIGGER supervisor_route_reservation_kept")
    finally:
        db.close()
    route.close()
    with pytest.raises(SupervisorRouteError):  # Exact schema: a dropped trigger is refused.
        make(two, create=False)


@LINUX
def test_busy_route_refuses_reserve_and_dispatch_without_effect(two):
    route = make(two)
    cmd = command(two["epoch"])
    receipt = route.reserve(cmd).receipt
    assert route._mutex.acquire(blocking=False)  # Another operation owns the route.
    try:
        with pytest.raises(SupervisorRouteBusy):
            route.reserve(command(two["epoch"]))
        assert route.dispatch(receipt, cmd).state == "busy"
        with pytest.raises(SupervisorRouteBusy):
            route.abandon_reservation(cmd.command_id)
    finally:
        route._mutex.release()
    assert tables(two["path"])[0] == [(cmd.command_id, 1, "reserved")] and two["sender"].calls == []
    assert route.dispatch(receipt, cmd).state == "completed"
    route.close()
