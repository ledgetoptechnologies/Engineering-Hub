"""Simulated Engine observations, actual private WAL snapshot/journal processes.

The baseline SQLite writer remains in the parent; snapshot and observation run
in separate fresh single-task children. No actual Docker or measured health is
claimed. Typed fake methods must never connect to the backend's inert UDS.
"""
from dataclasses import asdict
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

if sys.platform != 'linux':
    pytest.skip('Actual isolated Linux operator processes required', allow_module_level=True)

from tests.test_hub_container_executor_baseline_snapshot import rig, freeze, original_bytes

pytestmark = pytest.mark.skipif(os.getuid() != 10001 or os.getgid() != 10001,
                                reason='Actual isolated app UID/GID10001 required')


SCRIPT = r'''
from dataclasses import replace
import json, os, sys, time
from pathlib import Path
from uuid import uuid4
import httpx
from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.adapter import HostAdapter
from hub_container_executor.backend import ActualImageSelection, ContainerObservation, ProbeObservation, RunnerObservation
from hub_container_executor.baseline_observer import BaselineObserver, BaselineObservationError
from hub_container_executor.baseline_receipt import ProtectedBaselineProvider
from hub_container_executor.baseline_snapshot import FrozenBaselineSnapshot
from hub_container_executor.daemon_backend import DockerEngineBackend, DockerEngineBinding
from hub_container_executor.operation_journal import SQLiteOperationJournal
from hub_container_executor.runtime_profile import derive_runtime_profile, probe_set_digest
from tests.test_hub_container_executor_build_record import FakeStore, FakeTrust, make_record, verify_fixture

payload=json.load(sys.stdin)
root=Path(payload['root'])
case=payload['case']
saved=payload['snapshot']
snapshot=FrozenBaselineSnapshot(saved['sha256'], saved['size_bytes'],
    tuple(tuple(v) for v in saved['source_inventory']), tuple(tuple(v) for v in saved['source_identities']))
manifest, record, blobs=make_record('rollback')
data=manifest.model_dump(mode='json',by_alias=True)
data['state_policy']['baseline_backup_digest']=snapshot.sha256
manifest=PilotManifest.from_bytes(json.dumps(data).encode())
image=verify_fixture(manifest,record,blobs,FakeTrust(),role='rollback')
record_store=FakeStore({manifest.rollback.provenance.attestation_digest:record})
metadata_store=FakeStore(blobs)
directory=root.parent/'operator-journal'
directory.mkdir(mode=0o700)
journal=SQLiteOperationJournal(directory/'operations.sqlite3',create=True,allow_ephemeral_test_storage=True)
binding=DockerEngineBinding(runner_id=manifest.runner_binding.runner_id,
    daemon_identity=manifest.runner_binding.daemon_identity,platform=manifest.runner_binding.platform,
    unix_socket_path='/operator/never-connect/docker.sock',api_version='1.56',generation_root=str(root))
def forbidden_http(*args,**kwargs):
    raise AssertionError('No external HTTP permitted in synthetic observer tests')
backend=DockerEngineBackend(binding,operation_journal=journal,
    **({'test_transport':httpx.MockTransport(forbidden_http)} if case=='mock_transport' else {}))
backend._client.send=forbidden_http
cid='c'*64
defaults=HostAdapter._defaults_from_record(image)
secrets={'CODEX_TOKEN':'synthetic-codex','HERMES_TOKEN':'synthetic-hermes','CLAUDE_TOKEN':'synthetic-claude'}
profile=derive_runtime_profile(manifest,defaults,'baseline-one',secrets)
selection=ActualImageSelection(image.runnable_reference,image.platform_manifest_digest,image.image_config_id,image.platform)
calls={'runner':0,'image':0,'container':0,'probe':0,'guard':0}
flags={'owner':True}
class Resolver:
    def resolve_test_secret(self,slot,version):
        if case=='secret_invalid': return 'private-sentinel\n'
        for name,binding in [('CODEX_TOKEN',manifest.runtime.secret_bindings.codex),
                             ('HERMES_TOKEN',manifest.runtime.secret_bindings.hermes),
                             ('CLAUDE_TOKEN',manifest.runtime.secret_bindings.claude)]:
            if (slot,version)==(binding.slot_id,binding.version): return secrets[name]
        raise AssertionError('Unexpected secret slot')
def guard():
    calls['guard']+=1
    if case=='guard_error' or not flags['owner']: raise RuntimeError('private-sentinel')
    if case=='guard_false': return False
    if case=='deadline':
        now=time.monotonic()
        time.monotonic=lambda:now+10000
    return None
def runner(timeout):
    calls['runner']+=1
    return RunnerObservation('different' if case=='wrong_runner' else manifest.runner_binding.runner_id,
                             manifest.runner_binding.daemon_identity,manifest.runner_binding.platform)
def image_defaults(actual,timeout):
    calls['image']+=1
    assert actual==image
    return replace(defaults,image_config_id='sha256:'+'9'*64) if case=='wrong_image' else defaults
def container(container_id,timeout):
    calls['container']+=1
    actual_profile=replace(profile,uid=10002) if case=='wrong_profile' else profile
    actual_selection=replace(selection,image_config_id='sha256:'+'9'*64) if case=='wrong_selection' else selection
    status='exited' if case=='wrong_status' or (case=='changed_after_probe' and calls['probe']) else 'running'
    return ContainerObservation(container_id,actual_selection,actual_profile,'baseline-one',status)
def probes(container_id,actual_probes,operation_id,budgets):
    calls['probe']+=1
    assert container_id==cid and actual_probes==manifest.probes.rollback
    assert len(budgets)==3 and all(type(value) is int and value>0 for value in budgets)
    journal.begin(operation_id,'probe')
    if case!='pending_operation':
        journal.completed(operation_id,'probe','d'*64 if case=='wrong_operation_container' else cid)
    if case=='inventory_mutation':
        path=root/'baseline-one'/'hub.sqlite3'
        info=path.stat()
        os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns+1))
    if case=='frozen_mutation':
        frozen=root/'frozen.sqlite3'
        frozen.write_bytes(frozen.read_bytes()+b'x')
    if case=='owner_loss': flags['owner']=False
    if case=='probe_deadline':
        now=time.monotonic()
        time.monotonic=lambda:now+10000
    if case=='probe_exception': raise RuntimeError('private-sentinel')
    return ProbeObservation(cid,'sha256:'+'9'*64 if case=='wrong_probe_image' else image.image_config_id,
        'sha256:'+'9'*64 if case=='wrong_probe_hash' else probe_set_digest(actual_probes),
        'unhealthy' if case=='unhealthy' else 'healthy',False if case=='unverified' else True)
backend.inspect_runner=runner
backend.inspect_image=image_defaults
backend.inspect_container=container
backend.run_compiled_probes=probes
operation=payload['operation']
if case=='operation_replay':
    journal.begin(operation,'probe')
    journal.completed(operation,'probe',cid)
if case=='foreign_root':
    foreign=root/'operator-existing-artifact'
    foreign.write_bytes(b'preserve')
    foreign.chmod(0o600)
report={}
try:
    observer=BaselineObserver(manifest,backend=backend,record_store=record_store,metadata_store=metadata_store,
        provenance_verifier=FakeTrust(),secret_resolver=Resolver(),snapshot=snapshot,generation_root=root,
        source_generation_id='baseline-one',frozen_leaf='frozen.sqlite3',container_id=cid,owner_guard=guard,
        allow_ephemeral_test_storage=True)
    report['constructor_probe_calls']=calls['probe']
    try:
        draft=observer.observe(operation)
        receipt=json.loads(draft.receipt_bytes)
        provider=ProtectedBaselineProvider(FakeStore({draft.receipt_digest:draft.receipt_bytes}),
            receipt_digest=draft.receipt_digest,campaign_id=manifest.campaign_id,verified_rollback=image,
            expected_runtime_profile_digest=draft.runtime_profile_digest)
        baseline=provider.enrolled_baseline(manifest)
        report.update(ok=True,receipt=receipt,receipt_digest=draft.receipt_digest,
            identity_accepted=provider.verify_baseline(baseline,manifest),
            inventory_accepted=provider.baseline_inventory(manifest)==snapshot.source_inventory)
    except BaselineObservationError as error:
        report.update(ok=False,error=str(error))
    before=calls['probe']
    try:
        observer.observe(str(uuid4()))
        report['one_attempt_denied']=False
    except BaselineObservationError:
        report['one_attempt_denied']=calls['probe']==before
    if case=='success':
        # A new observer cannot reuse a completed private operation ID.
        another=BaselineObserver(manifest,backend=backend,record_store=record_store,metadata_store=metadata_store,
            provenance_verifier=FakeTrust(),secret_resolver=Resolver(),snapshot=snapshot,generation_root=root,
            source_generation_id='baseline-one',frozen_leaf='frozen.sqlite3',container_id=cid,owner_guard=guard,
            allow_ephemeral_test_storage=True)
        try:
            another.observe(operation)
            report['operation_reuse_denied']=False
        except BaselineObservationError:
            report['operation_reuse_denied']=calls['probe']==before
except BaselineObservationError as error:
    report.update(ok=False,error=str(error))
finally:
    report['calls']=calls
    report['root_entries']=sorted(path.name for path in root.iterdir())
    backend.close()
    journal.close()
print(json.dumps(report))
'''


