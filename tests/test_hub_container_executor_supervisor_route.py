"""Engine-free evidence for the disabled-by-default supervisor route foundation."""
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import pickle
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from engineering_hub.container_pilot_client import ContainerPilotResponse
from engineering_hub.container_pilot_manifest import Platform
from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins
from hub_container_executor import supervisor_route as route_module
from hub_container_executor.supervisor_route import (
    BackendPin, BackendReply, ObservedPeer, PeerMismatch, RouteCommand, RouteEpoch, SupervisorRoute,
    SupervisorRouteConfig, SupervisorRouteDisabled, SupervisorRouteError, SupervisorRouteRefused,
    SupervisorRouteRejected, UnixSocketBackend, SUBMIT_PATH, SIGNATURE_DOMAIN, binding_digest,
    binding_for, observation_nonce, verify_route_record,
)

LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Actual private Linux journal ownership")
BOOT = str(uuid4())
LAUNCH = str(uuid4())
TOKEN = "route-test-bearer-" + "Zq7" * 10


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def request_dict(command_id, fence, run_id=None):
    return {"schema_version": "container_pilot_v1", "command_id": command_id,
            "run_id": run_id or "11111111-1111-4111-8111-111111111111",
            "target_id": "engineering-hub:staging", "action": "deploy_container",
            "manifest_digest": "sha256:" + "5" * 64, "candidate_commit": "d" * 40,
            "policy_revision": 3, "fencing_generation": fence}


def echo(request):
    return {**request, "state": "pending", "reason": None, "replayed": False,
            "evidence": {"container_id": None, "image_config_id": None, "health": "unknown",
                         "evidence_verified": False}}


class Backend:
    def __init__(self, pin):
        self.calls = []
        self.peer = ObservedPeer(pin.host_pid, pin.uid, pin.gid, pin.process_start_ticks)
        self.status, self.body, self.error, self.after = 200, None, None, None

    def forward(self, *, method, path, body, timeout_ms, pin):
        self.calls.append((method, path, body))
        if self.after is not None:
            self.after()
        if self.error is not None:
            raise self.error
        raw = self.body if self.body is not None else canonical(echo(json.loads(body)))
        return BackendReply(self.status, raw, self.peer)


class Epochs:
    def __init__(self, epoch, pin):
        self.value = (epoch, pin)

    def __call__(self):
        return self.value


class Crash(BaseException):
    """Simulated process death after the effect, before any outcome write."""


