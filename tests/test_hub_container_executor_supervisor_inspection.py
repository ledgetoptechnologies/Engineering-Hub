"""Hub -> supervisor original-command inspection: authenticated, bounded, read-only."""
import base64
import dataclasses
import json
import os
from pathlib import Path
import secrets
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

from hub_container_executor import supervisor_route as route_module
from hub_container_executor.supervisor_route import (
    INSPECTION_PATH, INSPECTION_SIGNATURE_DOMAIN, MAX_RESPONSE_BYTES, BackendPin, HeldAttemptSource,
    InspectionAuthenticator,
    InspectionClientConfig, InspectionResolver, InspectionResolverConfig, InspectionResponderConfig,
    InspectionTokenAuthenticator, InspectionVerifier, PeerMismatch, PinnedKeyInspectionVerifier, RouteEpoch,
    SupervisorInspectionClient, SupervisorInspectionResponder, SupervisorPeerPin, SupervisorRouteDisabled,
    SupervisorRouteError, SupervisorRouteRejected, UnixSocketBackend, binding_digest, binding_for,
    dispatch_claim, inspection_key_id,
)
from tests.test_hub_container_executor_supervisor_route import (  # noqa: F401  (world is a fixture)
    BOOT, LAUNCH, LINUX, TOKEN, _COUNTING_SERVER, _self_ticks, canonical, command, world,
)

INSPECT_TOKEN = "ehri1." + "Insp3ct" * 6  # Test-only inspection credential.
REJECTION = b'{"error":{"code":"unauthorized"}}'


class JobStore(HeldAttemptSource):
    """Test-only authoritative Hub job store, doing what the contract demands.

    ``hold_version`` records the Hub's OWN job record (binding, dispatch claim,
    version). ``record_observation_in_hub_transaction`` stands in for the
    Hub's own DB transaction: under one lock it strictly parses the raw signed
    request, verifies the raw signed answer with its own pinned key, takes the
    outcome/binding/claim/time ONLY from those signed bytes, refuses if any
    module-computed hint disagrees, checks its job record and the hold
    version, then records the outcome (idempotently).
    """

    def __init__(self, verifier):
        self.lock, self.versions, self.jobs, self.applied, self.hub_calls = threading.Lock(), {}, {}, {}, []
        self.verifier, self.fail_next_hold = verifier, False

    def hold_version(self, command, pin, dispatch_counter):
        with self.lock:
            if self.fail_next_hold:
                self.fail_next_hold = False
                raise OSError("job store temporarily unavailable")
            binding = binding_for(command, pin)
            self.jobs.setdefault(command.command_id, (binding, dispatch_claim(binding, dispatch_counter)))
            return self.versions.setdefault(command.command_id, "v1." + command.command_id)

    def change(self, command_id):  # The job moved on (re-queued, cancelled, re-pinned...).
        with self.lock:
            self.versions[command_id] += "+"

    def record_observation_in_hub_transaction(self, version, observation):
        with self.lock:
            self.hub_calls.append((version, observation))
            try:
                raw = observation.request
                request = json.loads(raw)
                if type(raw) is not bytes or canonical(request) != raw:
                    return False
                payload = self.verifier.verify(observation.answer, request=request)  # Own pinned key.
                command_id = request["binding"]["command_id"]
                signed = (command_id, payload["status"], payload["route_state"], request["binding_digest"],
                          request["dispatch"], payload["answered_at_ms"], payload["key_id"])
                hints = (observation.command_id, observation.status, observation.route_state,
                         observation.binding_digest, observation.dispatch, observation.answered_at_ms,
                         observation.key_id)
                if hints != signed or (request["binding"], request["dispatch"]) != self.jobs[command_id]:
                    return False
                if self.versions[command_id] != version:
                    return False
            except Exception:
                return False
            previous = self.applied.setdefault(command_id, payload["status"])
            return previous == payload["status"]


def raw_key(key):
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def own_peer(ticks_offset=0, uid_offset=0):
    return SupervisorPeerPin(os.getpid(), os.getuid() + uid_offset, os.getgid(), _self_ticks() + ticks_offset)


def _http(status, body):
    return b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (status, len(body)) + body


