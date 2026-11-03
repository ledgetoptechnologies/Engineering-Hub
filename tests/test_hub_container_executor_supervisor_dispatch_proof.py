"""Supervisor half of the one-use dispatch claim proof (docs/supervisor-dispatch-claim-proof.md)."""
import json
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from hub_container_executor import supervisor_route as route_module
from hub_container_executor.supervisor_route import (
    CHALLENGE_PROTOCOL, EXPIRED_AUDIENCE, EXPIRED_DOMAIN, EXPIRED_PROTOCOL, HUB_PROOF_DOMAIN, RESERVE_DOMAIN,
    ChallengeStatementSigner, DispatchChallenge, ExpiredStatementVerifier, HubDispatchProofVerifier,
    SupervisorRouteBusy, SupervisorRouteError, SupervisorRouteRejected,
)
from tests.test_hub_container_executor_supervisor_route import LINUX, command, world  # noqa: F401
from tests.test_hub_container_executor_supervisor_launch_counter import _raw
from tests.test_hub_container_executor_supervisor_reservation import (  # noqa: F401  (two is a fixture)
    WINDOW_NS, FixtureSigner, forge, hub_proof, make, proof_payload, two, unknown_nonce,
)


class CountingSigner(FixtureSigner):
    """TEST-ONLY statement signer that records every signing call."""

    def __init__(self):
        super().__init__(key_id="test-statement-key")
        self.calls = 0

    def sign(self, message):
        self.calls += 1
        return super().sign(message)


@pytest.fixture
def proof(two):
    two["statements"] = CountingSigner()
    two["statement_verifier"] = ExpiredStatementVerifier(public_key=two["statements"].public_key,
                                                         key_id=two["statements"].key_id)
    return two


def route_for(proof, **overrides):
    overrides.setdefault("statement_signer", proof["statements"])
    return make(proof, **overrides)


def reserved(proof, route):
    cmd = command(proof["epoch"])
    return cmd, route.reserve(cmd).receipt


def challenge_rows(path):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT nonce, state FROM supervisor_route_challenge ORDER BY rowid").fetchall()


def sends(proof):
    return len(proof["sender"].calls)


# ---------------------------------------------------------------- defaults

@LINUX
def test_defaults_refuse_challenge_dispatch_proof_and_statement(proof):
    route = proof["make"](reservation_signer=proof["signer"], backend=proof["sender"], boot_clock=proof["clock"])
    cmd, receipt = reserved(proof, route)
    for call in (lambda: route.issue_dispatch_challenge(cmd.command_id),
                 lambda: route.dispatch(receipt, cmd, b"{}"),
                 lambda: route.expired_statement(unknown_nonce())):
        with pytest.raises(SupervisorRouteError) as raised:
            call()
        assert "not configured" in str(raised.value)
    with pytest.raises(SupervisorRouteError):
        HubDispatchProofVerifier().verify(b"{}")
    with pytest.raises(SupervisorRouteError):
        ExpiredStatementVerifier().verify(b"{}", nonce=unknown_nonce())
    with pytest.raises(SupervisorRouteError):
        ChallengeStatementSigner().sign(b"x")
    for half in (dict(public_key=proof["hub"].public_key), dict(key_id="k"), dict(public_key=b"x" * 31, key_id="k")):
        with pytest.raises(SupervisorRouteError):
            HubDispatchProofVerifier(**half)
    assert challenge_rows(proof["path"]) == [] and sends(proof) == 0
    route.close()
    for bad in (dict(dispatch_proof_verifier=object()), dict(statement_signer=ChallengeStatementSigner()),
                dict(boot_clock="not callable")):
        with pytest.raises(SupervisorRouteError):
            route_for(proof, create=False, **bad)


# ---------------------------------------------------------------- challenge