@pytest.fixture
def world(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    key = Ed25519PrivateKey.generate()
    release = CurrentExecutorReleasePins(
        "existing-issuer", key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        "c" * 40, "sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64,
        Platform(os="linux", architecture="amd64", variant=None), "sha256:" + "4" * 64)
    pins = CurrentLaunchPins(LAUNCH, BOOT, 4321, 98765, "a" * 64, "engine:pinned", 500, 123, 2**40)
    pin = BackendPin.from_launch_pins(pins, uid=10001, gid=10001)
    epoch = RouteEpoch(LAUNCH, 5, BOOT)
    backend = Backend(pin)
    epochs = Epochs(epoch, pin)
    state = {"key": key, "release": release, "pin": pin, "epoch": epoch, "backend": backend,
             "epochs": epochs, "path": str(private / "route.sqlite")}

    def make(create=True, timeout_ms=2000, **overrides):
        config = SupervisorRouteConfig(enabled=True, journal_path=state["path"], create_journal=create,
                                       allow_ephemeral_test_storage=True, forward_timeout_ms=timeout_ms)
        args = dict(config=config, release=release, signing_key=key, epoch_source=epochs, backend=backend)
        args.update(overrides)
        return SupervisorRoute(**args)
    state["make"] = make
    return state


def command(epoch, *, command_id=None, fence=7, body=None, inner_id=None, inner_fence=None,
            run_id=None, path=SUBMIT_PATH, method="POST"):
    command_id = command_id or str(uuid4())
    if body is None:
        body = canonical(request_dict(inner_id or command_id, inner_fence or fence, run_id))
    return RouteCommand(method, path, body, command_id, fence, epoch)


def intent_rows(path):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT command_id, state FROM supervisor_route_intent").fetchall()


def verify(world, result, cmd, *, pin=None, status=200, body=None):
    return verify_route_record(result.record, public_key=world["release"].public_key,
                               issuer_id="existing-issuer",
                               expected_binding=binding_for(cmd, pin or world["pin"]),
                               received_status=status,
                               received_body=result.response if body is None else body)


def test_disabled_by_default_refuses_construction(world):
    assert SupervisorRouteConfig().enabled is False
    base = dict(release=world["release"], signing_key=world["key"], epoch_source=world["epochs"],
                backend=world["backend"])
    for config in (SupervisorRouteConfig(), SupervisorRouteConfig(journal_path=world["path"]),
                   SupervisorRouteConfig(enabled=1, journal_path=world["path"], create_journal=True),
                   None, {"enabled": True}):
        with pytest.raises(SupervisorRouteDisabled):
            SupervisorRoute(config=config, **base)
    assert not Path(world["path"]).exists()
    assert not any(isinstance(v, SupervisorRoute) for v in vars(route_module).values())
    root = Path(__file__).parents[1]
    importers = [p for p in [*root.glob("hub_container_executor/*.py"), *root.glob("src/**/*.py")]
                 if p.name != "supervisor_route.py" and "supervisor_route" in p.read_text(encoding="utf-8")]
    assert importers == []  # Not wired into any service or composition.
    assert world["backend"].calls == []


@LINUX
def test_import_is_inert(tmp_path):
    script = r'''
import os, pathlib, socket, sqlite3, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'src'))
def forbidden(*a, **k):
    raise AssertionError('effect at import')
sqlite3.connect = socket.socket.bind = socket.socket.connect = os.open = os.mkdir = forbidden
import hub_container_executor.supervisor_route
print('inert')
'''
    result = subprocess.run([sys.executable, "-c", script, str(Path(__file__).parents[1])], cwd=tmp_path,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0 and result.stdout.strip() == "inert"
    assert not list(tmp_path.iterdir())


@LINUX
def test_completed_record_signed_over_full_binding_and_idempotent_replay(world):
    route = world["make"]()
    cmd = command(world["epoch"])
    result = route.submit(cmd)
    assert result.state == "completed"
    payload = verify(world, result, cmd)
    assert payload["outcome"] == "accepted"
    assert payload["binding"]["body_sha256"] == hashlib.sha256(cmd.body).hexdigest()
    assert payload["binding"]["epoch"] == {"launch_id": LAUNCH, "launch_counter": 5, "boot_id": BOOT}
    assert payload["binding"]["backend"]["container_id"] == "a" * 64
    assert payload["observation_nonce"] == observation_nonce(binding_digest(binding_for(cmd, world["pin"])))
    with pytest.raises(SupervisorRouteError):
        verify(world, result, command(world["epoch"], command_id=cmd.command_id, fence=8))
    again = route.submit(cmd)
    assert again.state == "completed" and again.replayed and again.record == result.record
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
@pytest.mark.parametrize("mismatch", ["command_id", "fence"])
def test_outer_ids_must_match_exact_body_before_any_journal_write(world, mismatch):
    route = world["make"]()
    outer = str(uuid4())
    if mismatch == "command_id":
        cmd = command(world["epoch"], command_id=outer, inner_id=str(uuid4()))
    else:
        cmd = command(world["epoch"], command_id=outer, fence=7, inner_fence=9)
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.submit(cmd)
    assert raised.value.reason == mismatch + "_mismatch"
    assert route.inspect(outer) is None and intent_rows(world["path"]) == []
    for bad in (command(world["epoch"], body=b'{"command_id":"x"}'),
                command(world["epoch"], method="GET", body=b"", path=SUBMIT_PATH + "/" + outer),
                command(world["epoch"], path=SUBMIT_PATH + "/" + outer + "/reconcile", body=b"")):
        with pytest.raises(SupervisorRouteRejected):
            route.submit(bad)
    assert intent_rows(world["path"]) == [] and world["backend"].calls == []
    route.close()


@LINUX
def test_mismatched_observer_and_command_backend_is_held(world):
    route = world["make"]()
    pin = world["pin"]
    world["backend"].peer = ObservedPeer(pin.host_pid + 1, pin.uid, pin.gid, pin.process_start_ticks)
    cmd = command(world["epoch"])
    result = route.submit(cmd)
    assert result.state == "held" and result.error == "backend_identity_mismatch" and result.record is None
    assert route.inspect(cmd.command_id).state == "held"
    assert route.submit(cmd).state == "held"
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
def test_peer_mismatch_before_send_is_refused_terminally(world):
    route = world["make"]()
    world["backend"].error = PeerMismatch()
    cmd = command(world["epoch"])
    result = route.submit(cmd)
    assert result.state == "refused" and result.error == "backend_identity_mismatch"
    world["backend"].error = None
    assert route.submit(cmd).state == "refused"  # Not redispatched under the same command_id.
    assert route.inspect(cmd.command_id).state == "refused"
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
@pytest.mark.parametrize("lost", ["crash", "connection_reset"])
def test_restart_with_lost_response_replays_held_without_second_forward(world, lost):
    route = world["make"]()
    cmd = command(world["epoch"])
    if lost == "crash":
        world["backend"].error = Crash()
        with pytest.raises(Crash):
            route.submit(cmd)
    else:
        world["backend"].error = ConnectionResetError()
        assert route.submit(cmd).state == "held"
    assert len(world["backend"].calls) == 1
    route.close()  # Process "restarts" with the same durable journal.
    world["backend"].error = None
    restarted = world["make"](create=False)
    assert restarted.inspect(cmd.command_id).state == "held"
    replay = restarted.submit(cmd)
    assert replay.state == "held" and replay.replayed
    assert replay.error == ("interrupted" if lost == "crash" else "transport_lost")
    assert len(world["backend"].calls) == 1
    restarted.close()


@LINUX
@pytest.mark.parametrize("change", ["body", "fence"])
def test_conflicting_replay_rejected_without_forward(world, change):
    route = world["make"]()
    cmd = command(world["epoch"])
    assert route.submit(cmd).state == "completed"
    variant = {"body": dict(run_id=str(uuid4())), "fence": dict(fence=8)}[change]
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.submit(command(world["epoch"], command_id=cmd.command_id, **variant))
    assert raised.value.reason == "binding_conflict"
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
def test_journal_write_failure_before_forward_refuses(world, monkeypatch):
    route = world["make"]()
    # Simulated disk/open failure on every journal connection.
    monkeypatch.setattr(route._journal, "_connection", lambda **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(SupervisorRouteRefused):
        route.submit(command(world["epoch"]))
    monkeypatch.undo()
    # Real lock held by another writer beyond the bounded busy wait.
    blocker = sqlite3.connect(world["path"], isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SupervisorRouteRefused):
            route.submit(command(world["epoch"]))
    finally:
        blocker.rollback()
        blocker.close()
    assert world["backend"].calls == []
    assert route.submit(command(world["epoch"])).state == "completed"  # Same route, lock released.
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
@pytest.mark.parametrize("scope", ["outcome_only", "whole_journal"])
def test_journal_failure_after_forward_is_held_not_success(world, monkeypatch, scope):
    route = world["make"]()
    journal = route._journal

    def fail(*args, **kwargs):
        raise route_module.RouteJournalError("disk")
    monkeypatch.setattr(journal, "record_outcome", fail)
    if scope == "whole_journal":
        monkeypatch.setattr(journal, "mark_unknown", fail)
    cmd = command(world["epoch"])
    result = route.submit(cmd)
    assert result.state == "held" and result.error == "outcome_unrecorded" and result.record is None
    monkeypatch.undo()
    inspected = route.inspect(cmd.command_id)
    assert inspected.state == "held"
    assert inspected.error == ("outcome_unrecorded" if scope == "outcome_only" else "interrupted")
    assert route.submit(cmd).state == "held"
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
def test_older_epoch_rejected_durably_and_never_forwarded(world):
    route = world["make"]()
    assert route.submit(command(world["epoch"])).state == "completed"
    older = RouteEpoch(LAUNCH, 4, BOOT)
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.submit(command(older))  # Hub claims an older epoch than current.
    assert raised.value.reason == "stale_epoch"
    world["epochs"].value = (older, world["pin"])  # Even a regressed epoch source is rejected.
    with pytest.raises(SupervisorRouteRejected) as raised:
        route.submit(command(older))
    assert raised.value.reason == "stale_epoch"
    route.close()
    restarted = world["make"](create=False)
    with pytest.raises(SupervisorRouteRejected):
        restarted.submit(command(older))
    same_counter_other_launch = RouteEpoch(str(uuid4()), 5, BOOT)
    world["epochs"].value = (same_counter_other_launch, world["pin"])
    with pytest.raises(SupervisorRouteRejected) as raised:
        restarted.submit(command(same_counter_other_launch))
    assert raised.value.reason == "epoch_conflict"
    assert len(world["backend"].calls) == 1
    restarted.close()


@LINUX
def test_response_classification(world):
    route = world["make"]()
    newer = RouteEpoch(str(uuid4()), 6, BOOT)
    world["backend"].after = lambda: setattr(world["epochs"], "value", (newer, world["pin"]))
    first = command(world["epoch"])
    assert route.submit(first).state == "held"
    assert route.inspect(first.command_id).error == "epoch_changed"
    world["backend"].after = None
    other = canonical(echo(request_dict(str(uuid4()), 7)))
    cases = ((200, b'{"a":1,"a":2}', "held", "malformed_response"),
             (200, b"not json", "held", "malformed_response"),
             (200, other, "held", "malformed_response"),  # Does not echo this command.
             (503, b'{"error":{"code":"command_busy"}}', "held", "backend_ambiguous"),
             (500, b'{"error":{"code":"internal_error"}}', "held", "backend_ambiguous"),
             (401, b'{"error":{"code":"invalid_payload"}}', "held", "malformed_response"),
             (401, b'{"error":{"code":"unauthorized"}}', "rejected", None),
             (403, b'{"error":{"code":"forbidden"}}', "rejected", None),
             (400, b'{"error":{"code":"invalid_payload"}}', "rejected", None))
    for status, body, state, error in cases:
        world["backend"].status, world["backend"].body = status, body
        cmd = command(newer)
        result = route.submit(cmd)
        assert (result.state, result.error) == (state, error), (status, body)
        if state == "rejected":
            payload = verify(world, result, cmd, status=status)
            assert payload["outcome"] == "rejected"
            assert route.submit(cmd).state == "rejected"  # Terminal, not held, not re-forwarded.
    assert len(world["backend"].calls) == 1 + len(cases)
    route.close()


@LINUX
def test_inspect_never_forwards_or_writes(world):
    route = world["make"]()
    unknown = str(uuid4())
    assert route.inspect(unknown) is None
    cmd = command(world["epoch"])
    route.submit(cmd)
    before = Path(world["path"]).read_bytes()
    for _ in range(3):
        assert route.inspect(cmd.command_id).state == "completed"
        assert route.inspect(unknown) is None
    assert Path(world["path"]).read_bytes() == before
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
def test_in_progress_reported_for_concurrent_identical_replay(world):
    route = world["make"]()
    cmd = command(world["epoch"])
    seen = {}
    world["backend"].after = lambda: seen.setdefault("row", route._from_row(
        route._journal.read(cmd.command_id), replayed=True))
    assert route.submit(cmd).state == "completed"
    assert seen["row"].state == "in_progress"
    route.close()


def resign(world, payload):
    signature = base64.urlsafe_b64encode(world["key"].sign(
        SIGNATURE_DOMAIN + canonical(payload))).rstrip(b"=").decode()
    return canonical({"payload": payload, "signature": signature})


@LINUX
def test_verifier_enforces_shape_hash_status_and_pinned_peer(world):
    route = world["make"]()
    cmd = command(world["epoch"])
    result = route.submit(cmd)
    genuine = verify(world, result, cmd)
    route.close()

    def args(**kwargs):
        values = dict(public_key=world["release"].public_key, issuer_id="existing-issuer",
                      expected_binding=binding_for(cmd, world["pin"]), received_status=200,
                      received_body=result.response)
        values.update(kwargs)
        return values

    def check(raw, **kwargs):
        with pytest.raises(SupervisorRouteError):
            verify_route_record(raw, **args(**kwargs))
    assert verify_route_record(resign(world, genuine), **args()) == genuine  # Re-sign baseline is valid.
    check(resign(world, {**genuine, "extra": 1}))  # Extra key.
    check(resign(world, {k: v for k, v in genuine.items() if k != "recorded_at_ms"}))  # Missing key.
    check(resign(world, {**genuine, "binding": {**genuine["binding"], "extra": 1}}))
    check(resign(world, {**genuine, "backend_status": "200"}))  # Wrong type.
    check(resign(world, {**genuine, "observed_peer": {**genuine["observed_peer"], "extra": 0}}))
    envelope = json.loads(result.record)
    check(canonical({**envelope, "extra": 1}))
    check(result.record, received_body=result.response + b" ")  # Tampered response bytes.
    check(resign(world, {**genuine, "response_sha256": "0" * 64}))  # Tampered recorded hash.
    check(result.record, received_status=201)  # Status mismatch.
    check(resign(world, {**genuine, "backend_status": 201}))
    check(resign(world, {**genuine, "outcome": "rejected"}))
    wrong_peer = {**genuine["observed_peer"], "pid": genuine["observed_peer"]["pid"] + 1}
    check(resign(world, {**genuine, "observed_peer": wrong_peer}))  # Observed peer != pinned.
    other_pin = BackendPin("b" * 64, 4321, 98765, 10001, 10001, "engine:pinned", 500, 123)
    check(result.record, expected_binding=binding_for(cmd, other_pin))
    other_key = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    check(result.record, public_key=other_key)


def _self_ticks():
    raw = Path("/proc/self/stat").read_text()
    return int(raw[raw.rfind(")") + 2:].split()[19])


def _own_pin(ticks_offset=0):
    return BackendPin("a" * 64, os.getpid(), _self_ticks() + ticks_offset, os.getuid(), os.getgid(),
                      "engine:pinned", 500, 123)


_COUNTING_SERVER = r'''
import socket, sys
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[1]); listener.listen(1)
print('ready', flush=True)
peer, _ = listener.accept(); peer.settimeout(10)
total = 0
while True:
    part = peer.recv(65536)
    if not part:
        break
    total += len(part)
print(total, flush=True)
'''


@LINUX
@pytest.mark.parametrize("wrong", ["start_ticks_same_process", "different_process"])
def test_wrong_peer_receives_zero_bytes(world, wrong):
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "b.sock")
    received = []
    server = thread = process = None
    try:
        if wrong == "different_process":
            process = subprocess.Popen([sys.executable, "-c", _COUNTING_SERVER, path], stdout=subprocess.PIPE,
                                       text=True)
            assert process.stdout.readline().strip() == "ready"
            pin = _own_pin()  # Exact pin for THIS process; the listener is another PID.
        else:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)

            def serve():
                peer, _ = server.accept()
                peer.settimeout(10)
                total = 0
                while True:
                    part = peer.recv(65536)
                    if not part:
                        break
                    total += len(part)
                received.append(total)
                peer.close()
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            pin = _own_pin(ticks_offset=1)
        world["epochs"].value = (world["epoch"], pin)
        route = world["make"](backend=UnixSocketBackend(path, bearer_token=TOKEN))
        cmd = command(world["epoch"])
        result = route.submit(cmd)
        assert result.state == "refused" and result.error == "backend_identity_mismatch"
        assert route.inspect(cmd.command_id).state == "refused"
        route.close()
        if process is not None:
            out, _ = process.communicate(timeout=15)
            received.append(int(out.strip()))
        else:
            thread.join(10)
        assert received == [0]  # One connection, zero request bytes, so zero requests handled.
    finally:
        if process is not None and process.poll() is None:
            process.kill()
        if server is not None:
            server.close()
        if os.path.exists(path):
            os.unlink(path)
        os.rmdir(directory)


class _Adapter:
    def __init__(self):
        self.submitted = []

    def submit(self, request):
        self.submitted.append(request)
        return ContainerPilotResponse.model_validate(echo(request.model_dump(mode="json")))

    def get(self, command_id):
        return None

    def reconcile(self, command_id):
        return None


@LINUX
def test_real_pilot_asgi_bearer_classification_and_token_never_recorded(world, caplog):
    uvicorn = pytest.importorskip("uvicorn")
    from hub_container_executor.asgi import create_app
    caplog.set_level(logging.DEBUG)
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "executor.sock")
    adapter = _Adapter()
    server = uvicorn.Server(uvicorn.Config(create_app(adapter, bearer_token=TOKEN), uds=path,
                                           lifespan="off", log_level="debug", access_log=True))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.025)
        assert server.started
        world["epochs"].value = (world["epoch"], _own_pin())
        good = UnixSocketBackend(path, bearer_token=TOKEN)
        first_journal = world["path"]
        route = world["make"](backend=good)
        cmd = command(world["epoch"])
        accepted = route.submit(cmd)
        assert accepted.state == "completed" and len(adapter.submitted) == 1
        assert verify(world, accepted, cmd, pin=_own_pin())["outcome"] == "accepted"
        route_repr = repr(route)
        route.close()
        world["path"] = first_journal + "-2"
        route = world["make"](backend=UnixSocketBackend(path, bearer_token="wrong-" + TOKEN))
        denied = command(world["epoch"])
        rejected = route.submit(denied)
        assert rejected.state == "rejected" and rejected.error is None
        assert verify(world, rejected, denied, pin=_own_pin(), status=401)["outcome"] == "rejected"
        assert route.submit(denied).state == "rejected" and len(adapter.submitted) == 1
        route.close()
        missing = UnixSocketBackend(os.path.join(directory, "absent.sock"), bearer_token=TOKEN)
        with pytest.raises(SupervisorRouteError) as raised:
            missing.forward(method="POST", path=SUBMIT_PATH, body=cmd.body, timeout_ms=500, pin=_own_pin())
        chain, error = [], raised.value
        while error is not None:
            chain += [str(error), repr(error), repr(error.args)]
            error = error.__cause__ or error.__context__
        stored = b"".join(Path(p).read_bytes() for p in (first_journal, world["path"]))
        texts = [*chain, repr(good), str(good), route_repr, repr(accepted), repr(rejected),
                 accepted.record.decode(), rejected.record.decode(), caplog.text,
                 stored.decode("latin-1")]
        assert all(TOKEN not in text for text in texts)
        with pytest.raises(TypeError):
            pickle.dumps(good)
    finally:
        server.should_exit = True
        thread.join(10)
        if os.path.exists(path):
            os.unlink(path)
        os.rmdir(directory)


