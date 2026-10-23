"""Nonce-start proof lifetime with real synthetic Ed25519 signatures."""
from dataclasses import replace

import pytest

from engineering_hub import current_executor_observation as module
from tests.test_hub_current_executor_observation import setup


@pytest.mark.parametrize('offset', [-1000, 0, 1000, 4000])
def test_signed_lifetime_is_anchored_to_nonce_start_not_response_finish(setup, offset):
    clock, _, _, signer, observer = setup
    signer.delay = .5
    def shifted(payload):
        payload['observed_at_ms'] += offset
        payload['expires_at_ms'] += offset
    signer.mutate = shifted
    proof = observer.observe_verified()
    assert proof.started_monotonic == 100
    assert proof.expires_monotonic == 103
    assert clock.mono == 100.5
    assert proof.expires_monotonic != clock.mono + 3


def test_issuer_positive_skew_does_not_extend_expired_proof_after_delay(setup):
    _, release, launch, signer, _ = setup
    observer = module.CurrentExecutorObserver(release=release, launch=launch,
        transport=signer, exchange_timeout_ms=5000)
    signer.delay = 3.5
    def future(payload):
        payload['observed_at_ms'] += 4000
        payload['expires_at_ms'] += 4000
    signer.mutate = future
    with pytest.raises(module.CurrentExecutorObservationError):
        observer.observe_verified()
    assert observer._closed and len(signer.calls) == 1


def test_original_approval_ceiling_clips_nonce_start_lifetime(setup):
    clock, release, launch, signer, _ = setup
    launch = replace(launch, approval_expires_at_ms=int(clock.wall * 1000) + 1000)
    signer.launch = launch
    observer = module.CurrentExecutorObserver(release=release, launch=launch, transport=signer)
    signer.delay = .25
    proof = observer.observe_verified()
    assert proof.expires_monotonic == observer._deadline == 101
