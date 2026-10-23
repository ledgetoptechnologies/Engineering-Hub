"""Synthetic current observations with real Ed25519; no live identity proof."""
import base64
from dataclasses import replace
import json
import os
import subprocess
import sys
import threading
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from engineering_hub import current_executor_observation as observation
from engineering_hub.container_pilot_manifest import Platform
from engineering_hub.startup_reconciliation import ExecutorSessionIdentity


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


class Clock:
    wall = 1_800_000_000.0
    mono = 100.0


class Signer:
    def __init__(self, release, launch, key, clock):
        self.release, self.launch, self.key, self.clock = release, launch, key, clock
        self.calls = []
        self.mutate = lambda payload: None
        self.encode = canonical
        self.domain = observation.SIGNATURE_DOMAIN
        self.delay = 0
        self.before = lambda: None

    def observe_current_launch(self, *, nonce, timeout_ms):
        self.calls.append((nonce, timeout_ms))
        self.before()
        payload = {name: getattr(self.release, name) for name in observation._RELEASE_FIELDS}
        payload["platform"] = self.release.platform.model_dump(mode="json")
        payload.update({name: getattr(self.launch, name) for name in observation._LAUNCH_FIELDS})
        payload.update(protocol=observation.PROTOCOL, audience=observation.AUDIENCE,
                       nonce=nonce, observation_id=str(uuid4()),
                       observed_at_ms=int(self.clock.wall * 1000),
                       expires_at_ms=min(int(self.clock.wall * 1000) + 3000,
                                         self.launch.approval_expires_at_ms))
        self.mutate(payload)
        signature = self.key.sign(self.domain + canonical(payload))
        envelope = {"payload": payload,
                    "signature": base64.urlsafe_b64encode(signature).decode().rstrip("=")}
        self.clock.mono += self.delay
        self.clock.wall += self.delay
        return self.encode(envelope)