class _Supervisor:
    """In-process (pinnable PID) supervisor listener; answers via the responder
    unless ``script(request_body, responder)`` overrides the reply chunks."""

    def __init__(self, path, responder):
        self.responder, self.script, self.received, self.threads = responder, None, [], []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        self.listener.listen(8)
        self.acceptor = threading.Thread(target=self._accept, daemon=True)
        self.acceptor.start()

    def _accept(self):
        while True:
            try:
                peer, _ = self.listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._serve, args=(peer,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _serve(self, peer):
        try:
            peer.settimeout(10)
            data, head, body = b"", None, None
            while True:
                part = peer.recv(65536)
                if not part:
                    break
                data += part
                if b"\r\n\r\n" in data:
                    head, rest = data.split(b"\r\n\r\n", 1)
                    lines = head.split(b"\r\n")
                    length = int([l for l in lines if l.lower().startswith(b"content-length:")][0].split(b":")[1])
                    if len(rest) >= length:
                        body = rest[:length]
                        break
            self.received.append(len(data))
            if body is None:
                return
            lines = head.decode().split("\r\n")
            method, path, _ = lines[0].split(" ")
            headers = dict(l.split(": ", 1) for l in lines[1:])
            if self.script is not None:
                chunks = self.script(body, self.responder)
            else:
                chunks = [(0, _http(*self.responder.answer(method=method, path=path,
                                                           authorization=headers.get("Authorization"),
                                                           body=body)))]
            for delay, chunk in chunks:
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


@pytest.fixture
def insp(world, tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir(mode=0o700)
    key = Ed25519PrivateKey.generate()  # Test-only supervisor inspection key.
    route = world["make"]()
    responder = SupervisorInspectionResponder(config=InspectionResponderConfig(enabled=True), route=route,
                                              authenticator=InspectionTokenAuthenticator(INSPECT_TOKEN),
                                              signing_key=key)
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "inspect.sock")
    server = _Supervisor(path, responder)
    state = {"key": key, "route": route, "responder": responder, "server": server, "socket": path,
             "ledger": str(hub / "inspection.sqlite"), "resolvers": [],
             # The authoritative job store outlives any resolver.
             "store": JobStore(PinnedKeyInspectionVerifier(raw_key(key)))}

    def client(peer=None, verifier=None, credential=INSPECT_TOKEN, timeout_ms=600, socket_path=path):
        return SupervisorInspectionClient(
            config=InspectionClientConfig(enabled=True, socket_path=socket_path, timeout_ms=timeout_ms),
            credential=credential, supervisor_peer=peer or own_peer(),
            verifier=verifier or PinnedKeyInspectionVerifier(raw_key(key)))

    def resolver(create=True, ledger=None, source=None, **kwargs):
        made = InspectionResolver(config=InspectionResolverConfig(
            enabled=True, ledger_path=ledger or state["ledger"], create_ledger=create,
            allow_ephemeral_test_storage=True), client=client(**kwargs), source=source or state["store"])
        state["resolvers"].append(made)
        return made
    state["client"], state["resolver"] = client, resolver
    try:
        yield state
    finally:
        for made in state["resolvers"]:
            made.close()
        try:
            state["route"].close()
        except Exception:
            pass
        server.close()
        os.unlink(path)
        os.rmdir(directory)


def counter_of(world, command_id):
    with sqlite3.connect(Path(world["path"]).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT dispatch_counter FROM supervisor_route_intent WHERE command_id=?",
                          (command_id,)).fetchone()[0]


def route_rows(world):
    with sqlite3.connect(Path(world["path"]).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT * FROM supervisor_route_intent ORDER BY command_id").fetchall()


def ledger_state(insp, command_id):
    with sqlite3.connect(Path(insp["ledger"]).as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT state, route_state FROM supervisor_route_inspection WHERE command_id=?",
                         (command_id,)).fetchone()
    return row


def launch(world, insp, outcome="completed"):
    """Submit one command through the route so its journal row ends in ``outcome``."""
    backend = world["backend"]
    backend.status, backend.body, backend.error = 200, None, None
    if outcome == "rejected":
        backend.status, backend.body = 401, REJECTION
    elif outcome == "refused":
        backend.error = PeerMismatch()
    elif outcome == "unknown":
        backend.error = OSError()
    cmd = command(world["epoch"])
    if outcome == "intent":  # Admitted, never forwarded nor resolved.
        binding = binding_for(cmd, world["pin"])
        insp["route"]._journal.admit(cmd.command_id, binding_digest(binding), binding, world["epoch"],
                                     secrets.token_hex(16))
    else:
        result = insp["route"].submit(cmd)
        assert result.state == {"unknown": "held"}.get(outcome, outcome)
    backend.status, backend.body, backend.error = 200, None, None
    return cmd, counter_of(world, cmd.command_id)


def held_attempt(world, insp, outcome="completed", **kwargs):
    cmd, counter = launch(world, insp, outcome)
    resolver = insp["resolver"](**kwargs)
    resolver.hold(cmd, world["pin"], counter)
    return resolver, cmd, counter


def sign(key, payload):
    signature = base64.urlsafe_b64encode(key.sign(INSPECTION_SIGNATURE_DOMAIN + canonical(payload))
                                         ).rstrip(b"=").decode()
    return canonical({"payload": payload, "signature": signature})


def genuine(body, responder):
    status, raw = responder.answer(method="POST", path=INSPECTION_PATH, authorization="Bearer " + INSPECT_TOKEN,
                                   body=body)
    assert status == 200
    return raw


def scripted(mutate):
    """Reply with ``mutate(genuine_answer_payload, request_body) -> raw`` as a 200."""
    def script(body, responder):
        envelope = json.loads(genuine(body, responder))
        return [(0, _http(200, mutate(envelope, body)))]
    return script


# --- Disabled by default / default-refusing interfaces -----------------------

def test_inspection_disabled_by_default(world, tmp_path):
    assert InspectionClientConfig().enabled is False
    assert InspectionResponderConfig().enabled is False
    assert InspectionResolverConfig().enabled is False
    peer = SupervisorPeerPin(1, 0, 0, 1)
    verifier = PinnedKeyInspectionVerifier(raw_key(Ed25519PrivateKey.generate()))
    for config in (InspectionClientConfig(), InspectionClientConfig(enabled=1, socket_path="/tmp/x"), None):
        with pytest.raises(SupervisorRouteDisabled):
            SupervisorInspectionClient(config=config, credential=INSPECT_TOKEN, supervisor_peer=peer,
                                       verifier=verifier)
    for config in (InspectionResponderConfig(), InspectionResponderConfig(enabled="yes"), None):
        with pytest.raises(SupervisorRouteDisabled):
            SupervisorInspectionResponder(config=config, route=None, authenticator=InspectionAuthenticator(),
                                          signing_key=Ed25519PrivateKey.generate())
    ledger = tmp_path / "ledger.sqlite"
    for config in (InspectionResolverConfig(), InspectionResolverConfig(enabled=1, ledger_path=str(ledger)), None):
        with pytest.raises(SupervisorRouteDisabled):
            InspectionResolver(config=config, client=None, source=None)
    assert not ledger.exists()
    with pytest.raises(SupervisorRouteError):
        InspectionVerifier().verify(b"{}", request={})
    with pytest.raises(SupervisorRouteError):
        InspectionAuthenticator().authenticate("Bearer " + INSPECT_TOKEN)
    instances = (SupervisorInspectionClient, SupervisorInspectionResponder, InspectionResolver)
    assert not any(isinstance(v, instances) for v in vars(route_module).values())


@LINUX
def test_default_refusing_verifier_and_authenticator_keep_everything_held(world, insp):
    class Unconfigured(InspectionVerifier):
        key_id = inspection_key_id(raw_key(insp["key"]))  # Right key id, but verify() is not overridden.
    resolver, cmd, counter = held_attempt(world, insp, verifier=Unconfigured())
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "unverified")
    insp["responder"]._authenticator = InspectionAuthenticator()  # Default-refusing authenticator.
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "http_status")
    assert ledger_state(insp, cmd.command_id) == ("held", None)


# --- Credential separation ---------------------------------------------------

@LINUX
def test_launch_bearer_never_authorizes_inspection_and_vice_versa(world, insp):
    with pytest.raises(SupervisorRouteError):
        InspectionTokenAuthenticator(TOKEN)  # A launch bearer cannot be enrolled for inspection.
    with pytest.raises(SupervisorRouteError):
        insp["client"](credential=TOKEN)  # Nor used by the inspection client.
    with pytest.raises(SupervisorRouteError):
        UnixSocketBackend("/tmp/x.sock", bearer_token=INSPECT_TOKEN)  # Inspection token cannot launch.
    cmd, counter = launch(world, insp)
    request = insp["client"]().request_for(cmd, world["pin"], counter)
    body = canonical(request)
    responder = insp["responder"]
    for authorization in ("Bearer " + TOKEN, "Bearer " + INSPECT_TOKEN + "x", "bearer " + INSPECT_TOKEN,
                          INSPECT_TOKEN, "Bearer  " + INSPECT_TOKEN, None, ""):
        status, raw = responder.answer(method="POST", path=INSPECTION_PATH, authorization=authorization,
                                       body=body)
        assert (status, raw) == (401, b'{"error":{"code":"unauthorized"}}')  # Unsigned denial.
    assert genuine(body, responder)  # The enrolled inspection token alone works.
    other = "ehri1." + "0ther" * 8  # Well-formed inspection credential that is not enrolled.
    resolver = insp["resolver"](credential=other)
    resolver.hold(cmd, world["pin"], counter)
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "http_status")
    assert ledger_state(insp, cmd.command_id) == ("held", None)


# --- Positive cases ----------------------------------------------------------

