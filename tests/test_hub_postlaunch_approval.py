"""Independent approval crypto, synthetic signed launch, no installed services."""
import base64
from dataclasses import asdict
import json
from uuid import uuid4
import pytest

pytest.importorskip('fcntl', reason='Linux launch provider required')
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins
from hub_container_executor.runtime_identity import LaunchReceiptIdentityVerifier
from hub_container_executor.postlaunch_approval import DOMAIN, verify_postlaunch_approval, PostLaunchApprovalError
from tests.test_hub_container_executor_runtime_identity import clock, enrolled, Issuer


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def test_independent_postlaunch_approval_is_exact_and_expires(clock, enrolled):
    enrollment, issuer_key = enrolled
    verifier = LaunchReceiptIdentityVerifier(enrollment=enrollment,
        issuer_transport=Issuer(enrollment, issuer_key, clock),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=2000)
    approver = Ed25519PrivateKey.generate()
    approver_public = approver.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    release = CurrentExecutorReleasePins(**{name: getattr(enrollment, name)
        for name in CurrentExecutorReleasePins.__dataclass_fields__})
    receipt = verifier.accepted_launch_receipt()
    launch = CurrentLaunchPins(receipt[1], receipt[2], receipt[3], receipt[4], receipt[5],
                               'approved-engine', 500, 600, receipt[7])
    payload = {'schema': 'engineering-hub.independent-launch-approval.v1',
        'approval_id': str(uuid4()), 'receipt_id': receipt[0], 'launch': asdict(launch),
        'approved_at_ms': receipt[6], 'expires_at_ms': receipt[7]}
    def sign(data):
        signature = base64.urlsafe_b64encode(approver.sign(DOMAIN + canonical(data))).decode().rstrip('=')
        return canonical({'payload': data, 'signature': signature})
    try:
        accepted = verify_postlaunch_approval(sign(payload), verifier=verifier, release=release,
            approver_public_key=approver_public, expected_engine_id='approved-engine')
        assert accepted.launch == launch
        changed = dict(payload, launch=dict(payload['launch'], engine_pid=501))
        with pytest.raises(PostLaunchApprovalError):
            verify_postlaunch_approval(sign(changed), verifier=verifier, release=release,
                approver_public_key=approver_public, expected_engine_id='other-engine')
        forged = canonical({'payload': changed, 'signature': json.loads(sign(payload))['signature']})
        with pytest.raises(PostLaunchApprovalError):
            verify_postlaunch_approval(forged, verifier=verifier, release=release,
                approver_public_key=approver_public, expected_engine_id='approved-engine')
        with pytest.raises(PostLaunchApprovalError):
            verify_postlaunch_approval(sign(payload), verifier=verifier, release=release,
                approver_public_key=release.public_key, expected_engine_id='approved-engine')
        clock.wall += 31
        clock.mono += 31
        with pytest.raises(PostLaunchApprovalError):
            verify_postlaunch_approval(sign(payload), verifier=verifier, release=release,
                approver_public_key=approver_public, expected_engine_id='approved-engine')
    finally:
        verifier.close()