@LINUX
def test_challenge_is_recorded_sends_nothing_and_requires_a_live_reservation(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    challenge = route.issue_dispatch_challenge(cmd.command_id)
    assert type(challenge) is DispatchChallenge and sends(proof) == 0
    assert challenge.as_dict() == {"protocol": CHALLENGE_PROTOCOL, "nonce": challenge.nonce,
                                   "command_id": cmd.command_id, "counter": 1, "executor": proof["pin"].container_id,
                                   "supervisor_boot_id": proof["clock"].boot_id, "proof_window_ms": 5000}
    assert json.loads(challenge.canonical()) == challenge.as_dict()
    assert route.inspect_challenge(challenge.nonce) == ("outstanding", cmd.command_id, 1, proof["clock"].boot_id)
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.issue_dispatch_challenge(str(uuid4()))
    assert raised.value.reason == "reservation_missing"
    retired, _ = reserved(proof, route)
    route.abandon_reservation(retired.command_id)
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.issue_dispatch_challenge(retired.command_id)
    assert raised.value.reason == "reservation_abandoned"
    newer, newer_receipt = reserved(proof, route)
    newer_nonce = route.issue_dispatch_challenge(newer.command_id).nonce
    assert route.dispatch(newer_receipt, newer, hub_proof(proof, newer_receipt, newer, newer_nonce)).state == "completed"
    for stale, reason in ((cmd, "reservation_superseded"), (newer, "reservation_dispatched")):
        with pytest.raises(SupervisorRouteRejected) as raised:
            route.issue_dispatch_challenge(stale.command_id)
        assert raised.value.reason == reason
    route.close()


@LINUX
def test_outstanding_challenges_are_capped_per_executor(proof):
    route = route_for(proof)
    cmd, _ = reserved(proof, route)
    for _ in range(4):  # SupervisorRouteConfig().max_outstanding_challenges
        route.issue_dispatch_challenge(cmd.command_id)
    with pytest.raises(SupervisorRouteBusy):
        route.issue_dispatch_challenge(cmd.command_id)
    other, _ = reserved(proof, route)
    with pytest.raises(SupervisorRouteBusy):  # Same executor: same cap.
        route.issue_dispatch_challenge(other.command_id)
    proof["clock"].now += WINDOW_NS + 1  # Dead challenges no longer count, even before expiry.
    route.issue_dispatch_challenge(cmd.command_id)
    assert route.expire_challenges() == 4
    assert [state for _, state in challenge_rows(proof["path"])] == ["expired"] * 4 + ["outstanding"]
    route.close()


# ---------------------------------------------------------------- proof binding

@LINUX
def test_every_proof_binding_mismatch_is_held_with_zero_bytes_and_nothing_consumed(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = route.issue_dispatch_challenge(cmd.command_id).nonce
    other_cmd, _ = reserved(proof, route)
    other_nonce = route.issue_dispatch_challenge(other_cmd.command_id).nonce
    binding_cases = {  # Checked against the receipt, reservation row, command and boot in the dispatch txn.
        "fence": dict(fence=cmd.queue_fence + 1),
        "counter": dict(counter=2),
        "executor": dict(executor="b" * 64),
        "command_id": dict(command_id=str(uuid4())),
        "reservation_receipt_sha256": dict(reservation_receipt_sha256="0" * 64),
        "supervisor_boot_id": dict(supervisor_boot_id=str(uuid4())),
    }
    for field, change in binding_cases.items():
        result = route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce, **change))
        assert (result.state, result.error) == ("held", "proof_mismatch"), field
    for value, reason in ((unknown_nonce(), "challenge_unknown"), (other_nonce, "challenge_unknown")):
        result = route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, value))
        assert (result.state, result.error) == ("held", reason)
    good = proof_payload(proof, receipt, cmd, nonce)
    invalid = [  # Structure, types, key and domain: refused before the journal.
        forge({**good, "protocol": "other"}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "audience": "other"}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "hub_key_id": "other-key"}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "issued_at": 1730000000}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),  # No wall clock.
        forge({k: v for k, v in good.items() if k != "lease_epoch"}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "fence": True}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "fence": str(cmd.queue_fence)}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "lease_epoch": 0}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "counter": 1.0}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "job_id": "job 1"}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "challenge_nonce": nonce + "A"}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge({**good, "supervisor_boot_id": "not-a-uuid"}, signer=proof["hub"], domain=HUB_PROOF_DOMAIN),
        forge(good, signer=FixtureSigner(key_id=proof["hub"].key_id), domain=HUB_PROOF_DOMAIN),  # Wrong key.
        forge(good, signer=proof["hub"], domain=RESERVE_DOMAIN),  # Wrong domain.
        hub_proof(proof, receipt, cmd, nonce) + b" ", b"", b"not json",
    ]
    for value in invalid:
        result = route.dispatch(receipt, cmd, value)
        assert (result.state, result.error) == ("held", "proof_invalid"), value[:60]
    assert sends(proof) == 0
    assert route.inspect_challenge(nonce)[0] == "outstanding"  # Nothing consumed by any failure.
    assert route.inspect_reservation(cmd.command_id)[0] == "reserved"
    assert route.inspect(cmd.command_id) is None
    # Hub-side fields the supervisor cannot check are still bound by the signature.
    accepted = hub_proof(proof, receipt, cmd, nonce, job_id="job-7", run_id="run-9", worker="w-3", lease_epoch=42)
    assert route.dispatch(receipt, cmd, accepted).state == "completed" and sends(proof) == 1
    assert route.inspect_challenge(nonce)[0] == "consumed"
    route.close()


