"""Supervisor half of the one-use dispatch claim proof (docs/supervisor-dispatch-claim-proof.md)."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from hub_container_executor import supervisor_route as route_module
from hub_container_executor.supervisor_route import (
    CHALLENGE_AUDIENCE, CHALLENGE_DOMAIN, CHALLENGE_PROTOCOL, EXPIRED_AUDIENCE, EXPIRED_DOMAIN, EXPIRED_PROTOCOL,
    HUB_PROOF_DOMAIN, RESERVE_DOMAIN, BackendPin, ChallengeStatementSigner, DispatchChallengeSigner,
    DispatchChallengeVerifier, ExpiredStatementVerifier, HubDispatchProofVerifier, RouteEpoch, SupervisorRouteBusy,
    SupervisorRouteError, SupervisorRouteRejected, challenge_expectation,
)
from tests.test_hub_container_executor_supervisor_route import LINUX, command, world  # noqa: F401
from tests.test_hub_container_executor_supervisor_launch_counter import _raw
from tests.test_hub_container_executor_supervisor_reservation import (  # noqa: F401  (two is a fixture)
    CHALLENGE_HASHES, WINDOW_NS, FixtureSigner, forge, hub_proof, make, nonce_of, proof_payload, two,
    unknown_nonce,
)


class CountingSigner(FixtureSigner):
    """TEST-ONLY signer that records every signing call."""

    def __init__(self, key_id="test-statement-key"):
        super().__init__(key_id=key_id)
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


def bound_hash(path, nonce):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT challenge_sha256 FROM supervisor_route_challenge WHERE nonce=?",
                          (nonce,)).fetchone()[0]


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
                dict(challenge_signer=DispatchChallengeSigner()), dict(boot_clock="not callable")):
        with pytest.raises(SupervisorRouteError):
            route_for(proof, create=False, **bad)


# ---------------------------------------------------------------- challenge

@LINUX
def test_challenge_is_recorded_sends_nothing_and_requires_a_live_reservation(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    raw = route.issue_dispatch_challenge(cmd.command_id)
    assert type(raw) is bytes and sends(proof) == 0
    payload = proof["challenge_verifier"].verify(raw, expected=challenge_expectation(receipt, proof["pin"],
                                                                                     proof["epoch"]))
    assert payload == {"protocol": CHALLENGE_PROTOCOL, "audience": CHALLENGE_AUDIENCE, "key_id": "test-challenge-key",
                       "nonce": payload["nonce"], "command_id": cmd.command_id, "counter": 1,
                       "reservation_receipt_sha256": hashlib.sha256(receipt).hexdigest(),
                       "executor": proof["pin"].container_id, "pin": proof["pin"].as_dict(),
                       "epoch": proof["epoch"].as_dict(), "supervisor_boot_id": proof["clock"].boot_id,
                       "proof_window_ms": 5000}
    assert route.inspect_challenge(payload["nonce"]) == ("outstanding", cmd.command_id, 1, proof["clock"].boot_id)
    assert bound_hash(proof["path"], payload["nonce"]) == hashlib.sha256(raw).hexdigest()
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.issue_dispatch_challenge(str(uuid4()))
    assert raised.value.reason == "reservation_missing"
    retired, _ = reserved(proof, route)
    route.abandon_reservation(retired.command_id)
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.issue_dispatch_challenge(retired.command_id)
    assert raised.value.reason == "reservation_abandoned"
    newer, newer_receipt = reserved(proof, route)
    newer_nonce = nonce_of(route.issue_dispatch_challenge(newer.command_id))
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
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
    other_cmd, _ = reserved(proof, route)
    other_nonce = nonce_of(route.issue_dispatch_challenge(other_cmd.command_id))
    binding_cases = {  # Checked against the receipt, reservation row, command and boot in the dispatch txn.
        "fence": dict(fence=cmd.queue_fence + 1),
        "counter": dict(counter=2),
        "executor": dict(executor="b" * 64),
        "command_id": dict(command_id=str(uuid4())),
        "reservation_receipt_sha256": dict(reservation_receipt_sha256="0" * 64),
        "supervisor_boot_id": dict(supervisor_boot_id=str(uuid4())),
        "run_id": dict(run_id=str(uuid4())),  # Must equal the run_id inside the exact request body.
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
    # job_id, worker and lease_epoch exist in no supervisor state or body: bound only by the Hub signature.
    accepted = hub_proof(proof, receipt, cmd, nonce, job_id="job-7", worker="w-3", lease_epoch=42)
    assert route.dispatch(receipt, cmd, accepted).state == "completed" and sends(proof) == 1
    assert route.inspect_challenge(nonce)[0] == "consumed"
    route.close()


@LINUX
def test_replayed_proof_and_concurrent_dispatch_send_exactly_once(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    signed = hub_proof(proof, receipt, cmd, nonce_of(route.issue_dispatch_challenge(cmd.command_id)))
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
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
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
                       "reservation_status": "retired", "boot_id": proof["clock"].boot_id}
    for bad in (statement + b" ", forge(payload, signer=proof["statements"], domain=HUB_PROOF_DOMAIN)):
        with pytest.raises(SupervisorRouteError):
            proof["statement_verifier"].verify(bad, nonce=nonce)
    with pytest.raises(SupervisorRouteError):
        proof["statement_verifier"].verify(statement, nonce=unknown_nonce())
    assert route.expired_statement(nonce) == statement  # Repeatable, identical bytes (Ed25519).
    assert route.dispatch(receipt, cmd, signed).error == "reservation_abandoned"  # Retired: final.
    assert route.inspect_reservation(cmd.command_id)[0] == "abandoned"
    with pytest.raises(SupervisorRouteRejected):  # No new challenge for a retired reservation.
        route.issue_dispatch_challenge(cmd.command_id)
    assert sends(proof) == 0
    route.close()


@LINUX
def test_window_boundary_on_the_supervisor_clock(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
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
    nonce = nonce_of(route.issue_dispatch_challenge(delayed.command_id))
    signed = hub_proof(proof, delayed_receipt, delayed, nonce)  # Signed while the Hub lease was valid.
    proof["clock"].now += hub_lease_ns + 10**9  # Hub suspended past its own lease expiry.
    assert route.dispatch(delayed_receipt, delayed, signed).state == "completed"
    later, later_receipt = reserved(proof, route)
    later_nonce = nonce_of(route.issue_dispatch_challenge(later.command_id))
    later_signed = hub_proof(proof, later_receipt, later, later_nonce)
    proof["clock"].now += WINDOW_NS + 1  # Past the supervisor window.
    assert route.dispatch(later_receipt, later, later_signed).error == "challenge_expired"
    assert sends(proof) == 1
    route.close()


@LINUX
def test_previous_boot_challenge_is_refused_and_expirable(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
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
    used = nonce_of(route.issue_dispatch_challenge(used_cmd.command_id))
    assert route.dispatch(used_receipt, used_cmd, hub_proof(proof, used_receipt, used_cmd, used)).state == "completed"
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.expired_statement(used)  # A consumed nonce never gets an expired statement.
    assert raised.value.reason == "challenge_used" and proof["statements"].calls == 0
    dead_cmd, dead_receipt = reserved(proof, route)
    dead = nonce_of(route.issue_dispatch_challenge(dead_cmd.command_id))
    route.expired_statement(dead)  # Revoked (reservation retired) while still inside the window.
    late = route.dispatch(dead_receipt, dead_cmd, hub_proof(proof, dead_receipt, dead_cmd, dead))
    assert late.error == "reservation_abandoned" and sends(proof) == 1
    assert route.inspect_challenge(dead)[0] == "expired"
    t = "supervisor_route_challenge"
    db = _raw(proof["path"])
    try:
        for sql, args in ((f"UPDATE {t} SET state='expired', proof_sha256=NULL WHERE nonce=?", (used,)),
                          (f"UPDATE {t} SET state='consumed', proof_sha256='x' WHERE nonce=?", (dead,)),
                          (f"UPDATE {t} SET state='outstanding', proof_sha256=NULL WHERE nonce=?", (dead,)),
                          (f"UPDATE {t} SET issued_boottime_ns=issued_boottime_ns+1 WHERE nonce=?", (used,)),
                          (f"DELETE FROM {t}", ()),
                          (f"INSERT OR REPLACE INTO {t} SELECT nonce, command_id, executor_id, counter, "
                           f"issued_boottime_ns, boot_id, 'outstanding', NULL, NULL FROM {t} WHERE nonce=?", (used,)),
                          (f"INSERT INTO {t} VALUES (?, ?, ?, 1, 0, ?, 'consumed', 'x', '4444')",
                           (unknown_nonce(), str(uuid4()), "a" * 64, str(uuid4())))):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, args)
        fresh = unknown_nonce()
        db.execute(f"INSERT INTO {t} VALUES (?, ?, ?, 1, 0, ?, 'outstanding', NULL, NULL)",
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
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
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
        assert (outcome["value"].state, outcome["value"].error) == ("held", "reservation_abandoned")
        assert route.inspect_challenge(nonce)[0] == "expired" and sends(proof) == 0
        assert route.inspect_reservation(cmd.command_id)[0] == "abandoned"
    route.close()


@LINUX
@pytest.mark.parametrize("first", ["statement", "dispatch"])
def test_statement_for_one_nonce_retires_the_reservation_against_its_other_nonce(proof, monkeypatch, first):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    stated = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
    other = nonce_of(route.issue_dispatch_challenge(cmd.command_id))  # A second live challenge, same reservation.
    signed_other = hub_proof(proof, receipt, cmd, other)
    journal = route._journal
    paused, resume, outcome = threading.Event(), threading.Event(), {}
    target = "dispatch_reserved" if first == "statement" else "revoke_unconsumed"
    original = getattr(journal, target)

    def paused_then_original(*args, **kwargs):
        paused.set()
        assert resume.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(journal, target, paused_then_original)

    def loser():
        try:
            outcome["value"] = (route.dispatch(receipt, cmd, signed_other) if first == "statement"
                                else route.expired_statement(stated))
        except SupervisorRouteRejected as error:
            outcome["refused"] = error.reason
    worker = threading.Thread(target=loser)
    worker.start()
    try:
        assert paused.wait(10)
        if first == "statement":
            payload = proof["statement_verifier"].verify(route.expired_statement(stated), nonce=stated)
            assert payload["reservation_status"] == "retired"
        else:
            assert route.dispatch(receipt, cmd, signed_other).state == "completed"
    finally:
        resume.set()
        worker.join(10)
    if first == "statement":
        assert (outcome["value"].state, outcome["value"].error) == ("held", "reservation_abandoned")
        assert [route.inspect_challenge(n)[0] for n in (stated, other)] == ["expired", "expired"]
        assert route.inspect_reservation(cmd.command_id)[0] == "abandoned" and sends(proof) == 0
    else:
        assert outcome == {"refused": "challenge_used"} and proof["statements"].calls == 0
        assert route.inspect_challenge(other)[0] == "consumed" and sends(proof) == 1
    route.close()


@LINUX
def test_proof_mode_refuses_every_proofless_send_path_and_is_permanent(proof):
    route = route_for(proof)
    direct = command(proof["epoch"])
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.submit(direct)
    assert raised.value.reason == "proof_required"
    request = route_module._submitted_request(direct)
    with pytest.raises(SupervisorRouteRejected) as raised:  # Even bypassing submit()'s own check.
        route._admitted(direct, request)
    assert raised.value.reason == "proof_required"
    with pytest.raises(SupervisorRouteRejected) as raised:  # And at the journal itself.
        binding = route_module.binding_for(direct, proof["pin"])
        route._journal.admit(direct.command_id, route_module.binding_digest(binding), binding, proof["epoch"],
                             "0" * 32)
    assert raised.value.reason == "proof_required"
    cmd, receipt = reserved(proof, route)
    assert route.dispatch(receipt, cmd, b"").error == "proof_invalid"  # dispatch needs a real proof.
    assert sends(proof) == 0 and route.inspect(direct.command_id) is None
    route.close()
    with pytest.raises(SupervisorRouteError):  # A later construction without a verifier is refused.
        route_for(proof, create=False, dispatch_proof_verifier=None)
    db = _raw(proof["path"])
    try:
        for sql in ("UPDATE supervisor_route_mode SET proof_required=1", "DELETE FROM supervisor_route_mode",
                    "INSERT OR REPLACE INTO supervisor_route_mode VALUES (1, 1)"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
    finally:
        db.close()
    reopened = route_for(proof, create=False)  # With a verifier it reopens, still in proof mode.
    with pytest.raises(SupervisorRouteRejected):
        reopened.submit(command(proof["epoch"]))
    assert sends(proof) == 0
    reopened.close()


class _RewritingDb:
    """Wraps one journal connection; rewrites only the challenge-consume UPDATE."""

    def __init__(self, db, mode):
        self._db, self._mode = db, mode

    def execute(self, sql, params=()):
        if sql.startswith("UPDATE supervisor_route_challenge SET state='consumed'"):
            if self._mode == "no_row":
                sql += " AND 0"  # Affects zero rows.
            else:
                params = ("f" * 64, params[1])  # Consumes with a different proof hash.
        return self._db.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._db, name)


@LINUX
@pytest.mark.parametrize("mode", ["no_row", "wrong_hash"])
def test_consume_is_read_back_before_any_byte_is_sent(proof, monkeypatch, mode):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
    original = route._journal._connection

    @contextmanager
    def rewriting(*, readonly=False):
        with original(readonly=readonly) as db:
            yield _RewritingDb(db, mode)
    monkeypatch.setattr(route._journal, "_connection", rewriting)
    result = route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce))
    monkeypatch.undo()
    assert (result.state, result.error) == ("held", "challenge_consume_failed") and sends(proof) == 0
    assert route.inspect_challenge(nonce)[0] == "outstanding"  # Rolled back: nothing consumed or marked.
    assert route.inspect_reservation(cmd.command_id)[0] == "reserved" and route.inspect(cmd.command_id) is None
    assert route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce)).state == "completed"
    route.close()


@LINUX
@pytest.mark.parametrize("failure", ["before_commit", "fsync_after_commit"])
def test_statement_is_signed_only_after_a_durable_commit(proof, monkeypatch, failure):
    route = route_for(proof)
    cmd, _ = reserved(proof, route)
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
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
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
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


# ---------------------------------------------------------------- signed challenge

@LINUX
def test_challenge_signer_and_verifier_default_refuse(proof):
    route = route_for(proof, challenge_signer=None)  # Verifier configured, no challenge signer.
    cmd, receipt = reserved(proof, route)
    with pytest.raises(SupervisorRouteError) as raised:
        route.issue_dispatch_challenge(cmd.command_id)
    assert "challenge signer not configured" in str(raised.value)
    assert challenge_rows(proof["path"]) == []  # Refused before the journal: nothing recorded.
    with pytest.raises(SupervisorRouteError):
        DispatchChallengeSigner().sign(b"x")
    route.close()
    route = route_for(proof, create=False)
    raw = route.issue_dispatch_challenge(cmd.command_id)
    with pytest.raises(SupervisorRouteError):
        DispatchChallengeVerifier().verify(raw, expected=challenge_expectation(receipt, proof["pin"], proof["epoch"]))
    route.close()


@LINUX
def test_challenge_verifier_refuses_wrong_key_binding_tamper_and_non_canonical(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    raw = route.issue_dispatch_challenge(cmd.command_id)
    verifier = proof["challenge_verifier"]
    expected = challenge_expectation(receipt, proof["pin"], proof["epoch"])
    payload = verifier.verify(raw, expected=expected)
    other_pin = BackendPin(proof["pin"].container_id, proof["pin"].host_pid + 1, proof["pin"].process_start_ticks,
                           proof["pin"].uid, proof["pin"].gid, proof["pin"].engine_id, proof["pin"].engine_pid,
                           proof["pin"].engine_start_ticks)  # Same container, different process.
    for wrong in ({**expected, "pin": other_pin.as_dict()},
                  {**expected, "epoch": RouteEpoch(str(uuid4()), 5, proof["epoch"].boot_id).as_dict()},
                  {**expected, "counter": 2}, {**expected, "executor": "b" * 64},
                  {**expected, "reservation_receipt_sha256": "0" * 64}, {**expected, "command_id": str(uuid4())},
                  {**expected, "counter": True}, {**expected, "extra": 1},
                  {k: v for k, v in expected.items() if k != "pin"}):
        with pytest.raises(SupervisorRouteError):
            verifier.verify(raw, expected=wrong)
    with pytest.raises(SupervisorRouteError):  # The helper itself refuses a receipt for another pin/epoch.
        challenge_expectation(receipt, other_pin, proof["epoch"])
    other_key = FixtureSigner(key_id="test-challenge-key")
    bad = [
        raw + b" ", json.dumps(json.loads(raw)).encode(),  # Non-canonical.
        canonical_with(raw, proof_window_ms=30000), canonical_with(raw, nonce=unknown_nonce()),  # Tampered.
        forge(payload, signer=other_key, domain=CHALLENGE_DOMAIN),  # Wrong key.
        forge(payload, signer=proof["challenger"], domain=HUB_PROOF_DOMAIN),  # Wrong domain.
        forge({**payload, "issued_boottime_ns": 1}, signer=proof["challenger"], domain=CHALLENGE_DOMAIN),
        forge({k: v for k, v in payload.items() if k != "epoch"}, signer=proof["challenger"], domain=CHALLENGE_DOMAIN),
        forge({**payload, "audience": "other"}, signer=proof["challenger"], domain=CHALLENGE_DOMAIN),
        forge({**payload, "proof_window_ms": 999}, signer=proof["challenger"], domain=CHALLENGE_DOMAIN),
        forge({**payload, "executor": "b" * 64}, signer=proof["challenger"], domain=CHALLENGE_DOMAIN),
    ]
    for value in bad:
        with pytest.raises(SupervisorRouteError):
            verifier.verify(value, expected=expected)
    for pinned in (DispatchChallengeVerifier(public_key=other_key.public_key, key_id="test-challenge-key"),
                   DispatchChallengeVerifier(public_key=proof["challenger"].public_key, key_id="other-key")):
        with pytest.raises(SupervisorRouteError):
            pinned.verify(raw, expected=expected)
    route.close()


def canonical_with(raw, **changes):
    envelope = json.loads(raw)
    return json.dumps({**envelope, "payload": {**envelope["payload"], **changes}}, sort_keys=True,
                      separators=(",", ":")).encode()


@LINUX
def test_challenge_for_one_reservation_cannot_be_replayed_for_another(proof):
    route = route_for(proof)
    a, a_receipt = reserved(proof, route)
    b, b_receipt = reserved(proof, route)
    a_raw = route.issue_dispatch_challenge(a.command_id)
    a_nonce = nonce_of(a_raw)
    with pytest.raises(SupervisorRouteError):  # Hub verifier: bound to A's reservation and counter.
        proof["challenge_verifier"].verify(a_raw, expected=challenge_expectation(b_receipt, proof["pin"],
                                                                                 proof["epoch"]))
    replay = route.dispatch(b_receipt, b, hub_proof(proof, b_receipt, b, a_nonce))  # A's nonce + hash for B.
    assert replay.error == "challenge_unknown"
    b_nonce = nonce_of(route.issue_dispatch_challenge(b.command_id))
    swapped = hub_proof(proof, b_receipt, b, b_nonce, challenge_sha256=hashlib.sha256(a_raw).hexdigest())
    assert route.dispatch(b_receipt, b, swapped).error == "proof_mismatch"  # B's nonce, A's challenge bytes.
    assert sends(proof) == 0 and route.inspect_challenge(b_nonce)[0] == "outstanding"
    assert route.dispatch(b_receipt, b, hub_proof(proof, b_receipt, b, b_nonce)).state == "completed"
    route.close()


@LINUX
def test_proof_with_wrong_challenge_hash_is_held_with_zero_bytes(proof):
    route = route_for(proof)
    cmd, receipt = reserved(proof, route)
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
    for wrong in ("0" * 64, hashlib.sha256(b"other").hexdigest()):
        result = route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce, challenge_sha256=wrong))
        assert (result.state, result.error) == ("held", "proof_mismatch")
    for invalid in ("A" * 64, "0" * 63, 7):  # Wrong type/shape: refused by the proof verifier.
        result = route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce, challenge_sha256=invalid))
        assert result.error == "proof_invalid"
    assert sends(proof) == 0 and route.inspect_challenge(nonce)[0] == "outstanding"
    assert route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce)).state == "completed"
    route.close()


@LINUX
@pytest.mark.parametrize("failure", ["issue_rolled_back", "issue_fsync", "bind_fails"])
def test_challenge_is_signed_only_after_its_row_commit_is_durable(proof, monkeypatch, failure):
    signer = CountingSigner(key_id="test-challenge-key")
    route = route_for(proof, challenge_signer=signer)
    cmd, receipt = reserved(proof, route)
    journal = route._journal
    if failure == "issue_rolled_back":
        original, calls = journal._schema, []

        def failing_schema(db):
            if journal.last_phase == "in_transaction" and not calls:
                calls.append(1)
                raise sqlite3.OperationalError("disk I/O error")
            return original(db)
        monkeypatch.setattr(journal, "_schema", failing_schema)
    elif failure == "issue_fsync":
        monkeypatch.setattr(route_module.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync")))
    else:
        def bind_fails(*args):
            raise route_module.RouteJournalError("disk")
        monkeypatch.setattr(journal, "bind_challenge", bind_fails)
    with pytest.raises(SupervisorRouteError):
        route.issue_dispatch_challenge(cmd.command_id)
    monkeypatch.undo()
    rows = challenge_rows(proof["path"])
    if failure == "issue_rolled_back":
        assert rows == [] and signer.calls == 0  # Nothing committed, nothing signed.
    elif failure == "issue_fsync":
        assert [s for _, s in rows] == ["outstanding"] and signer.calls == 0  # Committed, not proved durable.
    else:
        assert [s for _, s in rows] == ["outstanding"] and signer.calls == 1  # Signed, hash not bound: withheld.
    if rows:
        nonce = rows[0][0]
        assert bound_hash(proof["path"], nonce) is None
        # Whatever bytes a signer may have produced, an unbound challenge can never be consumed.
        forged_hash = CHALLENGE_HASHES.get(nonce, "0" * 64)
        result = route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce, challenge_sha256=forged_hash))
        assert result.error == "proof_mismatch" and sends(proof) == 0
    raw = route.issue_dispatch_challenge(cmd.command_id)  # Healthy again: a fresh signed challenge works.
    assert route.dispatch(receipt, cmd, hub_proof(proof, receipt, cmd, nonce_of(raw))).state == "completed"
    route.close()


@LINUX
def test_challenge_hash_is_bound_once_and_required_for_consumption(proof):
    route = route_for(proof)
    cmd, _ = reserved(proof, route)
    nonce = nonce_of(route.issue_dispatch_challenge(cmd.command_id))
    t = "supervisor_route_challenge"
    db = _raw(proof["path"])
    try:
        for sql, args in ((f"UPDATE {t} SET challenge_sha256=? WHERE nonce=?", ("1" * 64, nonce)),  # Rebind.
                          (f"UPDATE {t} SET challenge_sha256=NULL WHERE nonce=?", (nonce,)),
                          (f"INSERT INTO {t} VALUES (?, ?, ?, 1, 0, ?, 'outstanding', NULL, ?)",
                           (unknown_nonce(), str(uuid4()), "a" * 64, str(uuid4()), "2" * 64))):  # Pre-bound.
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, args)
        unbound = unknown_nonce()
        db.execute(f"INSERT INTO {t} VALUES (?, ?, ?, 1, 0, ?, 'outstanding', NULL, NULL)",
                   (unbound, str(uuid4()), "a" * 64, str(uuid4())))
        with pytest.raises(sqlite3.IntegrityError):  # Consuming needs a bound challenge hash (CHECK).
            db.execute(f"UPDATE {t} SET state='consumed', proof_sha256='x' WHERE nonce=?", (unbound,))
        db.execute(f"UPDATE {t} SET state='expired' WHERE nonce=?", (unbound,))  # Unbound may only expire.
        with pytest.raises(sqlite3.IntegrityError):  # And can never be bound afterwards.
            db.execute(f"UPDATE {t} SET challenge_sha256=? WHERE nonce=?", ("3" * 64, unbound))
    finally:
        db.close()
    route.close()


# ---------------------------------------------------------------- legacy -> proof mode race

def _paused_before_send(monkeypatch, route):
    """Pause a one-step submit after its admit commit, before its send guard."""
    paused, resume = threading.Event(), threading.Event()
    original = route._journal.legacy_send_guard

    def wait_then_guard(*args, **kwargs):
        paused.set()
        assert resume.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(route._journal, "legacy_send_guard", wait_then_guard)
    return paused, resume


def _submit_in_thread(route, cmd):
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("result", route.submit(cmd)))
    worker.start()
    return worker, outcome


@LINUX
def test_proof_mode_entry_refused_while_a_legacy_send_is_in_flight(proof, monkeypatch):
    legacy = route_for(proof, dispatch_proof_verifier=None)
    paused, resume = _paused_before_send(monkeypatch, legacy)
    cmd = command(proof["epoch"])
    worker, outcome = _submit_in_thread(legacy, cmd)
    try:
        assert paused.wait(10)  # Admitted (durable intent), not yet sent.
        with pytest.raises(SupervisorRouteError):  # The flock: no second instance on this journal.
            route_for(proof, create=False)
        with pytest.raises(SupervisorRouteRejected) as raised:  # Mode entry itself, flock aside.
            legacy._journal.enter_mode(proof_required=True)
        assert raised.value.reason == "legacy_in_flight" and sends(proof) == 0
    finally:
        resume.set()
        worker.join(10)
    assert outcome["result"].state == "completed" and sends(proof) == 1
    assert legacy._journal.enter_mode(proof_required=True) is True  # Resolved: now allowed.
    with pytest.raises(SupervisorRouteRejected):
        legacy.submit(command(proof["epoch"]))  # admit() sees the durable mode.
    legacy.close()


@LINUX
@pytest.mark.parametrize("entered_by", ["journal_after_marking_held", "flock_ignoring_writer"])
def test_paused_legacy_send_is_refused_once_proof_mode_is_entered(proof, monkeypatch, entered_by):
    legacy = route_for(proof, dispatch_proof_verifier=None)
    paused, resume = _paused_before_send(monkeypatch, legacy)
    cmd = command(proof["epoch"])
    worker, outcome = _submit_in_thread(legacy, cmd)
    try:
        assert paused.wait(10)
        if entered_by == "journal_after_marking_held":
            digest = route_module.binding_digest(route_module.binding_for(cmd, proof["pin"]))
            legacy._journal.mark_unknown(cmd.command_id, digest, "interrupted")  # Operator: held.
            assert legacy._journal.enter_mode(proof_required=True) is True
        else:
            db = _raw(proof["path"])  # An old/direct writer: ignores the flock and legacy_in_flight.
            try:
                db.execute("INSERT INTO supervisor_route_mode VALUES (1, 1)")
            finally:
                db.close()
    finally:
        resume.set()
        worker.join(10)
    result = outcome["result"]
    assert (result.state, result.error) == ("held", "proof_required") and sends(proof) == 0  # Never sent.
    assert legacy.inspect(cmd.command_id).state == "held"
    legacy.close()


@LINUX
def test_no_writer_can_commit_proof_mode_or_resolve_the_intent_mid_send(proof):
    entered, release = threading.Event(), threading.Event()

    class Blocking(type(proof["sender"])):
        def forward(self, **kwargs):
            entered.set()
            assert release.wait(10)
            return super().forward(**kwargs)
    legacy = route_for(proof, dispatch_proof_verifier=None, backend=Blocking(proof["pin"]))
    cmd = command(proof["epoch"])
    worker, outcome = _submit_in_thread(legacy, cmd)
    try:
        assert entered.wait(10)  # Inside the backend exchange, send guard held.
        db = sqlite3.connect(Path(proof["path"]).as_uri() + "?mode=rw", uri=True, isolation_level=None,
                             timeout=0.2)
        try:
            for sql in ("INSERT INTO supervisor_route_mode VALUES (1, 1)",
                        "UPDATE supervisor_route_intent SET state='unknown', error='interrupted'"):
                with pytest.raises(sqlite3.OperationalError):  # database is locked: cannot COMMIT.
                    db.execute(sql)
        finally:
            db.close()
    finally:
        release.set()
        worker.join(10)
    assert outcome["result"].state == "completed"
    db = _raw(proof["path"])
    try:
        db.execute("INSERT INTO supervisor_route_mode VALUES (1, 1)")  # After the exchange: commits.
    finally:
        db.close()
    legacy.close()


# ---------------------------------------------------------------- rollback-journal mode only

def _to_wal(path):
    db = _raw(path)
    assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    return db


@LINUX
def test_wal_mode_journal_is_refused_on_open(proof, tmp_path):
    from tests.test_hub_container_executor_supervisor_launch_counter import gate_config
    route = route_for(proof)
    route.close()
    _to_wal(proof["path"]).close()  # Converted while the route was closed.
    assert Path(proof["path"]).read_bytes()[18:20] == b"\x02\x02"
    with pytest.raises(SupervisorRouteError):
        route_for(proof, create=False)
    assert Path(proof["path"]).read_bytes()[18:20] == b"\x02\x02"  # Refused, NOT silently converted back.
    gate_dir = tmp_path / "gate"
    gate_dir.mkdir(mode=0o700)
    gate_path = gate_dir / "gate.sqlite"
    route_module.DispatchCounterGate(config=gate_config(gate_path)).close()
    _to_wal(gate_path).close()
    with pytest.raises(SupervisorRouteError):  # Same store base: the executor gate refuses too.
        route_module.DispatchCounterGate(config=gate_config(gate_path, create=False))


@LINUX
def test_send_guard_refuses_a_journal_switched_to_wal_with_zero_bytes(proof, monkeypatch):
    legacy = route_for(proof, dispatch_proof_verifier=None)
    paused, resume = _paused_before_send(monkeypatch, legacy)
    cmd = command(proof["epoch"])
    worker, outcome = _submit_in_thread(legacy, cmd)
    wal = None
    try:
        assert paused.wait(10)  # Admitted under rollback-journal mode, not yet sent.
        wal = _to_wal(proof["path"])  # Kept open: WAL readers then open normally.
    finally:
        resume.set()
        worker.join(10)
    try:
        result = outcome["result"]
        assert (result.state, result.error) == ("held", "send_guard_failed") and sends(proof) == 0
    finally:
        wal.close()
    legacy._journal.close()
