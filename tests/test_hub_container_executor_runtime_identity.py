"""Real crypto over synthetic issuer receipts; not installed launch/provenance proof."""
import base64
import asyncio
import inspect
import json
import os
import select
import signal
import threading
import uuid
from dataclasses import replace

import pytest
pytest.importorskip("fcntl")  # Host executor providers are Linux-only.
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import hub_container_executor.runtime_identity as identity
from engineering_hub.container_pilot_manifest import Platform


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


class Clock:
    wall = 1_800_000_000.0
    mono = 100.0


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(identity.time, "time", lambda: clock.wall)
    monkeypatch.setattr(identity.time, "monotonic", lambda: clock.mono)
    return clock


class Issuer:
    """Test-only signer. No protected key file, network or daemon is accessed."""
    def __init__(self, enrollment, key, clock, *, mutate=None, encode=None, delay=0):
        self.enrollment = enrollment
        self.key = key
        self.clock = clock
        self.mutate = mutate
        self.encode = encode
        self.delay = delay
        self.calls = []
        self.closed = False

    def issue_receipt(self, *, nonce, timeout_ms):
        self.calls.append((nonce, timeout_ms))
        e = self.enrollment
        payload = {
            "protocol": "engineering-hub.executor-launch.v1",
            "audience": "engineering-hub.container-executor",
            "nonce": nonce,
            "receipt_id": str(uuid.uuid4()), "launch_id": str(uuid.uuid4()),
            "boot_id": str(uuid.uuid4()), "issuer_id": e.issuer_id,
            "authenticated_host_pid": 1234, "process_start_ticks": 98765,
            "container_id": "a" * 64,
            "runnable_digest": e.runnable_digest,
            "platform_manifest_digest": e.platform_manifest_digest,
            "image_config_id": e.image_config_id,
            "platform": e.platform.model_dump(mode="json"),
            "implementation_commit": e.implementation_commit,
            "launch_profile_digest": e.launch_profile_digest,
            "issued_at_ms": int(self.clock.wall * 1000),
            "expires_at_ms": int(self.clock.wall * 1000) + 30_000,
        }
        if self.mutate:
            self.mutate(payload)
        sig = self.key.sign(identity.SIGNATURE_DOMAIN + canonical(payload))
        envelope = {"payload": payload,
                    "signature": base64.urlsafe_b64encode(sig).decode().rstrip("=")}
        self.clock.mono += self.delay
        self.clock.wall += self.delay
        return self.encode(envelope) if self.encode else canonical(envelope)

    def close(self):
        self.closed = True


@pytest.fixture
def enrolled(clock):
    key = Ed25519PrivateKey.generate()
    enrollment = identity.ExecutorReleaseEnrollment(
        issuer_id="test-independent-issuer",
        public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        implementation_commit="c" * 40,
        runnable_digest="sha256:" + "1" * 64,
        platform_manifest_digest="sha256:" + "2" * 64,
        image_config_id="sha256:" + "3" * 64,
        platform=Platform(os="linux", architecture="amd64", variant=None),
        launch_profile_digest="sha256:" + "4" * 64,
    )
    return enrollment, key


def verifier(enrolled, clock, **issuer_options):
    e, key = enrolled
    issuer = Issuer(e, key, clock, **issuer_options)
    result = identity.LaunchReceiptIdentityVerifier(
        enrollment=e, issuer_transport=issuer, max_receipt_lifetime_seconds=60,
        exchange_timeout_ms=1000,
    )
    return result, issuer


def test_real_signature_exact_independent_enrollment_and_no_refresh(enrolled, clock):
    result, issuer = verifier(enrolled, clock)
    assert result.implementation_commit() == enrolled[0].implementation_commit
    assert result.implementation_commit() == enrolled[0].implementation_commit
    assert len(issuer.calls) == 1
    nonce, timeout = issuer.calls[0]
    assert len(nonce) == 64 and int(nonce, 16) >= 0
    assert timeout == 1000
    result.close()
    result.close()
    assert not issuer.closed  # Transport lifetime belongs to caller, not verifier.
    with pytest.raises(identity.RuntimeIdentityError):
        result.implementation_commit()
    assert len(issuer.calls) == 1


