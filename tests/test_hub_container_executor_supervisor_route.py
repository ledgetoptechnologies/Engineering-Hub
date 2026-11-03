"""Engine-free evidence for the disabled-by-default supervisor route foundation."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
import socketserver
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Route journal requires actual Linux kernel ownership")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from engineering_hub.container_pilot_manifest import Platform
from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins
from hub_container_executor import supervisor_route as route_module
from hub_container_executor.supervisor_route import (
    BackendPin, BackendReply, ObservedPeer, RouteCommand, RouteEpoch, RouteJournal, SupervisorRoute,
    SupervisorRouteConfig, SupervisorRouteDisabled, SupervisorRouteRefused, SupervisorRouteRejected,
    UnixSocketBackend, binding_digest, binding_for, observation_nonce, verify_route_record,
)

LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Actual private Linux journal ownership")
PATH = "/v1/container-pilot/commands"
BOOT = str(uuid4())
LAUNCH = str(uuid4())


class Backend:
    def __init__(self, pin):
        self.calls = []
        self.peer = ObservedPeer(pin.host_pid, pin.uid, pin.gid, pin.process_start_ticks)
        self.status, self.body, self.error, self.after = 202, b'{"state":"pending"}', None, None

    def forward(self, *, method, path, body, timeout_ms):
        self.calls.append((method, path, body))
        if self.after is not None:
            self.after()
        if self.error is not None:
            raise self.error
        return BackendReply(self.status, self.body, self.peer)


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
    pins = CurrentLaunchPins(LAUNCH, BOOT, 4321, 98765, "a" * 64, "engine:pinned", 500, 123,
                             2**40)
    pin = BackendPin.from_launch_pins(pins, uid=10001, gid=10001)
    epoch = RouteEpoch(LAUNCH, 5, BOOT)
    backend = Backend(pin)
    epochs = Epochs(epoch, pin)
    state = {"key": key, "release": release, "pin": pin, "epoch": epoch, "backend": backend,
             "epochs": epochs, "path": str(private / "route.sqlite")}

    def make(create=True, **overrides):
        config = SupervisorRouteConfig(enabled=True, journal_path=state["path"], create_journal=create,
                                       allow_ephemeral_test_storage=True, forward_timeout_ms=2000)
        args = dict(config=config, release=release, signing_key=key, epoch_source=epochs, backend=backend)
        args.update(overrides)
        return SupervisorRoute(**args)
    state["make"] = make
    return state


def command(epoch, *, command_id=None, body=b'{"command":"deploy"}', fence=7, path=PATH, method="POST"):
    return RouteCommand(method, path, body, command_id or str(uuid4()), fence, epoch)


def test_disabled_by_default_refuses_construction(world, tmp_path):
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
    assert result.state == "completed" and result.response == world["backend"].body
    binding = binding_for(cmd, world["pin"])
    payload = verify_route_record(result.record, public_key=world["release"].public_key,
                                  expected_binding=binding, issuer_id="existing-issuer")
    assert payload["binding"]["body_sha256"] == __import__("hashlib").sha256(cmd.body).hexdigest()
    assert payload["binding"]["epoch"] == {"launch_id": LAUNCH, "launch_counter": 5, "boot_id": BOOT}
    assert payload["binding"]["backend"]["container_id"] == "a" * 64
    assert payload["observation_nonce"] == observation_nonce(binding_digest(binding))
    other = binding_for(command(world["epoch"], command_id=cmd.command_id, fence=8), world["pin"])
    with pytest.raises(route_module.SupervisorRouteError):
        verify_route_record(result.record, public_key=world["release"].public_key,
                            expected_binding=other, issuer_id="existing-issuer")
    again = route.submit(cmd)
    assert again.state == "completed" and again.replayed and again.record == result.record
    assert len(world["backend"].calls) == 1
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
    first = restarted.inspect(cmd.command_id)
    assert first.state == "held"
    replay = restarted.submit(cmd)
    assert replay.state == "held" and replay.replayed
    assert replay.error == ("interrupted" if lost == "crash" else "transport_lost")
    assert len(world["backend"].calls) == 1
    restarted.close()


@LINUX
@pytest.mark.parametrize("change", ["body", "fence", "path", "method"])
def test_conflicting_replay_rejected_without_forward(world, change):
    route = world["make"]()
    cmd = command(world["epoch"])
    assert route.submit(cmd).state == "completed"
    variant = {"body": dict(body=b'{"command":"other"}'), "fence": dict(fence=8),
               "path": dict(path=PATH + "/x/reconcile"), "method": dict(method="GET", body=b"")}[change]
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
    monkeypatch.setattr(journal, "record_completed", fail)
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
    # Even if the epoch source itself regresses, the durable high-water rejects.
    world["epochs"].value = (older, world["pin"])
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
def test_epoch_change_mid_command_and_malformed_reply_are_held(world):
    route = world["make"]()
    newer = RouteEpoch(str(uuid4()), 6, BOOT)
    world["backend"].after = lambda: setattr(world["epochs"], "value", (newer, world["pin"]))
    first = command(world["epoch"])
    assert route.submit(first).state == "held"
    assert route.inspect(first.command_id).error == "epoch_changed"
    world["backend"].after = None
    for body, status, error in ((b'{"a":1,"a":2}', 200, "malformed_response"),
                                (b"not json", 200, "malformed_response"),
                                (b'{"x":1}', 503, "backend_ambiguous")):
        world["backend"].body, world["backend"].status = body, status
        cmd = command(newer)
        result = route.submit(cmd)
        assert result.state == "held" and result.error == error
    assert len(world["backend"].calls) == 4
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
    world["backend"].after = lambda: seen.setdefault("row", route._held_from_row(
        route._journal.read(cmd.command_id), replayed=True))
    assert route.submit(cmd).state == "completed"
    assert seen["row"].state == "in_progress"
    route.close()


def _proc_start():
    raw = Path("/proc/self/stat").read_text()
    return int(raw[raw.rfind(")") + 2:].split()[19])


@LINUX
@pytest.mark.parametrize("pinned", ["exact", "wrong_start"])
def test_real_unix_socket_peer_identity(world, pinned):
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "b.sock")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"state":"pending"}'
            self.send_response(202)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = socketserver.UnixStreamServer(path, Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ticks = _proc_start() + (0 if pinned == "exact" else 1)
        pin = BackendPin("a" * 64, os.getpid(), ticks, os.getuid(), os.getgid(), "engine:pinned", 500, 123)
        world["epochs"].value = (world["epoch"], pin)
        route = world["make"](backend=UnixSocketBackend(path))
        result = route.submit(command(world["epoch"]))
        if pinned == "exact":
            assert result.state == "completed" and result.response == b'{"state":"pending"}'
        else:
            assert result.state == "held" and result.error == "backend_identity_mismatch"
        route.close()
    finally:
        server.shutdown()
        server.server_close()
        os.unlink(path)
        os.rmdir(directory)
