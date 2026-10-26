"""Image-owned offline Python/UI checks for the modular staging backend."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

FILES = ('pyproject.toml', 'uv.lock', 'hub-ui/package.json', 'hub-ui/package-lock.json')
PYTHON = '/opt/validation-venv/bin/python'
NPM = '/usr/local/bin/npm'
MANIFEST_SCHEMA = 'engineering-hub.modular-ci-cache.v2'
REPORTERS = ('modular_ci_pytest_reporter.py', 'modular_ci_vitest_reporter.mjs')
MAX_FILE = 4 * 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024


class CIError(ValueError):
    pass


def read_regular(path, maximum=MAX_FILE):
    if path.is_symlink():
        raise CIError('invalid_ci_input')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise CIError('invalid_ci_input')
        data = bytearray()
        while chunk := os.read(fd, min(65536, maximum + 1 - len(data))):
            data.extend(chunk)
            if len(data) > maximum:
                raise CIError('ci_input_limit')
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise CIError('ci_input_changed')
        return bytes(data)
    finally:
        os.close(fd)


def unique_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CIError('duplicate_ci_manifest_key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise CIError('invalid_ci_manifest') from exc


def verify_dependencies(workspace, cache):
    raw = read_regular(cache / 'modular-cache.json', 16384)
    manifest = unique_json(raw)
    if (type(manifest) is not dict or set(manifest) != {
            'schema', 'dependency_sha256', 'cache_base_image', 'prepared_source_commit', 'bootstrap_sha256',
            'reporter_sha256'}
            or manifest['schema'] != MANIFEST_SCHEMA
            or type(manifest['dependency_sha256']) is not dict
            or set(manifest['dependency_sha256']) != set(FILES)
            or not isinstance(manifest['cache_base_image'], str)
            or re.fullmatch(r'(?:[^\s@]+@)?sha256:[0-9a-f]{64}', manifest['cache_base_image']) is None
            or not isinstance(manifest['prepared_source_commit'], str)
            or re.fullmatch(r'[0-9a-f]{40}', manifest['prepared_source_commit']) is None):
        raise CIError('invalid_ci_manifest')
    if manifest['bootstrap_sha256'] != hashlib.sha256(read_regular(Path(__file__))).hexdigest():
        raise CIError('ci_bootstrap_binding_mismatch')
    if type(manifest['reporter_sha256']) is not dict or set(manifest['reporter_sha256']) != set(REPORTERS):
        raise CIError('invalid_ci_manifest')
    for name in REPORTERS:
        expected = manifest['reporter_sha256'][name]
        if (type(expected) is not str or re.fullmatch(r'[0-9a-f]{64}', expected) is None
                or hashlib.sha256(read_regular(cache / name)).hexdigest() != expected):
            raise CIError('ci_reporter_binding_mismatch')
    for name in FILES:
        expected = manifest['dependency_sha256'][name]
        if (type(expected) is not str or re.fullmatch(r'[0-9a-f]{64}', expected) is None
                or hashlib.sha256(read_regular(workspace / name)).hexdigest() != expected):
            raise CIError('ci_dependency_mismatch')
    return manifest, hashlib.sha256(raw).hexdigest()


def node_facade(workspace, cache):
    """Keep packages immutable while Vite/Vitest caches stay job-local."""
    source = cache / 'node_modules'
    if not source.is_dir() or source.is_symlink():
        raise CIError('ci_node_cache_missing')
    entries = sorted(source.iterdir())
    if not entries or len(entries) > 5000:
        raise CIError('ci_node_cache_invalid')
    target = workspace / 'hub-ui/node_modules'
    if target.exists() or target.is_symlink():
        raise CIError('source_node_modules_not_allowed')
    target.mkdir(mode=0o700)
    for entry in entries:
        # Never inherit mutable caches from preparation or link them read-only.
        if entry.name in {'.vite', '.vite-temp', '.vitest-cache', '.cache'}:
            continue
        (target / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())


def export_dist(workspace, output):
    root = workspace / 'hub-ui/dist'
    if root.is_symlink() or not root.is_dir():
        raise CIError('ci_dist_missing')
    files, total = [], 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise CIError('ci_dist_unsafe')
        for name in names:
            path = Path(directory) / name
            total += path.stat().st_size
            files.append(path)
            if len(files) > 5000 or total > MAX_OUTPUT:
                raise CIError('ci_dist_limit')
    if not (root / 'index.html').is_file():
        raise CIError('ci_dist_index_missing')
    for path in files:
        destination = output / path.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb') as stream:
            stream.write(read_regular(path, MAX_OUTPUT))
    return {'files': len(files), 'bytes': total}


def write_json(path, value):
    raw = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(raw) > 65536:
        raise CIError('ci_summary_limit')
    # A failed/short write must not publish an empty or partial summary.
    fd, temporary = tempfile.mkstemp(prefix='.eh-ci-summary-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            raise CIError('ci_summary_exists')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_report(evidence, name):
    """Bounded source-produced accounting, never trusted execution provenance."""
    maximum = 256 * 1024 if name == 'python_pytest' else 65536
    path = evidence / ('python-summary.json' if name == 'python_pytest' else 'ui-summary.json')
    try:
        value = unique_json(read_regular(path, maximum))
        schema = ('engineering-hub.modular-ci-python-report.v1' if name == 'python_pytest'
                  else 'engineering-hub.modular-ci-vitest.v1')
        counts = ({'collected', 'deselected', 'executed', 'passed', 'failed', 'errors', 'skipped',
                   'xfailed', 'xpassed', 'collection_errors'} if name == 'python_pytest'
                  else {'passed', 'failed', 'skipped', 'pending', 'unknown', 'total'})
        if (type(value) is not dict or type(value.get('state')) is not str
                or value['state'] not in {'passed', 'failed'}
                or type(value.get('complete')) is not bool or value.get('schema') != schema
                or type(value.get('outcomes')) is not dict or set(value['outcomes']) != counts
                or any(type(number) is not int or not 0 <= number <= 2**63 - 1
                       for number in value['outcomes'].values())):
            raise CIError('invalid_ci_report')
        if value['state'] == 'passed':
            outcomes = value['outcomes']
            if name == 'python_pytest':
                consistent = (outcomes['failed'] == outcomes['errors'] == outcomes['collection_errors'] == 0
                              and outcomes['collected'] > 0
                              and outcomes['executed'] == outcomes['collected']
                              and outcomes['executed'] == sum(outcomes[key] for key in
                                  ('passed', 'failed', 'errors', 'skipped', 'xfailed', 'xpassed')))
            else:
                consistent = (outcomes['failed'] == outcomes['pending'] == outcomes['unknown'] == 0
                              and outcomes['total'] > 0
                              and outcomes['total'] == outcomes['passed'] + outcomes['skipped'])
            if not consistent:
                raise CIError('invalid_ci_report')
        return {'available': True, 'state': value['state'], 'complete': value['complete'],
                'outcomes': value['outcomes']}
    except (CIError, OSError, ValueError):
        return {'available': False, 'state': 'failed', 'complete': False}


def scratch_facts(root):
    """Child observation only; the worker owns quota/lease/mapping verification."""
    rows = []
    for line in read_regular(Path('/proc/self/mountinfo'), 1024 * 1024).decode().splitlines():
        left, right = line.split(' - ', 1)
        fields, tail = left.split(), right.split()
        if fields[4] == str(root):
            rows.append((fields, tail))
    if len(rows) != 1:
        raise CIError('ci_scratch_mount_required')
    fields, tail = rows[0]
    options = set(fields[5].split(',')) | set(tail[2].split(','))
    if (tail[0] != 'ext4' or not {'rw', 'nosuid', 'nodev'} <= options
            or {'ro', 'noexec'} & options):
        raise CIError('ci_scratch_mount_policy')
    fs = os.statvfs(root)
    if fs.f_bavail * fs.f_frsize < 512 * 1024**2 or fs.f_favail < 1024:
        raise CIError('ci_scratch_capacity')


def _phase_layout(root, stage):
    if stage not in {'build', 'test'}:
        raise CIError('invalid_ci_stage')
    phase = root / ('eh-ci-' + stage)
    phase.mkdir(mode=0o700)
    for name in ('home', 'tmp', 'pytest-cache', 'npm-cache'):
        (phase / name).mkdir(mode=0o700)
    return ({'HOME': str(phase / 'home'), 'TMPDIR': str(phase / 'tmp'),
             'TMP': str(phase / 'tmp'), 'TEMP': str(phase / 'tmp'),
             'npm_config_cache': str(phase / 'npm-cache')},
            phase / 'pytest', phase / 'pytest-cache')


def temporary_layout(stage):
    signal = os.environ.get('EH_CI_SCRATCH')
    if signal is None:
        if stage == 'test':
            raise CIError('ci_scratch_required')
        return {'HOME': '/tmp/eh-ci-home'}, Path('/tmp/pytest'), Path('/tmp/pytest-cache')
    if signal != '1':
        raise CIError('invalid_ci_scratch_signal')
    root = Path('/ci-scratch')
    if root.is_symlink() or not root.is_dir():
        raise CIError('ci_scratch_mount_required')
    scratch_facts(root)
    # Each stage runs once per original worker command. Refuse a pre-existing
    # phase directory instead of traversing or clearing source-created paths.
    return _phase_layout(root, stage)


def run_stage(stage, *, workspace=Path('/workspace'), cache=Path('/opt/eh-ci'),
              output=Path('/output'), evidence=Path('/test-output'), runner=subprocess.run):
    if stage not in {'build', 'test'}:
        raise CIError('invalid_ci_stage')
    # No source config/plugin/package command executes until all hashes match.
    manifest, binding = verify_dependencies(workspace, cache)
    report = {'schema': 'engineering-hub.modular-ci-result.v1', 'stage': stage,
              'cache_binding_sha256': binding,
              'dependency_sha256': manifest['dependency_sha256'],
              'checks': [], 'state': 'failed', 'commit_verification': 'declared'}
    env = os.environ.copy()
    temporary_env, pytest_temp, pytest_cache = temporary_layout(stage)
    env.update(PYTHONPATH=str(workspace / 'src') + os.pathsep + str(workspace),
               PYTHONDONTWRITEBYTECODE='1',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', CI='true',
               PIP_NO_INDEX='1', npm_config_offline='true', npm_config_audit='false',
               npm_config_fund='false', NODE_OPTIONS='--max-old-space-size=320',
               EH_CI_VITEST_REPORT_DIR=str(evidence),
               EH_RESPONSIVENESS_ARTIFACT_DIR=str(evidence / 'responsiveness'))
    env.update(temporary_env)
    node_facade(workspace, cache)
    commands = ([('ui_build', [NPM, 'run', 'build', '--', '--base', './'], workspace / 'hub-ui')]
                if stage == 'build' else [
                    ('python_pytest', [PYTHON, '-I', '-B', str(cache / REPORTERS[0]),
                                      '--source-root', str(workspace),
                                      '--report', str(evidence / 'python-summary.json'), '--', '-q',
                                      '--basetemp=' + str(pytest_temp), '-o', 'cache_dir=' + str(pytest_cache),
                                      ], workspace),
                    ('ui_unit', [NPM, 'test', '--', '--maxWorkers=1', '--no-file-parallelism',
                                 '--reporter=' + str(cache / REPORTERS[1])], workspace / 'hub-ui')])
    try:
        if stage == 'test':
            build = unique_json(read_regular(output / 'ci-build.json', 16384))
            if (type(build) is not dict or build.get('state') != 'passed'
                    or build.get('cache_binding_sha256') != binding
                    or build.get('dependency_sha256') != manifest['dependency_sha256']):
                raise CIError('ci_build_binding_mismatch')
        for name, argv, cwd in commands:
            if stage == 'test' and name == 'ui_unit':
                try:
                    scratch_facts(Path('/ci-scratch'))
                except (CIError, OSError, ValueError):
                    report['checks'].append({'name': name, 'exit_code': None,
                                             'state': 'blocked', 'reason': 'ci_scratch_unavailable',
                                             'report': {'available': False, 'state': 'failed',
                                                        'complete': False}})
                    raise CIError('ci_scratch_unavailable') from None
            result = runner(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            check=False, shell=False)
            check = {'name': name, 'exit_code': result.returncode}
            if stage == 'test':
                check['report'] = check_report(evidence, name)
            report['checks'].append(check)
        if any(check['exit_code'] != 0 or 'report' in check and
               (check['report']['state'] != 'passed' or check['report']['complete'] is not True)
               for check in report['checks']):
            raise CIError('ci_checks_failed')
        if stage == 'build':
            report['dist'] = export_dist(workspace, output)
        report['state'] = 'passed'
        if stage == 'build':
            write_json(output / 'ci-build.json', report)
    finally:
        # Worker owns log/deadline/process cleanup. Never kill/retry job processes here.
        write_json(evidence / (stage + '-summary.json'), report)
    return report


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Expected fixed build or test stage')
    try:
        run_stage(sys.argv[1])
    except (CIError, OSError, ValueError):
        raise SystemExit('Offline modular CI failed; inspect bounded worker evidence') from None


if __name__ == '__main__':
    main()
