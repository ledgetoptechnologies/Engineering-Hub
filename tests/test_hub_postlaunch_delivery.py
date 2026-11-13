"""Synthetic signed post-launch delivery; read-only mounts are tested elsewhere."""
import base64
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import threading
from uuid import uuid4
import pytest

pytest.importorskip('fcntl', reason='native Linux launcher required')
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins
from hub_container_executor.runtime_identity import LaunchReceiptIdentityVerifier
from hub_container_executor import postlaunch_delivery as delivery
from hub_container_executor.postlaunch_approval import DOMAIN
from tests.test_hub_container_executor_runtime_identity import clock, enrolled, Issuer


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


@pytest.mark.asyncio
async def test_postlaunch_wait_receives_independent_record_then_revocation_denies(tmp_path, monkeypatch, clock, enrolled):
    enrollment, issuer_key = enrolled
    verifier = LaunchReceiptIdentityVerifier(enrollment=enrollment,
        issuer_transport=Issuer(enrollment, issuer_key, clock),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=2000)
    release = CurrentExecutorReleasePins(**{name: getattr(enrollment, name)
        for name in CurrentExecutorReleasePins.__dataclass_fields__})
    receipt = verifier.accepted_launch_receipt()
    launch = CurrentLaunchPins(receipt[1], receipt[2], receipt[3], receipt[4], receipt[5],
                               'approved-engine', 500, 600, receipt[7])
    approver = Ed25519PrivateKey.generate()
    public = approver.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = {'schema': 'engineering-hub.independent-launch-approval.v1', 'approval_id': str(uuid4()),
        'receipt_id': receipt[0], 'launch': asdict(launch), 'approved_at_ms': receipt[6], 'expires_at_ms': receipt[7]}
    sig = base64.urlsafe_b64encode(approver.sign(DOMAIN + canonical(payload))).decode().rstrip('=')
    raw = canonical({'payload': payload, 'signature': sig})
    target = tmp_path / 'approved.json'
    # Synthetic test replaces mount checking only. Production always calls the
    # fixed protected reader with require_readonly=True on every use.
    monkeypatch.setattr(delivery, 'read_protected_file', lambda path, **_: Path(path).read_bytes())
    def publish():
        pending = tmp_path / 'pending'
        pending.write_bytes(raw)
        os.replace(pending, target)
    timer = threading.Timer(.1, publish)
    timer.start()
    try:
        lease = await delivery.wait_for_postlaunch_approval(str(target), verifier=verifier, release=release,
            approver_public_key=public, expected_engine_id='approved-engine', timeout_ms=3000)
        assert lease.check().launch == launch
        target.unlink()
        with pytest.raises(Exception): lease.check()
    finally:
        timer.join(timeout=2)
        verifier.close()
    assert not timer.is_alive()


@pytest.mark.asyncio
async def test_pending_wait_cancels_and_closes_inotify(tmp_path, monkeypatch, clock, enrolled):
    enrollment, issuer_key = enrolled
    verifier = LaunchReceiptIdentityVerifier(enrollment=enrollment,
        issuer_transport=Issuer(enrollment, issuer_key, clock),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=2000)
    release = CurrentExecutorReleasePins(**{name: getattr(enrollment, name)
        for name in CurrentExecutorReleasePins.__dataclass_fields__})
    closed = []
    real_close = delivery.os.close
    def close(fd):
        closed.append(fd)
        return real_close(fd)
    monkeypatch.setattr(delivery.os, 'close', close)
    task = asyncio.create_task(delivery.wait_for_postlaunch_approval(str(tmp_path / 'absent'),
        verifier=verifier, release=release, approver_public_key=bytes(32),
        expected_engine_id='approved-engine', timeout_ms=3000))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert closed
    finally:
        verifier.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['within', 'boundary', 'read', 'verify', 'lease_check'])
async def test_approval_wait_rechecks_deadline_after_each_acceptance_stage(tmp_path, monkeypatch, clock, enrolled, phase):
    enrollment, issuer_key = enrolled
    verifier = LaunchReceiptIdentityVerifier(enrollment=enrollment,
        issuer_transport=Issuer(enrollment, issuer_key, clock),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=2000)
    release = CurrentExecutorReleasePins(**{name: getattr(enrollment, name)
        for name in CurrentExecutorReleasePins.__dataclass_fields__})
    receipt = verifier.accepted_launch_receipt()
    launch = CurrentLaunchPins(receipt[1], receipt[2], receipt[3], receipt[4], receipt[5],
                               'approved-engine', 500, 600, receipt[7])
    approver = Ed25519PrivateKey.generate()
    public = approver.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    payload = {'schema': 'engineering-hub.independent-launch-approval.v1', 'approval_id': str(uuid4()),
        'receipt_id': receipt[0], 'launch': asdict(launch), 'approved_at_ms': receipt[6], 'expires_at_ms': receipt[7]}
    signature = base64.urlsafe_b64encode(approver.sign(DOMAIN + canonical(payload))).decode().rstrip('=')
    target = tmp_path / 'approved.json'
    target.write_bytes(canonical({'payload': payload, 'signature': signature}))
    reads = verifies = 0
    original_verify = delivery.verify_postlaunch_approval

    def read(path, **_kwargs):
        nonlocal reads
        reads += 1
        if reads == 1 and phase in ('read', 'within', 'boundary'):
            clock.mono += .2 if phase == 'read' else .1 if phase == 'boundary' else .05
        return Path(path).read_bytes()

    def verify(*args, **kwargs):
        nonlocal verifies
        result = original_verify(*args, **kwargs)  # Real independent signature validation.
        verifies += 1
        if (phase == 'verify' and verifies == 1) or (phase == 'lease_check' and verifies == 2):
            clock.mono += .2
        return result

    # Only the mount-protected file reader is replaced, as in the other fixture.
    monkeypatch.setattr(delivery, 'read_protected_file', read)
    monkeypatch.setattr(delivery, 'verify_postlaunch_approval', verify)
    arguments = dict(verifier=verifier, release=release, approver_public_key=public,
                     expected_engine_id='approved-engine', timeout_ms=100)
    try:
        if phase == 'within':
            lease = await delivery.wait_for_postlaunch_approval(str(target), **arguments)
            assert lease.approval.launch == launch and verifies == 2
        else:
            with pytest.raises(delivery.PostLaunchApprovalError):
                await delivery.wait_for_postlaunch_approval(str(target), **arguments)
            assert verifies == (0 if phase in ('read', 'boundary') else 2)
    finally:
        verifier.close()


@pytest.mark.asyncio
async def test_approval_wait_zero_timeout_remains_invalid(tmp_path, clock, enrolled):
    enrollment, issuer_key = enrolled
    verifier = LaunchReceiptIdentityVerifier(enrollment=enrollment,
        issuer_transport=Issuer(enrollment, issuer_key, clock),
        max_receipt_lifetime_seconds=60, exchange_timeout_ms=2000)
    release = CurrentExecutorReleasePins(**{name: getattr(enrollment, name)
        for name in CurrentExecutorReleasePins.__dataclass_fields__})
    try:
        with pytest.raises(delivery.PostLaunchApprovalError):
            await delivery.wait_for_postlaunch_approval(str(tmp_path / 'absent'), verifier=verifier,
                release=release, approver_public_key=bytes(32), expected_engine_id='approved-engine', timeout_ms=0)
    finally:
        verifier.close()