class _ScriptedServer:
    """In-process (pinnable PID) raw HTTP peer; one script per connection."""

    def __init__(self, path, scripts):
        self.scripts, self.threads = list(scripts), []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(4)
        self.acceptor = threading.Thread(target=self._accept, daemon=True)
        self.acceptor.start()

    def _accept(self):
        while self.scripts:
            try:
                peer, _ = self.listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(peer, self.scripts.pop(0)), daemon=True)
            thread.start()
            self.threads.append(thread)

    @staticmethod
    def _serve(peer, script):
        try:
            peer.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                data += peer.recv(65536)
            head, body = data.split(b"\r\n\r\n", 1)
            length = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")][0]
                         .split(b":")[1])
            while len(body) < length:
                body += peer.recv(65536)
            for delay, chunk in script(body):
                time.sleep(delay)
                peer.sendall(chunk)
        except OSError:
            pass
        finally:
            peer.close()

    def close(self):
        self.listener.close()
        for thread in self.threads:
            thread.join(10)


def _good(body):
    raw = canonical(echo(json.loads(body)))
    return [(0, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(raw) + raw)]


def _framed(headers, payload=None):
    def script(body):
        raw = canonical(echo(json.loads(body))) if payload is None else payload
        head = b"HTTP/1.1 200 OK\r\n" + b"".join(h.replace(b"{n}", b"%d" % len(raw)) + b"\r\n" for h in headers)
        return [(0, head + b"\r\n" + raw)]
    return script