@pytest.mark.parametrize("field,value", [
    ("protocol", "other.v1"), ("audience", "another-consumer"),
    ("nonce", "0" * 64), ("issuer_id", "unapproved-issuer"),
    ("implementation_commit", "d" * 40),
    ("runnable_digest", "sha256:" + "9" * 64),
    ("platform_manifest_digest", "sha256:" + "9" * 64),
    ("image_config_id", "sha256:" + "9" * 64),
    ("launch_profile_digest", "sha256:" + "9" * 64),
    ("platform", {"os": "linux", "architecture": "arm64", "variant": "v8"}),
    ("receipt_id", "not-a-uuid"), ("launch_id", "not-a-uuid"),
    ("boot_id", "not-a-uuid"), ("container_id", "A" * 64),
    ("authenticated_host_pid", True), ("authenticated_host_pid", 0),
    ("process_start_ticks", -1), ("process_start_ticks", "1"),
    ("issued_at_ms", 1.0), ("expires_at_ms", True),
])
def test_correctly_signed_but_unenrolled_or_malformed_claim_rejected(enrolled, clock, field, value):
    with pytest.raises(identity.RuntimeIdentityError):
        verifier(enrolled, clock, mutate=lambda p: p.__setitem__(field, value))


@pytest.mark.parametrize("change", ["extra", "missing", "uuid-upper", "platform-extra"])
def test_closed_payload_schema(enrolled, clock, change):
    def mutate(p):
        if change == "extra":
            p["unapproved"] = "value"
        elif change == "missing":
            del p["boot_id"]
        elif change == "uuid-upper":
            p["launch_id"] = "ABCDEFAB-1234-4234-8234-ABCDEFABCDEF"
        else:
            p["platform"]["extra"] = "value"
    with pytest.raises(identity.RuntimeIdentityError):
        verifier(enrolled, clock, mutate=mutate)


@pytest.mark.parametrize("kind", ["wrong-key", "wrong-domain", "tampered", "bad-base64", "padded", "short"])
def test_real_signature_boundary(enrolled, clock, kind):
    def encode(envelope):
        if kind == "wrong-key":
            signature = Ed25519PrivateKey.generate().sign(
                identity.SIGNATURE_DOMAIN + canonical(envelope["payload"]))
            envelope["signature"] = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        elif kind == "wrong-domain":
            signature = enrolled[1].sign(canonical(envelope["payload"]))
            envelope["signature"] = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        elif kind == "tampered":
            envelope["payload"]["authenticated_host_pid"] += 1
        elif kind == "bad-base64":
            envelope["signature"] = "!" * 86
        elif kind == "padded":
            envelope["signature"] += "=="
        else:
            envelope["signature"] = "AA"
        return canonical(envelope)
    with pytest.raises(identity.RuntimeIdentityError):
        verifier(enrolled, clock, encode=encode)


@pytest.mark.parametrize("kind", ["noncanonical", "duplicate", "extra", "missing", "oversize", "text", "array", "bad-json"])
def test_envelope_bytes_and_exact_schema(enrolled, clock, kind):
    def encode(e):
        if kind == "noncanonical":
            return json.dumps(e, indent=2).encode()
        if kind == "duplicate":
            raw = canonical(e)
            return b'{"payload":{},' + raw[1:]
        if kind == "extra":
            e["unapproved"] = True
        if kind == "missing":
            del e["signature"]
        if kind == "oversize":
            return b" " * 8193
        if kind == "text":
            return canonical(e).decode()
        if kind == "array":
            return b"[]"
        if kind == "bad-json":
            return b"{"
        return canonical(e)
    with pytest.raises(identity.RuntimeIdentityError):
        verifier(enrolled, clock, encode=encode)


@pytest.mark.parametrize("issued_delta,expiry_delta", [
    (6000, 30_000), (-6000, 30_000), (0, 0), (0, -1), (0, 60_001),
])
def test_temporal_bounds(enrolled, clock, issued_delta, expiry_delta):
    def mutate(p):
        now = int(clock.wall * 1000)
        p["issued_at_ms"] = now + issued_delta
        p["expires_at_ms"] = now + expiry_delta
    with pytest.raises(identity.RuntimeIdentityError):
        verifier(enrolled, clock, mutate=mutate)


def test_exchange_deadline_no_retry(enrolled, clock):
    e, key = enrolled
    issuer = Issuer(e, key, clock, delay=1.001)
    with pytest.raises(identity.RuntimeIdentityError):
        identity.LaunchReceiptIdentityVerifier(
            enrollment=e, issuer_transport=issuer, max_receipt_lifetime_seconds=60,
            exchange_timeout_ms=1000,
        )
    assert len(issuer.calls) == 1


