from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from engineering_hub.reachability import (
    ObserverContext, RouteObservation, TopicBinding, project_reachability,
    project_topic_snapshot,
)


NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def binding(**changes):
    return TopicBinding(**({
        "topic": "hub-build", "agent": "hermes", "session_id": "private-session",
        "session_generation": 2, "connector_generation": 3,
        "observer_id": "private-observer",
    } | changes))


def context(**changes):
    return ObserverContext(**({
        "observer_id": "private-observer", "epoch": "private-epoch",
        "now": NOW, "max_age_seconds": 120,
    } | changes))


def evidence(**changes):
    return RouteObservation(**({
        **binding().model_dump(exclude={"binding_revision"}), "observer_epoch": "private-epoch",
        "sequence": 1, "observed_at": NOW - timedelta(seconds=1),
        "provenance": "connector_observer", "event": "ready",
        "native_attachment": "verified", "runtime_acceptance": "idle",
    } | changes))


def project(events=(), **changes):
    return project_reachability(**({
        "topic": "hub-build", "agent": "hermes", "binding": binding(),
        "observer": context(), "observations": tuple(events),
    } | changes))


def test_unconfigured_registry_is_not_authoritative_absence():
    assert project(binding=None, observer=None).status == "unknown_stale"
    assert project(binding=None, binding_snapshot_complete=True).status == "unlinked"
    assert project(binding=binding(topic="other")).status == "unknown_stale"
    assert project(binding=binding(topic="other"),
                   binding_snapshot_complete=True).status == "unlinked"


def test_linked_route_without_listener_is_unverified():
    assert project().status == "linked_unverified"
    assert project().reason == "no_observer_evidence"


@pytest.mark.parametrize("changes", [{"topic": "other"}, {"agent": "claude"},
                                     {"observer_id": "other-observer"}])
def test_other_topic_agent_or_observer_does_not_prove_ready(changes):
    assert project([evidence(**changes)]).status == "linked_unverified"


@pytest.mark.parametrize("changes", [{"session_generation": 1},
                                     {"connector_generation": 2},
                                     {"session_id": "old-session"}])
def test_wrong_binding_generation_never_falls_back_to_older_ready(changes):
    result = project([evidence(), evidence(sequence=2, **changes)])
    assert result.status == "unknown_stale"
    assert result.reason == "binding_generation_mismatch"


def test_observer_restart_cannot_reuse_cached_ready():
    result = project([evidence()], observer=context(epoch="new-epoch"))
    assert result.status == "unknown_stale"
    assert result.reason == "observer_restarted"
    assert project([evidence()], observer=None).status == "unknown_stale"


def test_new_epoch_ready_supersedes_old_epoch_large_sequence():
    assert project([evidence(sequence=999, observer_epoch="old"),
                    evidence(sequence=1)]).status == "reachable"


def test_disconnect_sequence_wins_over_timestamp_and_input_order():
    disconnected = evidence(event="disconnected", sequence=2,
                            observed_at=NOW - timedelta(seconds=2))
    assert project([disconnected, evidence()]).status == "offline"
    assert project([evidence(), disconnected]).reason == "session_disconnected"
    # Reconnection requires new exact-session verified evidence, not an unrelated
    # generic stream or another topic coming online.
    assert project([disconnected, evidence(sequence=3, topic="other")]).status == "offline"
    assert project([disconnected, evidence(sequence=3)]).status == "reachable"


def test_busy_requires_verified_queue_and_is_not_completion():
    result = project([evidence(event="busy_queued", provenance="runtime_control",
                               runtime_acceptance="queue")])
    assert result.status == "busy_queued"
    assert result.advisory_only is True
    assert result.transport_state == "attached"
    assert result.availability_state == "busy"
    assert "delivered" not in result.model_dump()
    assert "completed" not in result.model_dump()


def test_explicit_offline_and_stale_disconnect_are_distinct():
    assert project([evidence(event="offline")]).status == "offline"
    stale = evidence(event="disconnected", observed_at=NOW - timedelta(seconds=121))
    assert project([stale]).status == "unknown_stale"
    assert project([stale]).reason == "evidence_expired"


def test_generic_bus_stream_is_not_exact_session_readiness():
    result = project([evidence(event="stream_connected", provenance="bus_stream")])
    assert result.status == "linked_unverified"
    assert result.reason == "stream_only"
    with pytest.raises(ValidationError):
        evidence(event="ready", provenance="bus_stream")


def test_expiry_and_future_observations_fail_closed():
    assert project([evidence(observed_at=NOW - timedelta(seconds=120))]).status == "reachable"
    assert project([evidence(observed_at=NOW - timedelta(seconds=121))]).status == "unknown_stale"
    assert project([evidence(observed_at=NOW + timedelta(seconds=1))]).reason == "evidence_in_future"


def test_duplicate_identical_sequence_is_safe_conflicting_sequence_is_not():
    assert project([evidence(), evidence()]).status == "reachable"
    assert project([evidence(), evidence(event="offline")]).reason == "conflicting_evidence"


