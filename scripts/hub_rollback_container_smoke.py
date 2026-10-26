"""Two immutable Hub images, disposable state only; never production rollout.

Direct mode proves old code reads candidate-written state. Backup-restore mode
instead proves old code reads the coherent PRE-upgrade backup (candidate writes
are deliberately absent). Neither mode proves protected-owner or host recovery.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

PACKAGE = 'ghcr.io/ledgetoptechnologies/engineering-hub-control'
spec = importlib.util.spec_from_file_location('rollback_durable_helpers',
    Path(__file__).with_name('hub_durable_container_smoke.py'))
durable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(durable)
_deadline = None
_term_issued = set()


def validate(image, commit):
    if (type(image) is not str or re.fullmatch(re.escape(PACKAGE) + r'@sha256:[a-f0-9]{64}', image) is None
            or type(commit) is not str or re.fullmatch('[a-f0-9]{40}', commit) is None):
        raise ValueError('Fixed Hub package digest and exact commit required')


def _docker(args, *, check=True, timeout=30):
    """Bound public CLI output and one attempt; kill/reap only this CLI on timeout."""
    limit = min(time.monotonic() + timeout, _deadline or float('inf'))
    if time.monotonic() >= limit:
        raise TimeoutError('Smoke deadline expired')
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        child = subprocess.Popen(['docker', *args], stdout=out, stderr=err)
        try:
            while child.poll() is None:
                if time.monotonic() >= limit or out.tell() + err.tell() > 256 * 1024:
                    raise TimeoutError('CLI outcome unknown; no command replay')
                time.sleep(.02)
            if out.tell() + err.tell() > 256 * 1024:
                raise ValueError('CLI output exceeded public receipt bound')
            out.seek(0); err.seek(0)
            result = subprocess.CompletedProcess(args, child.returncode,
                out.read().decode('utf-8'), err.read().decode('utf-8'))
            if check and result.returncode:
                raise RuntimeError('Docker command failed; private output withheld')
            return result
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def _image(ref, commit):
    validate(ref, commit)
    template = ('{"id":{{json .Id}},"revision":{{json (index .Config.Labels '
        '"org.opencontainers.image.revision")}},"user":{{json .Config.User}},'
        '"digests":{{json .RepoDigests}}}')
    value = json.loads(_docker(['image', 'inspect', '--format', template, ref], timeout=15).stdout)
    if (type(value) is not dict or set(value) != {'id', 'revision', 'user', 'digests'}
            or type(value['id']) is not str or not re.fullmatch('sha256:[a-f0-9]{64}', value['id'])
            or value['revision'] != commit or value['user'] != '568:568'
            or type(value['digests']) is not list or ref not in value['digests']):
        raise ValueError('Image identity/commit/UID/digest mismatch')
    return value['id']


def _stop(name, owner, *, already_terminal=False):
    identity = durable._container(name, owner)
    until = time.monotonic() + 12
    while True:
        value = json.loads(_docker(['container', 'inspect', '--format',
            '{"running":{{json .State.Running}},"pid":{{json .State.Pid}},'
            '"oom":{{json .State.OOMKilled}},"exit":{{json .State.ExitCode}}}', identity]).stdout)
        if (type(value) is not dict or set(value) != {'running','pid','oom','exit'}
                or type(value['running']) is not bool or type(value['pid']) is not int
                or type(value['oom']) is not bool or type(value['exit']) is not int):
            raise ValueError('Original terminal observation invalid')
        if value['running'] is False:
            if value['pid'] != 0 or value['oom'] is not False or value['exit'] not in ((0,) if already_terminal else (0,143)):
                raise ValueError('Original writer not proven normally stopped')
            return
        if already_terminal:
            raise ValueError('Restore helper not terminal; no new writer admitted')
        if identity not in _term_issued:
            _term_issued.add(identity)  # Unknown signal ACK is never resubmitted.
            _docker(['kill', '--signal', 'SIGTERM', identity], timeout=10)
        if time.monotonic() >= until:
            raise TimeoutError('TERM-only writer still held; no SIGKILL fallback')
        time.sleep(.05)


def _profile(identity, image, volumes):
    durable._profile(identity, volumes)
    value = json.loads(_docker(['container', 'inspect', '--format',
        '{"image":{{json .Image}},"swap":{{json .HostConfig.MemorySwap}},'
        '"restart":{{json .HostConfig.RestartPolicy.Name}},"logs":{{json .HostConfig.LogConfig}}}', identity]).stdout)
    if (value.get('image') != image or value.get('swap') != 512 * 1024 ** 2
            or value.get('restart') != 'no' or value.get('logs') != {
                'Type': 'json-file', 'Config': {'max-size': '128k', 'max-file': '1'}}):
        raise ValueError('Image or bounded runtime profile changed')


def _options(name, owner, volume):
    return durable._options(name, owner, volume) + ['--memory-swap', '512m', '--restart', 'no',
        '--log-driver', 'json-file', '--log-opt', 'max-size=128k', '--log-opt', 'max-file=1']


def _probe(identity, mode, commit, task=None, *, candidate_write=False):
    code = durable.PROBE
    if candidate_write:
        code = code.replace("'durability-fixture'", "'candidate-write-fixture'")
    args = ['exec', identity, 'python', '-c', code, mode, commit]
    if task is not None:
        args.extend([task['task_id'], str(task['revision'])])
    result = json.loads(_docker(args).stdout)
    if (type(result) is not dict or set(result) != {'task_id', 'revision', 'state', 'build_commit', 'auth_denials'}
            or type(result['task_id']) is not str or str(uuid.UUID(result['task_id'])) != result['task_id']
            or type(result['revision']) is not int or result['revision'] != 2
            or result['state'] != 'claimed' or result['build_commit'] != commit or result['auth_denials'] is not True
            or (task is not None and result != dict(task, build_commit=commit))):
        raise ValueError('Original task/write/auth observation mismatch')
    return result


def run_smoke(baseline, baseline_commit, candidate, candidate_commit, *, rollback_mode='direct'):
    global _deadline, _term_issued
    validate(baseline, baseline_commit); validate(candidate, candidate_commit)
    if rollback_mode not in {'direct', 'backup-restore'} or baseline == candidate or baseline_commit == candidate_commit:
        raise ValueError('Distinct baseline and candidate required')
    _deadline = time.monotonic() + 480
    _term_issued = set()
    durable._docker = _docker
    old_id, new_id = _image(baseline, baseline_commit), _image(candidate, candidate_commit)
    if old_id == new_id:
        raise ValueError('Distinct immutable image IDs required')
    owner = uuid.uuid4().hex
    prefix = 'hub-rollback-' + owner
    volumes = [prefix + '-state', prefix + '-restore']
    names = [prefix + '-baseline', prefix + '-candidate', prefix + '-old', prefix + '-copy']
    issued_names, issued_volumes = [], []
    report = {'schema': 'engineering-hub.disposable-rollback.v1', 'state': 'failed',
        'baseline': {'digest': baseline, 'commit': baseline_commit, 'image_id': old_id},
        'candidate': {'digest': candidate, 'commit': candidate_commit, 'image_id': new_id},
        'rollback_mode': rollback_mode, 'protected_owner_restore': 'not_tested',
        'nas_or_host_rollout': 'not_tested', 'cleanup': 'pending',
        'fixture_owner': owner, 'fixture_names': names, 'fixture_volumes': volumes}

    def app(name, image, volume):
        if durable._container(name, owner, missing_ok=True) is not None:
            raise ValueError('Refuse existing fixture')
        issued_names.append(name)  # Unknown create acknowledgement is inspected, never replayed.
        _docker(_options(name, owner, volume) + [image])
        identity = durable._container(name, owner)
        _profile(identity, image, {'/var/lib/hub': (volume, True)})
        _docker(['start', identity])
        return identity

    try:
        for volume in volumes[:1 if rollback_mode == 'direct' else 2]:
            if durable._volume(volume, owner, missing_ok=True):
                raise ValueError('Refuse existing fixture')
            issued_volumes.append(volume)
            _docker(['volume', 'create', '--driver', 'local', '--label', durable.LABEL + '=' + owner, volume])
            durable._volume(volume, owner)
        original = app(names[0], old_id, volumes[0])
        initial = _probe(original, 'seed', baseline_commit)
        backup = durable._backup_report(json.loads(_docker(['exec', original, 'python', '-c', durable.BACKUP]).stdout))
        _stop(names[0], owner)
        upgraded = app(names[1], new_id, volumes[0])
        _probe(upgraded, 'verify', candidate_commit, initial)
        written = _probe(upgraded, 'seed', candidate_commit, candidate_write=True)
        if written['task_id'] == initial['task_id']:
            raise ValueError('Candidate did not perform a distinct durable write')
        _probe(upgraded, 'verify', candidate_commit, written)
        _stop(names[1], owner)
        rollback_volume = volumes[0]
        if rollback_mode == 'backup-restore':
            if durable._container(names[3], owner, missing_ok=True) is not None:
                raise ValueError('Refuse existing fixture')
            issued_names.append(names[3])
            _docker(_options(names[3], owner, volumes[1]) + [
                '--mount', 'type=volume,source=' + volumes[0] + ',target=/backup-source,readonly',
                '--entrypoint', 'python', old_id, '-c', durable.RESTORE])
            helper = durable._container(names[3], owner)
            _profile(helper, old_id, {'/var/lib/hub': (volumes[1], True), '/backup-source': (volumes[0], False)})
            restored = durable._backup_report(json.loads(_docker(['start', '--attach', helper]).stdout), restored=True)
            _stop(names[3], owner, already_terminal=True)
            if (restored['sha256'], restored['size_bytes']) != (backup['sha256'], backup['size_bytes']):
                raise ValueError('Original backup bytes differ')
            rollback_volume = volumes[1]
        rolled_back = app(names[2], old_id, rollback_volume)
        _probe(rolled_back, 'verify', baseline_commit, initial)
        if rollback_mode == 'direct':
            _probe(rolled_back, 'verify', baseline_commit, written)
        else:
            absence = ("import json,os;from engineering_hub.repository import HubRepository;"
                "r=HubRepository(os.environ['HUB_DB']);assert not any(t['id']==" + repr(written['task_id']) +
                " for t in r.list_tasks());print('original-backup-only')")
            if _docker(['exec', rolled_back, 'python', '-c', absence]).stdout.strip() != 'original-backup-only':
                raise ValueError('Backup restore included candidate writes')
        _stop(names[2], owner)
        report.update(state='passed', auth_denials='passed', baseline_state='matched',
            candidate_write='distinct_task_claimed_revision2',
            candidate_state_after_rollback='matched' if rollback_mode == 'direct' else 'intentionally_discarded')
    except Exception:
        report['error'] = 'verification_failed_private_output_withheld'
    finally:
        _deadline = time.monotonic() + 120
        errors = []
        for name in reversed(issued_names):
            try:
                identity = durable._container(name, owner, missing_ok=True)
                if identity:
                    _stop(name, owner)
                    _docker(['rm', identity])  # Never force an unknown/live writer.
            except Exception:
                errors.append('container_cleanup_unverified')
        for volume in reversed(issued_volumes) if not errors else []:
            try:
                if durable._volume(volume, owner, missing_ok=True):
                    _docker(['volume', 'rm', volume])
            except Exception:
                errors.append('volume_cleanup_unverified')
        report['cleanup'] = 'held' if errors else 'completed'
        if errors:
            report['state'] = 'failed'; report['cleanup_errors'] = errors
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('baseline-image', 'baseline-commit', 'candidate-image', 'candidate-commit'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--rollback-mode', choices=('direct', 'backup-restore'), default='direct')
    args = parser.parse_args()
    try:
        report = run_smoke(args.baseline_image, args.baseline_commit, args.candidate_image,
            args.candidate_commit, rollback_mode=args.rollback_mode)
    except Exception:
        report = {'state': 'failed', 'error': 'image_or_input_unverified_no_fixture_started'}
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report['state'] == 'passed' else 1)


if __name__ == '__main__':
    main()
