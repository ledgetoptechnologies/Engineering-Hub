"""Narrow typed interfaces for a future operator-pinned daemon implementation.

No concrete Docker client is shipped here. Unit tests inject explicitly named
fakes; production effects require independent implementation/review/acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from engineering_hub.container_pilot_manifest import Platform, PilotManifest, Probe

from .build_record import VerifiedImage


@dataclass(frozen=True)
class RunnerObservation:
    runner_id: str
    daemon_identity: str
    platform: Platform


@dataclass(frozen=True)
class ImageDefaults:
    image_config_id: str
    platform: Platform
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    environment: tuple[str, ...]


@dataclass(frozen=True)
class EffectiveProfile:
    uid: int
    gid: int
    supplementary_groups: tuple[int, ...]
    read_only_root: bool
    cap_drop: tuple[str, ...]
    no_new_privileges: bool
    privileged: bool
    pid_mode: str
    ipc_mode: str
    network_mode: str
    published_ports: tuple[()]
    memory_bytes: int
    cpu_millicores: int
    pids_limit: int
    tmpfs: tuple[tuple[str, int, int, bool, bool], ...]
    mounts: tuple[tuple[str, ...], ...]
    state_destination: str
    state_mode: str
    state_generation_id: str
    environment: tuple[tuple[str, str], ...]
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    restart_policy: str


@dataclass(frozen=True)
class ContainerPlan:
    resource_name: str
    image: VerifiedImage
    immutable_reference: str
    pull: Literal[False]
    profile: EffectiveProfile


@dataclass(frozen=True)
class ActualImageSelection:
    runnable_reference: str
    platform_manifest_digest: str
    image_config_id: str
    platform: Platform


@dataclass(frozen=True)
class ContainerObservation:
    container_id: str
    selected_image: ActualImageSelection
    profile: EffectiveProfile
    state_generation_id: str
    status: Literal["created", "running", "exited", "unknown"]


@dataclass(frozen=True)
class ProbeObservation:
    container_id: str
    image_config_id: str
    profile_digest: str
    health: Literal["healthy", "unhealthy", "unknown"]
    evidence_verified: bool


@dataclass(frozen=True)
class OperationObservation:
    operation_id: str
    state: Literal["pending", "unknown", "completed", "terminal_failed"]
    terminal: bool
    container_id: str | None = None


@dataclass(frozen=True)
class BaselineIdentity:
    baseline_id: str
    backup_digest: str
    container_id: str
    image_config_id: str
    state_generation_id: str
    healthy_at_enrollment: bool
    proof_id: str


@dataclass(frozen=True)
class StateGeneration:
    generation_id: str
    previous_generation_id: str
    source_backup_digest: str
    generation_digest: str
    size_bytes: int
    baseline_unchanged: bool


@dataclass(frozen=True)
class ContainerPlanInput:
    manifest: PilotManifest
    request_run_id: str
    role: Literal["candidate", "rollback"]
    image: VerifiedImage
    profile: EffectiveProfile


class PilotBackend(Protocol):
    """An implementation must use an operator-pinned local daemon binding only."""

    def inspect_runner(self, timeout_ms: int) -> RunnerObservation: ...
    def inspect_image(self, image: VerifiedImage, timeout_ms: int) -> ImageDefaults: ...
    def inspect_container(self, container_id: str, timeout_ms: int) -> ContainerObservation | None: ...
    def create_container(self, plan: ContainerPlan, operation_id: str,
                         timeout_ms: int) -> str: ...
    def start_container(self, container_id: str, operation_id: str, timeout_ms: int) -> None: ...
    def stop_container(self, container_id: str, operation_id: str, timeout_ms: int) -> None: ...
    def run_compiled_probes(self, container_id: str, probes: tuple[Probe, ...],
                            operation_id: str, timeout_ms: tuple[int, ...]) -> ProbeObservation: ...
    def inspect_operation(self, operation_id: str, timeout_ms: int) -> OperationObservation: ...


class BaselineProvider(Protocol):
    def enrolled_baseline(self, manifest: PilotManifest) -> BaselineIdentity | None: ...
    def verify_baseline(self, baseline: BaselineIdentity, manifest: PilotManifest) -> bool: ...


class StateProvisioner(Protocol):
    def prepare_candidate(self, manifest: PilotManifest, run_id: str, baseline: BaselineIdentity,
                          operation_id: str) -> StateGeneration: ...
    def prepare_rollback(self, manifest: PilotManifest, run_id: str, baseline: BaselineIdentity,
                         operation_id: str) -> StateGeneration: ...


class TestSecretResolver(Protocol):
    def resolve_test_secret(self, slot_id: str, version: int) -> str: ...