@pytest.fixture
def setup(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(observation.time, "time", lambda: clock.wall)
    monkeypatch.setattr(observation.time, "monotonic", lambda: clock.mono)
    key = Ed25519PrivateKey.generate()
    release = observation.CurrentExecutorReleasePins(
        "existing-issuer", key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        "c" * 40, "sha256:" + "1" * 64, "sha256:" + "2" * 64,
        "sha256:" + "3" * 64, Platform(os="linux", architecture="amd64", variant=None),
        "sha256:" + "4" * 64)
    launch = observation.CurrentLaunchPins(str(uuid4()), str(uuid4()), 1234, 98765,
        "a" * 64, "engine:independently-pinned", 500, 123, int(clock.wall * 1000) + 30000)
    signer = Signer(release, launch, key, clock)
    observer = observation.CurrentExecutorObserver(release=release, launch=launch, transport=signer)
    return clock, release, launch, signer, observer


def denied(observer):
    with pytest.raises(observation.CurrentExecutorObservationError,
                       match="^Current executor identity unavailable$"):
        observer()


def test_fresh_every_call_exact_gate_identity(setup):
    clock, release, launch, signer, observer = setup
    assert observer() == ExecutorSessionIdentity(launch.launch_id, release.image_config_id)
    clock.wall += 1
    clock.mono += 1
    assert observer() == ExecutorSessionIdentity(launch.launch_id, release.image_config_id)
    assert len(signer.calls) == 2
    assert signer.calls[0][0] != signer.calls[1][0]
    assert all(0 < timeout <= 2000 for _, timeout in signer.calls)
    observer.close()
    denied(observer)
    assert len(signer.calls) == 2


@pytest.mark.parametrize("field,value", [
    ("protocol", "engineering-hub.executor-launch.v1"),
    ("audience", "engineering-hub.container-executor"), ("nonce", "0" * 64),
    ("nonce", "\u2603"), ("observation_id", "not-a-uuid"),
    ("launch_id", str(uuid4())), ("boot_id", str(uuid4())),
    ("authenticated_host_pid", 999), ("authenticated_host_pid", True),
    ("process_start_ticks", 999), ("container_id", "b" * 64),
    ("engine_id", "other"), ("engine_pid", 999), ("engine_start_ticks", 999),
    ("issuer_id", "other"), ("implementation_commit", "d" * 40),
    ("runnable_digest", "sha256:" + "5" * 64),
    ("platform_manifest_digest", "sha256:" + "5" * 64),
    ("image_config_id", "sha256:" + "5" * 64),
    ("launch_profile_digest", "sha256:" + "5" * 64),
    ("platform", {"os": "linux", "architecture": "arm64", "variant": None}),
    ("approval_expires_at_ms", 1_800_000_050_000),
    ("observed_at_ms", True), ("expires_at_ms", True),
    ("observed_at_ms", 1_800_000_005_001),
    ("observed_at_ms", 1_799_999_990_000),
    ("expires_at_ms", 1_800_000_000_000),
    ("expires_at_ms", 1_800_000_006_000),
])
def test_signed_mismatch_is_sticky(setup, field, value):
    _, _, _, signer, observer = setup
    signer.mutate = lambda payload: payload.__setitem__(field, value)
    denied(observer)
    signer.mutate = lambda payload: None
    denied(observer)
    assert len(signer.calls) == 1


@pytest.mark.parametrize("encode", [
    lambda e: canonical(e) + b" ",
    lambda e: b'{"payload":{},"payload":{},"signature":"x"}',
    lambda e: b"x" * 8193,
    lambda e: "not bytes",
    lambda e: canonical({**e, "extra": 1}),
    lambda e: canonical({**e, "signature": "x" * 86}),
    lambda e: canonical({**e, "signature": e["signature"] + "="}),
    lambda e: b'{"payload":NaN,"signature":"x"}',
    lambda e: b'{"payload":1.0,"signature":"x"}',
    lambda e: canonical({**e, "payload": {**e["payload"], "extra": 1}}),
    lambda e: canonical({**e, "payload": {k:v for k,v in e["payload"].items() if k != "engine_id"}}),
])
def test_strict_envelope(setup, encode):
    _, _, _, signer, observer = setup
    signer.encode = encode
    denied(observer)


def test_old_receipt_domain_rejected(setup):
    _, _, _, signer, observer = setup
    signer.domain = b"engineering-hub-launch-receipt-v1\x00"
    denied(observer)


def test_wrong_signing_key(setup):
    _, _, _, signer, observer = setup
    signer.key = Ed25519PrivateKey.generate()
    denied(observer)


def test_response_replay_is_rejected(setup):
    _, _, _, signer, observer = setup
    retained = []
    def encode(envelope):
        if not retained:
            retained.append(canonical(envelope))
        return retained[0]
    signer.encode = encode
    observer()
    denied(observer)
    assert len(signer.calls) == 2


def test_duplicate_observation_id_with_fresh_signature_rejected(setup):
    _, _, _, signer, observer = setup
    value = str(uuid4())
    signer.mutate = lambda p: p.__setitem__("observation_id", value)
    observer()
    denied(observer)


def test_nonce_rng_repeat_denied_before_exchange(setup, monkeypatch):
    _, _, _, signer, observer = setup
    monkeypatch.setattr(observation.secrets, "token_hex", lambda _: "a" * 64)
    observer()
    denied(observer)
    assert len(signer.calls) == 1


@pytest.mark.parametrize("delay", [2.0, 2.1, 30.0])
def test_whole_exchange_budget(setup, delay):
    _, _, _, signer, observer = setup
    signer.delay = delay
    denied(observer)


def test_crypto_time_included(setup, monkeypatch):
    clock, _, _, _, observer = setup
    real = observation.Ed25519PublicKey
    class Slow:
        @staticmethod
        def from_public_bytes(raw):
            verifier = real.from_public_bytes(raw)
            class Wrapped:
                def verify(self, signature, data):
                    verifier.verify(signature, data)
                    clock.mono += 2
            return Wrapped()
    monkeypatch.setattr(observation, "Ed25519PublicKey", Slow)
    denied(observer)


@pytest.mark.parametrize("mono,wall", [(130, 1_800_000_000), (100, 1_800_000_030),
                                      (99, 1_800_000_000), (100, 1_799_999_999)])
def test_original_expiry_and_clock_regression(setup, mono, wall):
    clock, _, _, signer, observer = setup
    clock.mono, clock.wall = mono, wall
    denied(observer)
    assert not signer.calls


def test_reentry_caught_by_transport_still_seals(setup):
    _, _, _, signer, observer = setup
    signer.before = lambda: denied(observer)
    denied(observer)
    assert len(signer.calls) == 1


def test_close_inside_transport_seals(setup):
    _, _, _, signer, observer = setup
    signer.before = observer.close
    denied(observer)


def test_pid_check_precedes_lock(setup, monkeypatch):
    _, _, _, _, observer = setup
    class Forbidden:
        def __enter__(self):
            pytest.fail("inherited lock acquired")
    observer._lock = Forbidden()
    monkeypatch.setattr(observation.os, "getpid", lambda: observer._pid + 1)
    denied(observer)
    with pytest.raises(observation.CurrentExecutorObservationError):
        observer.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="real fork requires Linux")
