"""Actual private HTTPS/controller/queue wire tests; explicitly no Docker backend.

The backend below only copies synthetic fixture bytes. These tests do not prove
Docker isolation, Git provenance, NAS connectivity or browser/phone delivery.
"""
import asyncio
import base64
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import io
import ipaddress
import json
import os
import shutil
import socket
import ssl
import threading
import uuid
import zipfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest
import pytest_asyncio

from engineering_hub.repository import HubRepository, HubError
from engineering_hub.staging_client import StagingClient, StagingConnectionConfig
from engineering_hub.staging_controller import StagingController, compose_queue
from engineering_hub.staging_resources import StagingResourceStore
from engineering_hub.work_queue import WorkAction, WorkQueue, WorkTarget
from hub_staging_service.__main__ import BoundedServer, handler
from hub_staging_service.core import Service


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Real private credential file modes require POSIX')
HTML = b'<!doctype html><title>Synthetic wire integration</title><p>fixture only</p>\n'
PROJECTS = {'engineering-hub': {'recipes': ['static']}}


def request():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as bundle:
        bundle.writestr('index.html', HTML)
    raw = stream.getvalue()
    return {'request_id': str(uuid.uuid4()), 'project': 'engineering-hub', 'recipe': 'static',
            'source_commit': 'a' * 40, 'source_digest': hashlib.sha256(raw).hexdigest(),
            'source_zip_base64': base64.b64encode(raw).decode()}


class SyntheticCopyBackend:
    """Synthetic non-Docker backend: never executes fixture code or subprocesses."""
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()

    def block(self):
        self.entered.clear()
        self.release.clear()

    def run(self, paths, profile, command_id):
        self.entered.set()
        if not self.release.wait(8):
            raise RuntimeError('Synthetic fixture gate was not released')
        for kind in ('build', 'preview'):
            shutil.copyfile(paths['workspace'] / 'index.html', paths[kind] / 'index.html')
        for stage in ('build', 'test'):
            (paths['test_run'] / (stage + '.log')).write_bytes(b'')
        return {'preview_ready': True, 'stages': {
            stage: {'exit_code': 0, 'log_bytes': 0} for stage in ('build', 'test')}}


