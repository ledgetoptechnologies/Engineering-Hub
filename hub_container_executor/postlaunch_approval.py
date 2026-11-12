"""Verify a separately signed, post-launch approval before constructing pins.

This helper does not wait for a file, start a runner, enroll a key, or grant
execution. Its independent approval signer must be pre-enrolled by the operator.
"""
import base64
from dataclasses import dataclass, fields
import hashlib
import json
import re
import time
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins
from .runtime_identity import LaunchReceiptIdentityVerifier

DOMAIN = b'engineering-hub-independent-launch-approval-v1\x00'
MAX_BYTES = 16384


class PostLaunchApprovalError(ValueError):
    """Sanitized denial; do not expose signed records or secret configuration."""


@dataclass(frozen=True)
class AcceptedPostLaunchApproval:
    launch: CurrentLaunchPins
    approval_id: str
    expires_at_ms: int


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def verify_postlaunch_approval(raw, *, verifier, release, approver_public_key, expected_engine_id):
    """Check an independent signature against the live, verified launch receipt.

    The approved launch's Engine PID/start are supplied by the independent
    approval, not learned from a self-asserted observation. The caller must
    subsequently obtain a fresh signed current observation using these pins.
    This function supplies no execution or hold-release permission.
    """
    try:
        if (type(raw) is not bytes or not 1 <= len(raw) <= MAX_BYTES
                or type(verifier) is not LaunchReceiptIdentityVerifier
                or type(release) is not CurrentExecutorReleasePins
                or type(approver_public_key) is not bytes or len(approver_public_key) != 32
                or approver_public_key == release.public_key or type(expected_engine_id) is not str):
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError
                result[key] = value
            return result
        def reject(_): raise ValueError
        envelope = json.loads(raw, object_pairs_hook=unique, parse_float=reject, parse_constant=reject)
        if type(envelope) is not dict or set(envelope) != {'payload', 'signature'} or _canonical(envelope) != raw:
            raise ValueError
        payload, signature = envelope['payload'], envelope['signature']
        if (type(payload) is not dict or set(payload) != {'schema', 'approval_id', 'receipt_id', 'launch', 'approved_at_ms', 'expires_at_ms'}
                or payload['schema'] != 'engineering-hub.independent-launch-approval.v1'
                or type(signature) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{86}', signature)):
            raise ValueError
        decoded = base64.urlsafe_b64decode(signature + '==')
        if len(decoded) != 64 or base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() != signature:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(approver_public_key).verify(decoded, DOMAIN + _canonical(payload))
        if (type(payload['approval_id']) is not str or str(UUID(payload['approval_id'])) != payload['approval_id']
                or type(payload['receipt_id']) is not str or str(UUID(payload['receipt_id'])) != payload['receipt_id']
                or type(payload['launch']) is not dict or set(payload['launch']) != {f.name for f in fields(CurrentLaunchPins)}):
            raise ValueError
        launch = CurrentLaunchPins(**payload['launch'])
        receipt = verifier.accepted_launch_receipt()
        expected_release = (release.issuer_id, release.implementation_commit, release.runnable_digest,
            release.platform_manifest_digest, release.image_config_id, release.launch_profile_digest,
            (release.platform.os, release.platform.architecture, release.platform.variant), hashlib.sha256(release.public_key).digest())
        if (receipt[8:] != expected_release or launch.engine_id != expected_engine_id
                or (payload['receipt_id'], launch.launch_id, launch.boot_id, launch.authenticated_host_pid,
                    launch.process_start_ticks, launch.container_id) != receipt[:6]):
            raise ValueError
        approved, expires = payload['approved_at_ms'], payload['expires_at_ms']
        now = int(time.time() * 1000)
        if (type(approved) is not int or type(expires) is not int or not receipt[6] <= approved <= now < expires
                or expires > receipt[7] or expires > launch.approval_expires_at_ms):
            raise ValueError
        # Recheck receipt's fixed monotonic and wall-clock ceiling after parsing.
        if verifier.accepted_launch_receipt() != receipt or int(time.time() * 1000) >= expires:
            raise ValueError
        return AcceptedPostLaunchApproval(launch, payload['approval_id'], expires)
    except Exception:
        raise PostLaunchApprovalError('Independent launch approval unavailable') from None
