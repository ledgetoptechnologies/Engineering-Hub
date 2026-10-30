"""Run inside a disposable networkless Linux container as root; fixtures are synthetic."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from hub_staging_service.runtime_inventory import _empty, _source, _timestamp, _utc

root = Path('/opt/preflight-fixture')
root.mkdir(mode=0o755)
snapshot = root / 'snapshot.json'
token = root / 'token'
token.write_text('synthetic-token-for-test-only')
token.chmod(0o600)
os.chown(token, 12345, 12345)

def write_snapshot(age=0):
    now = _utc() - datetime.timedelta(seconds=age)
    data = _empty(now)
    data['sources'] = [_source('host', 'observed', _timestamp(now))]
    snapshot.write_text(json.dumps(data))
    snapshot.chmod(0o644)

def run(uid=12345, gid=12345):
    def drop():
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    p = subprocess.run([sys.executable, '-m', 'hub_staging_service.observer_preflight',
                        '--snapshot', str(snapshot), '--token-file', str(token),
                        '--expected-uid', '12345', '--expected-gid', '12345'],
                       preexec_fn=drop, capture_output=True, text=True)
    assert not p.stderr and 'synthetic-token' not in p.stdout
    return p.returncode, json.loads(p.stdout)

def expect(identity='observed', snap='observed', cred='observed', uid=12345):
    status, result = run(uid=uid)
    assert status == (0 if identity == snap == cred == 'observed' else 1)
    assert result == {'identity': identity, 'snapshot': snap, 'credential': cred,
                            'firewall_admission_tls_acceptance': 'not_assessed'}

write_snapshot()
expect()
expect('unavailable', 'unavailable', 'unavailable', uid=12346)
root.chmod(0o777)
expect(snap='unavailable', cred='unavailable')
root.chmod(0o755)
snapshot.chmod(0o666)
expect(snap='unavailable')
snapshot.chmod(0o644)
os.chown(snapshot, 12346, 12345)
expect(snap='unavailable')
os.chown(snapshot, 0, 0)
token.chmod(0o644)
expect(cred='unavailable')
token.chmod(0o600)
os.chown(token, 12346, 12345)
expect(cred='unavailable')
os.chown(token, 12345, 12345)
write_snapshot(200)
expect(snap='unavailable')
write_snapshot()
hardlink = root / 'hardlink'
os.link(token, hardlink)
expect(cred='unavailable')
hardlink.unlink()
real_token = root / 'real-token'
token.rename(real_token)
token.symlink_to(real_token.name)
assert token.is_symlink() and real_token.is_file()
assert token.resolve(strict=True) == real_token
expect(cred='unavailable')
print('POSIX preflight integration: 10 cases passed')
