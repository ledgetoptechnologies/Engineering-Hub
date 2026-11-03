"""Pure planner tests: crash points, idempotence, scopes, anomalies, validation."""
from dataclasses import replace
import hashlib

import pytest

from engineering_hub import dispatch_attempt_recovery as recovery
from engineering_hub.dispatch_attempt_recovery import (
    Anomaly, Barrier, Completion, CompleteRevocation, DispatchAttemptRecoveryError,
    Gate, Hold, PendingRevocation, Resolution, RetryAllowed, RunR1, RunR2, plan,
)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def barrier(bid, job="job-1", attempt=1, kind="session", state="open"):
    return Barrier(bid, job, attempt, kind, state)


def resolution(bid, kind="expired_unconsumed"):
    return Resolution(bid, kind, sha("statement:" + bid), sha("receipt:" + bid), sha("challenge:" + bid))


def gate(bid, job="job-1", session="s-1", installation="i-1", state="open"):
    evidence = None if state == "open" else sha("statement:" + bid)
    return Gate(job, bid, session, installation, state, evidence)


def snapshot(**overrides):
    base = dict(barriers=(), resolutions=(), gates=(), pending=(), completions=(),
                verified_evidence={}, session_installations={})
    base.update(overrides)
    return base


def run(**overrides):
    return plan(**snapshot(**overrides))


def apply(state, actions):
    """Tiny model of R1, R2 and completion writes; RetryAllowed changes nothing."""
    state = dict(state)
    for action in actions:
        if type(action) is RunR1:
            kind = state["verified_evidence"][action.barrier_id]
            state["barriers"] = tuple(replace(b, state="resolved") if b.barrier_id == action.barrier_id else b
                                      for b in state["barriers"])
            state["resolutions"] = (*state["resolutions"], resolution(action.barrier_id, kind))
        elif type(action) is RunR2:
            state["gates"] = tuple(gate(g.barrier_id, g.job_id, g.session_id, g.installation_id, "resolved")
                                   if g.barrier_id == action.barrier_id else g for g in state["gates"])
        elif type(action) is CompleteRevocation:
            state["completions"] = (*state["completions"], Completion(action.revocation_id))
    return state


def converge(state, limit=6):
    previous = None
    for _ in range(limit):
        actions = plan(**state)
        if not any(isinstance(a, recovery.MUTATING) for a in actions):
            assert plan(**state) == actions
            return state, actions
        assert actions != previous
        previous = actions
        state = apply(state, actions)
    raise AssertionError("planner did not converge")


# Crash points ---------------------------------------------------------------

def test_in_flight_session_attempt_holds_without_evidence():
    assert run(barriers=(barrier("b1"),), gates=(gate("b1"),)) == (Hold("b1", "dispatch_in_flight"),)


def test_crash_after_hub_commit_before_session_commit_holds_and_never_creates_gate():
    actions = run(barriers=(barrier("b1"),))
    assert actions == (Hold("b1", "gateless_barrier_awaiting_expiry"),)
    assert not any(type(a) in (RunR1, RunR2, RetryAllowed) for a in actions)


def test_gateless_barrier_resolves_only_with_expiry_evidence():
    state = snapshot(barriers=(barrier("b1"),), verified_evidence={"b1": "expired_unconsumed"})
    assert plan(**state) == (RunR1("b1"),)
    _, final = converge(state)
    assert final == (RetryAllowed("job-1", 2),)
    launched = run(barriers=(barrier("b1"),), verified_evidence={"b1": "launched"})
    assert launched == (Anomaly("b1", "gateless_barrier_not_expired"),)


def test_crash_between_r1_and_r2_runs_r2():
    actions = run(barriers=(barrier("b1", state="resolved"),), resolutions=(resolution("b1", "launched"),),
                  gates=(gate("b1"),))
    assert actions == (RunR2("job-1", "b1"),)


def test_crash_before_r1_commit_replans_r1():
    assert run(barriers=(barrier("b1"),), gates=(gate("b1"),),
               verified_evidence={"b1": "refused"}) == (RunR1("b1"),)


