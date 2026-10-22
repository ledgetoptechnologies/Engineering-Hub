"""Pure CI admission/profile/archive checks; no daemon or host changes."""
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest


def module(name):
    path = Path(__file__).resolve().parents[1] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


runner = module('local_development_ci')
bootstrap = module('local_ci_container')
IMAGE = 'sha256:' + 'a' * 64


def test_engine_requires_pinned_rootless_cgroup_v2_systemd():
    row = dict(ID='expected', OSType='linux', CgroupVersion='2', CgroupDriver='systemd',
               SecurityOptions=['name=seccomp', 'name=rootless'])
    runner.validate_engine(row, 'expected')
    for patch in ({'ID': 'other'}, {'OSType': 'windows'}, {'CgroupVersion': '1'},
                  {'CgroupDriver': 'none'}, {'SecurityOptions': ['name=seccomp']}):
        with pytest.raises(runner.CIError):
            runner.validate_engine({**row, **patch}, 'expected')


def test_fixed_create_profile_never_exposes_socket_network_or_host_credentials(tmp_path):
    args = runner.create_arguments(IMAGE, 'eh-local-ci-owned', tmp_path, 'all', 'b' * 32)
    assert args[:2] == ['container', 'create']
    assert args[args.index('--network') + 1] == 'none'
    assert args[args.index('--user') + 1] == '10001:10001'
    assert args[args.index('--memory') + 1] == str(runner.MEMORY)
    assert args[args.index('--memory-swap') + 1] == str(runner.MEMORY)
    assert args[args.index('--pids-limit') + 1] == '256'
    assert '--read-only' in args and '--privileged' not in args
    assert '--env' not in args and '--publish' not in args and '--volume' not in args
    assert args.count('--mount') == 1
    assert args[args.index('--mount') + 1] == f'type=bind,source={tmp_path},target=/input,readonly'
    assert args[-4:] == [IMAGE, '/opt/eh-ci/bootstrap.py', 'all', 'b' * 32]


def inspected(tmp_path):
    return dict(Image=IMAGE, Name='/own', Config=dict(User='10001:10001',
        Entrypoint=['/opt/validation-venv/bin/python'], Cmd=['/opt/eh-ci/bootstrap.py', 'python', 'b' * 32]),
        HostConfig=dict(ReadonlyRootfs=True, Privileged=False, NetworkMode='none', PortBindings={},
            CapAdd=[], CapDrop=['ALL'], SecurityOpt=['no-new-privileges=true'], NanoCpus=1_000_000_000,
            Memory=runner.MEMORY, MemorySwap=runner.MEMORY, PidsLimit=256,
            RestartPolicy={'Name': 'no'}, Devices=[], DeviceRequests=[],
            LogConfig={'Type': 'json-file', 'Config': {'max-size': '1m', 'max-file': '1'}},
            Tmpfs={'/tmp': f'rw,nosuid,nodev,exec,size={runner.TMPFS},mode=1777'}),
        Mounts=[dict(Type='bind', Source=str(tmp_path), Destination='/input', RW=False)])


def test_actual_inspection_rejects_profile_mount_and_identity_substitution(tmp_path):
    row = inspected(tmp_path)
    check = lambda value: runner.verify_created(value, IMAGE, 'own', tmp_path, 'python', 'b' * 32)
    check(row)
    for section, key, value in [('HostConfig', 'Memory', 0), ('HostConfig', 'MemorySwap', -1),
        ('HostConfig', 'NetworkMode', 'host'), ('HostConfig', 'Privileged', True),
        ('HostConfig', 'ReadonlyRootfs', False), ('Config', 'User', '0:0'),
        ('HostConfig', 'Tmpfs', {'/tmp': 'rw,exec,size=999999999999'}),
        ('HostConfig', 'IpcMode', 'host'), ('HostConfig', 'PidMode', 'host'),
        ('HostConfig', 'LogConfig', {'Type': 'json-file', 'Config': {}})]:
        mutated = json.loads(json.dumps(row))
        mutated[section][key] = value
        with pytest.raises(runner.CIError):
            check(mutated)
    changed = json.loads(json.dumps(row))
    changed['Mounts'][0]['RW'] = True
    with pytest.raises(runner.CIError):
        check(changed)