@LINUX
def test_replayed_proof_and_concurrent_dispatch_send_exactly_once(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    signed = hub_proof(proof, receipt, cmd, route.issue_dispatch_challenge(cmd.command_id).nonce)
    results, barrier = [], threading.Barrier(6)

    def go():
        barrier.wait(10)
        results.append(route.dispatch(receipt, cmd, signed).state)
    threads = [threading.Thread(target=go) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sends(proof) == 1 and results.count("completed") == 1
    replay = route.dispatch(receipt, cmd, signed)  # The consumed proof again.
    assert replay.state == "held" and sends(proof) == 1
    route.close()


# ---------------------------------------------------------------- freshness window

@LINUX
def test_window_exceeded_is_held_then_expires_with_a_verifiable_statement(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = route.issue_dispatch_challenge(cmd.command_id).nonce
    signed = hub_proof(proof, receipt, cmd, nonce)
    proof["clock"].now += WINDOW_NS + 1
    late = route.dispatch(receipt, cmd, signed)
    assert (late.state, late.error) == ("held", "challenge_expired") and sends(proof) == 0
    assert route.inspect_challenge(nonce)[0] == "outstanding"  # A failed dispatch records nothing.
    assert route.expire_challenges() == 1 and route.inspect_challenge(nonce)[0] == "expired"
    statement = route.expired_statement(nonce)
    payload = proof["statement_verifier"].verify(statement, nonce=nonce)
    assert payload == {"protocol": EXPIRED_PROTOCOL, "audience": EXPIRED_AUDIENCE, "key_id": "test-statement-key",
                       "nonce": nonce, "command_id": cmd.command_id, "counter": 1, "status": "expired_unconsumed",
                       "boot_id": proof["clock"].boot_id}
    for bad in (statement + b" ", forge(payload, signer=proof["statements"], domain=HUB_PROOF_DOMAIN)):
        with pytest.raises(SupervisorRouteError):
            proof["statement_verifier"].verify(bad, nonce=nonce)
    with pytest.raises(SupervisorRouteError):
        proof["statement_verifier"].verify(statement, nonce=unknown_nonce())
    assert route.expired_statement(nonce) == statement  # Repeatable, identical bytes (Ed25519).
    assert route.dispatch(receipt, cmd, signed).error == "challenge_expired"  # Expired is final.
    fresh = route.issue_dispatch_challenge(cmd.command_id).nonce  # The reservation itself is still usable.
    assert route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, fresh)).state == "completed"
    route.close()


@LINUX
def test_window_boundary_on_the_supervisor_clock(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = route.issue_dispatch_challenge(cmd.command_id).nonce
    proof["clock"].now += WINDOW_NS  # Exactly the window: still fresh.
    assert route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce)).state == "completed"
    route.close()