def _trickle_headers(body):
    return [(0, b"HTTP/1.1 200 OK\r\nX-Slow: ")] + [(0.05, b"a")] * 60


def _trickle_body(body):
    raw = canonical(echo(json.loads(body)))
    return ([(0, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(raw))]
            + [(0.05, raw[i:i + 1]) for i in range(len(raw))])


def _stall_headers(body):
    return [(0, b"HTTP/1.1 200 OK\r\nContent-Le"), (3, b"")]


def _stall_body(body):
    raw = canonical(echo(json.loads(body)))
    return [(0, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(raw) + raw[:5]), (3, b"")]


def _short(body):
    raw = canonical(echo(json.loads(body)))
    return [(0, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % (len(raw) + 10) + raw)]


def _long(body):
    raw = canonical(echo(json.loads(body)))
    return [(0, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(raw) + raw + b"   ")]


def _open_after(body):
    return _good(body)[:1] + [(3, b"")]  # Never closes: no EOF within the deadline.


_BAD_RESPONSES = {
    "trickle_headers": _trickle_headers,
    "trickle_body": _trickle_body,
    "stalled_headers": _stall_headers,
    "stalled_body": _stall_body,
    "duplicate_content_length": _framed([b"Content-Length: {n}", b"Content-Length: {n}"]),
    "non_numeric_content_length": _framed([b"Content-Length: abc"]),
    "negative_content_length": _framed([b"Content-Length: -1"]),
    "oversized_content_length": _framed([b"Content-Length: %d" % (route_module.MAX_RESPONSE_BYTES + 1)]),
    "missing_content_length": _framed([]),
    "transfer_encoding": _framed([b"Content-Length: {n}", b"Transfer-Encoding: chunked"]),
    "body_shorter": _short,
    "body_longer": _long,
    "no_eof": _open_after,
    "header_count": _framed([b"Content-Length: {n}"] + [b"X-H%d: v" % i for i in range(40)]),
    "header_bytes": _framed([b"Content-Length: {n}", b"X-Big: " + b"v" * 9000]),
    "rejection_wrong_body": _framed([b"Content-Length: {n}"], b'{"error":{"code":"other"}}'),
}