def test_crash_after_r2_before_completion_completes_revocation():
    actions = run(barriers=(barrier("b1", state="resolved"),), resolutions=(resolution("b1", "launched"),),
                  gates=(gate("b1", state="resolved"),),
                  pending=(PendingRevocation("r1", "session", "s-1"),))
    assert actions == (CompleteRevocation("r1"), RetryAllowed("job-1", 2))


def test_completion_is_planned_only_after_r2():
    actions = run(barriers=(barrier("b1", state="resolved"),), resolutions=(resolution("b1", "launched"),),
                  gates=(gate("b1"),), pending=(PendingRevocation("r1", "session", "s-1"),))
    assert actions == (Hold("r1", "covering_gate_open"), RunR2("job-1", "b1"))


def test_completed_revocation_and_settled_job_are_no_ops():
    actions = run(barriers=(barrier("b1", state="resolved"),), resolutions=(resolution("b1", "launched"),),
                  gates=(gate("b1", state="resolved"),),
                  pending=(PendingRevocation("r1", "session", "s-1"),), completions=(Completion("r1"),))
    assert actions == (RetryAllowed("job-1", 2),)


def test_retry_attempt_follows_resolved_history():
    actions = run(barriers=(barrier("b1", state="resolved"), barrier("b2", attempt=2)),
                  resolutions=(resolution("b1", "refused"),),
                  gates=(gate("b1", state="resolved"), gate("b2")))
    assert actions == (Hold("b2", "dispatch_in_flight"),)


# Idempotence ----------------------------------------------------------------

def test_full_recovery_converges_and_replan_is_stable():
    state = snapshot(
        barriers=(barrier("b1"), barrier("b2", job="job-2"), barrier("b3", job="job-3", kind="legacy_credential")),
        gates=(gate("b1"), gate("b2", job="job-2", session="s-2")),
        pending=(PendingRevocation("r1", "installation", "i-1"), PendingRevocation("r2", "session", "s-2")),
        verified_evidence={"b1": "launched", "b2": "expired_unconsumed", "b3": "refused"})
    final_state, final = converge(state)
    assert final == (RetryAllowed("job-1", 2), RetryAllowed("job-2", 2), RetryAllowed("job-3", 2))
    assert {c.revocation_id for c in final_state["completions"]} == {"r1", "r2"}
    assert all(g.state == "resolved" for g in final_state["gates"])


def test_partial_evidence_converges_to_holds_only():
    state = snapshot(barriers=(barrier("b1"), barrier("b2", job="job-2")),
                     gates=(gate("b1"), gate("b2", job="job-2", session="s-2")),
                     pending=(PendingRevocation("r1", "installation", "i-1"),),
                     verified_evidence={"b1": "launched"})
    _, final = converge(state)
    assert final == (Hold("b2", "dispatch_in_flight"), Hold("r1", "covering_gate_open"), RetryAllowed("job-1", 2))


# Scope rules ----------------------------------------------------------------

def _two_sessions(first_state, second_state):
    return dict(
        barriers=(barrier("b1", state="resolved"), barrier("b2", job="job-2", state=second_state)),
        resolutions=(resolution("b1", "launched"),) + ((resolution("b2", "launched"),) if second_state == "resolved" else ()),
        gates=(gate("b1", state=first_state), gate("b2", job="job-2", session="s-2", state=first_state if second_state == "resolved" else "open")),
    )


def test_installation_revocation_waits_for_every_gate_of_every_session():
    base = _two_sessions("resolved", "open")
    pending = (PendingRevocation("ri", "installation", "i-1"), PendingRevocation("rs", "session", "s-1"),
               PendingRevocation("rg", "grant", "i-1"))
    actions = run(**base, pending=pending)
    assert CompleteRevocation("rs") in actions
    assert Hold("ri", "covering_gate_open") in actions and Hold("rg", "covering_gate_open") in actions
    assert CompleteRevocation("ri") not in actions and CompleteRevocation("rg") not in actions
    done = run(**_two_sessions("resolved", "resolved"), pending=pending)
    assert {a for a in done if type(a) is CompleteRevocation} == {
        CompleteRevocation("ri"), CompleteRevocation("rs"), CompleteRevocation("rg")}


