"""Bounded synthetic Linux tests of the bootstrap child environment.

The test explicitly MODELS systemd's %h/%t and ${NOTIFY_SOCKET} expansion before
executing the extracted argv through the real /usr/bin/env. It substitutes only
the final daemon helper with a fixed env collector or Python fixture; it never starts systemd,
RootlessKit, Docker, SSH/GPG agents, mapping helpers or host services. Datagram
delivery is not proof of systemd255 notification attribution or daemon readiness.
No current process environment or real credentials are copied into subprocesses.
"""

import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
from uuid import uuid4

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Synthetic Linux env/Unix-socket tests")
UNIT = Path(__file__).resolve().parents[1] / "deployment/staging-bootstrap/eh-pilot-docker.service"
HELPER = "/usr/bin/dockerd-rootless.sh"
EXPECTED_NAMES = {
    "HOME", "PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "NOTIFY_SOCKET",
    "DOCKERD_ROOTLESS_ROOTLESSKIT_NET", "DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR",
    "DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS",
}
CANARIES = {
    "HOME": "/fixture-untrusted-home", "PATH": "/fixture-untrusted-path",
    "XDG_RUNTIME_DIR": "/fixture-untrusted-runtime",
    "DBUS_SESSION_BUS_ADDRESS": "fixture-untrusted-bus",
    "SSH_AUTH_SOCK": "/fixture-unused-ssh-socket", "SSH_AGENT_PID": "12345",
    "GNUPGHOME": "/fixture-unused-gpg-home", "GPG_AGENT_INFO": "fixture-unused-gpg",
    "DOCKER_HOST": "fixture-unused-host", "DOCKER_CONTEXT": "fixture-unused-context",
    "DOCKER_CONFIG": "/fixture-unused-config", "DOCKER_CERT_PATH": "/fixture-unused-cert",
    "DOCKER_TLS": "1", "DOCKER_TLS_VERIFY": "1", "DOCKERD": "/fixture-never-executed",
    "_DOCKERD_ROOTLESS_CHILD": "fixture-not-a-child",
    "DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS": "fixture-unused-flags",
    "DOCKERD_ROOTLESS_ROOTLESSKIT_NET": "fixture-wrong-network",
    "DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS": "true",
    "DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR": "/fixture-wrong-state",
    "HTTP_PROXY": "fixture-unused-proxy", "HTTPS_PROXY": "fixture-unused-proxy",
    "ALL_PROXY": "fixture-unused-proxy", "AWS_ACCESS_KEY_ID": "synthetic-not-a-real-key",
    "PYTHONPATH": "/fixture-unused-pythonpath", "BASH_ENV": "/fixture-unused-bashenv",
}


@pytest.fixture
def private_root():
    # Short private path avoids sockaddr_un limits even under long pytest roots.
    with tempfile.TemporaryDirectory(prefix="eh-env-", dir="/tmp") as root:
        yield Path(root)


def _modeled_argv(root, notify):
    lines = [line.split("=", 1)[1] for line in UNIT.read_text().splitlines()
             if line.startswith("ExecStart=")]
    assert len(lines) == 1
    raw = shlex.split(lines[0])
    assert raw[:2] == ["/usr/bin/env", "-i"]
    assert raw.count(HELPER) == 1
    # Deliberately a model, NOT an invocation or emulation of systemd's parser.
    expanded = [value.replace("%h", str(root)).replace("%t", str(root))
                .replace("${NOTIFY_SOCKET}", notify) for value in raw]
    assert all("%" not in value and "${" not in value for value in expanded)
    return expanded


def _run_fixture(argv, script, *, extra_args=()):
    index = argv.index(HELPER)
    command = [*argv[:index], sys.executable, "-I", "-S", "-c", script,
               *argv[index + 1:], *extra_args]
    return subprocess.run(command, env=dict(CANARIES), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=5, check=False)


