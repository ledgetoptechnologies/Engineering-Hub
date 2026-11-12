"""Explicit operator signer for a reviewed, fresh post-launch proposal.

Only a pre-existing root-private policy and distinct Ed25519 key authorize this
one record. No enrollment, key generation, renewal, or overwrite is provided.
The receiving directory is independently provisioned and read-only to runner.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import secrets
import stat
import sys
import time
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from engineering_hub.container_pilot_manifest import Platform
from engineering_hub.current_executor_observation import CurrentExecutorReleasePins
from .launch_supervisor import _canonical, _identity, _path, _require, _root_file, _root_parent, _strict
from .postlaunch_approval import DOMAIN, MAX_BYTES, verify_launch_proposal


class OperatorApprovalError(ValueError):
    pass


def _policy(raw):
    data = _strict(raw, MAX_BYTES)
    names = {'schema', 'release', 'expected_engine_id', 'approver_public_key', 'receipt_id',
             'receipt_sha256', 'launch_sha256', 'receipt_expires_at_ms', 'approval_expires_at_ms',
             'output_path', 'output_uid', 'output_gid'}
    _require(type(data) is dict and set(data) == names and data['schema'] == 'engineering-hub.operator-approval-policy.v1')
    release = data['release']
    _require(type(release) is dict and set(release) == set(CurrentExecutorReleasePins.__dataclass_fields__))
    release = dict(release)
    _require(type(release['public_key']) is str and len(release['public_key']) == 64)
    release['public_key'] = bytes.fromhex(release['public_key'])
    release['platform'] = Platform.model_validate(release['platform'])
    release = CurrentExecutorReleasePins(**release)
    _require(type(data['expected_engine_id']) is str and bool(data['expected_engine_id']))
    _require(type(data['approver_public_key']) is str and len(data['approver_public_key']) == 64)
    approver_public = bytes.fromhex(data['approver_public_key'])
    _require(len(approver_public) == 32 and approver_public != release.public_key)
    _require(type(data['receipt_id']) is str and str(UUID(data['receipt_id'])) == data['receipt_id'])
    for name in ('receipt_sha256', 'launch_sha256'):
        _require(type(data[name]) is str and len(data[name]) == 64 and all(c in '0123456789abcdef' for c in data[name]))
    for name in ('receipt_expires_at_ms', 'approval_expires_at_ms'):
        _require(type(data[name]) is int and data[name] > 0)
    for name in ('output_uid', 'output_gid'):
        _require(type(data[name]) is int and 0 < data[name] < 2**32 - 1)
    return data, release, approver_public, _path(data['output_path'])


def _output_parent(path, uid, gid):
    # Walk trusted root ancestors, then open the separately provisioned private
    # runner-owned delivery directory by descriptor with no symlink following.
    grandparent = _root_parent(path.parent, private=False)
    try:
        fd = os.open(path.parent.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=grandparent)
        try:
            info = os.fstat(fd)
            _require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and info.st_gid == gid
                     and stat.S_IMODE(info.st_mode) == 0o700
                     and _identity(info) == _identity(os.stat(path.parent.name, dir_fd=grandparent, follow_symlinks=False))
                     and path.parent.resolve(strict=True) == path.parent)
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(grandparent)


def publish(*, policy_file, proposal_file, signing_key_file):
    """Publish one approved record; a pre-existing output is never replaced."""
    parent = stage = fd = None
    try:
        _require(sys.platform == 'linux' and os.getuid() == os.geteuid() == 0)
        policy_path, proposal_path, key_path = map(_path, (policy_file, proposal_file, signing_key_file))
        _require(len({policy_path, proposal_path, key_path}) == 3)
        policy_raw, policy_id = _root_file(policy_path, MAX_BYTES)
        proposal_raw, proposal_id = _root_file(proposal_path, MAX_BYTES)
        key_raw, key_id = _root_file(key_path, 32)
        _require(len(key_raw) == 32)
        policy, release, approver_public, output = _policy(policy_raw)
        _require(output not in {policy_path, proposal_path, key_path})
        signer = Ed25519PrivateKey.from_private_bytes(key_raw)
        _require(signer.public_key().public_bytes(serialization.Encoding.Raw,
                 serialization.PublicFormat.Raw) == approver_public)
        proposal = verify_launch_proposal(proposal_raw, release=release,
                                           expected_engine_id=policy['expected_engine_id'])
        launch_hash = hashlib.sha256(_canonical(asdict(proposal.launch))).hexdigest()
        _require(proposal.receipt_id == policy['receipt_id'] and proposal.receipt_sha256 == policy['receipt_sha256']
                 and launch_hash == policy['launch_sha256'])
        now = int(time.time() * 1000)
        expires = min(policy['approval_expires_at_ms'], policy['receipt_expires_at_ms'],
                      proposal.launch.approval_expires_at_ms)
        _require(now < expires)
        payload = {'schema': 'engineering-hub.independent-launch-approval.v1', 'approval_id': str(uuid4()),
                   'receipt_id': proposal.receipt_id, 'launch': asdict(proposal.launch),
                   'approved_at_ms': now, 'expires_at_ms': expires}
        signature = base64.urlsafe_b64encode(signer.sign(DOMAIN + _canonical(payload))).rstrip(b'=').decode()
        raw = _canonical({'payload': payload, 'signature': signature})
        _require(len(raw) <= MAX_BYTES)
        parent = _output_parent(output, policy['output_uid'], policy['output_gid'])
        # Stage under the root-private policy directory so the runner cannot
        # swap the source entry before the atomic move into its directory.
        stage = _root_parent(policy_path, private=True)
        _require(os.fstat(stage).st_dev == os.fstat(parent).st_dev)
        temporary = '.approval-' + secrets.token_hex(32)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=stage)
        os.fchown(fd, policy['output_uid'], policy['output_gid'])
        _require(stat.S_IMODE(os.fstat(fd).st_mode) == 0o600)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            _require(count > 0)
            view = view[count:]
        os.fsync(fd)
        info = os.fstat(fd)
        _require(info.st_nlink == 1 and info.st_uid == policy['output_uid'] and info.st_gid == policy['output_gid']
                 and _identity(info) == _identity(os.stat(temporary, dir_fd=stage, follow_symlinks=False)))
        _require(_root_file(policy_path, MAX_BYTES)[1] == policy_id and _root_file(proposal_path, MAX_BYTES)[1] == proposal_id
                 and _root_file(key_path, 32)[1] == key_id)
        _require(verify_launch_proposal(proposal_raw, release=release,
                 expected_engine_id=policy['expected_engine_id']) == proposal and int(time.time() * 1000) < expires)
        _require(_identity(os.stat(output.parent, follow_symlinks=False)) == _identity(os.fstat(parent)))
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        _require(rename(stage, os.fsencode(temporary), parent, os.fsencode(output.name), 1) == 0)
        os.fsync(parent)
        os.fsync(stage)
    except Exception:
        # If publication fails, preserve any temporary evidence; never replace
        # or delete another operator's existing approval.
        raise OperatorApprovalError('Operator approval unavailable; preserve evidence') from None
    finally:
        if fd is not None: os.close(fd)
        if parent is not None: os.close(parent)
        if stage is not None: os.close(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--proposal', required=True)
    parser.add_argument('--signing-key', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if not args.execute:
        print('not_executed')
        return 0
    try:
        publish(policy_file=args.policy, proposal_file=args.proposal, signing_key_file=args.signing_key)
        print('approval_published')
        return 0
    except Exception:
        print('operator approval unavailable; preserve evidence', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