@pytest.mark.parametrize('name,kind', [('../escape', 'file'), ('/absolute', 'file'),
    ('.git/config', 'file'), ('link', 'symlink'), ('hardlink', 'hardlink'), ('fifo', 'fifo')])
def test_snapshot_rejects_escape_links_and_special_files(tmp_path, name, kind):
    archive = tmp_path / 'source.tar'
    with tarfile.open(archive, 'w:') as stream:
        entry = tarfile.TarInfo(name)
        entry.type = {'file': tarfile.REGTYPE, 'symlink': tarfile.SYMTYPE,
                      'hardlink': tarfile.LNKTYPE, 'fifo': tarfile.FIFOTYPE}[kind]
        if kind == 'file':
            entry.size = 1
            stream.addfile(entry, io.BytesIO(b'x'))
        else:
            entry.linkname = '../outside'
            stream.addfile(entry)
    with pytest.raises(ValueError):
        bootstrap.extract_snapshot(archive, tmp_path / 'work')


def test_snapshot_accepts_bounded_regular_code_only(tmp_path):
    archive = tmp_path / 'source.tar'
    with tarfile.open(archive, 'w:') as stream:
        entry = tarfile.TarInfo('src/example.py')
        raw = b'print("test")\n'
        entry.size = len(raw)
        stream.addfile(entry, io.BytesIO(raw))
    bootstrap.extract_snapshot(archive, tmp_path / 'work')
    assert (tmp_path / 'work/src/example.py').read_bytes() == raw


@pytest.mark.parametrize('engine', ['tcp://127.0.0.1:2375', 'ssh://host', 'unix://relative', '/var/run/docker.sock'])
def test_no_remote_or_ambient_engine_fallback(engine):
    with pytest.raises(runner.CIError):
        runner.engine_socket(engine)


@pytest.fixture
def cached_profile(tmp_path, monkeypatch):
    inputs, cache, work = tmp_path / 'input', tmp_path / 'cache', tmp_path / 'work'
    inputs.mkdir()
    cache.mkdir()
    intent = 'a' * 32
    (inputs / 'admit.json').write_text(json.dumps({'intent': intent}))
    hashes = {}
    with tarfile.open(inputs / 'source.tar', 'w:') as stream:
        for name in bootstrap.FILES:
            raw = b'{"synthetic":true}\n'
            entry = tarfile.TarInfo(name)
            entry.size = len(raw)
            stream.addfile(entry, io.BytesIO(raw))
            hashes[name] = hashlib.sha256(raw).hexdigest()
    (cache / 'dependencies.json').write_text(json.dumps(hashes))
    modules = cache / 'node_modules'
    (modules / '.bin').mkdir(parents=True)
    (modules / 'tool').mkdir()
    (modules / 'tool/run.js').write_text('synthetic tool')
    (modules / '.bin/tool').symlink_to('../tool/run.js')
    monkeypatch.setattr(bootstrap, 'INPUT', inputs)
    monkeypatch.setattr(bootstrap, 'WORK', work)
    monkeypatch.setattr(bootstrap, 'CACHE', cache)
    for key in ('PYTHONPATH', 'HOME', 'HF_HUB_OFFLINE', 'PYTHONDONTWRITEBYTECODE'):
        monkeypatch.setenv(key, 'before-test')
    commands = []
    monkeypatch.setattr(bootstrap.subprocess, 'run', lambda argv, **kwargs: commands.append((argv, kwargs)))
    return inputs, cache, work, intent, commands


@pytest.mark.parametrize('profile,count', [('python', 1), ('ui', 2), ('all', 3)])
def test_image_owned_cached_profiles_are_fixed_and_preserve_npm_links(cached_profile, profile, count):
    inputs, cache, work, intent, commands = cached_profile
    bootstrap.run_profile(profile, intent)
    assert len(commands) == count
    assert all(options['check'] is True for _, options in commands)
    if profile in {'ui', 'all'}:
        assert commands[-2][0] == ['npm', 'test']
        assert commands[-1][0] == ['npm', 'run', 'build']
        linked = work / 'hub-ui/node_modules/.bin/tool'
        assert linked.is_symlink() and linked.read_text() == 'synthetic tool'
    if profile in {'python', 'all'}:
        assert commands[0][0][:3] == ['/opt/validation-venv/bin/python', '-m', 'pytest']
        assert '-x' in commands[0][0]


