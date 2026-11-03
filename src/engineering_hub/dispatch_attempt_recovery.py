"""Pure recovery planner for attempt-scoped dispatch holds; design only, disabled.

Nothing here reads a database, verifies a signature, signs, sends or releases a
hold. Callers pass immutable snapshots of the Hub barrier store and the session
gate store, read under the documented lock order (session writer, then Hub),
and get back a deterministic tuple of idempotent actions. The names are neutral:
an adapter must map them onto the Hub owner schema and the session gate schema
before any use. No such adapter exists, and nothing calls this module.

Protocol modelled (see docs/session-dispatch-revocation.md):

* Stage: session write, Hub write; Hub commits the open barrier first, then the
  session commits the gate. A crash between the two commits leaves a session
  barrier with no gate. No proof left, so only an ``expired_unconsumed``
  statement may resolve it. The planner never creates a gate.
* R1 (Hub only): verified evidence resolves the barrier. The planner emits it
  only for barrier IDs in ``verified_evidence``; it never infers evidence.
* R2 (session, Hub read held): the gate follows a resolved barrier.
* A pending revocation completes only once every gate covering its subject is
  already resolved in the snapshot, so completion is planned after R2.
* Any inconsistency for a job yields ``Anomaly`` and no release, R1, R2 or retry
  for that job. Restore or rollback of either store is out of scope.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re

AUTHORITY_KINDS = frozenset(("session", "legacy_credential"))
STATES = frozenset(("open", "resolved"))
RESOLUTION_KINDS = frozenset(("launched", "refused", "expired_unconsumed"))
SCOPES = frozenset(("session", "installation", "grant"))
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")


class DispatchAttemptRecoveryError(ValueError):
    """Invalid snapshot; no partial plan is ever returned."""


def _require(condition):
    if not condition:
        raise DispatchAttemptRecoveryError("invalid dispatch recovery snapshot")


def _id(value):
    _require(type(value) is str and _ID.match(value) is not None)


def _sha(value):
    _require(type(value) is str and _HEX.match(value) is not None)


@dataclass(frozen=True)
class Barrier:
    barrier_id: str
    job_id: str
    attempt: int
    authority_kind: str
    state: str

    def __post_init__(self):
        _id(self.barrier_id); _id(self.job_id)
        _require(type(self.attempt) is int and 1 <= self.attempt < 2**63)
        _require(type(self.authority_kind) is str and self.authority_kind in AUTHORITY_KINDS)
        _require(type(self.state) is str and self.state in STATES)


@dataclass(frozen=True)
class Resolution:
    barrier_id: str
    kind: str
    statement_sha256: str
    receipt_sha256: str
    challenge_sha256: str

    def __post_init__(self):
        _id(self.barrier_id)
        _require(type(self.kind) is str and self.kind in RESOLUTION_KINDS)
        for value in (self.statement_sha256, self.receipt_sha256, self.challenge_sha256):
            _sha(value)


@dataclass(frozen=True)
class Gate:
    job_id: str
    barrier_id: str
    session_id: str
    installation_id: str
    state: str
    resolution_evidence_sha256: str | None

    def __post_init__(self):
        for value in (self.job_id, self.barrier_id, self.session_id, self.installation_id):
            _id(value)
        _require(type(self.state) is str and self.state in STATES)
        if self.state == "open":
            _require(self.resolution_evidence_sha256 is None)
        else:
            _sha(self.resolution_evidence_sha256)


@dataclass(frozen=True)
class PendingRevocation:
    revocation_id: str
    scope: str
    subject: str

    def __post_init__(self):
        _id(self.revocation_id); _id(self.subject)
        _require(type(self.scope) is str and self.scope in SCOPES)


@dataclass(frozen=True)
class Completion:
    revocation_id: str

    def __post_init__(self):
        _id(self.revocation_id)


# Actions. Only RunR1, RunR2 and CompleteRevocation ask for a write; the caller
# must still re-verify everything in its own transaction before writing.
@dataclass(frozen=True)
class Anomaly:
    ref: str
    reason: str


@dataclass(frozen=True)
class Hold:
    ref: str
    reason: str


@dataclass(frozen=True)
class RunR1:
    barrier_id: str


@dataclass(frozen=True)
class RunR2:
    job_id: str
    barrier_id: str


@dataclass(frozen=True)
class CompleteRevocation:
    revocation_id: str


@dataclass(frozen=True)
class RetryAllowed:
    job_id: str
    next_attempt: int


_ORDER = (Anomaly, Hold, RunR1, RunR2, CompleteRevocation, RetryAllowed)
MUTATING = (RunR1, RunR2, CompleteRevocation)


def _sort_key(action):
    return (_ORDER.index(type(action)), tuple(getattr(action, name) for name in action.__dataclass_fields__))


def _rows(values, kind):
    _require(not isinstance(values, (str, bytes, Mapping)))
    try:
        rows = tuple(values)
    except TypeError:
        raise DispatchAttemptRecoveryError("invalid dispatch recovery snapshot") from None
    _require(all(type(row) is kind for row in rows))
    return rows


def _unique(keys):
    keys = tuple(keys)
    _require(len(keys) == len(set(keys)))


def _mapping(value, check_value):
    if value is None:
        return {}
    _require(isinstance(value, Mapping))
    result = dict(value)
    for key, item in result.items():
        _id(key); check_value(item)
    return result


def _kind(value):
    _require(type(value) is str and value in RESOLUTION_KINDS)


def plan(*, barriers, resolutions, gates, pending, completions,
         verified_evidence=None, session_installations=None):
    """Return a sorted tuple of actions, or raise on an invalid snapshot.

    ``verified_evidence`` maps barrier_id to the resolution kind of evidence the
    caller has already authenticated and persisted for R1. ``session_installations``
    optionally maps session_id to installation_id for a consistency check.
    """
    barriers = _rows(barriers, Barrier)
    resolutions = _rows(resolutions, Resolution)
    gates = _rows(gates, Gate)
    pending = _rows(pending, PendingRevocation)
    completions = _rows(completions, Completion)
    evidence = _mapping(verified_evidence, _kind)
    sessions = _mapping(session_installations, _id)
    _unique(b.barrier_id for b in barriers)
    _unique(r.barrier_id for r in resolutions)
    _unique(g.barrier_id for g in gates)
    _unique(p.revocation_id for p in pending)
    _unique(c.revocation_id for c in completions)

    actions = set()
    tainted = set()

    def anomaly(ref, reason, job_id=None):
        actions.add(Anomaly(ref, reason))
        if job_id is not None:
            tainted.add(job_id)

    by_id = {b.barrier_id: b for b in barriers}
    resolution_of = {r.barrier_id: r for r in resolutions}
    gate_of = {g.barrier_id: g for g in gates}
    jobs = {}
    for barrier in barriers:
        jobs.setdefault(barrier.job_id, []).append(barrier)

    for job_id, rows in jobs.items():
        rows.sort(key=lambda b: (b.attempt, b.barrier_id))
        if [b.attempt for b in rows] != list(range(1, len(rows) + 1)):
            anomaly(job_id, "non_monotonic_attempts", job_id)
        if len({b.authority_kind for b in rows}) != 1:
            anomaly(job_id, "authority_kind_changed", job_id)
        opened = [b for b in rows if b.state == "open"]
        if len(opened) > 1:
            anomaly(job_id, "duplicate_open_barrier", job_id)
        elif opened and opened[0] is not rows[-1]:
            anomaly(job_id, "open_barrier_not_latest", job_id)

    for barrier in barriers:
        resolution = resolution_of.get(barrier.barrier_id)
        if barrier.state == "open" and resolution is not None:
            anomaly(barrier.barrier_id, "resolution_without_resolved_barrier", barrier.job_id)
        if barrier.state == "resolved" and resolution is None:
            anomaly(barrier.barrier_id, "resolved_barrier_without_resolution", barrier.job_id)
        gate = gate_of.get(barrier.barrier_id)
        if gate is None:
            if (barrier.authority_kind == "session" and resolution is not None
                    and resolution.kind != "expired_unconsumed"):
                anomaly(barrier.barrier_id, "gateless_barrier_not_expired", barrier.job_id)
            if (barrier.authority_kind == "session" and barrier.state == "open"
                    and barrier.barrier_id in evidence and evidence[barrier.barrier_id] != "expired_unconsumed"):
                anomaly(barrier.barrier_id, "gateless_barrier_not_expired", barrier.job_id)

    for resolution in resolutions:
        if resolution.barrier_id not in by_id:
            anomaly(resolution.barrier_id, "resolution_without_barrier")

    for barrier_id in evidence:
        if barrier_id not in by_id:
            anomaly(barrier_id, "evidence_without_barrier")

    for gate in gates:
        barrier = by_id.get(gate.barrier_id)
        if barrier is None:
            anomaly(gate.barrier_id, "gate_without_barrier", gate.job_id)
            continue
        if barrier.job_id != gate.job_id:
            anomaly(gate.barrier_id, "gate_barrier_job_mismatch", gate.job_id)
            tainted.add(barrier.job_id)
            continue
        if barrier.authority_kind != "session":
            anomaly(gate.barrier_id, "gate_on_legacy_barrier", gate.job_id)
        if gate.state == "resolved" and barrier.state == "open":
            anomaly(gate.barrier_id, "resolved_gate_on_open_barrier", gate.job_id)
        resolution = resolution_of.get(gate.barrier_id)
        if (gate.state == "resolved" and resolution is not None
                and gate.resolution_evidence_sha256 != resolution.statement_sha256):
            anomaly(gate.barrier_id, "gate_evidence_mismatch", gate.job_id)
        if gate.session_id in sessions and sessions[gate.session_id] != gate.installation_id:
            anomaly(gate.barrier_id, "gate_installation_mismatch", gate.job_id)

    for job_id, rows in jobs.items():
        if job_id in tainted:
            continue
        settled = True
        for barrier in rows:
            gate = gate_of.get(barrier.barrier_id)
            if barrier.state == "open":
                settled = False
                if barrier.barrier_id in evidence:
                    actions.add(RunR1(barrier.barrier_id))
                elif barrier.authority_kind == "session" and gate is None:
                    actions.add(Hold(barrier.barrier_id, "gateless_barrier_awaiting_expiry"))
                else:
                    actions.add(Hold(barrier.barrier_id, "dispatch_in_flight"))
            elif gate is not None and gate.state == "open":
                settled = False
                actions.add(RunR2(job_id, barrier.barrier_id))
        if settled:
            actions.add(RetryAllowed(job_id, rows[-1].attempt + 1))

    done = {c.revocation_id for c in completions}
    known = {p.revocation_id for p in pending}
    for revocation_id in done - known:
        anomaly(revocation_id, "completion_without_pending")
    for revocation in pending:
        if revocation.revocation_id in done:
            continue
        if revocation.scope == "session":
            covering = [g for g in gates if g.session_id == revocation.subject]
        else:
            covering = [g for g in gates if g.installation_id == revocation.subject]
        if not covering:
            anomaly(revocation.revocation_id, "pending_without_gate")
        elif any(g.job_id in tainted or (g.barrier_id in by_id and by_id[g.barrier_id].job_id in tainted)
                 for g in covering):
            actions.add(Hold(revocation.revocation_id, "covering_gate_anomaly"))
        elif any(g.state == "open" for g in covering):
            actions.add(Hold(revocation.revocation_id, "covering_gate_open"))
        else:
            actions.add(CompleteRevocation(revocation.revocation_id))
    return tuple(sorted(actions, key=_sort_key))
