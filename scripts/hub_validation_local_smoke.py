"""Explicit disposable local Engine acceptance; no service or live credentials.

Use a preloaded image built from a clean committed checkout and its original OCI
archive. Synthetic task reviews and trusted local enrollment are test fixtures,
not CI attestations or production approval. Never use a live Hub database here.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from uuid import uuid4

from engineering_hub.repository import HubRepository
from engineering_hub.work_queue import WorkTarget, WorkWorker
from engineering_hub.work_queue_config import configured_work_queue
from hub_validation_worker.core import (
    ValidationEnrollment, ValidationLedger, ValidationProfile, ValidationWorker,
)
from hub_validation_worker.docker_backend import DockerCLIBackend, DockerImageSelection


@contextmanager
def preserved_fixture():
    # Intentionally retain the tiny synthetic databases, including on unknown
    # Engine outcome. Never erase the only original intent needed for inspection.
    root = Path(tempfile.mkdtemp(prefix='hub-validation-local-'))
    os.chmod(root, 0o700)
    try:
        yield root
    except BaseException:
        print(json.dumps({'retained_fixture_directory': str(root), 'acceptance': 'incomplete'}))
        raise


def selection_from_archive(archive_path, expected_index):
    if re.fullmatch(r'sha256:[a-f0-9]{64}', expected_index) is None:
        raise RuntimeError('Invalid fixture index')
    archive_path = Path(archive_path)
    if not archive_path.is_file() or archive_path.stat().st_size > 2 * 1024**3:
        raise RuntimeError('Bounded local fixture archive required')
    with tarfile.open(archive_path, 'r:') as archive:
        members = []
        for member in archive:
            if len(members) == 512:
                raise RuntimeError('Fixture archive has too many entries')
            members.append(member)
        def blob(digest):
            name = 'blobs/sha256/' + digest.removeprefix('sha256:')
            matches = [member for member in members if member.name == name]
            if len(matches) != 1 or not matches[0].isfile() or not 0 < matches[0].size <= 65536:
                raise RuntimeError('Bounded original OCI blob required')
            with archive.extractfile(matches[0]) as source:
                raw = source.read(65537)
            if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
                raise RuntimeError('Original OCI blob digest mismatch')
            return raw
        index = blob(expected_index)
        candidates = [item for item in json.loads(index)['manifests']
                      if item.get('platform') == {'os': 'linux', 'architecture': 'amd64'}]
        if len(candidates) != 1:
            raise RuntimeError('Unique amd64 fixture required')
        return DockerImageSelection.from_oci(index, blob(candidates[0]['digest']), platform='linux/amd64')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker-executable', required=True)
    parser.add_argument('--docker-config-directory', required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--daemon-id', required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--index', required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--windows-test-storage', action='store_true')
    args = parser.parse_args()
    if re.fullmatch(r'[a-f0-9]{40}', args.source_commit) is None:
        raise RuntimeError('Exact fixture source commit required')
    selected = selection_from_archive(args.archive, args.index)
    backend = DockerCLIBackend(args.docker_executable, args.context, args.daemon_id,
                               config_directory=args.docker_config_directory,
                               image_selection=selected)
    prefix = [args.docker_executable, '--config', args.docker_config_directory, '--context', args.context]

    def docker(*command):
        # Fixed synthetic acceptance commands only. Never print daemon output.
        result = subprocess.run([*prefix, *command], check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=20, env=backend._environment)
        if len(result.stdout) > 65536:
            raise RuntimeError('Fixture inspect output bound exceeded')
        return result.stdout.decode()

    with preserved_fixture() as root:
        repo = HubRepository(root / 'synthetic-hub.sqlite3')
        ledger = ValidationLedger(root / 'receipts.sqlite3', create=True,
                                  allow_windows_test_storage=args.windows_test_storage)
        queue = configured_work_queue(repo, machine_id='local-disposable-engine')
        queue.completion_verifier = ledger.verify_completion
        task = repo.create_task('human:42', 'engineering-hub', 'Disposable acceptance only', str(uuid4()))
        task = repo.claim_task('codex', task['id'], task['revision'])
        task = repo.set_candidate('codex', task['id'], task['revision'], args.source_commit)
        task = repo.review_task('claude', task['id'], task['revision'], args.source_commit, 'approve')
        target = WorkTarget(task['id'], args.source_commit, selected.runnable_reference)
        profile = ValidationProfile(memory_bytes=2147483648, cpu_millicores=2000,
                                    tmpfs_bytes=268435456)
        worker = WorkWorker('codex', str(uuid4()))
        results = []
        production = queue.submit(actor='codex', request_id='unapproved-production',
                                  action_key='engineering-hub.validate', environment='production', target=target)
        commands = (
            ('python', '-m', 'pytest', '-q', 'tests/test_hub_work_queue.py',
             'tests/test_hub_work_queue_api.py', 'tests/test_hub_work_queue_bootstrap.py',
             '--basetemp', '/tmp/validation-case', '-o', 'cache_dir=/tmp/validation-cache'),
            ('python', '-c', 'raise SystemExit(7)'),
        )
        for position, command in enumerate(commands):
            submitted = queue.submit(actor='codex', request_id='actual-' + str(position),
                                     action_key='engineering-hub.validate', environment='staging', target=target)
            claim = queue.claim(worker=worker, lease_seconds=600)
            assert claim['id'] == submitted['id'] and queue.claim(worker=worker) is None
            enrollment = ValidationEnrollment('engineering-hub.validate', 'engineering-hub',
                'local-disposable-engine', args.source_commit, selected.runnable_reference,
                selected.image_config_id, 'linux/amd64', command, profile, 120)
            result = ValidationWorker(queue, ledger, backend, (enrollment,)).run_once(
                job_id=claim['id'], worker=worker, fence=claim['fence'])
            expected = 'succeeded' if position == 0 else 'failed'
            if result['state'] != expected:
                raise RuntimeError('Actual validation held; inspect original deterministic container before retry')
            assert ledger.verify_completion(claim, result['evidence_id'], expected)
            assert not ledger.verify_completion(claim, str(uuid4()), expected)
            reopened = ValidationLedger(ledger.path, create=False,
                                        allow_windows_test_storage=args.windows_test_storage)
            assert reopened.verify_completion(claim, result['evidence_id'], expected)
            receipt = ledger.inspect(job_id=claim['id'], fence=claim['fence'])
            name = 'hub-validation-' + claim['id'] + '-' + str(claim['fence'])
            original_id = receipt['container_id']
            assert type(original_id) is str and re.fullmatch(r'[a-f0-9]{64}', original_id)
            # Dispose only the original, independently verified stopped object.
            state = json.loads(docker('container', 'inspect', original_id))[0]
            assert state['Id'] == original_id and state['Name'] == '/' + name
            labels = state['Config']['Labels']
            assert labels['engineering-hub.validation.job'] == claim['id']
            assert labels['engineering-hub.validation.fence'] == str(claim['fence'])
            assert labels['engineering-hub.validation.intent'] == receipt['intent_id']
            assert state['State']['Status'] == 'exited' and state['State']['Running'] is False
            assert state['State']['Pid'] == 0 and re.fullmatch(r'[a-f0-9]{64}', state['Id'])
            docker('container', 'rm', original_id)  # No force/stop or unknown-object cleanup.
            results.append(result['state'])
        assert queue.claim(worker=worker) is None  # Production approval is still absent.
        assert repo.get_task(task['id'])['status'] == 'verified' and repo.list_runs() == []
        print(json.dumps({'actual_engine': True, 'actual_fixed_tests_passed': results[0] == 'succeeded',
                          'actual_nonzero_exit_recorded': results[1] == 'failed',
                          'trusted_receipt_reopen': True, 'forged_receipt_denied': True,
                          'verified_release': True, 'production_still_blocked': True,
                          'deployment_runs_created': 0, 'owned_stopped_containers_removed': 2,
                          'storage_mode': 'windows-test-only' if os.name != 'posix' else 'private-posix',
                          'retained_fixture_directory': str(root),
                          'synthetic_review_and_enrollment': True,
                          'nas_remote_reboot_acceptance': False}, sort_keys=True))


if __name__ == '__main__':
    main()