@LINUX
def test_authority_is_barrier_held_ownership_not_the_hub_wall_clock_lease(proof):
    """Defined behaviour: the supervisor cannot see the Hub's wall-clock lease.
    A proof delayed past the Hub-side lease expiry (here: a 2 s lease, a 3 s
    Hub suspend) is ACCEPTED while the supervisor window (5 s from challenge
    issue, on its own CLOCK_BOOTTIME) holds, and REFUSED once it passes."""
    route = route_for(proof)
    hub_lease_ns = 2 * 10**9
    delayed, delayed_receipt = reserved(proof, route)
    nonce = route.issue_dispatch_challenge(delayed.command_id).nonce
    signed = hub_proof(proof, delayed_receipt, delayed, nonce)  # Signed while the Hub lease was valid.
    proof["clock"].now += hub_lease_ns + 10**9  # Hub suspended past its own lease expiry.
    assert route.dispatch(delayed_receipt, delayed, signed).state == "completed"
    later, later_receipt = reserved(proof, route)
    later_nonce = route.issue_dispatch_challenge(later.command_id).nonce
    later_signed = hub_proof(proof, later_receipt, later, later_nonce)
    proof["clock"].now += WINDOW_NS + 1  # Past the supervisor window.
    assert route.dispatch(later_receipt, later, later_signed).error == "challenge_expired"
    assert sends(proof) == 1
    route.close()


@LINUX
def test_previous_boot_challenge_is_refused_and_expirable(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = route.issue_dispatch_challenge(cmd.command_id).nonce
    old_boot = proof["clock"].boot_id
    route.close()
    proof["clock"].boot_id = str(uuid4())  # Host rebooted; same journal.
    rebooted = route_for(proof, create=False)
    stale = rebooted.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce, supervisor_boot_id=old_boot))
    assert stale.error == "proof_mismatch"
    relabelled = rebooted.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce))  # Current boot id.
    assert relabelled.error == "challenge_expired" and sends(proof) == 0
    assert rebooted.inspect_challenge(nonce)[0] == "outstanding"
    payload = proof["statement_verifier"].verify(rebooted.expired_statement(nonce), nonce=nonce)
    assert payload["boot_id"] == old_boot and rebooted.inspect_challenge(nonce)[0] == "expired"
    assert rebooted.expire_challenges() == 0
    rebooted.close()


# ---------------------------------------------------------------- consume vs expire

@LINUX
def test_consumed_and_expired_are_mutually_exclusive(proof):
    route = route_for(proof)
    used_cmd, used_receipt = reserved(proof, route)
    used = route.issue_dispatch_challenge(used_cmd.command_id).nonce
    assert route.dispatch(used_receipt, used_cmd, hub_proof(proof, used_receipt, used_cmd, used)).state == "completed"
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.expired_statement(used)  # A consumed nonce never gets an expired statement.
    assert raised.value.reason == "challenge_used" and proof["statements"].calls == 0
    dead_cmd, dead_receipt = reserved(proof, route)
    dead = route.issue_dispatch_challenge(dead_cmd.command_id).nonce
    route.expired_statement(dead)  # Revoked while still inside the window.
    late = route.dispatch(dead_receipt, dead_cmd, hub_proof(proof, dead_receipt, dead_cmd, dead))
    assert late.error == "challenge_expired" and sends(proof) == 1
    t = "supervisor_route_challenge"
    db = _raw(proof["path"])
    try:
        for sql, args in ((f"UPDATE {t} SET state='expired', proof_sha256=NULL WHERE nonce=?", (used,)),
                          (f"UPDATE {t} SET state='consumed', proof_sha256='x' WHERE nonce=?", (dead,)),
                          (f"UPDATE {t} SET state='outstanding', proof_sha256=NULL WHERE nonce=?", (dead,)),
                          (f"UPDATE {t} SET issued_boottime_ns=issued_boottime_ns+1 WHERE nonce=?", (used,)),
                          (f"DELETE FROM {t}", ()),
                          (f"INSERT OR REPLACE INTO {t} SELECT nonce, command_id, executor_id, counter, "
                           f"issued_boottime_ns, boot_id, 'outstanding', NULL FROM {t} WHERE nonce=?", (used,)),
                          (f"INSERT INTO {t} VALUES (?, ?, ?, 1, 0, ?, 'consumed', 'x')",
                           (unknown_nonce(), str(uuid4()), "a" * 64, str(uuid4())))):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, args)
        fresh = unknown_nonce()
        db.execute(f"INSERT INTO {t} VALUES (?, ?, ?, 1, 0, ?, 'outstanding', NULL)",
                   (fresh, str(uuid4()), "a" * 64, str(uuid4())))
        with pytest.raises(sqlite3.IntegrityError):  # Consuming needs the proof hash (CHECK).
            db.execute(f"UPDATE {t} SET state='consumed' WHERE nonce=?", (fresh,))
        db.execute(f"UPDATE {t} SET state='expired' WHERE nonce=?", (fresh,))
        db.execute("DROP TRIGGER supervisor_route_challenge_kept")
    finally:
        db.close()
    route.close()
    with pytest.raises(SupervisorRouteError):  # Exact schema: a dropped trigger is refused.
        route_for(proof, create=False)