def test_public_projection_has_no_private_session_epoch_or_transport_material():
    result = project([evidence()])
    public = result.model_dump_json()
    for private in ["private-session", "private-observer", "private-epoch",
                    "session_generation", "connector_generation", "credentials"]:
        assert private not in public
    assert result.last_evidence_at == NOW - timedelta(seconds=1)
    assert result.state_since is None  # Last-seen is not transition history.


@pytest.mark.parametrize("changes", [
    {"sequence": True}, {"session_generation": "2"},
    {"connector_generation": 0}, {"topic": " "}, {"topic": "line\nbreak"},
    {"observed_at": NOW.replace(tzinfo=None)}, {"ready": True},
    {"provenance": "agent_body"}, {"credentials": "secret"},
])
def test_strict_evidence_rejects_body_assertions_coercion_and_bad_fields(changes):
    with pytest.raises(ValidationError):
        evidence(**changes)


def test_models_are_immutable_and_projection_has_no_effects():
    event = evidence()
    with pytest.raises(ValidationError):
        event.event = "offline"
    assert project([event]) == project([event])
    with pytest.raises(TypeError):
        project(binding_snapshot_complete="true")


def test_preflight_does_not_prevent_disconnect_before_send():
    ready = evidence()
    assert project([ready]).status == "reachable"
    later = project([ready, evidence(sequence=2, event="disconnected")])
    assert later.status == "offline"
    # This pure projector does not delete durable queued messages or issue sends.
    assert later.advisory_only is True


@pytest.mark.parametrize("changes", [{"native_attachment": "unverified"},
                                     {"runtime_acceptance": "unverified"},
                                     {"runtime_acceptance": "queue"}])
def test_ready_requires_native_attachment_and_owning_runtime_acceptance(changes):
    result = project([evidence(**changes)])
    assert result.status == "linked_unverified"
    assert result.reason == "runtime_readiness_unverified"


def test_public_revisions_are_registry_allocated_separate_from_private_generations():
    public_revision = "aa348f5a-1d1c-4e07-862f-4d7fce051f08"
    snapshot_revision = "6572ba19-354d-433c-8e40-9953bc4bde94"
    result = project([evidence()], binding=binding(binding_revision=public_revision),
                     observer=context(snapshot_revision=snapshot_revision))
    assert result.binding_revision == public_revision
    assert result.snapshot_revision == snapshot_revision
    assert result.evidence_level == "runtime_verified"
    with pytest.raises(ValidationError):
        binding(binding_revision="private-session")


def test_unconfigured_topic_snapshot_is_unknown_for_all_peers():
    snapshot = project_topic_snapshot("hub-build")
    assert {item["agent"] for item in snapshot["items"]} == {"codex", "claude", "hermes"}
    assert {item["status"] for item in snapshot["items"]} == {"unknown_stale"}
    assert snapshot["advisory"] is True
    assert snapshot["delivery_guaranteed"] is False


def test_topic_snapshot_projects_only_exact_binding_and_rejects_conflicts():
    snapshot = project_topic_snapshot(
        "hub-build", bindings=(binding(),), observer=context(),
        observations=(evidence(),), binding_snapshot_complete=True,
    )
    statuses = {item["agent"]: item["status"] for item in snapshot["items"]}
    assert statuses == {"hermes": "reachable", "codex": "unlinked", "claude": "unlinked"}
    duplicate = project_topic_snapshot("hub-build", bindings=(binding(), binding()),
                                       observer=context(), observations=(evidence(),))
    assert next(item for item in duplicate["items"] if item["agent"] == "hermes")["status"] == "unknown_stale"


@pytest.mark.parametrize("event,acceptance,status", [
    ("ready", "idle", "reachable"), ("busy_queued", "queue", "busy_queued"),
])
def test_verified_readiness_exposes_exact_evidence_expiry_deadline(event, acceptance, status):
    result = project([evidence(event=event, runtime_acceptance=acceptance)],
                     observer=context(max_age_seconds=30))
    assert result.status == status
    assert result.valid_until == NOW + timedelta(seconds=29)
    # The same cached observation does not renew its own deadline on a read.
    later = project([evidence(event=event, runtime_acceptance=acceptance)],
                    observer=context(now=NOW + timedelta(seconds=10), max_age_seconds=30))
    assert later.valid_until == result.valid_until


@pytest.mark.parametrize("changes", [
    {"event": "offline"}, {"event": "disconnected"},
    {"event": "stream_connected", "provenance": "bus_stream"},
    {"native_attachment": "unverified"},
    {"observed_at": NOW - timedelta(seconds=121)},
    {"observer_epoch": "old-epoch"}, {"session_generation": 1},
])
def test_unknown_unverified_and_offline_status_have_no_guessed_validity(changes):
    assert project([evidence(**changes)]).valid_until is None
    assert project().valid_until is None
    assert project(binding=None).valid_until is None
    assert project(binding=None, binding_snapshot_complete=True).valid_until is None


def test_clock_advance_expires_readiness_without_any_new_observation():
    event = evidence()
    assert project([event]).status == "reachable"
    expired = project([event], observer=context(now=NOW + timedelta(seconds=120)))
    assert expired.status == "unknown_stale"
    assert expired.valid_until is None
