"""Owned successor broker and adapter for the synthetic local acceptance path.

This component is not a staging authority. The caller owns the source broker,
its verified transfer receipt, and final volume cleanup. Uncertain effects are
retained by the broker journal and never retried here.
"""
from contextlib import ExitStack
from pathlib import Path
import os
import threading
import uuid

from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.backend import ContainerPlan
from hub_container_executor.baseline_receipt import ProtectedBaselineProvider
from hub_container_executor.baseline_snapshot import FrozenBaselineSnapshot
from hub_container_executor.build_record import VerifiedImage
from hub_container_executor.daemon_backend import DockerEngineBackend, DockerEngineBinding, _PROBE_SCRIPT
from hub_container_executor.runtime_profile import derive_runtime_profile
from hub_container_executor.state_provider import FrozenSQLiteStateProvisioner
from scripts.hub_local_native_binding import NativeTestBinding
from scripts.hub_local_test_engine_broker import ClosedBrokerSnapshot, EffectJournal, LocalTestBroker, LocalTestSocketServer
from scripts.hub_local_test_volume import OwnedVolume


class _SyntheticSecrets:
    def resolve_test_secret(self, slot, version):
        return 'synthetic-' + slot.removeprefix('test-')


class _SyntheticDriver:
    def __init__(self, commit): self._commit = commit
    def implementation_commit(self): return self._commit


class LocalSource:
    """One initial broker; a sealed exact baseline may transfer to a successor."""
    def __init__(self, *, manifest, candidate, rollback, volume, private_dir,
                 daemon_socket_identity, api_version='1.41', archive_budget=None):
        if (type(manifest) is not PilotManifest or type(candidate) is not VerifiedImage or candidate.role != 'candidate'
                or type(rollback) is not VerifiedImage or rollback.role != 'rollback' or type(volume) is not OwnedVolume):
            raise ValueError('Exact local source enrollment required')
        prefix = 'hub-pilot-' + uuid.UUID(volume.owner).hex[:24]
        if manifest.runtime.resource_name_prefix != prefix:
            raise ValueError('Local source owner mismatch')
        self.private, self.volume, self.manifest = Path(private_dir), volume, manifest
        root = Path(volume.mountpoint) / 'generations'
        images = {'candidate':candidate,'rollback':rollback}
        secrets = {'CODEX_TOKEN':'synthetic-codex','HERMES_TOKEN':'synthetic-hermes','CLAUDE_TOKEN':'synthetic-claude'}
        names = {'baseline':prefix+'-00000000-000-rollback','candidate':prefix+'-11111111-111-candidate',
                 'rollback':prefix+'-11111111-111-rollback'}
        generations = {'baseline':'baseline-g1','candidate':'candidate-placeholder','rollback':'rollback-placeholder'}
        stub = object.__new__(DockerEngineBackend); stub._gate_mount_verifier = None
        bodies = {}
        for role,name in names.items():
            image = candidate if role == 'candidate' else rollback
            profile = derive_runtime_profile(manifest,HostAdapter._defaults_from_record(image),generations[role],secrets)
            bodies[name] = stub._create_body(ContainerPlan(name,image,image.runnable_reference,False,profile),
                                              root/profile.state_generation_id,profile.environment)
        probes = []
        for role in ('candidate','rollback'):
            for probe in getattr(manifest.probes,role):
                probes.append({'AttachStdin':False,'AttachStdout':True,'AttachStderr':False,'Tty':False,
                    'User':'10001:10001','Cmd':['/usr/local/bin/python3','-I','-c',_PROBE_SCRIPT,
                    probe.kind,probe.path,str(probe.expected_status),probe.expectation_id,
                    str(probe.max_response_bytes),str(probe.timeout_ms),images[role].source_commit]})
        self._stack, self.server, self.thread, self.broker, self._closed = ExitStack(), None, None, None, False
        self._dispose_attempted, self._disposed, self._pid = False, False, os.getpid()
        try:
            journal = EffectJournal(self.private/'bootstrap-broker.sqlite'); self._stack.callback(journal.close)
            self.broker = LocalTestBroker(socket_path='/var/run/docker.sock',socket_identity=daemon_socket_identity,
                version=api_version,owner=volume.owner,volume=volume,images={image.runnable_reference:
                    (image.image_config_id,image.platform_manifest_digest) for image in images.values()},
                creates=bodies,exec_bodies=probes,journal=journal,archive_budget=archive_budget)
            self.server = LocalTestSocketServer(str(self.private/'bootstrap.sock'),self.broker)
            self.thread = threading.Thread(target=self.server.serve_forever)
            self._stack.callback(self._close_server)
            self.thread.start()
        except BaseException:
            self._stack.close()
            raise

    def _close_server(self):
        if not self._closed:
            self._closed = True
            if self.thread.ident is None:
                self.server.server_close()
                return
            try: self.server.shutdown()
            finally:
                try: self.thread.join(timeout=5)
                finally: self.server.server_close()
            if self.thread.is_alive(): raise RuntimeError('Local source broker did not drain')

    def seal(self):
        if os.getpid() != self._pid: raise RuntimeError('Local source process changed')
        snapshot = self.broker.seal()
        self._close_server()
        return snapshot

    def close(self):
        if os.getpid() != self._pid: raise RuntimeError('Local source process changed')
        if self._disposed: return
        if self._dispose_attempted: raise RuntimeError('Local source cleanup is uncertain')
        self._dispose_attempted = True
        self._stack.close()
        self._disposed = True