@pytest.mark.parametrize("field,value", [
    ("issuer_id", ""), ("issuer_id", "unapproved whitespace"),
    ("public_key", b"short"), ("public_key", "x" * 32),
    ("implementation_commit", "C" * 40), ("implementation_commit", "c" * 39),
    ("runnable_digest", "sha256:" + "A" * 64),
    ("platform_manifest_digest", "not-a-digest"),
    ("image_config_id", None), ("launch_profile_digest", 1),
    ("platform", {"os": "linux", "architecture": "amd64", "variant": None}),
])
def test_independent_enrollment_shape_is_closed(enrolled, field, value):
    with pytest.raises(identity.RuntimeIdentityError):
        replace(enrolled[0], **{field: value})


@pytest.mark.parametrize("field,value", [
    ("max_receipt_lifetime_seconds", True), ("max_receipt_lifetime_seconds", 0),
    ("max_receipt_lifetime_seconds", 3601), ("max_receipt_lifetime_seconds", 1.0),
    ("exchange_timeout_ms", True), ("exchange_timeout_ms", 99),
    ("exchange_timeout_ms", 10001), ("exchange_timeout_ms", 1000.0),
    ("enrollment", None), ("issuer_transport", object()),
])
def test_invalid_configuration_never_contacts_issuer(enrolled, clock, field, value):
    e, key = enrolled
    issuer = Issuer(e, key, clock)
    options = dict(enrollment=e, issuer_transport=issuer,
                   max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    options[field] = value
    with pytest.raises(identity.RuntimeIdentityError):
        identity.LaunchReceiptIdentityVerifier(**options)
    assert issuer.calls == []


def test_transport_failure_is_once_and_sanitized(enrolled, clock):
    class Broken:
        calls = 0
        def issue_receipt(self, **kwargs):
            self.calls += 1
            raise RuntimeError("secret transport path and credential must not escape")
    broken = Broken()
    with pytest.raises(identity.RuntimeIdentityError) as exc:
        identity.LaunchReceiptIdentityVerifier(
            enrollment=enrolled[0], issuer_transport=broken,
            max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    assert broken.calls == 1
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("bad_clock", [float("nan"), float("inf"), 99.0])
def test_nonfinite_or_rewound_monotonic_denied(enrolled, clock, bad_clock):
    result, issuer = verifier(enrolled, clock)
    clock.mono = bad_clock
    with pytest.raises(identity.RuntimeIdentityError):
        result.implementation_commit()
    assert len(issuer.calls) == 1
    result.close()


def test_fresh_nonce_per_verifier_and_old_receipt_replay_denied(enrolled, clock):
    captured = []
    def capture(envelope):
        raw = canonical(envelope)
        captured.append(raw)
        return raw
    first, issuer = verifier(enrolled, clock, encode=capture)
    class Replay:
        calls = []
        def issue_receipt(self, *, nonce, timeout_ms):
            self.calls.append(nonce)
            return captured[0]
    replay = Replay()
    try:
        with pytest.raises(identity.RuntimeIdentityError):
            identity.LaunchReceiptIdentityVerifier(
                enrollment=enrolled[0], issuer_transport=replay,
                max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
        assert len(replay.calls) == 1
        assert replay.calls[0] != issuer.calls[0][0]
    finally:
        first.close()


def test_exchange_latency_does_not_restart_receipt_ttl(enrolled, clock):
    result, issuer = verifier(enrolled, clock, delay=.5)
    clock.mono = 129.999
    assert result.implementation_commit() == enrolled[0].implementation_commit
    clock.mono = 130.0
    with pytest.raises(identity.RuntimeIdentityError):
        result.implementation_commit()
    assert len(issuer.calls) == 1
    result.close()


def test_actual_crypto_verification_time_counts_toward_exchange_deadline(enrolled, clock, monkeypatch):
    real = identity.Ed25519PublicKey
    class SlowVerification:
        @classmethod
        def from_public_bytes(cls, raw):
            public = real.from_public_bytes(raw)
            class Key:
                def verify(self, signature, message):
                    public.verify(signature, message)  # Still verifies real Ed25519.
                    clock.mono += 1.001
            return Key()
    monkeypatch.setattr(identity, "Ed25519PublicKey", SlowVerification)
    with pytest.raises(identity.RuntimeIdentityError):
        verifier(enrolled, clock)


def test_misdeclared_async_transport_is_not_executed(enrolled, clock):
    class AsyncIssuer:
        calls = 0
        async def issue_receipt(self, **kwargs):
            self.calls += 1
            return b"unreachable"
    issuer = AsyncIssuer()
    with pytest.raises(identity.RuntimeIdentityError):
        identity.LaunchReceiptIdentityVerifier(
            enrollment=enrolled[0], issuer_transport=issuer,
            max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    assert issuer.calls == 0


def test_sync_transport_returning_unstarted_coroutine_closes_only_that_coroutine(enrolled, clock):
    async def result():
        raise AssertionError("must not run")
    pending = result()
    class AsyncResult:
        def issue_receipt(self, **kwargs):
            return pending
    with pytest.raises(identity.RuntimeIdentityError):
        identity.LaunchReceiptIdentityVerifier(
            enrollment=enrolled[0], issuer_transport=AsyncResult(),
            max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
    assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_foreign_future_is_rejected_not_cancelled(enrolled, clock):
    future = asyncio.get_running_loop().create_future()
    class FutureResult:
        def issue_receipt(self, **kwargs):
            return future
    try:
        with pytest.raises(identity.RuntimeIdentityError):
            identity.LaunchReceiptIdentityVerifier(
                enrollment=enrolled[0], issuer_transport=FutureResult(),
                max_receipt_lifetime_seconds=60, exchange_timeout_ms=1000)
        assert not future.cancelled() and not future.done()
    finally:
        future.cancel()


def test_monotonic_expiry_survives_wall_clock_rollback(enrolled, clock):
    result, issuer = verifier(enrolled, clock)
    clock.wall -= 100_000
    clock.mono += 31
    with pytest.raises(identity.RuntimeIdentityError):
        result.implementation_commit()
    assert len(issuer.calls) == 1
    result.close()


def test_wall_clock_forward_jump_cannot_renew_monotonic_interval(enrolled, clock):
    result, _ = verifier(enrolled, clock)
    clock.wall += 100_000
    assert result.implementation_commit() == enrolled[0].implementation_commit
    clock.mono += 31
    with pytest.raises(identity.RuntimeIdentityError):
        result.implementation_commit()
    result.close()


def test_actual_fork_denied_before_inherited_lock_can_block(enrolled, clock):
    result, issuer = verifier(enrolled, clock)
    read_fd, write_fd = os.pipe()
    result._lock.acquire()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            try:
                result.implementation_commit()
            except identity.RuntimeIdentityError:
                os.write(write_fd, b"denied")
            else:
                os.write(write_fd, b"accepted")
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    reaped = False
    try:
        ready, _, _ = select.select([read_fd], [], [], 5)
        assert ready, "inherited verifier blocked on its inherited lock"
        assert os.read(read_fd, 32) == b"denied"
        _, status = os.waitpid(child, 0)
        reaped = True
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    finally:
        if not reaped:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(read_fd)
        result._lock.release()
        result.close()
    assert len(issuer.calls) == 1


def test_process_binding_and_thread_reads(enrolled, clock, monkeypatch):
    result, issuer = verifier(enrolled, clock)
    values = []
    threads = [threading.Thread(target=lambda: values.append(result.implementation_commit()))
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert values == [enrolled[0].implementation_commit] * 8
    monkeypatch.setattr(identity.os, "getpid", lambda: -1)
    with pytest.raises(identity.RuntimeIdentityError):
        result.implementation_commit()
    assert len(issuer.calls) == 1


@pytest.mark.parametrize("mode", ["valid", "wrong-commit", "expired", "missing"])
def test_actual_adapter_checks_receipt_provider_before_backend_inspection(tmp_path, enrolled, clock, mode):
    from tests.test_hub_container_executor_adapter import make_fixture, command
    adapter, backend, state, manifest = make_fixture(tmp_path)
    result = None
    try:
        e, key = enrolled
        commit = manifest.driver.implementation_commit if mode != "wrong-commit" else "f" * 40
        e = replace(e, implementation_commit=commit)
        result, _ = verifier((e, key), clock)
        if mode == "expired":
            clock.mono += 31
        adapter.driver_identity_verifier = result if mode != "missing" else None
        response = adapter.submit(command(manifest, "deploy_container"))
        if mode == "valid":
            assert response.state == "succeeded"
            assert "inspect_runner" in backend.events
        else:
            assert response.state == "held" and response.reason == "daemon_unavailable"
            assert "inspect_runner" not in backend.events
            assert state.calls == []
    finally:
        adapter.close()
        if result:
            result.close()