def test_sibling_session_revocation_ignores_other_sessions_gates():
    actions = run(**_two_sessions("resolved", "open"), pending=(PendingRevocation("rs2", "session", "s-2"),))
    assert Hold("rs2", "covering_gate_open") in actions
    actions = run(**_two_sessions("resolved", "open"), pending=(PendingRevocation("rs1", "session", "s-1"),))
    assert CompleteRevocation("rs1") in actions


def test_repeat_revocations_complete_independently():
    base = _two_sessions("resolved", "resolved")
    actions = run(**base, pending=(PendingRevocation("r1", "grant", "i-1"), PendingRevocation("r2", "grant", "i-1")),
                  completions=(Completion("r1"),))
    assert [a for a in actions if type(a) is CompleteRevocation] == [CompleteRevocation("r2")]


# Legacy ---------------------------------------------------------------------

def test_legacy_jobs_are_never_gated_and_recover_hub_only():
    state = snapshot(barriers=(barrier("b1", kind="legacy_credential"),))
    assert plan(**state) == (Hold("b1", "dispatch_in_flight"),)
    state["verified_evidence"] = {"b1": "launched"}
    _, final = converge(state)
    assert final == (RetryAllowed("job-1", 2),)


def test_gate_on_legacy_barrier_is_anomaly_and_releases_nothing():
    actions = run(barriers=(barrier("b1", kind="legacy_credential", state="resolved"),),
                  resolutions=(resolution("b1", "launched"),), gates=(gate("b1"),),
                  pending=(PendingRevocation("r1", "session", "s-1"),))
    assert actions == (Anomaly("b1", "gate_on_legacy_barrier"), Hold("r1", "covering_gate_anomaly"))


# Anomalies ------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, expected", [
    (dict(gates=(gate("b9"),)), Anomaly("b9", "gate_without_barrier")),
    (dict(barriers=(barrier("b1"),), resolutions=(resolution("b1"),), gates=(gate("b1"),)),
     Anomaly("b1", "resolution_without_resolved_barrier")),
    (dict(barriers=(barrier("b1", state="resolved"),), gates=(gate("b1"),)),
     Anomaly("b1", "resolved_barrier_without_resolution")),
    (dict(barriers=(barrier("b1"),), gates=(gate("b1", state="resolved"),)),
     Anomaly("b1", "resolved_gate_on_open_barrier")),
    (dict(barriers=(barrier("b1"), barrier("b2", attempt=2)), gates=(gate("b1"), gate("b2"))),
     Anomaly("job-1", "duplicate_open_barrier")),
    (dict(barriers=(barrier("b1", state="resolved"), barrier("b2", attempt=3)), resolutions=(resolution("b1"),)),
     Anomaly("job-1", "non_monotonic_attempts")),
    (dict(barriers=(barrier("b1"), barrier("b2", attempt=2, state="resolved")), resolutions=(resolution("b2"),)),
     Anomaly("job-1", "open_barrier_not_latest")),
    (dict(barriers=(barrier("b1", state="resolved"), barrier("b2", attempt=2, kind="legacy_credential")),
          resolutions=(resolution("b1"),)), Anomaly("job-1", "authority_kind_changed")),
    (dict(barriers=(barrier("b1", state="resolved"),), resolutions=(resolution("b1", "launched"),)),
     Anomaly("b1", "gateless_barrier_not_expired")),
    (dict(barriers=(barrier("b1", job="job-2"),), gates=(gate("b1"),)), Anomaly("b1", "gate_barrier_job_mismatch")),
    (dict(barriers=(barrier("b1", state="resolved"),), resolutions=(resolution("b1"),),
          gates=(replace(gate("b1", state="resolved"), resolution_evidence_sha256=sha("other")),)),
     Anomaly("b1", "gate_evidence_mismatch")),
    (dict(barriers=(barrier("b1"),), gates=(gate("b1"),), session_installations={"s-1": "i-2"}),
     Anomaly("b1", "gate_installation_mismatch")),
])
def test_job_anomalies_block_every_release_for_the_job(kwargs, expected):
    kwargs.setdefault("verified_evidence", {b.barrier_id: "expired_unconsumed" for b in kwargs.get("barriers", ())})
    actions = run(**kwargs)
    assert expected in actions
    assert not any(type(a) in (RunR1, RunR2, RetryAllowed, CompleteRevocation) for a in actions)