def private_tls(root):
    """A disposable CA and separate server certificate with the actual IP SAN."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic wire CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
              key_encipherment=False, data_encipherment=False, key_agreement=False,
              key_cert_sign=True, crl_sign=True, encipher_only=None, decipher_only=None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic loopback worker')]))
        .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))
    ca_path, cert, secret = (root / name for name in ('ca.pem', 'server.pem', 'server.key'))
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    secret.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    secret.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, secret)
    return ca_path, context


class Wire:
    def __init__(self, root):
        self.root, self.now = root, [1800000000]
        self.backend = SyntheticCopyBackend()
        profile = {'image_id': 'sha256:' + 'a' * 64, 'build_argv': ['synthetic-only'],
                   'test_argv': ['synthetic-only'], 'cpus': 1, 'memory_mb': 128,
                   'pids': 32, 'timeout_seconds': 30, 'output_bytes': 1048576, 'log_bytes': 65536}
        self.service = Service(root / 'worker', {'engineering-hub': {'recipes': {'static': profile}}},
                               self.backend, capacity_bytes=128 * 1024**2)
        token = 'synthetic-wire-only-' + uuid.uuid4().hex
        credential = root / 'credential'
        credential.write_text(token)
        credential.chmod(0o600)
        ca, context = private_tls(root)
        self.counts, self.received, self.guard = Counter(), [], threading.Lock()
        self.drop_jobs = self.drop_cleanup = False
        wire = self
        production_handler = handler(self.service, token, {ipaddress.ip_address('127.0.0.1')})

        class ObservedHandler(production_handler):
            def authorize(self):
                super().authorize()
                with wire.guard:
                    wire.counts[(self.command, self.path)] += 1
                    wire.received.append((self.command, self.path))

            def respond(self, status, value, content_type='application/json'):
                # dispatch has ALREADY called real Service.submit/cleanup here.
                # Return normally after closing the connection: no second 500,
                # handler exception or client-side synthetic transport is used.
                with wire.guard:
                    drop = (self.command == 'POST' and
                            (self.path == '/v1/jobs' and status == 202 and wire.drop_jobs or
                             self.path == '/v1/cleanup' and status == 200 and wire.drop_cleanup))
                    if drop:
                        if self.path == '/v1/jobs':
                            wire.drop_jobs = False
                        else:
                            wire.drop_cleanup = False
                if drop:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                return super().respond(status, value, content_type)

        self.server = BoundedServer(('127.0.0.1', 0), ObservedHandler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.config = StagingConnectionConfig('https://127.0.0.1:' + str(self.server.server_port),
            credential, ca_file=ca, enabled=True, projects=PROJECTS)
        self.controllers, self.repos = [], []
        try:
            self.new_controller()
        except BaseException:
            self.server.server_close()
            self.service.close()
            for repo in self.repos:
                repo.close()
            raise
        self.thread.start()

    def new_controller(self, existing_queue=None):
        repo = existing_queue.repo if existing_queue is not None else HubRepository(self.root / 'hub.db')
        if repo not in self.repos:
            self.repos.append(repo)
        principal = object()
        resources = StagingResourceStore(repo, admin_actors=frozenset({'human:1'}),
            control_principal=principal, clock=lambda: self.now[0])
        try:
            resources.set_policy('human:1', 'engineering-hub', 0,
                {'codex': ['read', 'build', 'test', 'cleanup', 'preview'],
                 'human:1': ['read', 'cleanup', 'preview']})
        except HubError as error:
            if error.code != 'revision_conflict':
                raise
        queue, actions = compose_queue(repo, existing_queue, PROJECTS_RECIPES, 'synthetic-host')
        controller = StagingController(repo, queue, StagingClient(self.config), resources=resources,
            control_principal=principal, actions=actions, poll_interval=0.01, max_poll_seconds=6)
        self.controllers.append(controller)
        self.controller, self.resources, self.repo = controller, resources, repo
        return controller

    def posts(self, path='/v1/jobs'):
        with self.guard:
            return self.counts[('POST', path)]

    async def wait_backend(self):
        assert await asyncio.wait_for(asyncio.to_thread(self.backend.entered.wait, 3), 4)

    async def close(self):
        self.backend.release.set()
        try:
            for controller in self.controllers:
                await asyncio.wait_for(controller.close(), 10)
        finally:
            try:
                await asyncio.wait_for(asyncio.to_thread(self.server.shutdown), 3)
            finally:
                self.server.server_close()
                self.thread.join(3)
                if self.service.thread:
                    self.service.thread.join(3)
                    assert not self.service.thread.is_alive()
                self.service.close()
                for repo in self.repos:
                    repo.close()
                assert not self.thread.is_alive()


PROJECTS_RECIPES = {'engineering-hub': ['static']}


@pytest_asyncio.fixture
async def wire(tmp_path):
    value = Wire(tmp_path)
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_real_tls_lost_acceptance_restart_get_reconciliation_reserves_machine(wire):
    first = await wire.controller.submit('codex', request())
    wire.drop_jobs = True
    assert (await wire.controller.run_once())['job']['state'] == 'held'
    second = await wire.controller.submit('codex', request())
    assert await wire.controller.run_once() is None
    assert wire.posts() == 1
    assert wire.controller.queue.get_job(second['job']['id'])['state'] == 'queued'
    await wire.controller.close()
    restarted = wire.new_controller()
    assert await restarted.run_once() is None  # The persisted physical hold survives restart.
    assert wire.posts() == 1
    done = await restarted.reconcile(first['job']['id'])
    assert done['job']['state'] == 'succeeded'
    assert wire.posts() == 1
    assert wire.counts[('GET', '/v1/jobs/' + first['job']['id'])] >= 1
    assert (await restarted.run_once())['job']['id'] == second['job']['id']
    assert wire.posts() == 2


@pytest.mark.asyncio
async def test_real_tls_revoked_grant_prevents_any_job_post(wire):
    submitted = await wire.controller.submit('codex', request())
    wire.resources.set_policy('human:1', 'engineering-hub', 1, {'codex': ['read', 'build']})
    assert await wire.controller.run_once() is None
    assert wire.posts() == 0
    assert wire.controller.queue.get_job(submitted['job']['id'])['state'] == 'queued'


@pytest.mark.asyncio
async def test_real_tls_existing_production_priority_and_upstream_verifiers(tmp_path):
    wire = Wire(tmp_path)
    try:
        # The same repository and physical queue are used by both environments.
        repo = wire.repo
        calls = Counter()

        def target(actor, key, environment, chosen):
            calls['target'] += 1
            return (actor, key, environment, chosen) == ('codex', 'production', 'production', pinned)

        def approval(actor, key, environment, approval_id, chosen):
            calls['approval'] += 1
            return (actor, key, environment, approval_id, chosen) == (
                'codex', 'production', 'production', 'approved', pinned)

        queue = WorkQueue(repo, {'production': WorkAction('synthetic-host', frozenset({'production'}),
                          project='engineering-hub')}, target_verifier=target, approval_verifier=approval)
        pinned = WorkTarget(str(uuid.uuid4()), 'b' * 40, 'sha256:' + 'c' * 64)
        production = queue.submit(actor='codex', request_id='synthetic-production', action_key='production',
                                  environment='production', target=pinned, approval_id='approved')
        controller = wire.new_controller(queue)
        staging = await controller.submit('codex', request())
        assert controller.queue is queue
        assert await controller.run_once() is None
        assert wire.posts() == 0
        assert queue.get_job(production['id'])['state'] == queue.get_job(staging['job']['id'])['state'] == 'queued'
        before = calls.copy()
        assert queue.target_verifier('codex', 'production', 'production', pinned) is True
        assert queue.approval_verifier('codex', 'production', 'production', 'approved', pinned) is True
        assert calls['target'] == before['target'] + 1 and calls['approval'] == before['approval'] + 1
    finally:
        await wire.close()


@pytest.mark.asyncio
async def test_real_tls_cancelled_caller_drains_same_claim_without_second_post(wire):
    wire.backend.block()
    first = await wire.controller.submit('codex', request())
    second = await wire.controller.submit('codex', request())
    caller = asyncio.create_task(wire.controller.run_once())
    await wire.wait_backend()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert wire.posts() == 1
    assert wire.controller.queue.get_job(first['job']['id'])['state'] == 'claimed'
    assert wire.controller.queue.get_job(second['job']['id'])['state'] == 'queued'
    closing = asyncio.create_task(wire.controller.close())
    await asyncio.sleep(0.03)
    assert not closing.done() and wire.posts() == 1
    wire.backend.release.set()
    await asyncio.wait_for(closing, 5)
    assert wire.controller.queue.get_job(first['job']['id'])['state'] == 'succeeded'
    restarted = wire.new_controller()
    assert (await restarted.run_once())['job']['id'] == second['job']['id']
    assert wire.posts() == 2


@pytest.mark.asyncio
async def test_real_tls_preview_and_cleanup_cas_lost_ack_original_get_only(wire):
    submitted = await wire.controller.submit('codex', request())
    assert (await wire.controller.run_once())['job']['state'] == 'succeeded'
    identity = submitted['resource_ids']['preview']
    fact = await wire.controller.client.resource(identity)
    assert fact['manifest'] == {'index.html': {'bytes': len(HTML), 'sha256': hashlib.sha256(HTML).hexdigest()}}
    assert await wire.controller.preview_file(identity, 'index.html') == (HTML, 'text/html')
    wire.now[0] += 32 * 86400
    stale = wire.resources.cleanup_plan('codex', 'engineering-hub', kinds=['preview'])
    with pytest.raises(HubError):
        await wire.controller.cleanup_apply('codex', stale['plan_id'], 'sha256:' + '0' * 64)
    assert wire.posts('/v1/cleanup') == 0
    wire.drop_cleanup = True
    result = await wire.controller.cleanup_apply('codex', stale['plan_id'], stale['digest'])
    assert result['failed'] and wire.posts('/v1/cleanup') == 1
    assert wire.resources.get_resource('codex', identity)['state'] == 'deleting'
    calls = list(wire.received)
    await wire.controller.close()
    wire.new_controller()  # Reconcile the persisted cleanup journal over a fresh TLS client.
    reconciled = await wire.controller.cleanup_apply('codex', stale['plan_id'], stale['digest'])
    assert reconciled['removed'] == [identity] and wire.posts('/v1/cleanup') == 1
    operations = [path for method, path in wire.received[len(calls):]
                  if method == 'GET' and path.startswith('/v1/operations/')]
    assert len(operations) == 1
    command = operations[0].rsplit('/', 1)[1]
    assert wire.service.operation(command)['state'] == 'succeeded'
    with wire.service.lock:
        sent = json.loads(wire.service.db.execute('SELECT payload FROM operations WHERE command_id=?',
                                                (command,)).fetchone()[0])
    assert sent['command_id'] == command
    assert sent['resources'] == [{'resource_id': identity, 'expected_revision': 1}]
    assert wire.resources.get_resource('codex', identity)['state'] == 'deleted'
    assert wire.service.metadata(identity)['state'] == 'deleted'
    assert wire.service.metadata(identity)['revision'] == 2


@pytest.mark.asyncio
async def test_real_tls_missing_deposited_proof_rejected_before_worker_effects(wire):
    submitted = await wire.controller.submit('codex', request(wire.repo))
    with wire.service.lock:
        wire.service.db.execute('DELETE FROM source_bindings WHERE source_id=?', (submitted['source_id'],))
        wire.service.db.commit()
    result = await wire.controller.run_once()
    assert result['job']['state'] == 'held'
    assert wire.posts() == 1
    assert not wire.backend.entered.is_set()
    with wire.service.lock:
        assert wire.service.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 0
        assert wire.service.db.execute('SELECT COUNT(*) FROM resources').fetchone()[0] == 0