def test_real_fork_denies_inherited_observer(setup):
    _, _, _, signer, observer = setup
    pid = os.fork()
    if pid == 0:
        try:
            observer()
        except observation.CurrentExecutorObservationError:
            os._exit(0)
        os._exit(1)
    assert os.waitpid(pid, 0)[1] == 0
    observer()
    assert len(signer.calls) == 1


def test_async_result_closed_without_launch(setup):
    _, release, launch, _, _ = setup
    async def wrong():
        pytest.fail("must not run")
    result = wrong()
    class Transport:
        def observe_current_launch(self, **kwargs):
            return result
    observer = observation.CurrentExecutorObserver(release=release, launch=launch, transport=Transport())
    denied(observer)
    assert result.cr_frame is None


@pytest.mark.parametrize("kind", ["async", "asyncgen", "object", "bool"])
def test_invalid_transport_before_exchange(setup, kind):
    _, release, launch, _, _ = setup
    async def call(**kwargs):
        return b""
    async def gen(**kwargs):
        yield b""
    class Transport:
        observe_current_launch = {"async": call, "asyncgen": gen,
                                  "object": object(), "bool": True}[kind]
    with pytest.raises(observation.CurrentExecutorObservationError):
        observation.CurrentExecutorObserver(release=release, launch=launch, transport=Transport())


@pytest.mark.parametrize("field,value", [("engine_pid", True), ("engine_start_ticks", 0),
    ("launch_id", "notuuid"), ("container_id", "a"), ("engine_id", "\u2603")])
def test_invalid_launch_pins(setup, field, value):
    _, _, launch, _, _ = setup
    with pytest.raises(observation.CurrentExecutorObservationError):
        replace(launch, **{field:value})


def test_defensive_copy_even_bypassed_frozen_source_mutation(setup):
    _, release, launch, signer, observer = setup
    object.__setattr__(release, "image_config_id", "sha256:" + "9" * 64)
    denied(observer)  # signer echoes mutated caller pins; retained independent copy rejects.


def test_bounded_number_of_observations(setup, monkeypatch):
    _, _, _, signer, observer = setup
    monkeypatch.setattr(observation, "MAX_OBSERVATIONS", 1)
    observer()
    denied(observer)
    assert len(signer.calls) == 1


@pytest.mark.parametrize("field,value", [("public_key", b"short"), ("issuer_id", "\u2603"),
    ("implementation_commit", "bad"), ("runnable_digest", "bad"),
    ("image_config_id", True), ("platform", {"os": "linux"})])
def test_invalid_release_pins(setup, field, value):
    _, release, _, _, _ = setup
    with pytest.raises(observation.CurrentExecutorObservationError):
        replace(release, **{field:value})


@pytest.mark.parametrize("option,value", [("exchange_timeout_ms", True),
    ("exchange_timeout_ms", 0), ("exchange_timeout_ms", 10001),
    ("max_observation_lifetime_ms", 99), ("max_observation_lifetime_ms", 10001)])
def test_invalid_budgets(setup, option, value):
    _, release, launch, signer, _ = setup
    with pytest.raises(observation.CurrentExecutorObservationError):
        observation.CurrentExecutorObserver(release=release, launch=launch,
                                             transport=signer, **{option:value})
    assert not signer.calls