@LINUX
@pytest.mark.parametrize("case", sorted(_BAD_RESPONSES))
def test_bounded_strict_response_framing_is_held_and_releases_lock(world, case):
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "s.sock")
    server = _ScriptedServer(path, [_BAD_RESPONSES[case], _good])
    try:
        world["epochs"].value = (world["epoch"], _own_pin())
        route = world["make"](backend=UnixSocketBackend(path, bearer_token=TOKEN), timeout_ms=600)
        cmd = command(world["epoch"])
        started = time.monotonic()
        result = route.submit(cmd)
        elapsed = time.monotonic() - started
        assert result.state == "held" and result.record is None, case
        assert elapsed < 0.6 + 0.4, (case, elapsed)  # Total deadline plus small slack.
        assert route._mutex.acquire(blocking=False)
        route._mutex.release()
        assert route.submit(cmd).state == "held"  # Never redispatched.
        second = route.submit(command(world["epoch"]))
        assert second.state == "completed"  # Lock released; next command proceeds.
        route.close()
    finally:
        server.close()
        os.unlink(path)
        os.rmdir(directory)


@LINUX
def test_strict_reader_accepts_exact_framing_and_exact_rejection(world):
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "s.sock")
    rejection = b'{"error":{"code":"unauthorized"}}'
    server = _ScriptedServer(path, [_good, lambda body: [(0, b"HTTP/1.1 401 Unauthorized\r\n"
                                    b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                                    % len(rejection) + rejection)]])
    try:
        world["epochs"].value = (world["epoch"], _own_pin())
        route = world["make"](backend=UnixSocketBackend(path, bearer_token=TOKEN), timeout_ms=600)
        assert route.submit(command(world["epoch"])).state == "completed"
        assert route.submit(command(world["epoch"])).state == "rejected"
        route.close()
    finally:
        server.close()
        os.unlink(path)
        os.rmdir(directory)