class LocalSuccessor:
    """One admitted bound run, with a serial owned broker and adapter."""
    def __init__(self, *, manifest, candidate, rollback, records, metadata, verifier,
                 baseline_provider, snapshot, source_broker, source_snapshot, volume,
                 private_dir, daemon_socket_identity, api_version='1.41'):
        if (type(manifest) is not PilotManifest or type(candidate) is not VerifiedImage or candidate.role != 'candidate'
                or type(rollback) is not VerifiedImage or rollback.role != 'rollback'
                or type(baseline_provider) is not ProtectedBaselineProvider or type(snapshot) is not FrozenBaselineSnapshot
                or type(source_broker) is not LocalTestBroker or type(source_snapshot) is not ClosedBrokerSnapshot
                or type(volume) is not OwnedVolume or source_broker.volume != volume):
            raise ValueError('Exact local successor enrollment required')
        self.manifest, self.candidate, self.rollback = manifest, candidate, rollback
        self.records, self.metadata, self.verifier = records, metadata, verifier
        self.baseline_provider, self.snapshot = baseline_provider, snapshot
        self.source_broker, self.source_snapshot, self.volume = source_broker, source_snapshot, volume
        self.private = Path(private_dir); self.generation_root = Path(volume.mountpoint) / 'generations'
        self.identity, self.version = daemon_socket_identity, api_version
        self._stack, self._attempted, self.server, self.thread, self.broker = ExitStack(), False, None, None, None
        self._lock, self._closed = threading.RLock(), False
        self._pid = os.getpid()
        self._server_closed, self._adapter = False, None
        self._close_attempted, self._cleaned_snapshot = False, None

    def prepare(self, bound):
        if os.getpid() != self._pid:
            raise RuntimeError('Local successor process changed')
        with self._lock:
            if self._closed:
                raise RuntimeError('Local successor is closed')
            return self._prepare(bound)

    def _prepare(self, bound):
        if self._attempted or type(bound) is not NativeTestBinding:
            raise RuntimeError('Local successor already attempted or unbound')
        self._attempted = True
        manifest = self.manifest
        images = {'candidate': self.candidate, 'rollback': self.rollback}
        secrets = {'CODEX_TOKEN':'synthetic-codex','HERMES_TOKEN':'synthetic-hermes','CLAUDE_TOKEN':'synthetic-claude'}
        profiles = {
            'baseline':derive_runtime_profile(manifest,HostAdapter._defaults_from_record(self.rollback),'baseline-g1',secrets),
            'candidate':derive_runtime_profile(manifest,HostAdapter._defaults_from_record(self.candidate),bound.candidate_generation,secrets),
            'rollback':derive_runtime_profile(manifest,HostAdapter._defaults_from_record(self.rollback),bound.rollback_generation,secrets)}
        names = {'baseline':bound.baseline_name,'candidate':bound.candidate_name,'rollback':bound.rollback_name}
        stub = object.__new__(DockerEngineBackend); stub._gate_mount_verifier = None
        bodies = {}
        for role, name in names.items():
            image = self.candidate if role == 'candidate' else self.rollback
            profile = profiles[role]
            plan = ContainerPlan(name,image,image.runnable_reference,False,profile)
            bodies[name] = stub._create_body(plan,self.generation_root/profile.state_generation_id,profile.environment)
        probes = []
        for role in ('candidate','rollback'):
            for probe in getattr(manifest.probes,role):
                probes.append({'AttachStdin':False,'AttachStdout':True,'AttachStderr':False,'Tty':False,
                    'User':'10001:10001','Cmd':['/usr/local/bin/python3','-I','-c',_PROBE_SCRIPT,
                    probe.kind,probe.path,str(probe.expected_status),probe.expectation_id,
                    str(probe.max_response_bytes),str(probe.timeout_ms),images[role].source_commit]})
        journal = EffectJournal(self.private/'broker.sqlite'); self._stack.callback(journal.close)
        broker = LocalTestBroker(socket_path='/var/run/docker.sock',socket_identity=self.identity,version=self.version,
            owner=self.volume.owner,volume=self.volume,images={image.runnable_reference:
                (image.image_config_id,image.platform_manifest_digest) for image in images.values()},
            creates=bodies,exec_bodies=probes,journal=journal,archive_budget=self.source_broker.archive_budget)
        self.broker = broker
        baseline = broker.transfer_baseline_from(source=self.source_broker,snapshot=self.source_snapshot,
            manifest=manifest,rollback_image=self.rollback,provider=self.baseline_provider)
        path = str(self.private/'broker.sock')
        self.server = LocalTestSocketServer(path,broker)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self._stack.callback(self._close_server)
        self.thread.start()
        binding = DockerEngineBinding(manifest.runner_binding.runner_id,manifest.runner_binding.daemon_identity,
            manifest.runner_binding.platform,path,self.version,str(self.generation_root))
        backend = DockerEngineBackend(binding); self._stack.callback(backend.close)
        provider = FrozenSQLiteStateProvisioner(manifest,baseline,baseline_provider=self.baseline_provider,
            frozen_backup=self.generation_root/'frozen.sqlite3',generation_root=self.generation_root,
            reservation_root=Path(self.volume.mountpoint)/'reservations',
            baseline_generation_inventory=self.snapshot.source_inventory,create=True,allow_ephemeral_test_storage=True)
        self._stack.callback(provider.close)
        self._adapter = HostAdapter(self.private/'adapter.sqlite3',manifest,record_store=self.records,metadata_store=self.metadata,
            provenance_verifier=self.verifier,backend=backend,baseline_provider=self.baseline_provider,
            state_provisioner=provider,secret_resolver=_SyntheticSecrets(),
            driver_identity_verifier=_SyntheticDriver(manifest.driver.implementation_commit))
        return self._adapter

    def _close_server(self):
        if self._server_closed:
            return
        self._server_closed = True
        if self.thread.ident is None:
            self.server.server_close()
            return
        try: self.server.shutdown()
        finally:
            try: self.thread.join(timeout=5)
            finally: self.server.server_close()
        if self.thread.is_alive(): raise RuntimeError('Local broker did not drain')

    def seal_and_cleanup(self, coordinator):
        """Clean exact owned containers only after this coordinator drained."""
        from scripts.hub_local_native_coordinator import LocalNativeCoordinator
        if os.getpid() != self._pid:
            raise RuntimeError('Local successor process changed')
        with self._lock:
            if (self._closed or type(coordinator) is not LocalNativeCoordinator or coordinator._closed is not True
                    or coordinator._pid != os.getpid()
                    or self._adapter is None or coordinator.client.adapter is not self._adapter
                    or self.broker is None or self.thread is None):
                raise RuntimeError('Local coordinator is not drained')
            snapshot = self.broker.seal()
            self._close_server()
            cleaned = self.broker.cleanup_owned(snapshot)
            self._cleaned_snapshot = cleaned
            return cleaned

    def close(self):
        """Drain preparation; caller must first drain and close the adapter."""
        if os.getpid() != self._pid:
            raise RuntimeError('Local successor process changed')
        with self._lock:
            if self._closed: return
            if self._close_attempted: raise RuntimeError('Local successor cleanup is uncertain')
            self._close_attempted = True
            self._stack.close()
            self._closed = True
