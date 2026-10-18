"""Opt-in source adapter with durable admission and an intentionally abstract backend.

This module never selects a daemon, resolves credentials by default, or opens a
listener. A real backend, provenance verifier, baseline, state provisioner and
versioned test-secret resolver remain explicit operator-owned dependencies.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

from engineering_hub.container_pilot_client import (
    ContainerPilotEvidence,
    ContainerPilotRequest,
    ContainerPilotResponse,
)
from engineering_hub.container_pilot_manifest import PilotManifest, Probe

from .backend import (
    ActualImageSelection,
    BaselineIdentity,
    ContainerObservation,
    ContainerPlan,
    EffectiveProfile,
    ImageDefaults,
    OperationObservation,
    PilotBackend,
    ProbeObservation,
    RunnerObservation,
    StateGeneration,
    TrustedDriverIdentityVerifier,
)
from .build_record import (
    BuildRecordError,
    ContentStore,
    TrustedBuildVerifier,
    VerifiedImage,
    verify_image_record,
)
from .ledger import Admission, InheritedLedgerUse, LedgerClosed, SQLiteLedger

_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class KnownTerminalFailure(RuntimeError):
    """Backend says this exact operation reached a terminal, non-late failure."""


class _TerminalOperationFailed(Exception):
    pass


class _Held(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class HostAdapter:
    """Explicitly instantiated adapter; construction does not create a listener."""

    def __init__(self, database_path, manifest: PilotManifest, *,
                 record_store: ContentStore | None = None,
                 metadata_store: ContentStore | None = None,
                 provenance_verifier: TrustedBuildVerifier | None = None,
                 backend: PilotBackend | None = None,
                 baseline_provider=None, state_provisioner=None, secret_resolver=None,
                 driver_identity_verifier: TrustedDriverIdentityVerifier | None = None):
        if type(manifest) is not PilotManifest:
            raise TypeError("shared PilotManifest required")
        self.manifest = PilotManifest.from_bytes(manifest.canonical_bytes())
        self.manifest_bytes = self.manifest.canonical_bytes()
        self.manifest_digest = self.manifest.digest()
        self.record_store = record_store
        self.metadata_store = metadata_store
        self.provenance_verifier = provenance_verifier
        self.backend = backend
        self.baseline_provider = baseline_provider
        self.state_provisioner = state_provisioner
        self.secret_resolver = secret_resolver
        self.driver_identity_verifier = driver_identity_verifier
        self._owner_pid = os.getpid()
        self._lifecycle = threading.Condition()
        self._active_operations = 0
        self._active_threads = {}
        self._closing = False
        self._finalizing_close = False
        self._closed = False
        self.ledger = SQLiteLedger(
            database_path,
            manifest_digest=self.manifest_digest,
            candidate_commit=self.manifest.candidate.source_commit,
            policy_revision=self.manifest.policy_revision,
        )

    @contextmanager
    def _active_operation(self):
        if os.getpid() != self._owner_pid:
            raise InheritedLedgerUse("adapter authority is bound to its creating process")
        thread_id = threading.get_ident()
        with self._lifecycle:
            if self._closing or self._closed:
                raise LedgerClosed("adapter is closing or closed")
            self._active_operations += 1
            self._active_threads[thread_id] = self._active_threads.get(thread_id, 0) + 1
        try:
            yield
        finally:
            finalize = False
            with self._lifecycle:
                self._active_operations -= 1
                remaining = self._active_threads[thread_id] - 1
                if remaining:
                    self._active_threads[thread_id] = remaining
                else:
                    del self._active_threads[thread_id]
                if (self._closing and self._active_operations == 0
                        and not self._finalizing_close):
                    self._finalizing_close = True
                    finalize = True
                self._lifecycle.notify_all()
            if finalize:
                self._finish_close()

    def _finish_close(self):
        try:
            self.ledger.close()
        finally:
            with self._lifecycle:
                self._closed = True
                self._finalizing_close = False
                self._lifecycle.notify_all()

    def close(self):
        if os.getpid() != self._owner_pid:
            self._closed = True
            self.ledger.close()
            return
        thread_id = threading.get_ident()
        with self._lifecycle:
            if self._closed:
                return
            self._closing = True
            # A callback may request shutdown reentrantly. Defer descriptor close
            # until its outer submit/reconcile lease has finished all ledger writes.
            if self._active_threads.get(thread_id, 0):
                return
            while self._active_operations or self._finalizing_close:
                self._lifecycle.wait()
                if self._closed:
                    return
            self._finalizing_close = True
        self._finish_close()

    def set_paused(self, paused: bool):
        """Local operator control; deliberately absent from the wire namespace."""
        with self._active_operation():
            self.ledger.set_paused(paused)

    def get(self, command_id: str) -> ContainerPilotResponse | None:
        with self._active_operation():
            return self.ledger.get(command_id)

    def submit(self, request: ContainerPilotRequest) -> ContainerPilotResponse:
        with self._active_operation():
            return self._submit_active(request)

    def _submit_active(self, request: ContainerPilotRequest) -> ContainerPilotResponse:
        admission = self.ledger.admit(request)
        if admission.replayed or not admission.accepted:
            return admission.response
        deadline = time.monotonic() + self.manifest.timeouts.total_command_ms / 1000
        try:
            return self._execute(request, deadline)
        except _Held as held:
            return self._finish(request, "held", held.reason)
        except _TerminalOperationFailed:
            return self._finish(request, "failed", "effect_failed")
        except (BuildRecordError, ValueError, TypeError):
            # Never reflect parser details, paths, builder metadata or secrets.
            return self._finish(request, "held", "image_unavailable")
        except Exception:
            # A timeout/transport exception may race a late engine operation.
            return self._finish(request, "held", "inspect_required")

    def _execute(self, request: ContainerPilotRequest, deadline: float) -> ContainerPilotResponse:
        if self.backend is None:
            raise _Held("daemon_unavailable")
        if any(value is None for value in (self.record_store, self.metadata_store,
                                            self.provenance_verifier, self.secret_resolver)):
            raise _Held("recovery_required")
        if request.action in {"deploy_container", "rollback_container"} and (
                self.baseline_provider is None or self.state_provisioner is None):
            raise _Held("recovery_required")

        role = "rollback" if request.action in {"rollback_container", "smoke_container_rollback"} else "candidate"
        image = self._verified_image(role)
        self._check_runner(deadline)
        defaults = self._image_defaults(image, deadline)
        secrets = self._resolve_secrets()

        if request.action in {"deploy_container", "rollback_container"}:
            baseline = self._get_baseline()
            rollback_image = image if role == "rollback" else self._verified_image("rollback")
            rollback_defaults = defaults if role == "rollback" else self._image_defaults(rollback_image, deadline)
            self._verify_baseline_container(baseline, rollback_image, rollback_defaults, secrets, deadline)
            method = (self.state_provisioner.prepare_candidate if role == "candidate"
                      else self.state_provisioner.prepare_rollback)
            state = self._effect(
                request, "state_copy" if role == "candidate" else "rollback_state",
                lambda operation_id, timeout: method(
                    self.manifest, request.run_id, baseline, operation_id,
                    deadline_monotonic=deadline,
                ),
                self.manifest.timeouts.total_command_ms, deadline,
                self._state_receipt,
            )
            self._validate_generation(state, baseline)
            profile = self._profile(image, defaults, state.generation_id, secrets)
            container_id = self._start_new_container(request, role, image, profile, state,
                                                     deadline)
            evidence = self._evidence(container_id, image.image_config_id, "unknown", True)
            self._timeout(deadline, self.manifest.timeouts.total_command_ms)
            return self.ledger.finish(request.command_id, state="succeeded", reason=None,
                                      evidence=evidence, generation_id=state.generation_id)

        target = self.ledger.target_snapshot(request.target_id)
        if not target or not target.get("container_id") or not target.get("generation_id"):
            raise _Held("recovery_required")
        container_id = target["container_id"]
        generation_id = target["generation_id"]
        profile = self._profile(image, defaults, generation_id, secrets)
        observation = self._inspect_container(container_id, image, profile, generation_id,
                                              {"running"}, deadline)
        probes = self.manifest.probes.candidate if role == "candidate" else self.manifest.probes.rollback
        expected_probe_digest = self._probe_digest(probes)
        outcome = self._effect(
            request, "probe",
            lambda operation_id, timeout: self.backend.run_compiled_probes(
                container_id, probes, operation_id, self._probe_timeouts(probes, deadline)
            ),
            self.manifest.timeouts.total_command_ms, deadline,
            self._probe_receipt,
        )
        if (type(outcome) is not ProbeObservation or outcome.container_id != container_id
                or outcome.image_config_id != image.image_config_id
                or outcome.profile_digest != expected_probe_digest):
            raise _Held("evidence_mismatch")
        if outcome.health == "unhealthy" and outcome.evidence_verified is True:
            evidence = self._evidence(container_id, image.image_config_id, "unhealthy", True)
            return self.ledger.finish(request.command_id, state="failed", reason="health_failed",
                                      evidence=evidence)
        if outcome.health != "healthy" or type(outcome.evidence_verified) is not bool or not outcome.evidence_verified:
            raise _Held("inspect_required")
        evidence = self._evidence(container_id, image.image_config_id, "healthy", True)
        self._timeout(deadline, self.manifest.timeouts.total_command_ms)
        return self.ledger.finish(request.command_id, state="succeeded", reason=None, evidence=evidence)

    def _verified_image(self, role: str) -> VerifiedImage:
        return verify_image_record(self.manifest, role, self.record_store, self.metadata_store,
                                   self.provenance_verifier)

    def _timeout(self, deadline: float, phase_limit: int) -> int:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise _Held("inspect_required")
        return max(1, min(phase_limit, remaining))

    def _probe_timeouts(self, probes: tuple[Probe, ...], deadline: float) -> tuple[int, ...]:
        remaining = self._timeout(deadline, self.manifest.timeouts.total_command_ms)
        budgets = []
        for index, probe in enumerate(probes):
            reserve_for_later = len(probes) - index - 1
            allocation = min(probe.timeout_ms, remaining - reserve_for_later)
            if allocation <= 0:
                raise _Held("inspect_required")
            budgets.append(allocation)
            remaining -= allocation
        return tuple(budgets)

    def _check_driver_identity(self):
        verifier = self.driver_identity_verifier
        if verifier is None:
            raise _Held("daemon_unavailable")
        try:
            observed_commit = verifier.implementation_commit()
            if inspect.iscoroutine(observed_commit):
                observed_commit.close()
        except Exception:
            raise _Held("daemon_unavailable") from None
        if (type(observed_commit) is not str
                or not re.fullmatch(r"[0-9a-f]{40}", observed_commit)
                or observed_commit != self.manifest.driver.implementation_commit):
            raise _Held("daemon_unavailable")

    def _check_runner(self, deadline):
        self._check_driver_identity()
        observed = self.backend.inspect_runner(self._timeout(deadline, self.manifest.timeouts.inspect_ms))
        expected = self.manifest.runner_binding
        if (type(observed) is not RunnerObservation or observed.runner_id != expected.runner_id
                or observed.daemon_identity != expected.daemon_identity
                or observed.platform != expected.platform):
            raise _Held("daemon_unavailable")

    def _image_defaults(self, image: VerifiedImage, deadline: float) -> ImageDefaults:
        observed = self.backend.inspect_image(
            image, self._timeout(deadline, self.manifest.timeouts.inspect_ms)
        )
        expected = self._defaults_from_record(image)
        if type(observed) is not ImageDefaults or observed != expected:
            raise _Held("evidence_mismatch")
        return observed

    @staticmethod
    def _defaults_from_record(image: VerifiedImage) -> ImageDefaults:
        root = image.image_config
        config = root.get("config") or {}
        entrypoint = config.get("Entrypoint") or ()
        command = config.get("Cmd") or ()
        environment = config.get("Env") or ()
        if (not isinstance(entrypoint, (list, tuple)) or any(not isinstance(x, str) for x in entrypoint)
                or not isinstance(command, (list, tuple)) or any(not isinstance(x, str) for x in command)
                or not isinstance(environment, (list, tuple)) or any(not isinstance(x, str) for x in environment)):
            raise _Held("image_unavailable")
        return ImageDefaults(image.image_config_id, image.platform, tuple(entrypoint),
                             tuple(command), tuple(environment))

    def _resolve_secrets(self) -> dict[str, str]:
        result = {}
        bindings = self.manifest.runtime.secret_bindings
        for name, slot in (("CODEX_TOKEN", bindings.codex), ("HERMES_TOKEN", bindings.hermes),
                           ("CLAUDE_TOKEN", bindings.claude)):
            try:
                value = self.secret_resolver.resolve_test_secret(slot.slot_id, slot.version)
            except Exception:
                raise _Held("recovery_required") from None
            if (not isinstance(value, str) or not value or len(value) > 4096
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise _Held("recovery_required")
            result[name] = value
        return result

    def _profile(self, image: VerifiedImage, defaults: ImageDefaults, generation_id: str,
                 secrets: dict[str, str]) -> EffectiveProfile:
        runtime = self.manifest.runtime
        environment = {}
        for item in defaults.environment:
            name, value = item.split("=", 1)
            environment[name] = value
        environment.update({"HUB_BASE_URL": runtime.environment.base_url,
                            "HUB_DB": runtime.environment.database})
        environment.update(secrets)
        tmp = runtime.tmpfs.tmp
        # Wire digits 1777 are octal permission bits, not decimal chmod input.
        tmp_mode = int(str(tmp.mode), 8)
        return EffectiveProfile(
            uid=runtime.user.uid, gid=runtime.user.gid,
            supplementary_groups=tuple(runtime.user.supplementary_groups),
            read_only_root=runtime.read_only_root, cap_drop=tuple(runtime.cap_drop),
            no_new_privileges=runtime.no_new_privileges, privileged=runtime.privileged,
            pid_mode=runtime.pid_mode, ipc_mode=runtime.ipc_mode,
            network_mode=runtime.network.mode, published_ports=(),
            memory_bytes=runtime.resources.memory_bytes,
            cpu_millicores=runtime.resources.cpu_millicores,
            pids_limit=runtime.resources.pids_limit,
            tmpfs=(("/tmp", tmp.size_bytes, tmp_mode, tmp.noexec, tmp.nosuid),),
            mounts=(("state_generation", runtime.state_mount.container_destination,
                     runtime.state_mount.mode, generation_id),
                    ("tmpfs", "/tmp", "rw", f"size={tmp.size_bytes}",
                     f"mode={tmp_mode:o}", "noexec", "nosuid")),
            state_destination=runtime.state_mount.container_destination,
            state_mode=runtime.state_mount.mode, state_generation_id=generation_id,
            environment=tuple(sorted(environment.items())),
            entrypoint=defaults.entrypoint, command=defaults.command,
            restart_policy=runtime.restart_policy,
        )

    def _get_baseline(self) -> BaselineIdentity:
        try:
            baseline = self.baseline_provider.enrolled_baseline(self.manifest)
            if inspect.iscoroutine(baseline):
                baseline.close()
                raise _Held("recovery_required")
            approved = baseline is not None and self.baseline_provider.verify_baseline(baseline, self.manifest)
            if inspect.iscoroutine(approved):
                approved.close()
        except Exception:
            raise _Held("recovery_required") from None
        state = self.manifest.state_policy
        if (type(approved) is not bool or not approved or type(baseline) is not BaselineIdentity
                or baseline.baseline_id != state.baseline_id
                or baseline.backup_digest != state.baseline_backup_digest
                or baseline.image_config_id != self.manifest.rollback.image_config_id
                or baseline.healthy_at_enrollment is not True
                or not _CONTAINER_ID.fullmatch(baseline.container_id)
                or not _OPAQUE.fullmatch(baseline.state_generation_id)
                or not _OPAQUE.fullmatch(baseline.proof_id)):
            raise _Held("recovery_required")
        return baseline

    def _verify_baseline_container(self, baseline: BaselineIdentity, image: VerifiedImage,
                                   defaults: ImageDefaults, secrets: dict[str, str], deadline: float):
        profile = self._profile(image, defaults, baseline.state_generation_id, secrets)
        self._inspect_container(baseline.container_id, image, profile,
                                baseline.state_generation_id, {"running"}, deadline)

    def _validate_generation(self, state, baseline: BaselineIdentity):
        if (type(state) is not StateGeneration
                or not _OPAQUE.fullmatch(state.generation_id)
                or state.generation_id == baseline.state_generation_id
                or state.previous_generation_id != baseline.state_generation_id
                or state.source_backup_digest != baseline.backup_digest
                or not _DIGEST.fullmatch(state.generation_digest)
                or type(state.size_bytes) is not int or state.size_bytes <= 0
                or state.size_bytes > self.manifest.retention.max_storage_bytes
                or state.baseline_unchanged is not True):
            raise _Held("evidence_mismatch")

    def _effect(self, request, kind, callback, phase_limit, deadline, receipt_builder):
        try:
            timeout = self._timeout(deadline, phase_limit)
            operation_id = self.ledger.begin_operation(request.command_id, kind)
        except PermissionError as error:
            if str(error) == "paused":
                raise _Held("paused") from None
            raise _Held("recovery_required") from None
        except RuntimeError:
            raise _Held("stale_fence") from None
        try:
            # Durable admission can wait on SQLite; recompute the budget only
            # after admission and never start an effect with an expired intent.
            timeout = self._timeout(deadline, phase_limit)
            result = callback(operation_id, timeout)
            # A deadline detects an overrun; it is not cancellation. Keep the
            # intent held so no later effect can run after an untrusted worker returns.
            self._timeout(deadline, phase_limit)
        except KnownTerminalFailure:
            try:
                self.ledger.record_terminal_failure(operation_id)
            except Exception:
                raise _Held("inspect_required") from None
            raise _TerminalOperationFailed from None
        except Exception:
            # Intent persists; no timeout or exception is permission to retry.
            raise _Held("inspect_required") from None
        receipt = receipt_builder(result)
        self.ledger.complete_operation(operation_id, receipt)
        return result

    @staticmethod
    def _state_receipt(state):
        if type(state) is not StateGeneration:
            raise _Held("evidence_mismatch")
        return {"generation_id": state.generation_id,
                "previous_generation_id": state.previous_generation_id,
                "source_backup_digest": state.source_backup_digest,
                "generation_digest": state.generation_digest,
                "size_bytes": state.size_bytes,
                "baseline_unchanged": state.baseline_unchanged}

    @staticmethod
    def _probe_receipt(outcome):
        if type(outcome) is not ProbeObservation:
            raise _Held("evidence_mismatch")
        return {"container_id": outcome.container_id,
                "image_config_id": outcome.image_config_id,
                "profile_digest": outcome.profile_digest,
                "health": outcome.health,
                "evidence_verified": outcome.evidence_verified}

    def _start_new_container(self, request: ContainerPilotRequest, role: str,
                             image: VerifiedImage, profile: EffectiveProfile,
                             state: StateGeneration, deadline: float) -> str:
        name = f"{self.manifest.runtime.resource_name_prefix}-{request.run_id[:12]}-{role}"
        plan = ContainerPlan(resource_name=name, image=image,
                             immutable_reference=image.runnable_reference, pull=False,
                             profile=profile)
        timeout_field = self.manifest.timeouts.create_ms
        container_id = self._effect(
            request, "create_" + role,
            lambda operation_id, timeout: self.backend.create_container(plan, operation_id, timeout),
            timeout_field, deadline,
            lambda value: {"container_id": value} if isinstance(value, str) else
            (_raise_held("evidence_mismatch")),
        )
        if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
            raise _Held("container_conflict")
        self._inspect_container(container_id, image, profile, state.generation_id,
                                {"created"}, deadline)
        self.ledger.record_target_instance(request.command_id, container_id,
                                           image.image_config_id, state.generation_id)
        self._effect(
            request, "start_" + role,
            lambda operation_id, timeout: self.backend.start_container(container_id, operation_id, timeout),
            self.manifest.timeouts.start_ms, deadline,
            lambda _value: {"container_id": container_id, "started": True},
        )
        self._inspect_container(container_id, image, profile, state.generation_id,
                                {"running"}, deadline)
        return container_id

    def _inspect_container(self, container_id: str, image: VerifiedImage,
                           profile: EffectiveProfile, generation_id: str,
                           allowed_status: set[str], deadline: float) -> ContainerObservation:
        try:
            actual = self.backend.inspect_container(
                container_id, self._timeout(deadline, self.manifest.timeouts.inspect_ms)
            )
        except Exception:
            raise _Held("daemon_unavailable") from None
        self._timeout(deadline, self.manifest.timeouts.inspect_ms)
        expected_image = ActualImageSelection(
            image.runnable_reference, image.platform_manifest_digest,
            image.image_config_id, image.platform,
        )
        if (type(actual) is not ContainerObservation or actual.container_id != container_id
                or not _CONTAINER_ID.fullmatch(actual.container_id)
                or actual.selected_image != expected_image
                or actual.profile != profile or actual.state_generation_id != generation_id
                or actual.status not in allowed_status):
            raise _Held("evidence_mismatch")
        return actual

    @staticmethod
    def _probe_digest(probes: tuple[Probe, ...]) -> str:
        raw = json.dumps([probe.model_dump(mode="json") for probe in probes],
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _evidence(container_id, image_config_id, health, verified):
        return ContainerPilotEvidence(container_id=container_id,
                                      image_config_id=image_config_id,
                                      health=health, evidence_verified=verified)

    def _finish(self, request, state, reason):
        return self.ledger.finish(
            request.command_id, state=state, reason=reason,
            evidence=self._evidence(None, None, "unknown", False),
        )

    def reconcile(self, command_id: str) -> ContainerPilotResponse | None:
        """Inspect exact durable operation IDs only; never re-effect or re-probe."""
        with self._active_operation():
            return self._reconcile_active(command_id)

    def _reconcile_active(self, command_id: str) -> ContainerPilotResponse | None:
        response = self.ledger.get(command_id)
        if response is None or response.state != "held":
            return response
        if self.backend is None:
            return response
        snapshot = self.ledger.reconcile_snapshot(command_id)
        if not snapshot:
            return response
        for operation in snapshot["operations"]:
            if operation["status"] == "terminal_failed":
                try:
                    observed = self.backend.inspect_operation(
                        operation["operation_id"], self.manifest.timeouts.inspect_ms
                    )
                except Exception:
                    return response
                if (type(observed) is OperationObservation
                        and observed.operation_id == operation["operation_id"]
                        and observed.state == "terminal_failed" and observed.terminal is True):
                    try:
                        return self.ledger.resolve_terminal_failure(command_id)
                    except Exception:
                        return response
                return response
            if operation["status"] == "completed":
                receipt = operation.get("receipt") or {}
                container_id = receipt.get("container_id") if isinstance(receipt, dict) else None
                if isinstance(container_id, str) and _CONTAINER_ID.fullmatch(container_id):
                    try:
                        observed_container = self.backend.inspect_container(
                            container_id, self.manifest.timeouts.inspect_ms
                        )
                    except Exception:
                        return response
                    if (observed_container is not None
                            and (type(observed_container) is not ContainerObservation
                                 or observed_container.container_id != container_id)):
                        return response
                continue
            if operation["status"] != "intent":
                continue
            try:
                observed = self.backend.inspect_operation(
                    operation["operation_id"], self.manifest.timeouts.inspect_ms
                )
            except Exception:
                return response
            if (type(observed) is OperationObservation
                    and observed.operation_id == operation["operation_id"]
                    and observed.state == "terminal_failed" and observed.terminal is True):
                try:
                    self.ledger.record_terminal_failure(operation["operation_id"])
                    return self.ledger.resolve_terminal_failure(command_id)
                except Exception:
                    return response
            if type(observed) is not OperationObservation or observed.operation_id != operation["operation_id"]:
                return response
            if observed.container_id is not None and _CONTAINER_ID.fullmatch(observed.container_id):
                try:
                    actual = self.backend.inspect_container(
                        observed.container_id, self.manifest.timeouts.inspect_ms
                    )
                except Exception:
                    return response
                if actual is not None and (type(actual) is not ContainerObservation
                                           or actual.container_id != observed.container_id):
                    return response
        return response


def _raise_held(reason):
    raise _Held(reason)
