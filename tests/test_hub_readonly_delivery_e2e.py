"""Synthetic Engine, real separate consumer and read-only bind-mount delivery.

Run explicitly as root inside a disposable mount namespace; never touches a
host Engine or persistent service. This is not a full installed-runner test.
"""
import asyncio
import ctypes
from dataclasses import asdict
import hashlib
import hmac
import secrets
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

import pytest

if sys.platform != 'linux':
    pytest.skip('Linux mount and peer-credential test', allow_module_level=True)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hub_container_executor import launch_supervisor as launch
from hub_container_executor.postlaunch_approval import verify_launch_proposal
from tests.test_hub_container_executor_launch_supervisor import (synthetic_issuer, short_root,
    root_journal, serve_fixture, current_pins)


_RECEIPT = 'HUB_READONLY_E2E_COMPLETED_8f2732ab'


def _require_child_success(returncode, output):
    if returncode != 0 or _RECEIPT not in output.splitlines():
        raise AssertionError('mount test did not complete: ' + output[-4000:])


def test_readonly_completion_requires_actual_receipt():
    with pytest.raises(AssertionError):
        _require_child_success(0, '1 skipped in 0.01s')
    with pytest.raises(AssertionError):
        _require_child_success(0, '1 passed in 0.01s')
    with pytest.raises(AssertionError):
        _require_child_success(1, _RECEIPT)
    _require_child_success(0, _RECEIPT + '\n1 passed')


def _consume_child_capability():
    try:
        fd = int(os.environ['HUB_READONLY_E2E_FD'])
        nonce = bytes.fromhex(os.environ['HUB_READONLY_E2E_NONCE'])
        if fd < 3 or len(nonce) != 32 or not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise ValueError()
        os.set_blocking(fd, False)
        received = os.read(fd, 33)
        os.close(fd)
        if not hmac.compare_digest(nonce, received):
            raise ValueError()
    except Exception:
        pytest.fail('child test capability unavailable', pytrace=False)


def test_child_marker_without_inherited_capability_is_rejected(monkeypatch):
    monkeypatch.setenv('HUB_READONLY_E2E_CHILD', '1')
    monkeypatch.delenv('HUB_READONLY_E2E_FD', raising=False)
    monkeypatch.delenv('HUB_READONLY_E2E_NONCE', raising=False)
    with pytest.raises(pytest.fail.Exception):
        _consume_child_capability()


def _cleanup_visible(path, identity):
    info = os.lstat(path)
    if (info.st_dev, info.st_ino) != identity or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RuntimeError('test directory identity changed; preserve evidence')
    leaf = path / 'issuer.sock'
    try:
        info = os.lstat(leaf)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
            raise RuntimeError('test socket identity changed; preserve evidence')
        os.unlink(leaf)
    try:
        os.rmdir(path / 'approval')  # Never follows symlinks or removes mounted contents.
    except FileNotFoundError:
        pass
    os.rmdir(path)