def test_unattached_anomalies():
    actions = run(resolutions=(resolution("b1"),), verified_evidence={"b2": "refused"},
                  pending=(PendingRevocation("r1", "session", "s-1"),), completions=(Completion("r9"),))
    assert actions == (Anomaly("b1", "resolution_without_barrier"), Anomaly("b2", "evidence_without_barrier"),
                       Anomaly("r1", "pending_without_gate"), Anomaly("r9", "completion_without_pending"))


def test_anomaly_on_one_job_does_not_block_another():
    actions = run(barriers=(barrier("b1"), barrier("b2", attempt=2), barrier("b3", job="job-2")),
                  gates=(gate("b3", job="job-2", session="s-2"),), verified_evidence={"b3": "launched"})
    assert Anomaly("job-1", "duplicate_open_barrier") in actions and RunR1("b3") in actions
    assert not any(type(a) is RunR1 and a.barrier_id in ("b1", "b2") for a in actions)


# Determinism and validation -------------------------------------------------

def test_output_is_sorted_and_independent_of_input_order():
    rows = dict(barriers=[barrier("b2", job="job-2"), barrier("b1"), barrier("b3", job="job-3", kind="legacy_credential")],
                gates=[gate("b2", job="job-2", session="s-2"), gate("b1")],
                pending=[PendingRevocation("r2", "session", "s-2"), PendingRevocation("r1", "session", "s-1")],
                verified_evidence={"b1": "launched"})
    first = run(**rows)
    second = run(**{k: (list(reversed(v)) if isinstance(v, list) else v) for k, v in rows.items()})
    assert first == second == (Hold("b2", "dispatch_in_flight"), Hold("b3", "dispatch_in_flight"),
                               Hold("r1", "covering_gate_open"), Hold("r2", "covering_gate_open"), RunR1("b1"))
    assert type(first) is tuple


@pytest.mark.parametrize("build", [
    lambda: Barrier("b1", "job-1", True, "session", "open"),
    lambda: Barrier("b1", "job-1", 0, "session", "open"),
    lambda: Barrier("b1", "job-1", 1, "operator", "open"),
    lambda: Barrier("b1", "job 1", 1, "session", "open"),
    lambda: Barrier("b1", "job-1", 1, "session", "closed"),
    lambda: Resolution("b1", "completed", sha("a"), sha("b"), sha("c")),
    lambda: Resolution("b1", "launched", "ab", sha("b"), sha("c")),
    lambda: Gate("job-1", "b1", "s-1", "i-1", "open", sha("x")),
    lambda: Gate("job-1", "b1", "s-1", "i-1", "resolved", None),
    lambda: PendingRevocation("r1", "machine", "s-1"),
    lambda: Completion(""),
])
def test_invalid_rows_raise(build):
    with pytest.raises(DispatchAttemptRecoveryError):
        build()


@pytest.mark.parametrize("kwargs", [
    dict(barriers=(barrier("b1"), barrier("b1", job="job-2"))),
    dict(resolutions=(resolution("b1"), resolution("b1", "refused"))),
    dict(gates=(gate("b1"), gate("b1", job="job-2"))),
    dict(pending=(PendingRevocation("r1", "session", "s-1"), PendingRevocation("r1", "grant", "i-1"))),
    dict(completions=(Completion("r1"), Completion("r1"))),
    dict(barriers="b1"),
    dict(barriers=({"barrier_id": "b1"},)),
    dict(gates=(resolution("b1"),)),
    dict(verified_evidence={"b1": "maybe"}),
    dict(verified_evidence=[("b1", "launched")]),
    dict(session_installations={"s-1": 7}),
])
def test_invalid_snapshot_raises_instead_of_partial_plan(kwargs):
    with pytest.raises(DispatchAttemptRecoveryError):
        run(**kwargs)
