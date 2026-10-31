"""Pure v2 commitment for canonical installed-runner configuration bytes.

Only the embedded profile digest is normalized. The independent root policy must
ALSO pin the exact raw digest and compare the embedded release/profile identity.
This helper creates no files, enrollments, providers, sockets or authority.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath

from .installed_runner import parse_configuration
from .runtime_identity import ExecutorReleaseEnrollment


DOMAIN = b'engineering-hub.executor-configuration-commitment.v2\x00'
SELF_REFERENCE_MARKER = 'engineering-hub.executor-profile.self-reference.v2'


@dataclass(frozen=True)
class ConfigurationCommitment:
    content_digest: str
    raw_digest: str
    release: ExecutorReleaseEnrollment


def configuration_commitment(raw: bytes) -> ConfigurationCommitment:
    """Validate the entire actual runner document before normalizing one leaf.

    Canonical/closed parsing rejects duplicate keys, unknown fields, aliases,
    floats, malformed or missing self digests, and invalid provider bindings.
    A provisional valid-format digest can be used when constructing the DAG;
    launch admission separately requires the final digest to match root policy.
    """
    parsed = parse_configuration(raw)
    normalized = json.loads(raw)
    normalized['release_enrollment']['launch_profile_digest'] = SELF_REFERENCE_MARKER
    canonical = json.dumps(normalized, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False).encode('utf-8')
    return ConfigurationCommitment(
        'sha256:' + hashlib.sha256(DOMAIN + canonical).hexdigest(),
        'sha256:' + hashlib.sha256(raw).hexdigest(), parsed.release)


COORDINATOR_DOMAIN = b'engineering-hub.coordinator-configuration-commitment.v1\x00'


@dataclass(frozen=True)
class CoordinatorConfigurationCommitment:
    content_digest: str
    raw_digest: str
    manifest_digest: str


def coordinator_configuration_commitment(raw: bytes) -> CoordinatorConfigurationCommitment:
    """Commit the exact closed coordinator config, including its pilot manifest.

    This is not enrollment or process proof. A trusted launch authority must pin
    both digests in a signed profile and root policy, respectively. The current
    supervisor continues to refuse coordinator launch until pair trust exists.
    """
    from .installed_coordinator import parse_configuration as parse_coordinator
    parsed = parse_coordinator(raw)
    return CoordinatorConfigurationCommitment(
        'sha256:' + hashlib.sha256(COORDINATOR_DOMAIN + raw).hexdigest(),
        'sha256:' + hashlib.sha256(raw).hexdigest(), parsed.manifest.digest())


def trusted_local_coordinator_commitment(profile, config_raw: bytes, enrollment_raw: bytes):
    """Cross-check signed profile bytes and independently captured local inputs.

    This pure validation is not wired to the refused supervisor launch path and
    does not prove host custody, mount identity or operator approval.
    """
    from engineering_hub.executor_launch_profile import (ExecutorLaunchProfile,
        TRUSTED_LOCAL_COORDINATOR_TARGETS, verify_profile_bytes)
    from engineering_hub.trusted_queued_session import parse_enrollment, SCHEMA_V3
    from .installed_coordinator import parse_configuration as parse_coordinator
    if type(profile) is not ExecutorLaunchProfile or profile.role != 'trusted-local-coordinator-v1':
        raise ValueError('trusted local coordinator commitment invalid')
    try:
        if ExecutorLaunchProfile.model_validate(profile.enrollment_value()) != profile:
            raise ValueError()
        config = parse_coordinator(config_raw)
        enrollment = parse_enrollment(enrollment_raw)
        executor_profile = verify_profile_bytes(enrollment.profile_bytes, enrollment.release.launch_profile_digest)
        mounts = {mount.role: mount for mount in profile.mounts}
        targets = TRUSTED_LOCAL_COORDINATOR_TARGETS
        require = (
            config.auth.human_auth_mode == 'github' and config.auth.github_client_secret == ''
            and enrollment.schema == SCHEMA_V3
            and enrollment.operator_actor == f'human:{config.auth.beau_github_id}'
            and enrollment.machine_id == 'staging-pc'
            and enrollment.target_id == config.manifest.target_id
            and enrollment.manifest_digest == config.manifest.digest()
            and enrollment.release.implementation_commit == config.manifest.driver.implementation_commit
            and enrollment.release.platform == config.manifest.runner_binding.platform
            and enrollment.launch.engine_id == config.manifest.runner_binding.daemon_identity
            and profile.daemon_policy.engine_id == enrollment.launch.engine_id
            and executor_profile.broker_mode is None and executor_profile.role is None
            and executor_profile.command_channel is None and executor_profile.daemon_policy is not None
            and executor_profile.daemon_policy.engine_id == enrollment.launch.engine_id
            and executor_profile.daemon_policy == profile.daemon_policy
            and PurePosixPath(config.database).parent == PurePosixPath(targets['local_state'])
            and PurePosixPath(config.coordinator_socket).parent == PurePosixPath(targets['local_browser_directory'])
            and PurePosixPath(config.executor_socket).parent == PurePosixPath(targets['local_executor_directory'])
            and config.secret_root == targets['local_secrets']
            and mounts['local_config'].content_digest == 'sha256:' + hashlib.sha256(config_raw).hexdigest()
            and mounts['local_enrollment'].content_digest == 'sha256:' + hashlib.sha256(enrollment_raw).hexdigest())
        if not require:
            raise ValueError()
        return coordinator_configuration_commitment(config_raw)
    except Exception:
        raise ValueError('trusted local coordinator commitment invalid') from None