@LINUX
def test_signed_launched_observed_and_applied_only_via_hub_transaction_across_restart(world, insp):
    resolver, cmd, counter = held_attempt(world, insp)
    before, calls = route_rows(world), list(world["backend"].calls)
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.status, result.observation.route_state, result.error) == (
        "applied", "launched", "completed", None)
    assert result.observation.historical is False
    assert ledger_state(insp, cmd.command_id) == ("observed_launched", "completed")
    assert route_rows(world) == before and world["backend"].calls == calls  # Read-only: nothing forwarded.
    connections = len(insp["server"].received)
    again = resolver.inspect(cmd, world["pin"], counter)
    assert (again.state, again.observation.historical) == ("applied", True)
    assert len(insp["server"].received) == connections  # Recorded outcome; supervisor not contacted.
    resolver.close()
    insp["resolvers"].remove(resolver)
    reopened = insp["resolver"](create=False)
    after = reopened.inspect(cmd, world["pin"], counter)
    assert (after.state, after.observation.route_state, after.observation.historical) == (
        "applied", "completed", True)
    assert len(insp["server"].received) == connections
    with pytest.raises(SupervisorRouteRejected):  # A recorded attempt never re-binds.
        reopened.hold(cmd, world["pin"], counter + 1)


@LINUX
@pytest.mark.parametrize("outcome", ["rejected", "refused"])
def test_signed_refused_observed_and_applied_only_via_hub_transaction(world, insp, outcome):
    resolver, cmd, counter = held_attempt(world, insp, outcome)
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.status, result.observation.route_state) == ("applied", "refused", outcome)
    assert ledger_state(insp, cmd.command_id) == ("observed_refused", outcome)


@LINUX
def test_held_restart_then_inspect(world, insp):
    resolver, cmd, counter = held_attempt(world, insp)
    resolver.close()
    insp["resolvers"].remove(resolver)
    reopened = insp["resolver"](create=False)  # The held row survives restart.
    assert ledger_state(insp, cmd.command_id) == ("held", None)
    assert reopened.inspect(cmd, world["pin"], counter).state == "applied"


# --- Held cases (each individually) ------------------------------------------

def _assert_held(world, insp, resolver, cmd, counter, error, status=None):
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error, result.status) == ("held", error, status)
    assert result.observation is None and not result.applied
    assert ledger_state(insp, cmd.command_id) == ("held", None)
    return result


@LINUX
@pytest.mark.parametrize("outcome", ["unknown", "intent"])
def test_signed_unknown_stays_held(world, insp, outcome):
    resolver, cmd, counter = held_attempt(world, insp, outcome)
    _assert_held(world, insp, resolver, cmd, counter, "unknown", "unknown")


@LINUX
def test_signed_not_found_stays_held(world, insp):
    cmd = command(world["epoch"])  # Never submitted: no journal row.
    resolver = insp["resolver"]()
    resolver.hold(cmd, world["pin"], 1)
    _assert_held(world, insp, resolver, cmd, 1, "not_found", "not_found")


@LINUX
def test_restored_route_journal_missing_row_stays_held(world, insp):
    resolver, cmd, counter = held_attempt(world, insp)
    insp["route"].close()
    os.replace(world["path"], world["path"] + ".lost")
    # Replaced journal (restore without the row): the launched row is gone, the effect is not.
    insp["route"] = world["make"](create=True)
    insp["responder"]._route = insp["route"]
    assert insp["route"].inspect(cmd.command_id) is None
    _assert_held(world, insp, resolver, cmd, counter, "not_found", "not_found")


@LINUX
@pytest.mark.parametrize("field", ["counter", "fence", "epoch", "body"])
def test_supervisor_row_mismatch_is_signed_unknown_and_held(world, insp, field):
    cmd, counter = launch(world, insp)
    pin = world["pin"]
    if field == "counter":
        asked = cmd, counter + 1
    elif field == "fence":
        asked = command(world["epoch"], command_id=cmd.command_id, fence=8), counter
    elif field == "epoch":
        asked = command(RouteEpoch(LAUNCH, 6, BOOT), command_id=cmd.command_id), counter
    else:
        asked = command(world["epoch"], command_id=cmd.command_id,
                        run_id="22222222-2222-4222-8222-222222222222"), counter
    resolver = insp["resolver"]()
    resolver.hold(asked[0], pin, asked[1])
    _assert_held(world, insp, resolver, asked[0], asked[1], "unknown", "unknown")


def _unsigned(envelope, body):
    return canonical({"payload": envelope["payload"]})


def _empty_signature(envelope, body):
    return canonical({**envelope, "signature": ""})


def _bad_signature(envelope, body):
    signature = envelope["signature"]
    flipped = ("B" if signature[10] == "A" else "A")
    return canonical({**envelope, "signature": signature[:10] + flipped + signature[11:]})


def _tampered_status(envelope, body):
    payload = {**envelope["payload"], "status": "refused", "route_state": "rejected"}
    return canonical({**envelope, "payload": payload})


def _non_canonical(envelope, body):
    return json.dumps(envelope, indent=1).encode()


_WRONG_KEY = Ed25519PrivateKey.generate()


def _wrong_key(envelope, body):
    return sign(_WRONG_KEY, envelope["payload"])  # Claims the pinned key id, signed by another key.


def _http_503(body, responder):
    return [(0, _http(503, b'{"error":{"code":"unavailable"}}'))]


def _http_401(body, responder):
    return [(0, _http(401, b'{"error":{"code":"unauthorized"}}'))]


_UNVERIFIED = {"unsigned": _unsigned, "empty_signature": _empty_signature, "bad_signature": _bad_signature,
               "tampered_status": _tampered_status, "non_canonical": _non_canonical, "wrong_key": _wrong_key}


@LINUX
@pytest.mark.parametrize("case", sorted(_UNVERIFIED))
def test_unverified_answers_stay_held(world, insp, case):
    resolver, cmd, counter = held_attempt(world, insp)
    insp["server"].script = scripted(_UNVERIFIED[case])
    _assert_held(world, insp, resolver, cmd, counter, "unverified")
    insp["server"].script = None
    assert resolver.inspect(cmd, world["pin"], counter).state == "applied"  # Held, not poisoned.


@LINUX
def test_supervisor_with_other_key_stays_held(world, insp):
    resolver, cmd, counter = held_attempt(world, insp)
    insp["responder"] = SupervisorInspectionResponder(
        config=InspectionResponderConfig(enabled=True), route=insp["route"],
        authenticator=InspectionTokenAuthenticator(INSPECT_TOKEN), signing_key=_WRONG_KEY)
    insp["server"].responder = insp["responder"]
    _assert_held(world, insp, resolver, cmd, counter, "http_status")  # Refuses a request for another key.
    # Even a genuine other-key answer to a request naming that key fails the pinned verifier.
    request = {**insp["client"]().request_for(cmd, world["pin"], counter), "key_id": insp["responder"].key_id}
    raw = genuine(canonical(request), insp["responder"])
    with pytest.raises(SupervisorRouteError):
        PinnedKeyInspectionVerifier(raw_key(insp["key"])).verify(raw, request=request)


@LINUX
@pytest.mark.parametrize("script", [_http_503, _http_401], ids=["503", "401"])
def test_http_denials_stay_held(world, insp, script):
    resolver, cmd, counter = held_attempt(world, insp)
    insp["server"].script = script
    _assert_held(world, insp, resolver, cmd, counter, "http_status")


@LINUX
def test_supervisor_journal_unavailable_stays_held(world, insp):
    resolver, cmd, counter = held_attempt(world, insp)
    insp["route"].close()  # Responder can no longer read its durable record.
    _assert_held(world, insp, resolver, cmd, counter, "http_status")


