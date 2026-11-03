"""Two-phase reserve/dispatch for the disabled-by-default supervisor route."""
import base64
import hashlib
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
    HUB_PROOF_AUDIENCE, HUB_PROOF_DOMAIN, HUB_PROOF_PROTOCOL, DispatchChallengeVerifier, HubDispatchProofVerifier,
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


class FakeBootClock:
    """TEST-ONLY stand-in for (boot_id, CLOCK_BOOTTIME ns)."""

    def __init__(self):
        self.boot_id, self.now = str(uuid4()), 10**12

    def __call__(self):
        return self.boot_id, self.now


WINDOW_NS = 5000 * 10**6  # SupervisorRouteConfig().proof_window_ms default.
OWNER_EPOCH = "3b0b8b1e-5c1a-4c8e-9f2a-6d4e2b7a9c01"  # The Hub barrier's owner epoch in test proofs.


@pytest.fixture
def two(world):
    world["signer"] = FixtureSigner()
    world["verifier"] = ReservationReceiptVerifier(public_key=world["signer"].public_key,
                                                   key_id=world["signer"].key_id)
    world["sender"] = DispatchBackend(world["pin"])
    world["hub"] = FixtureSigner(key_id="test-hub-dispatch-key")
    world["hub_verifier"] = HubDispatchProofVerifier(public_key=world["hub"].public_key,
                                                     key_id=world["hub"].key_id)
    world["clock"] = FakeBootClock()
    world["challenger"] = FixtureSigner(key_id="test-challenge-key")
    world["challenge_verifier"] = DispatchChallengeVerifier(public_key=world["challenger"].public_key,
                                                            key_id=world["challenger"].key_id)
    return world


def make(two, **overrides):
    overrides.setdefault("backend", two["sender"])
    overrides.setdefault("dispatch_proof_verifier", two["hub_verifier"])
    overrides.setdefault("boot_clock", two["clock"])
    overrides.setdefault("challenge_signer", two["challenger"])
    return two["make"](reservation_signer=two["signer"], **overrides)


CHALLENGE_HASHES = {}  # nonce -> SHA-256 of the exact signed challenge bytes the "Hub" received.


def nonce_of(raw):
    """Hub side: remember the exact challenge bytes' hash; return its nonce."""
    nonce = json.loads(raw)["payload"]["nonce"]
    CHALLENGE_HASHES[nonce] = hashlib.sha256(raw).hexdigest()
    return nonce


def proof_payload(two, receipt, cmd, nonce, **changes):
    """The claim the Hub would sign inside its barrier transaction."""
    try:
        reserved = json.loads(receipt)["payload"]
        counter, executor = reserved["counter"], reserved["pin"]["container_id"]
    except Exception:
        counter, executor = 1, two["pin"].container_id
    return {"protocol": HUB_PROOF_PROTOCOL, "audience": HUB_PROOF_AUDIENCE, "hub_key_id": two["hub"].key_id,
            "job_id": "job-1", "run_id": json.loads(cmd.body)["run_id"], "worker": "worker-1",
            "machine_id": "machine-1", "fence": cmd.queue_fence, "owner_epoch": OWNER_EPOCH,
            "reservation_receipt_sha256": hashlib.sha256(receipt).hexdigest(),
            "command_id": cmd.command_id, "counter": counter, "executor": executor, "challenge_nonce": nonce,
            "challenge_sha256": CHALLENGE_HASHES.get(nonce, "0" * 64),
            "supervisor_boot_id": two["clock"].boot_id, **changes}


def hub_proof(two, receipt, cmd, nonce, **changes):
    return forge(proof_payload(two, receipt, cmd, nonce, **changes), signer=two["hub"], domain=HUB_PROOF_DOMAIN)


def unknown_nonce():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def challenge_for(two, route, cmd):
    """A fresh challenge, or an unknown nonce where none can be issued."""
    try:
        return nonce_of(route.issue_dispatch_challenge(cmd.command_id))
    except SupervisorRouteBusy:
        two["clock"].now += WINDOW_NS + 1  # Let earlier live challenges lapse, then retry.
        route.expire_challenges()
        return nonce_of(route.issue_dispatch_challenge(cmd.command_id))
    except SupervisorRouteError:
        return unknown_nonce()