def test_real_env_filters_canaries_and_preserves_only_modeled_whitelist(private_root):
    notify = str(private_root / "notify.sock")
    argv = _modeled_argv(private_root, notify)
    # Inspect BEFORE Python startup: locale coercion may legitimately add
    # LC_CTYPE inside Python and is not inherited manager-environment leakage.
    index = argv.index(HELPER)
    result = subprocess.run([*argv[:index], "/usr/bin/env", "-0"], env=dict(CANARIES),
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
                            check=False)
    assert result.returncode == 0, "Synthetic environment fixture failed"
    assignments = [value.decode("utf-8") for value in result.stdout.split(b"\0") if value]
    environment = dict(value.split("=", 1) for value in assignments)
    assert len(assignments) == len(environment)
    assert set(environment) == EXPECTED_NAMES
    assert environment == {
        "HOME": str(private_root), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "XDG_RUNTIME_DIR": str(private_root),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(private_root / "bus"),
        "NOTIFY_SOCKET": notify, "DOCKERD_ROOTLESS_ROOTLESSKIT_NET": "none",
        "DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS": "false",
        "DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR": str(private_root / "eh-pilot-rootlesskit"),
    }
    assert not (set(environment) & (set(CANARIES) - EXPECTED_NAMES))
    arguments = _run_fixture(argv, "import json,sys; print(json.dumps(sys.argv[1:]))")
    assert arguments.returncode == 0, "Synthetic argv fixture failed"
    assert json.loads(arguments.stdout) == argv[index + 1:]


@pytest.mark.parametrize("mutation", ["reviewed", "missing", "true", "empty"])
def test_modeled_helper_default_requires_explicit_non_detached_override(private_root, mutation):
    argv = _modeled_argv(private_root, "@unused-fixture-notify")
    index = next(index for index, value in enumerate(argv)
                 if value.startswith("DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS="))
    if mutation == "missing":
        argv.pop(index)
    elif mutation != "reviewed":
        argv[index] = "DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=" + ("true" if mutation == "true" else "")
    # Models the installed helper's ${VARIABLE:-true} selection only. No
    # RootlessKit, namespace operation or daemon is executed by this fixture.
    script = """
import os,json
value=os.environ.get('DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS') or 'true'
print(json.dumps({'detach':value,'net':os.environ.get('DOCKERD_ROOTLESS_ROOTLESSKIT_NET')}))
"""
    result = _run_fixture(argv, script)
    assert result.returncode == 0
    modeled = json.loads(result.stdout)
    assert modeled['net'] == 'none'
    assert modeled['detach'] == ('false' if mutation == 'reviewed' else 'true')


NOTIFY_FIXTURE = """
import os,socket
address=os.environ.get('NOTIFY_SOCKET')
if not address:
    raise SystemExit(3)
if address.startswith('@'):
    address='\\0'+address[1:]
with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as client:
    client.settimeout(.5)
    try:
        client.sendto(b'READY=1',address)
    except OSError:
        raise SystemExit(4)
"""


@pytest.mark.parametrize("abstract", [False, True])
def test_modeled_notification_forwarding_delivers_private_datagram(private_root, abstract):
    notify = ("@eh-env-" + uuid4().hex if abstract else str(private_root / "notify.sock"))
    receiver_address = "\0" + notify[1:] if abstract else notify
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.settimeout(.5)
        receiver.bind(receiver_address)
        result = _run_fixture(_modeled_argv(private_root, notify), NOTIFY_FIXTURE)
        assert result.returncode == 0, "Synthetic notification fixture failed"
        assert receiver.recv(64) == b"READY=1"


@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_missing_or_changed_notification_assignment_cannot_notify_original_receiver(private_root, mutation):
    notify = str(private_root / "notify.sock")
    argv = _modeled_argv(private_root, notify)
    index = next(index for index, value in enumerate(argv) if value.startswith("NOTIFY_SOCKET="))
    if mutation == "missing":
        argv.pop(index)
    else:
        argv[index] = "NOTIFY_SOCKET=" + str(private_root / "absent.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.settimeout(.1)
        receiver.bind(notify)
        result = _run_fixture(argv, NOTIFY_FIXTURE)
        assert result.returncode in {3, 4}
        with pytest.raises(socket.timeout):
            receiver.recv(64)


def test_environment_filter_does_not_revoke_known_same_uid_socket_access(private_root):
    known_socket = str(private_root / "known-ssh-fixture.sock")
    script = """
import os,socket,sys
assert 'SSH_AUTH_SOCK' not in os.environ
with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as client:
    client.settimeout(.5)
    client.sendto(b'fixture-same-uid-access',sys.argv[-1])
"""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.settimeout(.5)
        receiver.bind(known_socket)
        result = _run_fixture(_modeled_argv(private_root, "@unused-fixture-notify"), script,
                              extra_args=(known_socket,))
        assert result.returncode == 0, "Synthetic same-UID fixture failed"
        assert receiver.recv(64) == b"fixture-same-uid-access"
