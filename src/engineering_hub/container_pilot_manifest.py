"""Closed, effect-free operator manifest for the isolated Linux Hub pilot.

This validates identities and policy, not image provenance, secret resolution,
backup consistency or actual Docker configuration. Those require the adapter's
independent verification. ``image_config_id`` is specifically a Docker config
ID; a syntactically identical OCI manifest digest is not thereby that identity.
No profile is enabled, credentials resolved or network request made here.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Opaque = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Milliseconds = Annotated[int, Field(ge=100, le=120_000)]
MAX_BYTES = 64 * 1024
SOURCE_REPOSITORY = "https://github.com/ledgetoptechnologies/Engineering-Hub"


class _Closed(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def literal_booleans_are_not_numbers(cls, value):
        # Pydantic's Literal[True/False] accepts 1/0 even with strict=True.
        if isinstance(value, dict):
            for name, field in cls.model_fields.items():
                key = field.alias or name
                if key in value and field.annotation in (Literal[True], Literal[False]):
                    if type(value[key]) is not bool:
                        raise ValueError("security boolean requires JSON boolean")
        return value


class Driver(_Closed):
    id: Literal["container_pilot_v1"]
    implementation_commit: Commit


class Platform(_Closed):
    os: Literal["linux"]
    architecture: Literal["amd64", "arm64"]
    variant: Literal["v8"] | None

    @model_validator(mode="after")
    def supported_variant(self):
        if self.architecture == "amd64" and self.variant is not None:
            raise ValueError("amd64 requires null variant")
        return self


class RunnerBinding(_Closed):
    runner_id: Opaque
    daemon_identity: Opaque
    platform: Platform


class Provenance(_Closed):
    build_context_digest: SHA256
    build_recipe_digest: SHA256
    build_inputs_digest: SHA256
    attestation_digest: SHA256


class ImageSelection(_Closed):
    image_config_id: SHA256
    source_repository: Literal["https://github.com/ledgetoptechnologies/Engineering-Hub"]
    source_commit: Commit
    provenance: Provenance


def _json_array(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError("array required")
    return tuple(value)


class RuntimeUser(_Closed):
    uid: Annotated[int, Field(ge=10001, le=10001)]
    gid: Annotated[int, Field(ge=10001, le=10001)]
    supplementary_groups: tuple[()]  # No inherited host groups.

    _groups = field_validator("supplementary_groups", mode="before")(_json_array)


class Network(_Closed):
    mode: Literal["none"]
    policy_digest: SHA256
    loopback_address: None
    host_port: None
    container_port: Annotated[int, Field(ge=8770, le=8770)]


class Resources(_Closed):
    memory_bytes: Annotated[int, Field(ge=128 * 1024**2, le=2 * 1024**3)]
    cpu_millicores: Annotated[int, Field(ge=100, le=2000)]
    pids_limit: Annotated[int, Field(ge=16, le=256)]
    scratch_bytes: Annotated[int, Field(ge=1024**2, le=256 * 1024**2)]


class TmpfsEntry(_Closed):
    size_bytes: Annotated[int, Field(ge=1024**2, le=256 * 1024**2)]
    mode: Annotated[int, Field(ge=1777, le=1777)]  # Wire uses decimal digits 1777, interpreted as octal mode by driver.
    noexec: Literal[True]
    nosuid: Literal[True]


class Tmpfs(_Closed):
    tmp: TmpfsEntry = Field(alias="/tmp")


class StateMount(_Closed):
    container_destination: Literal["/var/lib/hub"]
    mode: Literal["rw"]


class Environment(_Closed):
    base_url: Literal["http://127.0.0.1:8770"] = Field(alias="HUB_BASE_URL")
    database: Literal["/var/lib/hub/hub.sqlite3"] = Field(alias="HUB_DB")


class SecretSlot(_Closed):
    slot_id: Annotated[str, Field(min_length=6, max_length=96, pattern=r"^test-[a-z0-9][a-z0-9-]*$")]
    version: Annotated[int, Field(ge=1, le=2**63 - 1)]
    test_only: Literal[True]


class SecretBindings(_Closed):
    codex: SecretSlot = Field(alias="CODEX_TOKEN")
    hermes: SecretSlot = Field(alias="HERMES_TOKEN")
    claude: SecretSlot = Field(alias="CLAUDE_TOKEN")

    @model_validator(mode="after")
    def distinct_slots(self):
        if len({self.codex.slot_id, self.hermes.slot_id, self.claude.slot_id}) != 3:
            raise ValueError("agent secret slots must be distinct")
        return self


class Runtime(_Closed):
    resource_name_prefix: Annotated[str, Field(min_length=3, max_length=40, pattern=r"^hub-pilot-[a-z0-9][a-z0-9-]*$")]
    user: RuntimeUser
    read_only_root: Literal[True]
    cap_drop: tuple[Literal["ALL"]]
    no_new_privileges: Literal[True]
    privileged: Literal[False]
    pid_mode: Literal["private"]
    ipc_mode: Literal["private"]
    network: Network
    resources: Resources
    tmpfs: Tmpfs
    state_mount: StateMount
    environment: Environment
    secret_bindings: SecretBindings
    entrypoint_policy: Literal["image_default"]
    command_policy: Literal["image_default"]
    restart_policy: Literal["no"]

    _caps = field_validator("cap_drop", mode="before")(_json_array)

    @model_validator(mode="after")
    def scratch_bound(self):
        if self.tmpfs.tmp.size_bytes != self.resources.scratch_bytes:
            raise ValueError("tmpfs and scratch budget must agree")
        if self.resources.scratch_bytes > self.resources.memory_bytes:
            raise ValueError("scratch budget exceeds memory")
        return self


class StatePolicy(_Closed):
    mode: Literal["generation_copy"]
    baseline_id: Opaque
    baseline_backup_digest: SHA256
    no_external_writes: Literal[True]
    allow_migrations: Literal[False]
    retain_previous_unchanged: Literal[True]
    rollback_mode: Literal["restore_previous_generation"]


# Each expectation is compiled by the driver, never supplied as regex or code.
# The assets expectation checks the HTML and its bounded same-app asset links.
PROBE_PROFILE = (
    ("health_json", "/health", 200, "hub_health_v1"),
    ("application_assets", "/", 200, "hub_app_assets_v1"),
    ("auth_denial", "/api/hub/v1/overview", 401, "hub_auth_denied_v1"),
)


class Probe(_Closed):
    kind: Literal["health_json", "application_assets", "auth_denial"]
    method: Literal["GET"]
    path: Literal["/health", "/", "/api/hub/v1/overview"]
    expected_status: Annotated[int, Field(ge=100, le=599)]
    expectation_id: Literal["hub_health_v1", "hub_app_assets_v1", "hub_auth_denied_v1"]
    max_response_bytes: Annotated[int, Field(ge=1024, le=2 * 1024**2)]
    timeout_ms: Annotated[int, Field(ge=100, le=10_000)]

    @model_validator(mode="after")
    def compiled_expectation(self):
        if (self.kind, self.path, self.expected_status, self.expectation_id) not in PROBE_PROFILE:
            raise ValueError("probe does not match compiled expectation")
        return self


class Probes(_Closed):
    candidate: tuple[Probe, Probe, Probe]
    rollback: tuple[Probe, Probe, Probe]

    _arrays = field_validator("candidate", "rollback", mode="before")(_json_array)

    @model_validator(mode="after")
    def exact_ordered_profile(self):
        for probes in (self.candidate, self.rollback):
            if tuple((p.kind, p.path, p.expected_status, p.expectation_id) for p in probes) != PROBE_PROFILE:
                raise ValueError("complete ordered probe profile required")
        return self


class Timeouts(_Closed):
    create_ms: Milliseconds
    start_ms: Milliseconds
    stop_ms: Milliseconds
    inspect_ms: Milliseconds
    total_command_ms: Milliseconds

    @model_validator(mode="after")
    def bounded_phases(self):
        if max(self.create_ms, self.start_ms, self.stop_ms, self.inspect_ms) > self.total_command_ms:
            raise ValueError("phase timeout exceeds total")
        return self


class FailurePolicy(_Closed):
    known_failure: Literal["rollback_then_verify"]
    unknown: Literal["hold_inspect_only"]
    rollback_verification_failure: Literal["hold_reserved"]
    pause: Literal["deny_new_effects"]


class Retention(_Closed):
    max_generations: Annotated[int, Field(ge=2, le=4)]
    max_storage_bytes: Annotated[int, Field(ge=16 * 1024**2, le=8 * 1024**3)]
    keep_previous_until_verified: Literal[True]
    foreign_resource_policy: Literal["refuse"]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def _reject_number(_value):
    raise ValueError("manifest numbers must be finite integers")


def _check_model_shape(value):
    """Detect hidden model_copy extras that Pydantic's serializer would omit."""
    if isinstance(value, BaseModel):
        if set(value.__dict__) != set(type(value).model_fields):
            raise ValueError("invalid manifest model shape")
        if value.model_extra:
            raise ValueError("unexpected manifest model fields")
        for child in value.__dict__.values():
            _check_model_shape(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _check_model_shape(child)
    elif isinstance(value, dict):
        for child in value.values():
            _check_model_shape(child)


class PilotManifest(_Closed):
    schema_version: Literal["container_pilot_manifest_v1"]
    driver: Driver
    campaign_id: str
    target_id: Literal["engineering-hub:staging"]
    policy_revision: Annotated[int, Field(ge=1, le=1)]
    runner_binding: RunnerBinding
    candidate: ImageSelection
    rollback: ImageSelection
    runtime: Runtime
    state_policy: StatePolicy
    probes: Probes
    timeouts: Timeouts
    failure_policy: FailurePolicy
    retention: Retention

    @field_validator("campaign_id")
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("canonical campaign UUID required")
        return value

    @model_validator(mode="after")
    def probe_deadlines(self):
        if self.candidate.image_config_id == self.rollback.image_config_id:
            raise ValueError("candidate and rollback must select distinct image config IDs")
        for probes in (self.probes.candidate, self.probes.rollback):
            if sum(p.timeout_ms for p in probes) > self.timeouts.total_command_ms:
                raise ValueError("probe sequence exceeds command deadline")
        return self

    @classmethod
    def from_bytes(cls, raw: bytes) -> "PilotManifest":
        if type(raw) is not bytes or len(raw) > MAX_BYTES:
            raise ValueError("manifest requires bounded bytes")
        try:
            data = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object,
                              parse_float=_reject_number, parse_constant=_reject_number)
            # Reject escaped unpaired surrogates even in otherwise valid JSON.
            json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
        except (UnicodeError, RecursionError) as exc:
            raise ValueError("invalid manifest encoding or nesting") from exc
        return cls.model_validate(data)

    def canonical_bytes(self) -> bytes:
        # A model_copy/update or model_construct bypass must not become authority.
        # Round-trip through the complete schema, rather than trusting frozen=True.
        try:
            _check_model_shape(self)
            raw = json.dumps(self.model_dump(mode="json", by_alias=True, warnings=False), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, UnicodeError, RecursionError) as exc:
            raise ValueError("invalid manifest representation") from exc
        verified = PilotManifest.from_bytes(raw)
        return json.dumps(verified.model_dump(mode="json", by_alias=True), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()
