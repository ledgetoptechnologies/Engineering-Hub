"""Typed, inert workload-intent proposal for Viewer processing."""

from dataclasses import dataclass, field
import re
import uuid

from engineering_hub.work_queue import WorkTarget


_IDENTIFIER_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}")
_DIGEST_PATTERN = re.compile(r"sha256:[a-f0-9]{64}")
_PROVIDER_KINDS = frozenset({"nodeodx", "clusterodm"})


@dataclass(frozen=True)
class ViewerProcessingIntent:
    """Immutable metadata proposal that references an existing code target."""

    code_target: WorkTarget
    submission_id: str
    viewer_project_id: str
    dataset_id: str
    provider_id: str
    input_manifest_digest: str
    options_digest: str
    project: str = "3d-viewer"

    def __post_init__(self):
        if type(self.code_target) is not WorkTarget:
            raise ValueError("invalid Viewer processing intent")
        if type(self.project) is not str or self.project != "3d-viewer":
            raise ValueError("invalid Viewer processing intent")
        if type(self.submission_id) is not str:
            raise ValueError("invalid Viewer processing intent")
        try:
            canonical_submission = str(uuid.UUID(self.submission_id)) == self.submission_id
        except ValueError:
            canonical_submission = False
        if not canonical_submission:
            raise ValueError("invalid Viewer processing intent")
        if any(
            type(value) is not str or _IDENTIFIER_PATTERN.fullmatch(value) is None
            for value in (self.viewer_project_id, self.dataset_id, self.provider_id)
        ):
            raise ValueError("invalid Viewer processing intent")
        if any(
            type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None
            for value in (self.input_manifest_digest, self.options_digest)
        ):
            raise ValueError("invalid Viewer processing intent")


@dataclass(frozen=True)
class ProviderQueueObservation:
    """Allowlisted queue observation; intentionally never an admission proof."""

    kind: str
    queue_count: int | None
    state: str
    admission_allowed: bool = field(default=False, init=False)

    def __post_init__(self):
        if type(self.kind) is not str or self.kind not in _PROVIDER_KINDS:
            raise ValueError("invalid provider observation")
        if self.queue_count is not None and (
            type(self.queue_count) is not int or self.queue_count < 0
        ):
            raise ValueError("invalid provider observation")
        if self.queue_count is not None and self.queue_count > 0:
            expected_state = "queued_work_observed"
        elif self.kind == "nodeodx" and self.queue_count == 0:
            expected_state = "no_queued_work_observed"
        else:
            expected_state = "unknown"
        if type(self.state) is not str or self.state != expected_state:
            raise ValueError("invalid provider observation")


def observe_provider_info(info, *, provider_kind):
    """Read taskQueueCount only and report bounded, non-authoritative metadata."""
    if type(provider_kind) is not str or provider_kind not in _PROVIDER_KINDS:
        raise ValueError("unsupported provider kind")
    if type(info) is not dict:
        raise TypeError("provider info must be a dict")
    count = info.get("taskQueueCount")
    if type(count) is not int or count < 0:
        count = None
    if count is not None and count > 0:
        state = "queued_work_observed"
    elif provider_kind == "nodeodx" and count == 0:
        state = "no_queued_work_observed"
    else:
        state = "unknown"
    return ProviderQueueObservation(provider_kind, count, state)