@LINUX
@pytest.mark.parametrize("first", ["dispatch", "expiry"])
def test_dispatch_and_expiry_race_resolve_exactly_one_way(proof, monkeypatch, first):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = route.issue_dispatch_challenge(cmd.command_id).nonce
    signed = hub_proof(proof, receipt, cmd, nonce)
    journal = route._journal
    paused, resume, outcome = threading.Event(), threading.Event(), {}
    # The loser is paused just before its journal transaction; the winner runs fully.
    target = "revoke_unconsumed" if first == "dispatch" else "dispatch_reserved"
    original = getattr(journal, target)

    def paused_then_original(*args, **kwargs):
        paused.set()
        assert resume.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(journal, target, paused_then_original)

    def loser():
        try:
            outcome["value"] = (route.expired_statement(nonce) if first == "dispatch"
                                else route.dispatch(receipt, cmd, signed))
        except SupervisorRouteRejected as error:
            outcome["refused"] = error.reason
    worker = threading.Thread(target=loser)
    worker.start()
    try:
        assert paused.wait(10)
        if first == "dispatch":
            assert route.dispatch(receipt, cmd, signed).state == "completed"
        else:
            proof["statement_verifier"].verify(route.expired_statement(nonce), nonce=nonce)
    finally:
        resume.set()
        worker.join(10)
    if first == "dispatch":
        assert outcome == {"refused": "challenge_used"} and proof["statements"].calls == 0
        assert route.inspect_challenge(nonce)[0] == "consumed" and sends(proof) == 1
    else:
        assert (outcome["value"].state, outcome["value"].error) == ("held", "challenge_expired")
        assert route.inspect_challenge(nonce)[0] == "expired" and sends(proof) == 0
        assert route.inspect_reservation(cmd.command_id)[0] == "reserved"
    route.close()


@LINUX
@pytest.mark.parametrize("failure", ["before_commit", "fsync_after_commit"])
def test_statement_is_signed_only_after_a_durable_commit(proof, monkeypatch, failure):
    route = route_for(proof)
    cmd, _ = reserved(proof, route)
    nonce = route.issue_dispatch_challenge(cmd.command_id).nonce
    if failure == "before_commit":
        original = route._journal._schema
        calls = []

        def failing_schema(db):
            if route._journal.last_phase == "in_transaction" and not calls:
                calls.append(1)
                raise sqlite3.OperationalError("disk I/O error")
            return original(db)
        monkeypatch.setattr(route._journal, "_schema", failing_schema)
    else:
        monkeypatch.setattr(route_module.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(SupervisorRouteError):
        route.expired_statement(nonce)
    monkeypatch.undo()
    assert proof["statements"].calls == 0  # Nothing signed, nothing released.
    expected = "outstanding" if failure == "before_commit" else "expired"  # Rolled back vs committed, unsynced.
    assert route.inspect_challenge(nonce)[0] == expected
    statement = route.expired_statement(nonce)  # Retried once the journal is healthy.
    assert proof["statement_verifier"].verify(statement, nonce=nonce)["status"] == "expired_unconsumed"
    assert proof["statements"].calls == 1
    route.close()


# ---------------------------------------------------------------- persistence

@LINUX
def test_challenges_and_consumption_survive_restart(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = route.issue_dispatch_challenge(cmd.command_id).nonce
    route.close()
    restarted = route_for(proof, create=False)
    assert restarted.inspect_challenge(nonce)[0] == "outstanding"
    signed = hub_proof(proof, receipt, cmd, nonce)
    assert restarted.dispatch(receipt, cmd, signed).state == "completed"
    restarted.close()
    again = route_for(proof, create=False)
    assert again.inspect_challenge(nonce)[0] == "consumed"
    assert again.dispatch(receipt, cmd, signed).state == "held" and sends(proof) == 1
    with pytest.raises(SupervisorRouteRejected):
        again.expired_statement(nonce)
    again.close()