@LINUX
def test_failure_to_record_resolution_stays_held(world, insp, monkeypatch):
    resolver, cmd, counter = held_attempt(world, insp)

    def fail(*args, **kwargs):
        raise OSError()
    monkeypatch.setattr(resolver._ledger, "record_observation", fail)
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error, result.status) == ("held", "observation_unrecorded", "launched")
    assert ledger_state(insp, cmd.command_id) == ("held", None)


@LINUX
def test_no_held_row_sends_nothing(world, insp):
    cmd, counter = launch(world, insp)
    resolver = insp["resolver"]()
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "no_held_attempt")
    resolver.hold(cmd, world["pin"], counter)
    other = resolver.inspect(cmd, world["pin"], counter + 1)  # Different binding than the held row.
    assert (other.state, other.error) == ("held", "no_held_attempt")
    assert insp["server"].received == []  # Never connected.
    with pytest.raises(SupervisorRouteRejected):
        resolver.hold(cmd, world["pin"], counter + 1)


# --- Replayed answers --------------------------------------------------------

@LINUX
@pytest.mark.parametrize("field", ["counter", "fence", "epoch", "command", "executor", "nonce"])
def test_replayed_answer_for_another_binding_stays_held(world, insp, field):
    cmd, counter = launch(world, insp)
    pin = world["pin"]
    client = insp["client"]()
    first = client.request_for(cmd, pin, counter)
    captured = client.inspect(first)
    assert captured.verified and captured.status == "launched"  # Genuine for the first request only.
    other_pin = BackendPin("b" * 64, pin.host_pid, pin.process_start_ticks, pin.uid, pin.gid, pin.engine_id,
                           pin.engine_pid, pin.engine_start_ticks)
    asked = {"counter": (cmd, pin, counter + 1),
             "fence": (command(world["epoch"], command_id=cmd.command_id, fence=8), pin, counter),
             "epoch": (command(RouteEpoch(LAUNCH, 6, BOOT), command_id=cmd.command_id), pin, counter),
             "command": (command(world["epoch"]), pin, counter),
             "executor": (cmd, other_pin, counter),
             "nonce": (cmd, pin, counter)}[field]
    resolver = insp["resolver"]()
    resolver.hold(*asked)
    insp["server"].script = lambda body, responder: [(0, _http(200, captured.raw))]  # Replay verbatim.
    result = resolver.inspect(*asked)
    assert (result.state, result.error) == ("held", "unverified")
    assert ledger_state(insp, asked[0].command_id) == ("held", None)


# --- Peer pinning ------------------------------------------------------------

@LINUX
@pytest.mark.parametrize("wrong", ["start_ticks", "uid"])
def test_wrong_supervisor_peer_receives_zero_bytes(world, insp, wrong):
    peer = own_peer(ticks_offset=1) if wrong == "start_ticks" else own_peer(uid_offset=1)
    resolver, cmd, counter = held_attempt(world, insp, peer=peer)
    _assert_held(world, insp, resolver, cmd, counter, "peer_mismatch")
    deadline = time.monotonic() + 5
    while not insp["server"].received and time.monotonic() < deadline:
        time.sleep(0.01)
    assert insp["server"].received == [0]  # One connection, zero request bytes.


