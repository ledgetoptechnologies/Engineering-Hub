"""Canonical *nonsecret* effect identity; not an authorization or fence.

The opt-in bound-profile backend uses this module. Inputs must come from
independently verified authority before use; this function validates shape only. Environment
values, secret material, arbitrary Docker JSON, paths, and opaque extension
fields are intentionally excluded.
The projection is not a complete Docker request or probe digest and cannot
establish request equivalence, authentication, adoption or non-creation.
Syntax checks do not prove data is nonsecret: arbitrary identifiers or even
environment names can contain sensitive text. Use only independently trusted,
pre-enrolled opaque identifiers and approved environment names; never pass
caller-controlled secrets as identifiers. No such enrollment is implemented here.
"""
from dataclasses import dataclass
import json
import re
from uuid import UUID

_KINDS = frozenset({"create_candidate", "create_rollback", "start", "stop", "probe"})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTAINER = re.compile(r"[a-f0-9]{64}\Z")
_GENERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
_RESOURCE = re.compile(r"hub-pilot-[a-z0-9][a-z0-9-]{0,39}-[0-9a-f-]{12}-(candidate|rollback)\Z")


class EffectBindingCanonicalError(ValueError):
    """Sanitized invalid effect binding."""


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


@dataclass(frozen=True)
class NonsecretEffectBinding:
    operation_id: str
    run_id: str
    kind: str
    fence: int
    boot_id: str
    daemon_pid: int
    daemon_start_ticks: int
    owner_epoch: str
    resource_name: str | None
    container_id: str | None
    image_index_digest: str
    platform_manifest_digest: str
    image_config_id: str
    state_generation_id: str
    environment_names: tuple[str, ...]
    memory_bytes: int
    cpu_millicores: int
    pids_limit: int
    tmpfs_bytes: int
    probe_set_digest: str | None


def canonical_nonsecret_binding(binding: NonsecretEffectBinding) -> bytes:
    """Return stable bytes for the explicit allowlist; never read env values."""
    if type(binding) is not NonsecretEffectBinding:
        raise EffectBindingCanonicalError("Exact nonsecret effect binding required")
    if (not all(_uuid(x) for x in (binding.operation_id, binding.run_id, binding.boot_id, binding.owner_epoch))
            or type(binding.kind) is not str or binding.kind not in _KINDS
            or type(binding.fence) is not int or not 1 <= binding.fence <= 2**63-1
            or type(binding.daemon_pid) is not int or not 1 < binding.daemon_pid <= 2**63-1
            or type(binding.daemon_start_ticks) is not int or not 1 <= binding.daemon_start_ticks <= 2**63-1
            or any(type(d) is not str or not _DIGEST.fullmatch(d) for d in
                   (binding.image_index_digest, binding.platform_manifest_digest, binding.image_config_id))
            or type(binding.state_generation_id) is not str or not _GENERATION.fullmatch(binding.state_generation_id)
            or type(binding.memory_bytes) is not int or not 128 * 1024**2 <= binding.memory_bytes <= 2 * 1024**3
            or type(binding.cpu_millicores) is not int or not 100 <= binding.cpu_millicores <= 2000
            or type(binding.pids_limit) is not int or not 16 <= binding.pids_limit <= 256
            or type(binding.tmpfs_bytes) is not int or not 1024**2 <= binding.tmpfs_bytes <= 256 * 1024**2
            or type(binding.environment_names) is not tuple or len(binding.environment_names) > 256
            or any(type(n) is not str or not _ENV_NAME.fullmatch(n) for n in binding.environment_names)
            or len(set(binding.environment_names)) != len(binding.environment_names)):
        raise EffectBindingCanonicalError("Invalid nonsecret effect binding")
    if ((binding.kind == "probe" and (type(binding.probe_set_digest) is not str
                                      or not _DIGEST.fullmatch(binding.probe_set_digest)))
            or (binding.kind != "probe" and binding.probe_set_digest is not None)):
        raise EffectBindingCanonicalError("Invalid probe set identity")
    if binding.kind.startswith("create_"):
        if (type(binding.resource_name) is not str or not _RESOURCE.fullmatch(binding.resource_name)
                or not binding.resource_name.endswith("-" + binding.kind.removeprefix("create_"))
                or binding.container_id is not None):
            raise EffectBindingCanonicalError("Invalid create target")
    elif (binding.resource_name is not None or type(binding.container_id) is not str
          or not _CONTAINER.fullmatch(binding.container_id)):
        raise EffectBindingCanonicalError("Invalid existing target")
    data = {
        "schema": "hub-effect-binding-v1", "operation_id": binding.operation_id,
        "run_id": binding.run_id, "kind": binding.kind, "fence": binding.fence,
        "daemon": {"boot_id": binding.boot_id, "pid": binding.daemon_pid,
                   "start_ticks": binding.daemon_start_ticks},
        "owner_epoch": binding.owner_epoch, "resource_name": binding.resource_name,
        "container_id": binding.container_id, "image_index_digest": binding.image_index_digest,
        "platform_manifest_digest": binding.platform_manifest_digest,
        "image_config_id": binding.image_config_id, "state_generation_id": binding.state_generation_id,
        "environment_names": sorted(binding.environment_names),
        "probe_set_digest": binding.probe_set_digest,
        "runtime": {"uid": 10001, "gid": 10001, "read_only_root": True,
                    "cap_drop": ["ALL"], "no_new_privileges": True,
                    "network_mode": "none", "pid_mode": "private", "ipc_mode": "private",
                    "cgroupns_mode": "private", "memory_bytes": binding.memory_bytes,
                    "memory_swap_bytes": binding.memory_bytes, "cpu_millicores": binding.cpu_millicores,
                    "pids_limit": binding.pids_limit, "tmpfs_bytes": binding.tmpfs_bytes,
                    "tmpfs_mode": "1777", "tmpfs_noexec": True, "tmpfs_nosuid": True,
                    "state_destination": "/var/lib/hub", "state_mode": "rw", "restart": "no"},
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