def test_changed_dependency_locks_reject_before_source_execution(cached_profile):
    inputs, cache, work, intent, commands = cached_profile
    hashes = json.loads((cache / 'dependencies.json').read_text())
    hashes['uv.lock'] = '0' * 64
    (cache / 'dependencies.json').write_text(json.dumps(hashes))
    with pytest.raises(SystemExit, match='Dependency lock differs'):
        bootstrap.run_profile('all', intent)
    assert commands == []


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux admission flock')
def test_engine_admission_flock_is_shared_across_operations(tmp_path):
    path = tmp_path / 'admission.lock'
    first = runner.private_lock(path)
    try:
        with pytest.raises(runner.CIError):
            runner.private_lock(path)
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_nlink == 1
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.close(first)
    second = runner.private_lock(path)
    os.close(second)


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux metadata/nofollow checks')
@pytest.mark.parametrize('mutation', ['mode', 'symlink', 'hardlink'])
def test_engine_lock_cannot_follow_or_adopt_unsafe_inode(tmp_path, mutation):
    path = tmp_path / 'admission.lock'
    other = tmp_path / 'other'
    if mutation == 'symlink':
        other.write_bytes(b'')
        path.symlink_to(other)
    else:
        path.write_bytes(b'')
        path.chmod(0o644 if mutation == 'mode' else 0o600)
        if mutation == 'hardlink':
            os.link(path, other)
    with pytest.raises(runner.CIError):
        runner.private_lock(path)


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux socket admission')
@pytest.mark.parametrize('marker', ['HELD.json', 'STOPPING.json'])
def test_engine_markers_block_even_a_different_output_root(tmp_path, monkeypatch, marker):
    engine = tmp_path / 'engine'
    engine.mkdir(mode=0o700)
    output1, output2 = tmp_path / 'out1', tmp_path / 'out2'
    output1.mkdir(mode=0o700)
    output2.mkdir(mode=0o700)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(engine / 'engine.sock'))
    runner.write_intent(engine / marker, {'intent': 'unknown-old', 'output_root': str(output1)})
    monkeypatch.setattr(runner, '_run_admitted', lambda *_a: pytest.fail('engine effect admitted'))
    args = SimpleNamespace(image=IMAGE, ref='refs/heads/codex/example', timeout=10,
        checkout=str(tmp_path), output_root=str(output2), engine='unix://' + str(engine / 'engine.sock'))
    try:
        with pytest.raises(runner.CIError, match='admission denied'):
            runner.run(args)
    finally:
        sock.close()
    assert (engine / marker).exists()
    # Denial released only the flock, not retained original intent.
    fd = runner.private_lock(engine / 'admission.lock')
    os.close(fd)


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux private intent metadata')
def test_only_own_original_intent_can_be_cleared(tmp_path):
    path = tmp_path / 'HELD.json'
    runner.write_intent(path, {'intent': 'original', 'container_name': 'own'})
    with pytest.raises(FileExistsError):
        runner.write_intent(path, {'intent': 'replacement'})
    with pytest.raises(runner.CIError):
        runner.clear_own_intent(path, 'other')
    assert path.exists()
    runner.clear_own_intent(path, 'original')
    assert not path.exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux socket/flock')
def test_failure_before_effects_releases_both_admission_locks(tmp_path, monkeypatch):
    engine, output = tmp_path / 'engine', tmp_path / 'output'
    engine.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(engine / 'engine.sock'))
    def fail(*_args):
        raise OSError('synthetic evidence disk failure')
    monkeypatch.setattr(runner, '_run_admitted', fail)
    args = SimpleNamespace(image=IMAGE, ref='refs/heads/codex/example', timeout=10,
        checkout=str(tmp_path), output_root=str(output), engine='unix://' + str(engine / 'engine.sock'))
    try:
        with pytest.raises(OSError):
            runner.run(args)
        for path in (engine / 'admission.lock', output / 'runner.lock'):
            fd = runner.private_lock(path)
            os.close(fd)
    finally:
        sock.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux socket/flock')
