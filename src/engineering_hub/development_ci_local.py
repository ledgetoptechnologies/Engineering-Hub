"""Private same-host CI provider; no endpoint, daemon, activation or recovery.

The Hub ledger and enrolled controller must be on the SAME Linux host. This is
not a NAS-to-staging transport. Only private callback observations from this
process can complete a job; result.json is never read as execution evidence.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import uuid

from .development_ci import DevelopmentCIAttempt, DevelopmentCIManifest, DevelopmentCITerminal
from .lifecycle import finish_owned


def _isolated_runtime():
    return sys.flags.isolated == 1


def _load_controller(path, digest):
    """Load the exact installed bytes, not code from the candidate checkout."""
    if not _isolated_runtime():
        raise ValueError('Isolated trusted host interpreter required')
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('Canonical installed controller required')
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError('Private installed controller directory required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 65536):
            raise ValueError('Protected installed controller required')
        raw = os.read(fd, 65537)
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Installed controller digest mismatch')
    finally:
        os.close(fd)
    module = ModuleType('_eh_enrolled_ci_controller')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


class LocalDevelopmentCIProvider:
    """Explicit same-process bridge to an independently installed controller.

    Ref/checkout/output/socket are operator bindings, never agent payloads.
    Receipts are private process-local capabilities: no restart adoption. The
    queue's durable original attempt remains reserved on ambiguity or restart.
    Operators must exclude all other machine writers before composition.
    """
    def __init__(self, manifest, *, controller_path, checkout, development_ref,
                 engine_socket, output_root):
        if type(manifest) is not DevelopmentCIManifest:
            raise ValueError('Enrolled CI manifest required')
        self.manifest = manifest
        self.checkout = Path(checkout).resolve(strict=True)
        installed = Path(controller_path).resolve(strict=True)
        if self.checkout == installed or self.checkout in installed.parents:
            raise ValueError('Controller must be independent of candidate checkout')
        for entry in sys.path:
            location = Path(entry or os.getcwd()).resolve()
            if location == self.checkout or self.checkout in location.parents:
                raise ValueError('Candidate checkout cannot be an interpreter import path')
        # Reject candidate-hosted code BEFORE loading/executing any of its bytes.
        self.controller = _load_controller(controller_path, manifest.controller_digest)
        if (not re.fullmatch(r'refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,180}', development_ref)
            or development_ref == 'refs/heads/main'):
            raise ValueError('Fixed development ref required')
        self.ref, self.engine = development_ref, engine_socket
        self.output = self.controller.private_directory(output_root)
        self._attempts, self._receipts = set(), {}
        self._lock = asyncio.Lock()

    def _docker(self, *parts):
        # Separate private empty Docker config; no ambient credentials/context.
        with tempfile.TemporaryDirectory(prefix='inspect-', dir=self.output) as home:
            raw = self.controller.checked_output(
                ['/usr/bin/docker', '--config', home, '--host', self.engine, *parts],
                environment={'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8', 'HOME':home})
        return json.loads(raw)

    def _environment(self):
        self.controller.engine_socket(self.engine)
        self.controller.validate_engine(self._docker('info', '--format', '{{json .}}'), self.manifest.engine_id)
        image = self._docker('image', 'inspect', self.manifest.image_id)[0]
        labels = image['Config'].get('Labels', {})
        if image.get('Id') != self.manifest.image_id or tuple(
            'sha256:' + labels.get('io.engineering-hub.ci.' + label + '-sha256', '')
            for label in self.controller.FILES.values()) != self.manifest.dependency_digests:
            raise ValueError('Enrolled CI dependency image changed')

    async def invoke(self, attempt, admission_guard):
        async with self._lock:
            return await finish_owned(self._invoke(attempt, admission_guard))

    async def _invoke(self, attempt, admission_guard):
        if (type(attempt) is not DevelopmentCIAttempt
            or attempt.manifest_digest != self.manifest.digest
            or not re.fullmatch('[a-f0-9]{40}', attempt.source_commit)
            or attempt.attempt_id in self._attempts):
            raise ValueError('Original bound CI attempt required')
        admission_guard()
        self._attempts.add(attempt.attempt_id)  # never replay even failed admission
        loop = asyncio.get_running_loop()
        observations = []

        def guard():
            # Queue/owner callbacks stay on their event-loop thread. A late
            # rendezvous can only check admission, never launch a host effect.
            pending = Future()
            def check():
                if not pending.set_running_or_notify_cancel():
                    return
                try:
                    value = admission_guard()
                except BaseException as error:
                    pending.set_exception(error)
                else:
                    pending.set_result(value)
            loop.call_soon_threadsafe(check)
            try:
                return pending.result(timeout=10)
            except BaseException:
                pending.cancel()
                raise

        def observed(value):
            if observations:
                raise ValueError('Duplicate terminal observation')
            observations.append(json.loads(json.dumps(value, allow_nan=False)))

        def execute():
            self._environment()
            args = SimpleNamespace(checkout=str(self.checkout), ref=self.ref,
                image=self.manifest.image_id, engine=self.engine,
                engine_id=self.manifest.engine_id, output_root=str(self.output),
                profile=self.manifest.profile, timeout=self.manifest.budget_seconds)
            try:
                self.controller.run(args, expected_commit=attempt.source_commit,
                    admission_guard=guard, terminal_observer=observed)
            except Exception:
                # Cleanup may still have supplied a genuine terminal observation.
                # Never infer it from exit status or advisory result.json.
                if not observations:
                    raise
        await asyncio.to_thread(execute)
        if len(observations) != 1:
            raise ValueError('Private terminal observation unavailable')
        value = observations[0]
        if (value.get('source_commit') != attempt.source_commit
            or value.get('image_id') != self.manifest.image_id
            or value.get('engine_id') != self.manifest.engine_id
            or value.get('profile') != self.manifest.profile
            or value.get('terminal_verified') is not True
            or value.get('cgroup_terminal', {}).get('descendants_empty') is not True
            or not re.fullmatch('[a-f0-9]{64}', value.get('container_id', ''))
            or type(value.get('exit_code')) is not int
            or not 0 <= value['exit_code'] <= 255):
            raise ValueError('Bound terminal observation unavailable')
        receipt = 'ci-' + uuid.uuid4().hex
        self._receipts[receipt] = (attempt, value)
        return receipt

    async def verify_terminal(self, attempt, receipt_id):
        async with self._lock:
            return await finish_owned(asyncio.to_thread(self._verify, attempt, receipt_id))

    def _verify(self, attempt, receipt_id):
        bound, value = self._receipts[receipt_id]
        if bound != attempt:
            raise ValueError('Foreign terminal receipt')
        self._environment()
        row = self._docker('container', 'inspect', value['container_id'])[0]
        if row.get('Id') != value['container_id']:
            raise ValueError('Retained container identity changed')
        self.controller.verify_created(row, self.manifest.image_id,
            value['container_name'], Path(value['input_directory']), self.manifest.profile, value['intent'])
        state = row['State']
        if (state.get('Status') != 'exited'
            or state.get('Pid') != 0 or type(state.get('ExitCode')) is not int
            or any(state.get(k) for k in ('Running', 'Paused', 'Restarting'))
            or state.get('ExitCode') != value['exit_code']):
            raise ValueError('Retained container not terminal')
        # Timeout/cleanup failures cannot turn raw exit zero into success.
        exit_code = value['exit_code']
        if value.get('timed_out') or value.get('failed'):
            exit_code = exit_code or 124
        return DevelopmentCITerminal(attempt, receipt_id, self.manifest.engine_id,
            self.manifest.image_id, value['container_id'], exit_code, True, True)