def test_readonly_mount_separate_consumer_and_operator(request):
    if os.environ.get('HUB_RUN_READONLY_E2E') != '1' or os.getuid() != 0:
        pytest.skip('explicit root opt-in required for isolated bind-mount fixture')
    if os.environ.get('HUB_READONLY_E2E_CHILD') != '1':
        visible = Path(tempfile.mkdtemp(prefix='eh-ro-', dir='/tmp'))
        visible.chmod(0o755)
        info = os.lstat(visible)
        identity = (info.st_dev, info.st_ino)
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        process = None
        try:
            nonce = secrets.token_bytes(32)
            os.write(write_fd, nonce)
            os.close(write_fd)
            write_fd = None
            env = dict(os.environ, HUB_READONLY_E2E_CHILD='1', HUB_READONLY_E2E_FD=str(read_fd),
                       HUB_READONLY_E2E_NONCE=nonce.hex(), HUB_READONLY_E2E_DIR=str(visible))
            command = [sys.executable, '-m', 'pytest', '-q', '-s', '-p', 'no:cacheprovider',
                       str(Path(__file__).resolve()), '-k', 'readonly_mount_separate_consumer_and_operator']
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, env=env, start_new_session=True, pass_fds=(read_fd,))
            os.close(read_fd)
            read_fd = None
            try:
                output, _ = process.communicate(timeout=35)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    output, _ = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    output, _ = process.communicate(timeout=3)
                pytest.fail('bounded mount-test process timed out: ' + output[-2000:])
            _require_child_success(process.returncode, output)
        finally:
            if read_fd is not None: os.close(read_fd)
            if write_fd is not None: os.close(write_fd)
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
            _cleanup_visible(visible, identity)
        return
    _consume_child_capability()
    synthetic_issuer = request.getfixturevalue('synthetic_issuer')
    # Unshare first: every subsequent mount/umount belongs only to this test
    # process. The subprocess consumer inherits this private mount namespace.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(0x00020000) != 0:
        pytest.skip('private mount namespace unavailable')
    subprocess.run(['mount', '--make-rprivate', '/'], check=True, timeout=5)
    authority, _, _ = synthetic_issuer
    root = Path(authority._socket_path.parent)
    visible = Path(os.environ['HUB_READONLY_E2E_DIR'])
    mountpoint = visible / 'approval'
    mountpoint.mkdir(mode=0o700)
    source = root / 'delivery'
    source.mkdir(mode=0o700)
    os.chown(source, 12345, 12345)
    mounted = False
    child = None
    issuer = visible / 'issuer.sock'
    try:
        subprocess.run(['mount', '--bind', str(source), str(mountpoint)], check=True, timeout=5)
        mounted = True
        subprocess.run(['mount', '-o', 'remount,bind,ro', str(mountpoint)], check=True, timeout=5)
        authority._listener.close()
        authority._listener = socket.socket(socket.AF_UNIX)
        authority._listener.bind(str(issuer))
        os.chown(issuer, 0, 12345)
        issuer.chmod(0o660)
        authority._listener.listen(1)
        authority._consumer_uid = authority._consumer_gid = 12345
        signer = Ed25519PrivateKey.generate()
        public = signer.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        key = root / 'operator.key'
        key.write_bytes(signer.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                              serialization.NoEncryption()))
        key.chmod(0o600)
        release = {n: getattr(authority.release, n) for n in authority.release.__dataclass_fields__}
        release['public_key'] = release['public_key'].hex()
        release['platform'] = authority.release.platform.model_dump(mode='json')
        script = '''import asyncio,json,os,signal,sys\nfrom engineering_hub.container_pilot_manifest import Platform\nfrom engineering_hub.current_executor_observation import CurrentExecutorReleasePins\nfrom hub_container_executor.runtime_identity import ExecutorReleaseEnrollment,LaunchReceiptIdentityVerifier\nfrom hub_container_executor.launch_transport import UnixLaunchReceiptTransport\nfrom hub_container_executor.postlaunch_delivery import wait_for_postlaunch_approval\nfrom hub_container_executor.installed_runner import _continuous_bound_signals\nv=json.loads(input()); d=v['release']; d['public_key']=bytes.fromhex(d['public_key']); d['platform']=Platform(**d['platform']); e=ExecutorReleaseEnrollment(**d)\nos.setgroups([]); os.setgid(12345); os.setuid(12345); print('ready',flush=True); input()\nt=UnixLaunchReceiptTransport(socket_path=v['socket'],expected_issuer_uid=0,expected_issuer_gid=0); x=LaunchReceiptIdentityVerifier(enrollment=e,issuer_transport=t,max_receipt_lifetime_seconds=30,exchange_timeout_ms=2000); r=x.accepted_launch_receipt(); print(json.dumps(r[0]),flush=True)\nasync def run():\n with _continuous_bound_signals() as state:\n  p=CurrentExecutorReleasePins(**{n:getattr(e,n) for n in CurrentExecutorReleasePins.__dataclass_fields__}); print('waiting',flush=True)\n  lease=await wait_for_postlaunch_approval(v['approval'],verifier=x,release=p,approver_public_key=bytes.fromhex(v['public']),expected_engine_id='synthetic-engine',timeout_ms=10000); lease.check(); assert os.statvfs(v['approval']).f_flag & os.ST_RDONLY\n  try: open(v['approval'],'ab'); raise AssertionError('writable')\n  except OSError: pass\n  state.serving=True; print('accepted',flush=True); await state.stop.wait(); print('drained',flush=True)\nasyncio.run(run()); x.close(); t.close()\n'''
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
        child = subprocess.Popen([sys.executable, '-u', '-c', script], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        child.stdin.write(json.dumps({'release': release, 'socket': str(issuer), 'public': public.hex(),
                                      'approval': str(mountpoint / 'approval.json')}) + '\n')
        child.stdin.flush()
        assert child.stdout.readline().strip() == 'ready'
        authority._consumer.close()
        authority._consumer = launch._Process(child.pid)
        thread, served = serve_fixture(authority)
        child.stdin.write('go\n')
        child.stdin.flush()
        receipt_id = json.loads(child.stdout.readline())
        assert child.stdout.readline().strip() == 'waiting'
        thread.join(5)
        assert not thread.is_alive() and served == [receipt_id]
        proposal_file = root / 'proposal-ro.json'
        authority.write_approval_proposal(str(proposal_file), timeout_ms=1000)
        release_pin, _ = current_pins(authority)
        proposal = verify_launch_proposal(proposal_file.read_bytes(), release=release_pin,
                                          expected_engine_id='synthetic-engine')
        policy = {'schema': 'engineering-hub.operator-approval-policy.v1', 'release': release,
                  'expected_engine_id': 'synthetic-engine', 'approver_public_key': public.hex(),
                  'receipt_id': proposal.receipt_id, 'receipt_sha256': proposal.receipt_sha256,
                  'launch_sha256': hashlib.sha256(launch._canonical(asdict(proposal.launch))).hexdigest(),
                  'receipt_expires_at_ms': proposal.receipt_expires_at_ms,
                  'approval_expires_at_ms': proposal.receipt_expires_at_ms,
                  'output_path': str(source / 'approval.json'), 'output_uid': 12345, 'output_gid': 12345}
        policy_file = root / 'policy-ro.json'
        policy_file.write_bytes(launch._canonical(policy))
        policy_file.chmod(0o600)
        cmd = [sys.executable, '-m', 'hub_container_executor.operator_approval', '--execute',
               '--policy', str(policy_file), '--proposal', str(proposal_file), '--signing-key', str(key)]
        result = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=8)
        assert result.returncode == 0, result.stderr
        accepted = child.stdout.readline().strip()
        assert accepted == 'accepted', child.stderr.read() if not accepted else accepted
        child.send_signal(signal.SIGTERM)
        assert child.stdout.readline().strip() == 'drained'
        assert child.wait(timeout=5) == 0, child.stderr.read()
    finally:
        try:
            if child is not None:
                try:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=2)
                finally:
                    child.stdin.close()
                    child.stdout.close()
                    child.stderr.close()
        finally:
            if mounted:
                # Never recursively remove through a still-mounted path.
                subprocess.run(['umount', str(mountpoint)], check=True, timeout=5)
    print(_RECEIPT, flush=True)