def test_unknown_create_retains_engine_and_output_intent_before_effect(tmp_path, monkeypatch):
    engine, output = tmp_path / 'engine', tmp_path / 'output'
    engine.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(engine / 'engine.sock'))
    args = SimpleNamespace(image=IMAGE, ref='refs/heads/codex/example', timeout=10,
        checkout=str(tmp_path), output_root=str(output), engine='unix://' + str(engine / 'engine.sock'),
        engine_id='expected', profile='python')
    raw = b'synthetic lockfile'
    labels = {'io.engineering-hub.ci.' + name + '-sha256': hashlib.sha256(raw).hexdigest()
              for name in runner.FILES.values()}
    monkeypatch.setattr(runner.shutil, 'disk_usage', lambda _path: SimpleNamespace(free=4 * 1024**3))
    calls = []
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    def command(argv, **options):
        calls.append(argv)
        assert 'GIT_CONFIG_COUNT' not in options['environment']
        assert options['environment']['HOME'].startswith(str(output))
        if argv[0].endswith('/git'):
            if argv[1] == 'rev-parse': return b'c' * 40 + b'\n'
            if argv[1] == 'show': return raw
            if argv[1] == 'ls-tree': return b'100644 blob fake 1\tfile\0'
        if 'info' in argv:
            return json.dumps(dict(ID='expected', OSType='linux', CgroupVersion='2',
                CgroupDriver='systemd', SecurityOptions=['name=rootless'])).encode()
        if 'image' in argv:
            return json.dumps([dict(Id=IMAGE, Config={'Labels': labels})]).encode()
        if 'create' in argv:
            first = json.loads((engine / 'HELD.json').read_text())
            second = json.loads((output / 'HELD.json').read_text())
            assert first == second and first['output_root'] == str(output)
            assert first['intent'] and first['container_name'].startswith('eh-local-ci-')
            raise subprocess.TimeoutExpired(argv, 1)
        raise runner.CIError('uncertain synthetic CLI result')
    monkeypatch.setattr(runner, 'checked_output', command)
    def archive(argv, **_options):
        assert 'archive' in argv
        assert argv[:5] == ['/usr/bin/git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf']
        Path(argv[argv.index('--output') + 1]).write_bytes(b'synthetic archive')
    monkeypatch.setattr(runner.subprocess, 'run', archive)
    try:
        with pytest.raises(runner.CIError):
            runner.run(args)
    finally:
        sock.close()
    assert (engine / 'HELD.json').exists() and (output / 'HELD.json').exists()
    evidence = list(output.glob('run-*/result.json'))
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text())['state'] == 'held'
    assert json.loads(evidence[0].read_text())['failure_phase'] == 'container_creation'
    assert json.loads(evidence[0].read_text())['failure_code'] == 'cli_timeout'
    assert not any('start' in command for command in calls)


@pytest.fixture
def kernel_scope(tmp_path, monkeypatch):
    process = tmp_path / 'proc/42'
    process.mkdir(parents=True)
    state = ['S'] + ['0'] * 18 + ['123456']
    (process / 'stat').write_text('42 (synthetic process) ' + ' '.join(state))
    (process / 'cgroup').write_text('0::/user.slice/ci.scope\n')
    cgroups = tmp_path / 'cgroups'
    scope = cgroups / 'user.slice/ci.scope'
    scope.mkdir(parents=True)
    for name, value in {'cpu.max': '100000 100000', 'memory.max': str(runner.MEMORY),
        'memory.swap.max': '0', 'pids.max': '256', 'cgroup.events': 'populated 1\nfrozen 0\n'}.items():
        (scope / name).write_text(value)
    monkeypatch.setattr(runner, 'PROC_ROOT', tmp_path / 'proc')
    monkeypatch.setattr(runner, 'CGROUP_ROOT', cgroups)
    return process, scope