@LINUX
def test_other_process_supervisor_receives_zero_bytes(world, insp):
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "other.sock")
    process = subprocess.Popen([sys.executable, "-c", _COUNTING_SERVER, path], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        resolver, cmd, counter = held_attempt(world, insp, socket_path=path)  # Pin is THIS process.
        _assert_held(world, insp, resolver, cmd, counter, "peer_mismatch")
        out, _ = process.communicate(timeout=15)
        assert int(out.strip()) == 0
    finally:
        if process.poll() is None:
            process.kill()
        if os.path.exists(path):
            os.unlink(path)
        os.rmdir(directory)


# --- Bounded framing and total deadline --------------------------------------

def _answer(body, responder):
    return genuine(body, responder)


def _oversize(body, responder):
    raw = b"x" * (MAX_RESPONSE_BYTES + 1)
    return [(0, _http(200, raw))]


def _oversize_headers(body, responder):
    return [(0, b"HTTP/1.1 200 OK\r\nX-Big: " + b"v" * 9000 + b"\r\n\r\n")]


def _slow_headers(body, responder):
    return [(0, b"HTTP/1.1 200 OK\r\nX-Slow: ")] + [(0.05, b"a")] * 60


def _slow_body(body, responder):
    raw = _answer(body, responder)
    return [(0, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(raw))] + [(0.05, raw[i:i + 1])
                                                                                for i in range(len(raw))]


def _stalled(body, responder):
    return [(0, b"HTTP/1.1 200 OK\r\nContent-Le"), (3, b"")]


def _truncated(body, responder):
    raw = _answer(body, responder)
    return [(0, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(raw) + raw[:len(raw) // 2])]


def _longer(body, responder):
    raw = _answer(body, responder)
    return [(0, _http(200, raw) + b"  ")]


def _no_eof(body, responder):
    return [(0, _http(200, _answer(body, responder))), (3, b"")]


def _chunked(body, responder):
    raw = _answer(body, responder)
    return [(0, b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: %d\r\n\r\n" % len(raw) + raw)]


_BOUNDED = {"oversize_body": _oversize, "oversize_headers": _oversize_headers, "slow_headers": _slow_headers,
            "slow_body": _slow_body, "stalled": _stalled, "truncated": _truncated, "longer": _longer,
            "no_eof": _no_eof, "chunked": _chunked}


@LINUX
@pytest.mark.parametrize("case", sorted(_BOUNDED))
def test_bad_or_slow_responses_held_within_total_deadline(world, insp, case):
    resolver, cmd, counter = held_attempt(world, insp)
    insp["server"].script = _BOUNDED[case]
    started = time.monotonic()
    _assert_held(world, insp, resolver, cmd, counter, "transport_lost")
    assert time.monotonic() - started < 0.6 + 0.4, case  # Total deadline plus small slack.


@LINUX
def test_exact_framing_accepted(world, insp):
    resolver, cmd, counter = held_attempt(world, insp)
    insp["server"].script = lambda body, responder: [(0, _http(200, _answer(body, responder)))]
    assert resolver.inspect(cmd, world["pin"], counter).state == "applied"


# --- Ledger invariants -------------------------------------------------------

@LINUX
def test_ledger_triggers_refuse_rewrites_and_schema_tamper(world, insp):
    resolver, cmd, counter = held_attempt(world, insp)
    held_cmd, held_counter = launch(world, insp, "unknown")
    resolver.hold(held_cmd, world["pin"], held_counter)
    assert resolver.inspect(cmd, world["pin"], counter).state == "applied"
    db = sqlite3.connect(Path(insp["ledger"]).as_uri() + "?mode=rw", uri=True, isolation_level=None)
    try:
        table = "supervisor_route_inspection"
        for sql, args in (
                (f"UPDATE {table} SET state='held', route_state=NULL, key_id=NULL, answer=NULL "
                 "WHERE command_id=?", (cmd.command_id,)),
                (f"UPDATE {table} SET state='observed_refused', route_state='rejected' WHERE command_id=?",
                 (cmd.command_id,)),
                (f"UPDATE {table} SET dispatch_counter=99, state='observed_launched', route_state='completed', "
                 "key_id='k', answer='a' WHERE command_id=?", (held_cmd.command_id,)),
                (f"UPDATE {table} SET state='observed_launched', route_state='rejected', key_id='k', answer='a' "
                 "WHERE command_id=?", (held_cmd.command_id,)),
                (f"DELETE FROM {table}", ()),
                (f"INSERT OR REPLACE INTO {table} VALUES (?, 'd', 'b', 'e', 1, NULL, 'held', NULL, NULL, NULL, "
                 "NULL)", (cmd.command_id,)),
                (f"INSERT INTO {table} VALUES (?, 'd', 'b', 'e', 1, 'v', 'observed_launched', 'completed', 'k', "
                 "'a', 'r')", (str(uuid4()),)),
                (f"UPDATE {table} SET hold_version='other', state='observed_launched', route_state='completed', "
                 "key_id='k', answer='a', request='r' WHERE command_id=?", (held_cmd.command_id,)),
                (f"UPDATE {table} SET request=NULL WHERE command_id=?", (cmd.command_id,))):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, args)
        assert db.execute(f"SELECT command_id, state FROM {table} ORDER BY state").fetchall() == [
            (held_cmd.command_id, "held"), (cmd.command_id, "observed_launched")]
        db.execute(f"DROP TRIGGER {table}_no_delete")
    finally:
        db.close()
    resolver.close()
    insp["resolvers"].remove(resolver)
    with pytest.raises(SupervisorRouteError):
        insp["resolver"](create=False)  # Exact schema check refuses the tampered ledger.


@LINUX
def test_inspection_never_writes_or_forwards_on_the_route(world, insp):
    cmds = [launch(world, insp, outcome) for outcome in ("completed", "rejected", "refused", "unknown")]
    before, calls = route_rows(world), list(world["backend"].calls)
    client = insp["client"]()
    for cmd, counter in cmds:
        assert client.inspect(client.request_for(cmd, world["pin"], counter)).verified
    assert route_rows(world) == before and world["backend"].calls == calls


@LINUX
def test_request_reuses_dispatch_claim_and_refuses_inconsistent_claims(world, insp):
    cmd, counter = launch(world, insp)
    request = insp["client"]().request_for(cmd, world["pin"], counter)
    binding = binding_for(cmd, world["pin"])
    assert request["dispatch"] == dispatch_claim(binding, counter)  # Same fields, same canonical JSON.
    assert request["binding"] == binding and request["binding_digest"] == binding_digest(binding)
    claim = request["dispatch"]
    for changed in ({**claim, "counter": counter + 1}, {**claim, "queue_fence": 8},
                    {**claim, "executor_id": "b" * 64}, {**claim, "body_sha256": "0" * 64},
                    {**claim, "command_id": str(uuid4())}, {**claim, "epoch": {**claim["epoch"], "launch_counter": 6}},
                    {**claim, "extra": 1}):
        forged = {**request, "dispatch": changed}
        if changed.get("counter") == counter + 1:
            assert genuine(canonical(forged), insp["responder"])  # Consistent: a different attempt.
            continue
        status, _ = insp["responder"].answer(method="POST", path=INSPECTION_PATH,
                                             authorization="Bearer " + INSPECT_TOKEN, body=canonical(forged))
        assert status == 400
    for body in (canonical(request) + b" ", json.dumps(request).encode()):  # Non-canonical bytes.
        assert insp["responder"].answer(method="POST", path=INSPECTION_PATH,
                                        authorization="Bearer " + INSPECT_TOKEN, body=body)[0] == 400


# --- Replay re-verification, Hub-transaction source, bounded responder -------

def _resolved(world, insp):
    resolver, cmd, counter = held_attempt(world, insp)
    assert resolver.inspect(cmd, world["pin"], counter).state == "applied"
    return resolver, cmd, counter


def _reopen(insp, resolver, **kwargs):
    resolver.close()
    insp["resolvers"].remove(resolver)
    return insp["resolver"](create=False, **kwargs)


@LINUX
def test_replay_after_key_rotation_is_held(world, insp):
    resolver, cmd, counter = _resolved(world, insp)
    rotated = PinnedKeyInspectionVerifier(raw_key(Ed25519PrivateKey.generate()))
    reopened = _reopen(insp, resolver, verifier=rotated)
    result = reopened.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "replay_unverified")  # Never the stored terminal.
    assert ledger_state(insp, cmd.command_id) == ("observed_launched", "completed")  # Kept, not trusted.
    back = _reopen(insp, reopened)  # Original pinned key: the same evidence verifies again.
    assert back.inspect(cmd, world["pin"], counter).state == "applied"


@LINUX
def test_replay_after_restart_with_different_pin_is_held(world, insp):
    resolver, cmd, counter = _resolved(world, insp)
    reopened = _reopen(insp, resolver)
    pin = world["pin"]
    moved = BackendPin(pin.container_id, pin.host_pid + 1, pin.process_start_ticks, pin.uid, pin.gid,
                       pin.engine_id, pin.engine_pid, pin.engine_start_ticks)
    for args in ((cmd, moved, counter), (cmd, pin, counter + 1),
                 (command(world["epoch"], command_id=cmd.command_id, fence=8), pin, counter)):
        result = reopened.inspect(*args)
        assert (result.state, result.error) == ("held", "no_held_attempt")
    assert reopened.inspect(cmd, pin, counter).state == "applied"


_TRIGGER = "supervisor_route_inspection_observe_once"


def _flip(text, index):
    return text[:index] + ("B" if text[index] == "A" else "A") + text[index + 1:]


def _tamper_answer_payload(row):
    envelope = json.loads(row["answer"])
    envelope["payload"]["answered_at_ms"] += 1
    return {"answer": canonical(envelope).decode()}


def _tamper_answer_signature(row):
    envelope = json.loads(row["answer"])
    envelope["signature"] = _flip(envelope["signature"], 20)
    return {"answer": canonical(envelope).decode()}


def _tamper_status(row):
    return {"state": "observed_refused", "route_state": "rejected"}


def _tamper_request(row):
    request = json.loads(row["request"])
    request["request_nonce"] = "0" * 32  # Valid shape, but not what was signed.
    return {"request": canonical(request).decode()}


def _tamper_request_binding(row):
    request = json.loads(row["request"])
    request["binding"]["queue_fence"] = 8
    return {"request": canonical(request).decode()}


_TAMPER = {"answer_payload": _tamper_answer_payload, "answer_signature": _tamper_answer_signature,
           "status": _tamper_status, "request_nonce": _tamper_request, "request_binding": _tamper_request_binding,
           "missing_request": lambda row: {"request": ""}, "missing_answer": lambda row: {"answer": ""},
           "key_id": lambda row: {"key_id": "f" * 64}}


@LINUX
@pytest.mark.parametrize("case", sorted(_TAMPER))
def test_tampered_terminal_row_replays_held(world, insp, case):
    resolver, cmd, counter = _resolved(world, insp)
    db = sqlite3.connect(Path(insp["ledger"]).as_uri() + "?mode=rw", uri=True, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM supervisor_route_inspection WHERE command_id=?",
                              (cmd.command_id,)).fetchone())
        changes = _TAMPER[case](row)
        sets = ", ".join(f"{k}=?" for k in changes)
        with pytest.raises(sqlite3.IntegrityError):  # The trigger blocks rewriting a resolved row...
            db.execute(f"UPDATE supervisor_route_inspection SET {sets} WHERE command_id=?",
                       (*changes.values(), cmd.command_id))
        # ...so a same-UID attacker drops it, rewrites, and restores the identical trigger SQL,
        # which the exact schema check cannot notice. Only re-verification can.
        db.execute("BEGIN IMMEDIATE")
        db.execute(f"DROP TRIGGER {_TRIGGER}")
        db.execute(f"UPDATE supervisor_route_inspection SET {sets} WHERE command_id=?",
                   (*changes.values(), cmd.command_id))
        db.execute(route_module._OBSERVATION_SCHEMA[_TRIGGER])
        db.execute("COMMIT")
    finally:
        db.close()
    connections = len(insp["server"].received)
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "replay_unverified"), case
    reopened = _reopen(insp, resolver)  # Exact schema still matches; replay still held after restart.
    assert reopened.inspect(cmd, world["pin"], counter).state == "held"
    assert len(insp["server"].received) == connections  # Replay never re-asks the supervisor.


class _Source(HeldAttemptSource):
    """Versions like JobStore but with a scripted Hub-transaction result."""

    def __init__(self, result, version="v1"):
        self.result, self.version, self.calls = result, version, []

    def hold_version(self, command, pin, dispatch_counter):
        return self.version

    def record_observation_in_hub_transaction(self, version, observation):
        self.calls.append(version)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@LINUX
@pytest.mark.parametrize("source,error", [
    ("hub_false", "hub_not_recorded"), ("hub_truthy", "hub_not_recorded"), ("hub_raises", "hub_not_recorded")])
def test_observation_recorded_but_held_without_hub_transaction(world, insp, source, error):
    chosen = {"hub_false": _Source(False), "hub_truthy": _Source(1), "hub_raises": _Source(OSError())}[source]
    resolver, cmd, counter = held_attempt(world, insp, source=chosen)
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error, result.status, result.applied) == ("held", error, "launched", False)
    assert result.observation.status == "launched" and result.observation.historical is False
    assert ledger_state(insp, cmd.command_id) == ("observed_launched", "completed")  # Observation only.
    connections = len(insp["server"].received)
    replay = resolver.inspect(cmd, world["pin"], counter)
    assert (replay.state, replay.error, replay.observation.historical) == ("held", error, True)
    assert len(insp["server"].received) == connections
    with pytest.raises(SupervisorRouteError):
        InspectionResolver(config=InspectionResolverConfig(enabled=True, ledger_path=insp["ledger"]),
                           client=insp["client"](), source=None)


@LINUX
@pytest.mark.parametrize("when", ["before_inspection", "while_answer_in_flight"])
def test_source_change_refused_by_hub_transaction_and_stays_held(world, insp, when):
    store = JobStore(PinnedKeyInspectionVerifier(raw_key(insp["key"])))
    resolver, cmd, counter = held_attempt(world, insp, source=store)
    held_version = store.versions[cmd.command_id]
    if when == "before_inspection":
        store.change(cmd.command_id)
    else:
        def in_flight(body, responder):
            store.change(cmd.command_id)  # The job changes after hold_version, before the Hub transaction.
            return [(0, _http(200, genuine(body, responder)))]
        insp["server"].script = in_flight
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error, result.status) == ("held", "hub_not_recorded", "launched")
    assert store.applied == {} and store.hub_calls[-1][0] == held_version  # The Hub got the HOLD version.
    assert ledger_state(insp, cmd.command_id) == ("observed_launched", "completed")  # Observed, not released.


