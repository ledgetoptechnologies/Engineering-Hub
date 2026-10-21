"""Pure v2 commitment for canonical installed-runner configuration bytes.

Only the embedded profile digest is normalized. The independent root policy must
ALSO pin the exact raw digest and compare the embedded release/profile identity.
This helper creates no files, enrollments, providers, sockets or authority.
"""
from dataclasses import dataclass
import hashlib
import json

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