@pytest.mark.skipif(sys.platform != 'linux', reason='Pinned Linux cgroup directory FDs')
def test_kernel_evidence_is_retained_until_all_descendants_are_empty(kernel_scope):
    process, scope = kernel_scope
    pin = runner.verify_kernel_limits(42)
    try:
        assert pin.evidence['path'] == str(scope)
        assert pin.evidence['cpu_max'] == '100000 100000'
        assert pin.evidence['memory_max'] == str(runner.MEMORY)
        assert pin.evidence['memory_swap_max'] == '0'
        assert pin.evidence['pids_max'] == '256'
        with pytest.raises(runner.CIError, match='still has processes'):
            pin.verify_empty()
        (scope / 'cgroup.events').write_text('populated 0\nfrozen 0\n')
        assert pin.verify_empty() == {'state': 'empty', 'descendants_empty': True}
    finally:
        pin.close()
    with pytest.raises(runner.CIError):
        pin.verify_empty()


@pytest.mark.skipif(sys.platform != 'linux', reason='Pinned Linux cgroup directory FDs')
def test_missing_or_replaced_path_does_not_hide_a_populated_pinned_cgroup(kernel_scope):
    process, scope = kernel_scope
    pin = runner.verify_kernel_limits(42)
    retained = scope.with_name('renamed-old-ci.scope')
    scope.rename(retained)
    scope.mkdir()
    (scope / 'cgroup.events').write_text('populated 0\n')
    try:
        with pytest.raises(runner.CIError, match='still has processes'):
            pin.verify_empty()
        (retained / 'cgroup.events').write_text('populated 0\n')
        assert pin.verify_empty()['state'] == 'empty'
    finally:
        pin.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Pinned Linux cgroup deletion')
def test_only_deleted_pinned_scope_counts_as_removed(kernel_scope):
    process, scope = kernel_scope
    pin = runner.verify_kernel_limits(42)
    try:
        (scope / 'cgroup.events').unlink()
        with pytest.raises(runner.CIError, match='quiescence unavailable'):
            pin.verify_empty()
        for entry in scope.iterdir():
            entry.unlink()
        scope.rmdir()
        assert pin.verify_empty() == {'state': 'removed', 'descendants_empty': True,
                                      'removal_proof': 'kernel_deleted_fd'}
    finally:
        pin.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Pinned Linux cgroup deletion')
def test_deleted_pin_with_replacement_path_still_retains_ownership(kernel_scope):
    process, scope = kernel_scope
    pin = runner.verify_kernel_limits(42)
    try:
        for entry in scope.iterdir():
            entry.unlink()
        scope.rmdir()
        scope.mkdir()
        (scope / 'cgroup.events').write_text('populated 0\n')
        with pytest.raises(runner.CIError, match='quiescence unavailable'):
            pin.verify_empty()
    finally:
        pin.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Kernel deleted-FD marker')
def test_deleted_fd_proof_does_not_depend_on_cgroupfs_link_count(kernel_scope, monkeypatch):
    process, scope = kernel_scope
    pin = runner.verify_kernel_limits(42)
    original = os.fstat
    try:
        for entry in scope.iterdir():
            entry.unlink()
        scope.rmdir()
        info = original(pin.fd)
        def cgroupfs_stat(fd):
            if fd == pin.fd:
                return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_nlink=2)
            return original(fd)
        monkeypatch.setattr(runner.os, 'fstat', cgroupfs_stat)
        assert pin.verify_empty()['removal_proof'] == 'kernel_deleted_fd'
    finally:
        pin.close()


@pytest.mark.skipif(sys.platform != 'linux', reason='Pinned Linux resource checks')
@pytest.mark.parametrize('name,value', [('cpu.max', 'max 100000'), ('memory.max', 'max'),
                                      ('memory.swap.max', '1'), ('pids.max', 'max')])
def test_unbounded_kernel_values_never_admit_source_tests(kernel_scope, name, value):
    process, scope = kernel_scope
    (scope / name).write_text(value)
    before = len(os.listdir('/proc/self/fd'))
    with pytest.raises(runner.CIError):
        runner.verify_kernel_limits(42)
    assert len(os.listdir('/proc/self/fd')) == before