@LINUX
def test_hub_transaction_receives_exact_reverifiable_observation(world, insp):
    store = JobStore(PinnedKeyInspectionVerifier(raw_key(insp["key"])))
    resolver, cmd, counter = held_attempt(world, insp, source=store)
    result = resolver.inspect(cmd, world["pin"], counter)
    assert result.applied
    version, observation = store.hub_calls[-1]
    binding = binding_for(cmd, world["pin"])
    assert version == "v1." + cmd.command_id
    assert (observation.command_id, observation.binding_digest, observation.dispatch) == (
        cmd.command_id, binding_digest(binding), dispatch_claim(binding, counter))
    assert observation.key_id == inspection_key_id(raw_key(insp["key"]))
    signed = json.loads(observation.answer)["payload"]
    assert observation.answered_at_ms == signed["answered_at_ms"] and signed["status"] == "launched"
    assert signed["request"] == json.loads(observation.request)  # Exactly what was signed: re-verifiable.
    other = PinnedKeyInspectionVerifier(raw_key(Ed25519PrivateKey.generate()))
    with pytest.raises(SupervisorRouteError):
        other.verify(observation.answer, request=json.loads(observation.request))


@LINUX
def test_hub_reverification_failure_inside_transaction_keeps_held(world, insp):
    store = JobStore(PinnedKeyInspectionVerifier(raw_key(Ed25519PrivateKey.generate())))  # Hub pins another key.
    resolver, cmd, counter = held_attempt(world, insp, source=store)
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "hub_not_recorded") and store.applied == {}


@LINUX
def test_replay_is_historical_and_needs_the_hub_transaction(world, insp):
    store = JobStore(PinnedKeyInspectionVerifier(raw_key(insp["key"])))
    resolver, cmd, counter = held_attempt(world, insp, source=store)
    first = resolver.inspect(cmd, world["pin"], counter)
    assert first.applied and not first.observation.historical
    calls = len(store.hub_calls)
    replay = resolver.inspect(cmd, world["pin"], counter)
    assert replay.applied and replay.observation.historical  # The idempotent Hub transaction accepted it again...
    assert replay.observation.answered_at_ms == first.observation.answered_at_ms  # ...as the OLD signed fact.
    assert len(store.hub_calls) == calls + 1  # Replay went through the Hub transaction.
    store.change(cmd.command_id)
    stale = resolver.inspect(cmd, world["pin"], counter)
    assert (stale.state, stale.error, stale.observation.historical) == ("held", "hub_not_recorded", True)
    assert len(insp["server"].received) == 1  # Replays never re-ask the supervisor.


def _responder(insp, **config):
    return SupervisorInspectionResponder(
        config=InspectionResponderConfig(enabled=True, **config), route=insp["route"],
        authenticator=InspectionTokenAuthenticator(INSPECT_TOKEN), signing_key=insp["key"])


def _ask(responder, body):
    return responder.answer(method="POST", path=INSPECTION_PATH, authorization="Bearer " + INSPECT_TOKEN,
                            body=body)


@LINUX
def test_responder_bounds_validated(world, insp):
    for bad in ({"max_concurrent": 0}, {"max_concurrent": 9}, {"max_concurrent": True},
                {"read_budget_ms": 9}, {"read_budget_ms": 1001}, {"read_budget_ms": 50.0},
                {"max_authenticating": 0}, {"max_authenticating": 9}, {"max_authenticating": 1.0},
                {"answer_budget_ms": 19}, {"answer_budget_ms": 2001}, {"answer_budget_ms": 99},
                {"answer_budget_ms": True}):
        with pytest.raises(SupervisorRouteError):
            _responder(insp, **bad)


