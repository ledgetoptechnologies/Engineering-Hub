"""Explicit local resource composition for the owned host-executor service.

This privileged Python API consumes independently approved enrollment; it never
discovers it, issues it, calls Docker, or activates a service. Entering the
context can create private ledger/journal/reservation state. It is neither a
dry run nor an atomic multi-file transaction. Failed partial state is retained
for inspection, never deleted, adopted, retried, chmodded or repaired here.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import sys

from engineering_hub.container_pilot_manifest import PilotManifest, Platform

from .adapter import HostAdapter
from .baseline_receipt import ProtectedBaselineProvider
from .build_enrollment import OperatorEnrolledBuildVerifier
from .build_record import verify_image_record
from .daemon_backend import DockerEngineBackend, DockerEngineBinding
from .local_providers import ImmutableTestSecretResolver, PrivateContentStore
from .operation_journal import SQLiteOperationJournal
from .state_provider import FrozenSQLiteStateProvisioner


class LocalResourcesError(RuntimeError):
    """Safe composition failure without paths, protected bytes or credentials."""


def _path(value, *, directory=False, existing=False):
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts
            or "\x00" in str(path) or path == Path(path.anchor)):
        raise LocalResourcesError("Explicit canonical private paths required")
    if directory or existing:
        if path.resolve(strict=True) != path:
            raise LocalResourcesError("Explicit canonical private paths required")
        if directory and not path.is_dir():
            raise LocalResourcesError("Pre-provisioned private directories required")
    else:
        if path.parent.resolve(strict=True) != path.parent or not path.parent.is_dir():
            raise LocalResourcesError("Pre-provisioned canonical parent required")
        # Reject existing symbolic links, including dangling ones. Individual
        # providers subsequently enforce ownership, mode, inode and link count.
        if path.is_symlink() or (path.exists() and path.resolve(strict=True) != path):
            raise LocalResourcesError("Explicit canonical private paths required")
    return path


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _paths(*, binding, adapter_database, journal_database, record_root,
           metadata_root, baseline_receipt_root, secret_root, frozen_backup,
           reservation_root):
    adapter = _path(adapter_database)
    journal = _path(journal_database)
    roots = tuple(_path(value, directory=True) for value in (
        adapter.parent, journal.parent, record_root, metadata_root,
        baseline_receipt_root, secret_root, binding.generation_root,
        reservation_root,
    ))
    # Dedicated nonnested database parents keep every current and future
    # database sidecar outside app mounts and immutable protected stores.
    for index, root in enumerate(roots):
        if any(_overlap(root, other) for other in roots[index + 1:]):
            raise LocalResourcesError("Separate nonnested private roots required")
    identities = []
    for root in roots:
        info = root.stat()
        identities.append((info.st_dev, info.st_ino))
    if len(set(identities)) != len(roots):
        raise LocalResourcesError("Separate private directory identities required")
    backup = _path(frozen_backup, existing=True)
    socket = _path(binding.unix_socket_path)
    if backup.parent != roots[6] or any(_overlap(socket, root) for root in roots):
        raise LocalResourcesError("Separate daemon socket and explicit frozen backup required")
    # Spell out derived namespaces as well as checking their dedicated parents;
    # SQLite and both owner mechanisms persist these names across restarts.
    adapter_names = {adapter.with_name(adapter.name + suffix) for suffix in (
        "", ".owner.lock", ".evidence.key", "-wal", "-shm", "-journal")}
    journal_names = {journal.with_name(journal.name + suffix) for suffix in (
        "", ".pilot-owner.lock", "-wal", "-shm", "-journal")}
    if adapter_names & journal_names or socket in adapter_names | journal_names:
        raise LocalResourcesError("Separate private database namespaces required")
    return adapter, journal, roots, backup


@contextmanager
def local_host_adapter_resources(*, manifest, binding, adapter_database,
        journal_database, journal_create, record_root, metadata_root,
        baseline_receipt_root, secret_root, secret_bindings, build_enrollments,
        baseline_receipt_digest, baseline_runtime_profile_digest, frozen_backup,
        reservation_root, state_reservations_create, driver_identity_verifier):
    """Yield one freshly constructed exact HostAdapter with concrete providers.

    All directories must already exist, be canonical and pairwise nonnested;
    ledger and journal each require their own dedicated parent. Providers enforce
    private ownership/modes and local durable storage. No ephemeral-test bypass
    is exposed. Known directory-inode aliases are rejected at preflight; these
    snapshots do not exclude hostile filesystem replacement or mount races.
    UID/GID 10001 applies in the current user namespace, not proof of
    a safe rootless/host mapping. The frozen backup is a direct generation-root
    child. The binding supplies the one generation root and explicit Unix socket.

    Build enrollments and baseline receipt/profile pins are independent trusted
    inputs, not derived approval from content bytes. The mandatory caller-owned
    driver verifier is only interface-checked, not sampled, enrolled or closed.
    Secret bytes are read for each exact manifest test slot/version, never logged
    or retained by this helper; this cannot prove the bytes are not real secrets.

    journal_create/state_reservations_create are explicit booleans, not inferred
    from existence. HostAdapter retains its existing guarded ledger create/reopen
    semantics; this helper adds no atomic-create promise or automatic recovery.
    A later failure can leave state-reservation lock/header files or a private
    journal behind; closing resources does not remove or adopt those files.
    All constructed closers are attempted in reverse order, including after a
    failure. Close is synchronous and may block; serve_owned_host_executor drains
    accepted effects before closing the adapter and exiting this context off-loop.
    Direct users must likewise stop admission/drain work before context exit.
    """
    try:
        if (sys.platform != "linux" or os.getuid() != 10001 or os.getgid() != 10001
                or type(manifest) is not PilotManifest
                or type(binding) is not DockerEngineBinding
                or type(journal_create) is not bool
                or type(state_reservations_create) is not bool
                or not callable(getattr(driver_identity_verifier, "implementation_commit", None))):
            raise LocalResourcesError("Explicit private Linux resource configuration required")
        manifest = PilotManifest.from_bytes(manifest.canonical_bytes())
        # Frozen dataclasses can contain mutable Pydantic platform objects.
        # Snapshot and validate both rather than retaining caller-owned models.
        binding = DockerEngineBinding(
            runner_id=binding.runner_id, daemon_identity=binding.daemon_identity,
            platform=Platform.model_validate(binding.platform.model_dump(mode="json")),
            unix_socket_path=binding.unix_socket_path, api_version=binding.api_version,
            generation_root=binding.generation_root,
        )
        runner = manifest.runner_binding
        if (binding.runner_id != runner.runner_id
                or binding.daemon_identity != runner.daemon_identity
                or binding.platform != runner.platform):
            raise LocalResourcesError("Exact enrolled runner binding required")
        adapter_path, journal_path, roots, backup = _paths(
            binding=binding, adapter_database=adapter_database,
            journal_database=journal_database, record_root=record_root,
            metadata_root=metadata_root, baseline_receipt_root=baseline_receipt_root,
            secret_root=secret_root, frozen_backup=frozen_backup,
            reservation_root=reservation_root,
        )
        verifier = OperatorEnrolledBuildVerifier(enrollments=build_enrollments)
        with ExitStack() as resources:
            records = PrivateContentStore(roots[2], maximum_bytes=65536)
            resources.callback(records.close)
            metadata = PrivateContentStore(roots[3], maximum_bytes=1048576)
            resources.callback(metadata.close)
            receipts = PrivateContentStore(roots[4], maximum_bytes=65536)
            resources.callback(receipts.close)
            secrets = ImmutableTestSecretResolver(roots[5], bindings=secret_bindings)
            resources.callback(secrets.close)
            for slot in (manifest.runtime.secret_bindings.codex,
                         manifest.runtime.secret_bindings.hermes,
                         manifest.runtime.secret_bindings.claude):
                secrets.resolve_test_secret(slot.slot_id, slot.version)
            verify_image_record(manifest, "candidate", records, metadata, verifier)
            rollback = verify_image_record(manifest, "rollback", records, metadata, verifier)
            baseline = ProtectedBaselineProvider(
                receipts, receipt_digest=baseline_receipt_digest,
                campaign_id=manifest.campaign_id, verified_rollback=rollback,
                expected_runtime_profile_digest=baseline_runtime_profile_digest,
            )
            resources.callback(baseline.close)
            identity = baseline.enrolled_baseline(manifest)
            inventory = baseline.baseline_inventory(manifest)
            state = FrozenSQLiteStateProvisioner(
                manifest, identity, baseline_provider=baseline,
                frozen_backup=backup, generation_root=roots[6],
                reservation_root=roots[7], baseline_generation_inventory=inventory,
                create=state_reservations_create,
            )
            resources.callback(state.close)
            journal = SQLiteOperationJournal(journal_path, create=journal_create)
            resources.callback(journal.close)
            backend = DockerEngineBackend(binding, operation_journal=journal)
            resources.callback(backend.close)
            adapter = HostAdapter(
                adapter_path, manifest, record_store=records, metadata_store=metadata,
                provenance_verifier=verifier, backend=backend,
                baseline_provider=baseline, state_provisioner=state,
                secret_resolver=secrets, driver_identity_verifier=driver_identity_verifier,
            )
            resources.callback(adapter.close)
            yield adapter
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException:
        raise LocalResourcesError("Local host-executor resources unavailable") from None