def observe(rig, case='success'):
    snapshot = freeze(rig)
    result = subprocess.run([sys.executable, '-W', 'error::ResourceWarning', '-c', SCRIPT],
                             input=json.dumps(dict(root=str(rig[0]), snapshot=asdict(snapshot), case=case,
                                                   operation=str(uuid4()))),
                             capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ''
    report = json.loads(result.stdout)
    assert 'private-sentinel' not in result.stdout
    assert 'synthetic-codex' not in result.stdout and 'synthetic-hermes' not in result.stdout
    assert 'synthetic-claude' not in result.stdout
    return report, snapshot


def test_observed_draft_matches_protected_reader_without_publication_or_replay(rig):
    before = original_bytes(rig[1])
    report, snapshot = observe(rig)
    assert report['ok'] is True
    assert report['identity_accepted'] is True and report['inventory_accepted'] is True
    assert report['constructor_probe_calls'] == 0
    assert report['calls']['probe'] == 1 and report['calls']['container'] == 2
    assert report['one_attempt_denied'] is True and report['operation_reuse_denied'] is True
    assert report['receipt']['backup'] == dict(digest=snapshot.sha256, size_bytes=snapshot.size_bytes)
    assert report['receipt']['observation']['health'] == 'healthy'
    assert report['root_entries'] == ['baseline-one', 'frozen.sqlite3']
    assert original_bytes(rig[1]) == before


@pytest.mark.parametrize('case', ['wrong_runner', 'wrong_image', 'wrong_profile', 'wrong_selection', 'wrong_status',
                                 'secret_invalid', 'guard_error', 'guard_false', 'deadline', 'operation_replay'])
def test_preprobe_denial_never_executes_or_publishes(rig, case):
    before = original_bytes(rig[1])
    report, _ = observe(rig, case)
    assert report['ok'] is False and report['error'] == 'baseline_observation_unverified'
    assert report['calls']['probe'] == 0 and report['one_attempt_denied'] is True
    assert report['root_entries'] == ['baseline-one', 'frozen.sqlite3']
    assert original_bytes(rig[1]) == before


@pytest.mark.parametrize('case', ['unhealthy', 'unverified', 'wrong_probe_hash', 'wrong_probe_image',
                                 'wrong_operation_container', 'pending_operation', 'owner_loss',
                                 'probe_exception', 'probe_deadline', 'changed_after_probe',
                                 'inventory_mutation', 'frozen_mutation'])
def test_probe_or_postprobe_uncertainty_returns_no_draft_and_never_retries(rig, case):
    before = original_bytes(rig[1])
    report, _ = observe(rig, case)
    assert report['ok'] is False and report['error'] == 'baseline_observation_unverified'
    assert report['calls']['probe'] == 1 and report['one_attempt_denied'] is True
    assert 'receipt' not in report
    assert report['root_entries'] == ['baseline-one', 'frozen.sqlite3']
    assert original_bytes(rig[1]) == before


def test_foreign_root_collision_is_preserved_without_probe(rig):
    report, _ = observe(rig, 'foreign_root')
    assert report['ok'] is False and report['calls']['probe'] == 0
    assert (rig[0] / 'operator-existing-artifact').read_bytes() == b'preserve'


def test_explicit_mock_transport_cannot_issue_observed_draft(rig):
    report, _ = observe(rig, 'mock_transport')
    assert report['ok'] is False and report['calls']['probe'] == 0
    assert 'receipt' not in report