def send(two, route, receipt, cmd):
    """Hub flow: challenge, signed one-use proof, dispatch."""
    return route.dispatch(receipt, cmd, hub_proof(two, receipt, cmd, challenge_for(two, route, cmd)))


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
        route.dispatch(b"{}", cmd, b"{}")
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
    assert send(two, restarted, first.receipt, cmd).state == "completed"  # Reserved before the restart.
    assert parse_dispatch_header(two["sender"].dispatches[-1])["counter"] == 1
    restarted.close()


@LINUX
def test_conflicting_binding_and_mixed_paths_refused(two):
    # Before proof mode (no verifier) one-step submit still exists; the two
    # paths still cannot be mixed for one command_id.
    legacy = make(two, dispatch_proof_verifier=None)
    cmd = command(two["epoch"])
    legacy.reserve(cmd)
    for variant in (dict(fence=cmd.queue_fence + 1), dict(run_id=str(uuid4()))):
        with pytest.raises(SupervisorRouteRejected) as raised:
            legacy.reserve(command(two["epoch"], command_id=cmd.command_id, **variant))
        assert raised.value.reason == "binding_conflict"
    with pytest.raises(SupervisorRouteRejected) as raised:
        legacy.submit(cmd)  # One-step send of a reserved command.
    assert raised.value.reason == "reserved_command"
    sent = command(two["epoch"])
    assert legacy.submit(sent).state == "completed"
    legacy.close()
    route = make(two, create=False)  # Proof mode switched ON (permanently) for this journal.
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
    result = send(two, route, early_r.receipt, early)
    assert result.state == "completed" and len(two["sender"].calls) == 1
    assert parse_dispatch_header(two["sender"].dispatches[-1])["counter"] == early_r.counter == 1
    assert route.inspect_reservation(early.command_id)[0] == "dispatched"
    again = send(two, route, early_r.receipt, early)
    assert (again.state, again.error) == ("held", "already_dispatched")
    assert route.inspect(early.command_id).state == "completed" and len(two["sender"].calls) == 1
    assert send(two, route, late_r.receipt, late).state == "completed"  # Its own reserved counter.
    assert parse_dispatch_header(two["sender"].dispatches[-1])["counter"] == 2
    assert tables(two["path"]) == ([(early.command_id, 1, "dispatched"), (late.command_id, 2, "dispatched")],
                                   [(early.command_id, "completed", 1), (late.command_id, "completed", 2)])
    route.close()


@LINUX
def test_older_reservation_superseded_after_newer_dispatch_with_zero_bytes(two):
    route = make(two)
    a, b = command(two["epoch"]), command(two["epoch"])
    a_r, b_r = route.reserve(a), route.reserve(b)
    assert send(two, route, b_r.receipt, b).state == "completed"
    late = send(two, route, a_r.receipt, a)
    assert (late.state, late.error, late.record) == ("held", "reservation_superseded", None)
    assert len(two["sender"].calls) == 1  # Zero bytes for A.
    assert route.inspect_reservation(a.command_id)[0] == "reserved"  # Nothing marked.
    assert route.inspect(a.command_id) is None
    assert send(two, route, a_r.receipt, a).error == "reservation_superseded"  # Stays refused.
    assert route.abandon_reservation(a.command_id) is True  # The Hub can retire it explicitly.
    route.close()


@LINUX
def test_direct_submit_after_reserve_supersedes_the_earlier_reservation(two):
    legacy = make(two, dispatch_proof_verifier=None)  # Before proof mode: submit still exists.
    reserved = command(two["epoch"])
    reserved_r = legacy.reserve(reserved)
    direct = command(two["epoch"])
    assert legacy.submit(direct).state == "completed"  # Allocates and sends counter 2.
    assert parse_dispatch_header(two["sender"].dispatches[-1])["counter"] == 2
    legacy.close()
    route = make(two, create=False)  # Proof mode on: dispatch is now the only path.
    result = send(two, route, reserved_r.receipt, reserved)
    assert (result.state, result.error) == ("held", "reservation_superseded")
    assert len(two["sender"].calls) == 1 and route.inspect_reservation(reserved.command_id)[0] == "reserved"
    later = command(two["epoch"])
    assert send(two, route, route.reserve(later).receipt, later).state == "completed"  # 3 > 2: allowed.
    route.close()