@LINUX
def test_saturated_responder_answers_busy_without_queueing(world, insp, monkeypatch):
    cmd, counter = launch(world, insp)
    body = canonical(insp["client"]().request_for(cmd, world["pin"], counter))
    responder = _responder(insp, max_concurrent=2, answer_budget_ms=2000)
    original, entered, release = route_module._read_inspection_row, threading.Semaphore(0), threading.Event()

    def slow(*args):
        entered.release()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(route_module, "_read_inspection_row", slow)
    results = []
    workers = [threading.Thread(target=lambda: results.append(_ask(responder, body))) for _ in range(2)]
    for worker in workers:
        worker.start()
    assert entered.acquire(timeout=5) and entered.acquire(timeout=5)
    started = time.monotonic()
    for _ in range(3):
        assert _ask(responder, body) == (503, b'{"error":{"code":"busy"}}')  # Unsigned, immediate.
    assert time.monotonic() - started < 0.2
    assert responder.answer(method="POST", path=INSPECTION_PATH, authorization="Bearer " + TOKEN,
                            body=body)[0] == 401  # Unauthenticated callers never take a slot.
    release.set()
    for worker in workers:
        worker.join(10)
    assert [status for status, _ in results] == [200, 200]
    monkeypatch.undo()
    assert _ask(responder, body)[0] == 200  # Slots released.


@LINUX
@pytest.mark.parametrize("lock", ["journal_mutex", "sqlite_exclusive"])
def test_slow_journal_read_is_bounded_and_held(world, insp, lock):
    resolver, cmd, counter = held_attempt(world, insp)
    responder = _responder(insp, read_budget_ms=100)
    insp["server"].responder = responder
    body = canonical(insp["client"]().request_for(cmd, world["pin"], counter))
    holder = None
    if lock == "journal_mutex":
        locked, done = threading.Event(), threading.Event()

        def hold_mutex():
            with insp["route"]._journal._mutex:
                locked.set()
                done.wait(10)
        holder = threading.Thread(target=hold_mutex)
        holder.start()
        assert locked.wait(5)
    else:
        raw = sqlite3.connect(Path(world["path"]).as_uri() + "?mode=rw", uri=True, isolation_level=None)
        raw.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        assert _ask(responder, body) == (503, b'{"error":{"code":"unavailable"}}')
        assert time.monotonic() - started < 0.1 + 0.15  # Read budget plus small slack.
        started = time.monotonic()
        _assert_held(world, insp, resolver, cmd, counter, "http_status")
        assert time.monotonic() - started < 0.6
    finally:
        if holder is not None:
            done.set()
            holder.join(10)
        else:
            raw.execute("ROLLBACK")
            raw.close()
    assert _ask(responder, body)[0] == 200
    assert resolver.inspect(cmd, world["pin"], counter).state == "applied"


# --- Return-time deadline and bounded authentication -------------------------

class _SlowAuthenticator(InspectionTokenAuthenticator):
    """Delegates to the enrolled token, but blocks (then denies) "Bearer slow"
    and optionally sleeps before authenticating everyone else."""

    def __init__(self, token, *, delay=0.0):
        super().__init__(token)
        self.entered, self.release, self.delay = threading.Event(), threading.Event(), delay

    def authenticate(self, authorization):
        if authorization == "Bearer slow":
            self.entered.set()
            assert self.release.wait(10)
            raise SupervisorRouteError("denied")
        time.sleep(self.delay)
        return super().authenticate(authorization)


@LINUX
@pytest.mark.parametrize("outcome", ["not_found", "completed", "unknown", "slow_auth"])
def test_late_signed_answer_is_not_returned(world, insp, monkeypatch, outcome):
    if outcome == "not_found":
        cmd, counter = command(world["epoch"]), 1  # Missing-row branch.
    else:
        cmd, counter = launch(world, insp, "completed" if outcome == "slow_auth" else outcome)
    body = canonical(insp["client"]().request_for(cmd, world["pin"], counter))
    responder = _responder(insp, read_budget_ms=10, answer_budget_ms=50)
    assert _ask(responder, body)[0] == 200  # In time: signed.
    if outcome == "slow_auth":
        responder._authenticator = _SlowAuthenticator(INSPECT_TOKEN, delay=0.08)
    else:
        original = route_module._read_inspection_row

        def finishes_late(*args):
            row = original(*args)  # Completes within its own budget...
            time.sleep(0.08)  # ...but an uninterruptible step (fs call, close) overruns the deadline.
            return row
        monkeypatch.setattr(route_module, "_read_inspection_row", finishes_late)
    assert _ask(responder, body) == (503, b'{"error":{"code":"unavailable"}}')  # Unsigned.
    resolver = insp["resolver"]()
    resolver.hold(cmd, world["pin"], counter)
    insp["server"].responder = responder
    _assert_held(world, insp, resolver, cmd, counter, "http_status")


@LINUX
def test_slow_authenticator_is_bounded_and_cannot_take_authenticated_slots(world, insp, monkeypatch):
    cmd, counter = launch(world, insp)
    body = canonical(insp["client"]().request_for(cmd, world["pin"], counter))
    responder = _responder(insp, max_authenticating=1, max_concurrent=1, answer_budget_ms=2000)
    slow = _SlowAuthenticator(INSPECT_TOKEN)
    responder._authenticator = slow
    original, reading, read_release = route_module._read_inspection_row, threading.Event(), threading.Event()

    def blocked_read(*args):
        reading.set()
        assert read_release.wait(10)
        return original(*args)
    monkeypatch.setattr(route_module, "_read_inspection_row", blocked_read)
    results = {}
    authenticated = threading.Thread(target=lambda: results.update(a=_ask(responder, body)))
    authenticated.start()
    assert reading.wait(5)  # Past auth, holding the only post-auth slot.
    stuck = threading.Thread(target=lambda: results.update(s=responder.answer(
        method="POST", path=INSPECTION_PATH, authorization="Bearer slow", body=body)))
    stuck.start()
    assert slow.entered.wait(5)  # Holding the only pre-auth slot.
    started = time.monotonic()
    assert _ask(responder, body) == (503, b'{"error":{"code":"busy"}}')  # Bounded: no queueing.
    assert time.monotonic() - started < 0.2
    read_release.set()
    authenticated.join(10)
    assert results["a"][0] == 200  # The slow authenticator never touched the authenticated slot.
    slow.release.set()
    stuck.join(10)
    assert results["s"][0] == 401
    monkeypatch.undo()
    assert _ask(responder, body)[0] == 200  # Both pools released.


# --- Hold version must be valid; the Hub re-verifies the raw signed bytes ------