class _Blocking(Backend):
    """Fake backend that blocks mid-send until released; counts calls."""

    def __init__(self, pin):
        super().__init__(pin)
        self.entered, self.release = threading.Event(), threading.Event()
        self.interrupt = None

    def forward(self, **kwargs):
        if not self.calls:
            self.calls.append((kwargs["method"], kwargs["path"], kwargs["body"]))
            self.entered.set()
            assert self.release.wait(10)
            if self.interrupt is not None:
                raise self.interrupt
            self.calls.pop()
        return super().forward(**kwargs)


def test_busy_wait_is_validated(world):
    for value in (-1, 51, True, 1.0):
        config = SupervisorRouteConfig(enabled=True, journal_path=world["path"], create_journal=True,
                                       allow_ephemeral_test_storage=True, busy_wait_ms=value)
        with pytest.raises(SupervisorRouteError):
            SupervisorRoute(config=config, release=world["release"], signing_key=world["key"],
                            epoch_source=world["epochs"], backend=world["backend"])
    assert not Path(world["path"]).exists()


@LINUX
@pytest.mark.parametrize("busy_wait_ms", [0, 20])
def test_busy_admission_refuses_fast_without_effect_and_close_refuses_in_flight(world, busy_wait_ms):
    backend = _Blocking(world["pin"])
    config = SupervisorRouteConfig(enabled=True, journal_path=world["path"], create_journal=True,
                                   allow_ephemeral_test_storage=True, busy_wait_ms=busy_wait_ms)
    epoch_calls = []

    def epochs():
        epoch_calls.append(1)
        return world["epochs"]()
    route = SupervisorRoute(config=config, release=world["release"], signing_key=world["key"],
                            epoch_source=epochs, backend=backend)
    first, other = command(world["epoch"]), command(world["epoch"])
    results = {}
    worker = threading.Thread(target=lambda: results.setdefault("first", route.submit(first)))
    worker.start()
    try:
        assert backend.entered.wait(10)
        calls_before = len(epoch_calls)
        started = time.monotonic()
        busy = route.submit(other)
        assert time.monotonic() - started < busy_wait_ms / 1000 + 0.2
        assert busy.state == "busy" and busy.binding_digest is None and busy.record is None
        assert len(epoch_calls) == calls_before  # Not even the epoch source was consulted.
        assert intent_rows(world["path"]) == [(first.command_id, "intent")]  # No intent for `other`.
        assert len(backend.calls) == 1  # Zero bytes for the refused command.
        replay = route.submit(first)
        assert replay.state == "in_progress"  # Identical replay of the in-flight command.
        with pytest.raises(SupervisorRouteRejected) as raised:
            route.submit(command(world["epoch"], command_id=first.command_id, fence=8))
        assert raised.value.reason == "binding_conflict"
        with pytest.raises(route_module.SupervisorRouteInFlight) as refused:
            route.close()
        assert refused.value.command_id == first.command_id
    finally:
        backend.release.set()
        worker.join(10)
    assert results["first"].state == "completed"
    retried = route.submit(other)  # Same command_id, now admitted normally.
    assert retried.state == "completed" and not retried.replayed
    assert route.submit(other).state == "completed" and len(backend.calls) == 2
    route.close()
    with pytest.raises(SupervisorRouteError):
        route.submit(command(world["epoch"]))


