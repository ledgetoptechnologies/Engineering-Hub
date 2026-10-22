"""Isolated CI-only named-volume startup/recreation check; synthetic tokens only."""
import argparse
import json
import re
import subprocess
import time
import uuid
from hub_dashboard_container_smoke import PROBE


def run(*args, timeout=60):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except subprocess.CalledProcessError as error:
        # This helper accepts only fixed synthetic fixtures, never live tokens.
        raise RuntimeError('Release fixture command failed: ' + (error.stderr or '')[:4096]) from None


def owned_exists(kind, name, owner):
    label = '.Config.Labels' if kind == 'container' else '.Labels'
    result = subprocess.run(['docker', kind, 'inspect', '--format',
                             '{{index ' + label + ' "hub.release-owner"}}', name],
                            capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        if result.stdout.strip() != owner:
            raise RuntimeError('Release fixture ownership mismatch; not removed')
        return True
    if ('no such ' + kind) in result.stderr.lower():
        return False
    raise RuntimeError('Cannot verify release fixture cleanup: Docker inspect failed')


def remove_owned(kind, name, owner):
    if owned_exists(kind, name, owner):
        args = ['docker', kind, 'rm'] + (['-f'] if kind == 'container' else []) + [name]
        run(*args)
        if owned_exists(kind, name, owner):
            raise RuntimeError('Release fixture still exists after removal')


# Exact release-image raw-byte check of a unique synthetic CI-only volume.
# Never open SQLite, initialize schemas or checkpoint the cold files.
RAW_SNAPSHOT = r'''
import hashlib,json,os,stat
from pathlib import Path
root=Path('/var/lib/hub')
result={}
total=0
def identity(s):
    return (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
for p in sorted(root.rglob('*')):
    s=p.lstat()
    assert not stat.S_ISLNK(s.st_mode)
    if stat.S_ISDIR(s.st_mode): continue
    assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_size<=64*1024**2
    total+=s.st_size
    assert total<=256*1024**2
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        assert identity(os.fstat(fd))==identity(s)
        h=hashlib.sha256()
        size=0
        while part:=os.read(fd,65536):
            size+=len(part)
            assert size<=s.st_size
            h.update(part)
        assert size==s.st_size and identity(os.fstat(fd))==identity(s) and identity(p.lstat())==identity(s)
    finally: os.close(fd)
    result[str(p.relative_to(root))]={'size':size,'sha256':h.hexdigest()}
assert '.release-smoke' in result and 'hub.sqlite3' in result
print(json.dumps(result,sort_keys=True))
'''

STANDBY_ARGS = ['-I', '-B', '-m', 'engineering_hub.maintenance_standby', '--standby']
STARTED = {'status': 'maintenance_standby_no_runtime',
    'all_writers_stopped': 'not_established', 'database_opened': False,
    'network_listener_started': False, 'owner_constructed': False,
    'healthcheck_must_be_disabled_by_operator': True}
STOPPED = {'status': 'maintenance_standby_stopped', 'enrollment_or_runtime_started': False}


def fixture_arguments(name, owner, volume, *, readonly_volume=False):
    return ['--pull', 'never', '--name', name, '--label', 'hub.release-owner=' + owner,
        '--network', 'none', '--user', '568:568', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true', '--memory', '512m',
        '--memory-swap', '512m', '--cpus', '1', '--pids-limit', '128',
        '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777',
        '--no-healthcheck', '--restart=no', '--init=false', '--stop-signal', 'SIGTERM',
        '--mount', 'type=volume,source=' + volume + ',target=/var/lib/hub' +
        (',readonly' if readonly_volume else ''), '--entrypoint', 'python']


def snapshot_volume(image, owner, volume, names, suffix):
    name = 'hub-release-' + owner + '-raw-' + suffix
    names.append(name)
    result = json.loads(run('docker', 'run', *fixture_arguments(name, owner, volume,
        readonly_volume=True), image, '-I', '-B', '-c', RAW_SNAPSHOT))
    remove_owned('container', name, owner)
    names.remove(name)
    return result


def standby_pid1_acceptance(image, owner, volume, names):
    before = snapshot_volume(image, owner, volume, names, 'before')
    name = 'hub-release-' + owner + '-standby'
    names.append(name)
    run('docker', 'run', '-d', *fixture_arguments(name, owner, volume), image, *STANDBY_ARGS)
    deadline = time.monotonic() + 15
    while True:
        raw = run('docker', 'logs', name, timeout=5)
        if raw:
            assert [json.loads(line) for line in raw.splitlines()] == [STARTED]
            break
        assert time.monotonic() < deadline, 'Standby marker missing'
        time.sleep(0.1)
    observed = json.loads(run('docker', 'inspect', name))[0]
    assert observed['State']['Running'] is True and observed['State']['Pid'] > 1
    assert observed['Config']['User'] == '568:568'
    assert observed['Config']['Entrypoint'] == ['python'] and observed['Config']['Cmd'] == STANDBY_ARGS
    assert observed['Config']['Labels']['hub.release-owner'] == owner
    assert observed['Config']['Healthcheck']['Test'] == ['NONE'] and 'Health' not in observed['State']
    assert observed['HostConfig']['NetworkMode'] == 'none'
    assert observed['HostConfig']['PidMode'] == '' and observed['HostConfig']['Init'] is False
    assert observed['HostConfig']['RestartPolicy']['Name'] == 'no'
    assert observed['Config']['StopSignal'] == 'SIGTERM'
    identity, image_id = observed['Id'], observed['Image']
    pid1_arguments = json.loads(run('docker', 'exec', name, 'python', '-I', '-B', '-c',
        "import json; from pathlib import Path; print(json.dumps([v.decode('ascii') for v in "
        "Path('/proc/1/cmdline').read_bytes().split(bytes([0]))[:-1]]))"))
    assert pid1_arguments == ['python', *STANDBY_ARGS]
    assert snapshot_volume(image, owner, volume, names, 'during') == before
    # A timeout/forced exit cannot pass. Cleanup may force-remove only this
    # unique owned CI fixture, never a live runtime or host-mounted ledger.
    run('docker', 'stop', '--time', '10', name, timeout=20)
    ended = json.loads(run('docker', 'inspect', name))[0]
    assert ended['Id'] == identity and ended['Image'] == image_id
    assert ended['State']['Running'] is False and ended['State']['Pid'] == 0
    assert ended['State']['ExitCode'] == 0 and ended['State']['OOMKilled'] is False
    assert [json.loads(line) for line in run('docker', 'logs', name).splitlines()] == [STARTED, STOPPED]
    assert snapshot_volume(image, owner, volume, names, 'after') == before
    remove_owned('container', name, owner)
    names.remove(name)
    return {'maintenance_pid1': 'passed', 'container_id': identity, 'image_id': image_id,
        'uid': '568:568', 'stop_signal': 'SIGTERM', 'exit_code': 0,
        'seeded_raw_file_count': len(before), 'seeded_volume_bytes_unchanged': True,
        'healthcheck': 'disabled_not_healthy_claim', 'all_writers_stopped': 'not_established',
        'network': 'none', 'truenas': 'not_deployed'}


def smoke(image, commit):
    if not re.fullmatch(r"ghcr\.io/ledgetoptechnologies/engineering-hub-control:(?:build-)[a-z0-9-]+|ghcr\.io/ledgetoptechnologies/engineering-hub-control@sha256:[a-f0-9]{64}", image):
        raise ValueError("Expected the explicit Hub release artifact")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Expected exact source commit")
    owner = uuid.uuid4().hex
    volume = "hub-release-state-" + owner
    names = []
    probe = PROBE
    try:
        run("docker", "volume", "create", "--label", "hub.release-owner=" + owner, volume)
        for iteration in range(2):
            name = "hub-release-" + owner + "-" + str(iteration)
            names.append(name)
            run("docker", "run", "-d", "--pull", "never", "--name", name,
                "--label", "hub.release-owner=" + owner, "--network", "none",
                "--user", "568:568", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--memory", "512m",
                "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "128",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                "--mount", "type=volume,source=" + volume + ",target=/var/lib/hub",
                "-e", "HUB_BASE_URL=https://hub.ledgetoptechnologies.com",
                "-e", "HERMES_TOKEN=synthetic-release-hermes-only",
                "-e", "CODEX_TOKEN=synthetic-release-codex-only",
                "-e", "CLAUDE_TOKEN=synthetic-release-claude-only", image)
            report = json.loads(run("docker", "exec", name, "python", "-c", probe, commit))
            assert report["image_smoke"] == "passed" and report["build_commit"] == commit
            marker = "from pathlib import Path; p=Path('/var/lib/hub/.release-smoke'); "
            marker += "p.write_text('isolated-release-fixture')" if iteration == 0 else "assert p.read_text() == 'isolated-release-fixture'"
            run("docker", "exec", name, "python", "-c", marker)
            remove_owned('container', name, owner)
            names.remove(name)
        maintenance = standby_pid1_acceptance(image, owner, volume, names)
        return {"source_commit": commit, "image_smoke": "passed", "canonical_host": "hub.ledgetoptechnologies.com",
                "authenticated_agents": 3, "mcp_tools": 21, "human_sign_in": "disabled",
                "named_volume_recreation": "passed", "network": "none", "truenas": "not_deployed",
                "maintenance_standby": maintenance}
    finally:
        for name in names:
            remove_owned('container', name, owner)
        remove_owned('volume', volume, owner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.image, args.commit)))