@LINUX
def test_abandoned_or_undispatched_lower_reservations_do_not_block_a_higher_one(two):
    route = make(two)
    a, b, c = command(two["epoch"]), command(two["epoch"]), command(two["epoch"])
    route.reserve(a)
    b_r = route.reserve(b)
    c_r = route.reserve(c)
    assert route.abandon_reservation(a.command_id) is True
    assert send(two, route, c_r.receipt, c).state == "completed"  # 1 abandoned, 2 never dispatched: gaps.
    assert parse_dispatch_header(two["sender"].dispatches[-1])["counter"] == 3
    assert send(two, route, b_r.receipt, b).error == "reservation_superseded"
    assert len(two["sender"].calls) == 1
    db = _raw(two["path"])
    try:
        for sql in ("UPDATE supervisor_route_dispatched SET counter=2", "DELETE FROM supervisor_route_dispatched",
                    "UPDATE supervisor_route_dispatched SET counter=3"):
            with pytest.raises(sqlite3.IntegrityError):  # Dispatched high-water never goes back.
                db.execute(sql)
    finally:
        db.close()
    route.close()


@LINUX
def test_reserve_repeat_reports_state_and_refuses_terminal_reservations(two):
    route = make(two)
    pending, sent, retired = command(two["epoch"]), command(two["epoch"]), command(two["epoch"])
    fresh = route.reserve(pending)
    assert (fresh.state, fresh.replayed) == ("reserved", False)
    repeat = route.reserve(pending)
    assert (repeat.state, repeat.replayed, repeat.receipt) == ("reserved", True, fresh.receipt)
    assert send(two, route, route.reserve(sent).receipt, sent).state == "completed"
    route.reserve(retired)
    assert route.abandon_reservation(retired.command_id) is True
    for cmd, reason in ((sent, "reservation_dispatched"), (retired, "reservation_abandoned")):
        with pytest.raises(SupervisorRouteRejected) as raised:
            route.reserve(cmd)  # A terminal reservation never replays as if fresh.
        assert raised.value.reason == reason
        assert route.inspect_reservation(cmd.command_id)[0] == reason.split("_")[1]
    assert len(two["sender"].calls) == 1
    route.close()


@LINUX
def test_concurrent_dispatches_send_exactly_once(two):
    route = make(two, timeout_ms=2000)
    cmd = command(two["epoch"])
    receipt = route.reserve(cmd).receipt
    proof = hub_proof(two, receipt, cmd, nonce_of(route.issue_dispatch_challenge(cmd.command_id)))  # ONE proof.
    results, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait(10)
        results.append(route.dispatch(receipt, cmd, proof).state)
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
    other_proof = hub_proof(two, other_receipt, other, nonce_of(route.issue_dispatch_challenge(other.command_id)))
    claim = two["hub_verifier"].verify(other_proof)
    outcomes, barrier = [], threading.Barrier(8)

    def consume():
        barrier.wait(10)
        for _ in range(50):
            try:
                route._journal.dispatch_reserved(other.command_id, binding_digest(binding), binding,
                                                 other_receipt, 2, two["epoch"], secrets.token_hex(16),
                                                 proof=claim, proof_sha256=hashlib.sha256(other_proof).hexdigest(),
                                                 clock=two["clock"], window_ns=WINDOW_NS,
                                                 run_id=json.loads(other.body)["run_id"])
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
        result = send(two, route, value, target)
        assert (result.state, result.error, result.record) == ("held", reason, None), reason
    two["epochs"].value = (RouteEpoch(str(uuid4()), 6, BOOT), two["pin"])
    assert send(two, route, receipt, cmd).error == "epoch_changed"  # Current epoch moved on.
    two["epochs"].value = (two["epoch"], two["pin"])
    assert two["sender"].calls == [] and tables(two["path"])[1] == []  # Zero bytes, nothing consumed.
    assert route.inspect_reservation(cmd.command_id)[0] == "reserved"
    assert send(two, route, receipt, cmd).state == "completed"  # The genuine one still works once.
    route.close()