@pytest.mark.parametrize("remaining", [0, -1, 3_600_001])
def test_constructor_original_approval_ceiling(setup, remaining):
    clock, release, launch, signer, _ = setup
    launch = replace(launch, approval_expires_at_ms=int(clock.wall * 1000) + remaining)
    with pytest.raises(observation.CurrentExecutorObservationError):
        observation.CurrentExecutorObserver(release=release, launch=launch, transport=signer)
    assert not signer.calls


def test_short_remaining_approval_clips_transport_budget(setup):
    clock, release, launch, signer, _ = setup
    launch = replace(launch, approval_expires_at_ms=int(clock.wall * 1000) + 500)
    signer.launch = launch
    observer = observation.CurrentExecutorObserver(release=release, launch=launch, transport=signer)
    observer()
    assert signer.calls[0][1] <= 500
    clock.mono += 0.5  # Wall clock freezing cannot renew the fixed local ceiling.
    denied(observer)
    assert len(signer.calls) == 1


def test_concurrent_calls_serialize_not_reenter(setup):
    _, _, _, signer, observer = setup
    entered, release = threading.Event(), threading.Event()
    errors, results = [], []
    def block():
        entered.set()
        assert release.wait(5)
    signer.before = block
    def run():
        try:
            results.append(observer())
        except BaseException as error:
            errors.append(error)
    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    try:
        assert entered.wait(5)
        second.start()
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors and len(results) == 2 and len(signer.calls) == 2


def test_transport_exception_sanitized_and_never_retried(setup):
    _, _, _, signer, observer = setup
    def fail():
        raise RuntimeError("sensitive transport internals")
    signer.before = fail
    denied(observer)
    denied(observer)
    assert len(signer.calls) == 1


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_transport_baseexception_preserved_but_observer_sealed(setup, signal):
    _, _, _, signer, observer = setup
    def fail():
        raise signal()
    signer.before = fail
    with pytest.raises(signal):
        observer()
    denied(observer)
    assert len(signer.calls) == 1


def test_transport_cancelled_error_preserved_but_sealed(setup):
    import asyncio
    _, _, _, signer, observer = setup
    def fail():
        raise asyncio.CancelledError()
    signer.before = fail
    with pytest.raises(asyncio.CancelledError):
        observer()
    denied(observer)


@pytest.mark.parametrize("offset", [1, 1000, 5000])
def test_positive_issuer_clock_skew_within_bound(setup, offset):
    _, _, _, signer, observer = setup
    def shift(payload):
        payload["observed_at_ms"] += offset
        payload["expires_at_ms"] += offset
    signer.mutate = shift
    observer()


def test_mutex_wait_is_in_whole_call_deadline(setup):
    clock, _, _, signer, observer = setup
    class Lock:
        def acquire(self, *, timeout):
            assert 0 < timeout <= 2
            clock.mono += 2
            return True
        def release(self):
            pass
    observer._lock = Lock()
    denied(observer)
    assert not signer.calls


def test_mutex_timeout_seals(setup):
    _, _, _, signer, observer = setup
    class Lock:
        def acquire(self, *, timeout):
            return False
        def release(self):
            pytest.fail("must not release unowned lock")
    observer._lock = Lock()
    denied(observer)
    denied(observer)
    assert not signer.calls


def test_returned_future_is_not_cancelled_or_adopted(setup):
    import asyncio
    _, release, launch, _, _ = setup
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        class Transport:
            def observe_current_launch(self, **kwargs):
                return future
        observer = observation.CurrentExecutorObserver(release=release, launch=launch,
                                                       transport=Transport())
        denied(observer)
        assert not future.done()
    finally:
        loop.close()


def test_core_import_does_not_load_optional_executor():
    script = '''
import importlib.abc, sys
class DenyOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("hub_container_executor"):
            raise AssertionError("optional executor imported")
sys.meta_path.insert(0, DenyOptional())
from engineering_hub.current_executor_observation import CurrentExecutorObserver
assert not any(n.startswith("hub_container_executor") for n in sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