def _hold_version_of(insp, command_id):
    with sqlite3.connect(Path(insp["ledger"]).as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("SELECT hold_version FROM supervisor_route_inspection WHERE command_id=?",
                          (command_id,)).fetchall()


@LINUX
@pytest.mark.parametrize("version", ["default", None, "", "has space", "x" * 257, 7, "v\u00e9"])
def test_hold_without_valid_version_records_nothing(world, insp, version):
    source = HeldAttemptSource() if version == "default" else _Source(True, version=version)
    cmd, counter = launch(world, insp)
    resolver = insp["resolver"](source=source)
    with pytest.raises(SupervisorRouteRejected) as raised:
        resolver.hold(cmd, world["pin"], counter)
    assert raised.value.reason == "hold_version_unavailable"
    assert ledger_state(insp, cmd.command_id) is None  # Nothing recorded, nothing wedged.
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "no_held_attempt") and insp["server"].received == []


@LINUX
def test_temporary_source_failure_then_hold_succeeds(world, insp):
    store = insp["store"]
    store.fail_next_hold = True
    cmd, counter = launch(world, insp)
    resolver = insp["resolver"]()
    with pytest.raises(SupervisorRouteRejected):
        resolver.hold(cmd, world["pin"], counter)
    assert ledger_state(insp, cmd.command_id) is None
    resolver.hold(cmd, world["pin"], counter)  # Retry works: no immutable empty version was persisted.
    assert _hold_version_of(insp, cmd.command_id) == [("v1." + cmd.command_id,)]
    assert resolver.inspect(cmd, world["pin"], counter).state == "applied"


@LINUX
def test_repeat_hold_same_version_idempotent_changed_version_refused(world, insp):
    store = insp["store"]
    cmd, counter = launch(world, insp)
    resolver = insp["resolver"]()
    resolver.hold(cmd, world["pin"], counter)
    resolver.hold(cmd, world["pin"], counter)  # Same binding, same version: no-op.
    assert _hold_version_of(insp, cmd.command_id) == [("v1." + cmd.command_id,)]
    store.change(cmd.command_id)
    with pytest.raises(SupervisorRouteRejected) as raised:
        resolver.hold(cmd, world["pin"], counter)
    assert raised.value.reason == "hold_version_changed"
    assert _hold_version_of(insp, cmd.command_id) == [("v1." + cmd.command_id,)]  # Nothing changed.
    assert ledger_state(insp, cmd.command_id) == ("held", None)


def _tamper_hint(field):
    def tamper(observation):
        if field == "request":
            request = json.loads(observation.request)
            request["request_nonce"] = "0" * 32
            return dataclasses.replace(observation, request=canonical(request))
        values = {"status": "refused", "route_state": "rejected", "binding_digest": "0" * 64,
                  "command_id": str(uuid4()), "answered_at_ms": observation.answered_at_ms + 1,
                  "key_id": "f" * 64, "dispatch": {**observation.dispatch, "counter": 99}}
        return dataclasses.replace(observation, **{field: values[field]})
    return tamper


@LINUX
@pytest.mark.parametrize("field", ["status", "route_state", "binding_digest", "command_id", "answered_at_ms",
                                   "key_id", "dispatch", "request"])
@pytest.mark.parametrize("historical", [False, True])
def test_hub_refuses_observation_whose_hints_disagree_with_signed_bytes(world, insp, monkeypatch, field,
                                                                        historical):
    store = JobStore(PinnedKeyInspectionVerifier(raw_key(insp["key"])))
    resolver, cmd, counter = held_attempt(world, insp, source=store)
    if historical:
        resolver._source = _Source(False, version="v1." + cmd.command_id)  # Record the observation only.
        assert resolver.inspect(cmd, world["pin"], counter).error == "hub_not_recorded"
        store.hold_version(cmd, world["pin"], counter)
        resolver._source = store
    original = InspectionResolver._observation
    tamper = _tamper_hint(field)
    monkeypatch.setattr(InspectionResolver, "_observation",
                        staticmethod(lambda *args: tamper(original(*args))))
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "hub_not_recorded"), field
    assert store.applied == {} and store.hub_calls  # The Hub was asked and refused.
    monkeypatch.undo()
    assert resolver.inspect(cmd, world["pin"], counter).state == "applied"  # Untampered hints: accepted.


# --- Concurrent inspections: only persisted bytes reach the Hub ---------------

@LINUX
def test_concurrent_inspections_offer_only_the_persisted_observation(world, insp, monkeypatch):
    store = insp["store"]
    resolver, cmd, counter = held_attempt(world, insp)
    in_flight = threading.Barrier(2, timeout=10)

    def both_in_flight(body, responder):
        raw = genuine(body, responder)
        in_flight.wait()  # Both signed answers exist before either is returned.
        return [(0, _http(200, raw))]
    insp["server"].script = both_in_flight
    received, a_recorded = {}, threading.Event()
    client_inspect, record = resolver._client.inspect, resolver._ledger.record_observation

    def capture(request):
        answer = client_inspect(request)
        received[threading.current_thread().name] = answer.raw
        return answer

    def ordered(*args, **kwargs):
        if threading.current_thread().name == "B":
            assert a_recorded.wait(10)  # Deterministic: A commits first.
            return record(*args, **kwargs)
        try:
            return record(*args, **kwargs)
        finally:
            a_recorded.set()
    monkeypatch.setattr(resolver._client, "inspect", capture)
    monkeypatch.setattr(resolver._ledger, "record_observation", ordered)
    results = {}
    threads = [threading.Thread(name=name, target=lambda n=name: results.update(
        {n: resolver.inspect(cmd, world["pin"], counter)})) for name in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert received["A"] != received["B"]  # Distinct nonces: two different valid signed answers.
    with sqlite3.connect(Path(insp["ledger"]).as_uri() + "?mode=ro", uri=True) as db:
        persisted = db.execute("SELECT answer, request FROM supervisor_route_inspection WHERE command_id=?",
                               (cmd.command_id,)).fetchone()
    assert persisted[0].encode() == received["A"]  # A's bytes are what the ledger holds.
    offered = [observation.answer for _, observation in store.hub_calls]
    assert offered and all(answer == received["A"] for answer in offered)  # Never B's unpersisted bytes.
    assert all(observation.request == persisted[1].encode() for _, observation in store.hub_calls)
    assert results["A"].applied and not results["A"].observation.historical
    # B re-read and re-verified A's persisted row and offered THAT (idempotent Hub accepts it).
    assert results["B"].applied and results["B"].observation.historical
    assert results["B"].observation.answer == received["A"] and len(store.hub_calls) == 2


@LINUX
def test_readback_mismatch_after_write_rolls_back_and_stays_held(world, insp, monkeypatch):
    resolver, cmd, counter = held_attempt(world, insp)
    ledger, select, calls = resolver._ledger, resolver._ledger._select, []

    def lying_select(db, command_id):
        row = select(db, command_id)
        calls.append(row)
        if row is not None and row[6] != "held":  # Only the read-back of the just-written row.
            return (*row[:9], row[9] + " ", row[10])
        return row
    monkeypatch.setattr(ledger, "_select", lying_select)
    result = resolver.inspect(cmd, world["pin"], counter)
    assert (result.state, result.error) == ("held", "observation_unrecorded")
    monkeypatch.undo()
    assert ledger_state(insp, cmd.command_id) == ("held", None)  # Rolled back: nothing persisted.
    assert insp["store"].hub_calls == [] and len(calls) == 3  # Read, check, read-back; nothing offered.