@LINUX
def test_crash_between_dispatched_mark_and_send_stays_held(two, monkeypatch):
    route = make(two)
    crashed, faulted = command(two["epoch"]), command(two["epoch"])
    crashed_r, faulted_r = route.reserve(crashed), route.reserve(faulted)
    two["sender"].before_send = Crash()  # Dispatched + intent committed; dies before any byte.
    with pytest.raises(Crash):
        send(two, route, crashed_r.receipt, crashed)
    two["sender"].before_send = None
    original = route._journal.dispatch_reserved

    def commit_then_fault(*args, **kwargs):
        original(*args, **kwargs)  # Dispatched mark + intent COMMITTED here.
        raise OSError()
    monkeypatch.setattr(route._journal, "dispatch_reserved", commit_then_fault)
    result = send(two, route, faulted_r.receipt, faulted)
    assert (result.state, result.error) == ("held", "admission_uncertain")
    monkeypatch.undo()
    route.close()
    restarted = make(two, create=False)
    for cmd, receipt, error in ((crashed, crashed_r.receipt, "interrupted"),
                                (faulted, faulted_r.receipt, "interrupted")):
        assert restarted.inspect_reservation(cmd.command_id)[0] == "dispatched"
        held = restarted.inspect(cmd.command_id)
        assert (held.state, held.error) == ("held", error)
        assert send(two, restarted, receipt, cmd).error == "already_dispatched"  # Never resent.
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
    assert send(two, route, receipt, cmd).error == "reservation_abandoned"
    assert route.abandon_reservation(str(uuid4())) is False
    nxt = command(two["epoch"])
    assert route.reserve(nxt).counter == 2  # Gap: 1 consumed, never sent.
    assert send(two, route, route.inspect_reservation(nxt.command_id)[2], nxt).state == "completed"
    assert route.abandon_reservation(nxt.command_id) is False  # Dispatched is final.
    assert len(two["sender"].calls) == 1
    route.close()


@LINUX
def test_out_of_order_dispatch_is_refused_before_reaching_the_executor_gate(two, tmp_path):
    exec_dir = tmp_path / "executor"
    exec_dir.mkdir(mode=0o700)
    gate = DispatchCounterGate(config=gate_config(exec_dir / "gate.sqlite"))
    two["sender"].gate = gate
    route = make(two)
    first, second = command(two["epoch"]), command(two["epoch"])
    first_r, second_r = route.reserve(first), route.reserve(second)
    assert send(two, route, second_r.receipt, second).state == "completed"
    late = send(two, route, first_r.receipt, first)  # Lower counter after a higher one was sent.
    assert (late.state, late.error) == ("held", "reservation_superseded")
    assert len(two["sender"].dispatches) == 1  # Refused sender-side: never reached the executor.
    assert gate.inspect() == {"accepted": 2, "started": 2, "in_flight": None}
    route.close()
    gate.close()


@LINUX
def test_reservation_triggers_refuse_tamper(two):
    route = make(two)
    one, two_cmd = command(two["epoch"]), command(two["epoch"])
    route.reserve(one)
    send(two, route, route.reserve(two_cmd).receipt, two_cmd)
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
        assert send(two, route, receipt, cmd).state == "busy"
        with pytest.raises(SupervisorRouteBusy):
            route.abandon_reservation(cmd.command_id)
    finally:
        route._mutex.release()
    assert tables(two["path"])[0] == [(cmd.command_id, 1, "reserved")] and two["sender"].calls == []
    assert send(two, route, receipt, cmd).state == "completed"
    route.close()