@LINUX
def test_interrupt_of_submitting_thread_records_unknown_before_release(world):
    backend = _Blocking(world["pin"])
    backend.interrupt = KeyboardInterrupt()
    backend.release.set()
    route = world["make"](backend=backend)
    cmd = command(world["epoch"])
    with pytest.raises(KeyboardInterrupt):
        route.submit(cmd)
    assert intent_rows(world["path"]) == [(cmd.command_id, "unknown")]
    held = route.inspect(cmd.command_id)
    assert held.state == "held" and held.error == "interrupted"
    assert route.submit(cmd).state == "held"  # Unknown stays held; never re-forwarded.
    assert len(backend.calls) == 1
    assert route.submit(command(world["epoch"])).state == "completed"  # Mutex released afterwards.
    route.close()


@LINUX
def test_asyncio_cancellation_does_not_release_ownership_early(world):
    import asyncio
    backend = _Blocking(world["pin"])
    route = world["make"](backend=backend)
    cmd, other = command(world["epoch"]), command(world["epoch"])

    async def scenario():
        task = asyncio.create_task(asyncio.to_thread(route.submit, cmd))
        assert await asyncio.to_thread(backend.entered.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Caller gave up, but the admitted operation still owns the route.
        assert route.submit(other).state == "busy"
        assert route.submit(cmd).state == "in_progress"
        with pytest.raises(route_module.SupervisorRouteInFlight):
            route.close()
        assert route.inspect(cmd.command_id).state == "in_progress"
        # Release before asyncio.run joins its default executor thread.
        backend.release.set()
    started = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - started < 8
    assert route._mutex.acquire(timeout=10)  # Released only after the outcome is recorded.
    route._mutex.release()
    assert route.inspect(cmd.command_id).state == "completed"
    assert route.submit(other).state == "completed"
    route.close()


@LINUX
@pytest.mark.parametrize("fault", [OSError, KeyboardInterrupt])
def test_fault_right_after_intent_commit_records_unknown_before_release(world, monkeypatch, fault):
    route = world["make"]()
    journal = route._journal
    original_admit, original_mark = journal.admit, journal.abandon_attempt
    locked_while_marking = []

    def admit_then_fault(*args, **kwargs):
        result = original_admit(*args, **kwargs)  # Durable intent COMMITTED here.
        raise fault()  # E.g. parent fsync failure / interrupt before any connect.

    def mark(*args, **kwargs):
        locked_while_marking.append(route._mutex.locked())
        return original_mark(*args, **kwargs)
    monkeypatch.setattr(journal, "admit", admit_then_fault)
    monkeypatch.setattr(journal, "abandon_attempt", mark)
    cmd = command(world["epoch"])
    with pytest.raises(SupervisorRouteRefused if fault is OSError else KeyboardInterrupt):
        route.submit(cmd)
    monkeypatch.undo()
    assert locked_while_marking == [True]  # Recorded before the mutex was released.
    assert intent_rows(world["path"]) == [(cmd.command_id, "unknown")]
    assert route.inspect(cmd.command_id).state == "held"
    assert route.submit(cmd).state == "held"  # Identical replay held; never forwarded.
    assert world["backend"].calls == []
    assert route.submit(command(world["epoch"])).state == "completed"
    route.close()


@LINUX
def test_inspect_and_close_concurrent_with_in_flight_submit(world):
    backend = _Blocking(world["pin"])
    route = world["make"](backend=backend)
    cmd = command(world["epoch"])
    results = {}
    worker = threading.Thread(target=lambda: results.setdefault("r", route.submit(cmd)))
    worker.start()
    try:
        assert backend.entered.wait(10)
        before = Path(world["path"]).read_bytes()
        started = time.monotonic()
        for _ in range(5):
            assert route.inspect(cmd.command_id).state == "in_progress"
            assert route.inspect(str(uuid4())) is None
        with pytest.raises(route_module.SupervisorRouteInFlight) as refused:
            route.close()
        assert refused.value.command_id == cmd.command_id
        assert time.monotonic() - started < 2
        assert Path(world["path"]).read_bytes() == before  # inspect/close wrote nothing.
        assert route._mutex.locked() and worker.is_alive()  # Ownership untouched.
    finally:
        backend.release.set()
        worker.join(10)
    assert results["r"].state == "completed"
    route.close()


@LINUX
def test_provisional_in_progress_before_intent_does_not_stick(world):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def epochs():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
            raise RuntimeError("epoch source failed before intent")
        return world["epochs"]()
    route = world["make"](epoch_source=epochs)
    cmd = command(world["epoch"])
    errors = []

    def first():
        try:
            route.submit(cmd)
        except SupervisorRouteRefused as error:
            errors.append(error)
    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert entered.wait(10)
        provisional = route.submit(cmd)
        assert provisional.state == "in_progress" and provisional.binding_digest is None
        assert route.inspect(cmd.command_id) is None  # No durable intent yet.
    finally:
        release.set()
        worker.join(10)
    assert len(errors) == 1 and intent_rows(world["path"]) == [] and world["backend"].calls == []
    later = route.submit(cmd)
    assert later.state == "completed" and not later.replayed
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
def test_forked_child_close_refused_without_touching_mutex(world):
    route = world["make"]()
    assert route._mutex.acquire(blocking=False)  # Child inherits a LOCKED mutex.
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            route.close()
        except route_module.SupervisorRouteInFlight:
            code = 2  # Would mean it touched the inherited mutex.
        except SupervisorRouteError as error:
            code = 0 if "constructing process" in str(error) else 3
        except BaseException:
            code = 4
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    route._mutex.release()
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert route.submit(command(world["epoch"])).state == "completed"  # Parent still usable.
    route.close()


def full_rows(path):
    with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT * FROM supervisor_route_intent ORDER BY command_id").fetchall()


def _admit_raises(monkeypatch, journal, fault=OSError):
    original = journal.admit

    def admit_then_fault(*args, **kwargs):
        original(*args, **kwargs)
        raise fault()
    monkeypatch.setattr(journal, "admit", admit_then_fault)


@LINUX
@pytest.mark.parametrize("failure", ["admit_raises", "fsync_raises"])
def test_failing_replay_never_mutates_pre_existing_intent(world, monkeypatch, failure):
    route = world["make"]()
    cmd = command(world["epoch"])
    # Leave a genuine ambiguous 'intent' row: forward lost, unknown not recordable.
    world["backend"].error = ConnectionResetError()
    monkeypatch.setattr(route._journal, "mark_unknown", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert route.submit(cmd).state == "held"
    monkeypatch.undo()
    world["backend"].error = None
    before = full_rows(world["path"])
    assert [r[3] for r in before] == ["intent"] and before[0][8] is not None
    if failure == "admit_raises":
        _admit_raises(monkeypatch, route._journal)
    else:
        monkeypatch.setattr(route_module.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError()))
    with pytest.raises(SupervisorRouteRefused):
        route.submit(cmd)
    monkeypatch.undo()
    assert full_rows(world["path"]) == before  # Byte-for-byte, same state.
    replay = route.submit(cmd)
    assert replay.state == "held" and full_rows(world["path"]) == before
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
def test_fresh_failed_admit_marks_only_its_own_row(world, monkeypatch):
    route = world["make"]()
    prior = command(world["epoch"])
    world["backend"].error = ConnectionResetError()
    monkeypatch.setattr(route._journal, "mark_unknown", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert route.submit(prior).state == "held"  # Prior row left in 'intent'.
    monkeypatch.undo()
    world["backend"].error = None
    prior_row = full_rows(world["path"])[0]
    fresh = command(world["epoch"])
    _admit_raises(monkeypatch, route._journal)
    with pytest.raises(SupervisorRouteRefused):
        route.submit(fresh)
    monkeypatch.undo()
    rows = {row[0]: row for row in full_rows(world["path"])}
    assert rows[prior.command_id] == prior_row
    assert rows[fresh.command_id][3:6] == ("unknown", rows[fresh.command_id][4], "interrupted")
    assert len(world["backend"].calls) == 1
    route.close()


@LINUX
def test_recorded_and_mismatched_rows_untouched_by_failing_replay(world, monkeypatch):
    route = world["make"]()
    completed = command(world["epoch"])
    assert route.submit(completed).state == "completed"
    world["backend"].status, world["backend"].body = 401, b'{"error":{"code":"unauthorized"}}'
    rejected = command(world["epoch"])
    assert route.submit(rejected).state == "rejected"
    world["backend"].status, world["backend"].body = 200, None
    before = full_rows(world["path"])
    _admit_raises(monkeypatch, route._journal)
    for cmd in (completed, rejected):
        with pytest.raises(SupervisorRouteRefused):
            route.submit(cmd)
    with pytest.raises(SupervisorRouteRejected):  # Mismatched binding: conflict, no handler.
        route.submit(command(world["epoch"], command_id=completed.command_id, fence=8))
    monkeypatch.undo()
    assert full_rows(world["path"]) == before
    assert len(world["backend"].calls) == 2
    # The nonce is journal-internal: never in signed records or results.
    for row, cmd in zip(sorted(before), sorted((completed, rejected), key=lambda c: c.command_id)):
        attempt = row[8]
        assert attempt and len(attempt) == 32
        result = route.submit(cmd)
        assert attempt not in result.record.decode() and attempt not in repr(result)
        assert attempt not in json.dumps(json.loads(result.record))
    route.close()
