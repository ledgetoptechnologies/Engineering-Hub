"""Offline installed-image protected ledger smoke; disposable Docker state only.

No registry access, NAS/SSH, live credentials, published ports or human login.
The three bind sources are derived only from an inspected UUID-owned volume.
"""
import argparse
import json
import os
from pathlib import PurePosixPath
import re
import uuid

from hub_release_smoke import run
from hub_settings_container_smoke import SEED as SETTINGS_SEED, PROBE as SETTINGS_PROBE
from hub_unified_container_smoke import stop_owned, remove_owned_volume


SEED = SETTINGS_SEED + r'''
import hashlib, shutil, sqlite3, subprocess, sys
repo = HubRepository(root / 'hub.sqlite3')
decision = repo.create_decision('human:123','treedar','Synthetic preserved decision',['yes','no'])
with sqlite3.connect(root / 'hub.sqlite3') as db:
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    assert db.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
private = root / 'private'
private.mkdir(mode=0o700)
source = root / 'hub.sqlite3'
target = private / 'hub.sqlite3'
shutil.copyfile(source,target)
target.chmod(0o600)
original = source.read_bytes()
assert target.read_bytes() == original
# All synthetic writers above have exited their transactions. This untouched
# source is the fixture's verified backup, not an attestation about any live NAS.
def enrollment_cli(*args):
    return subprocess.run([sys.executable,'-I','-B','-m',
        'engineering_hub.pilot_enrollment',*args],capture_output=True,
        text=True,timeout=30,check=False)
before_inspection = target.read_bytes()
inspected = enrollment_cli('inspect','--database',str(target))
assert inspected.returncode == 0 and not inspected.stderr
document = json.loads(inspected.stdout)
assert target.read_bytes() == before_inspection
plan_file = private / 'enrollment-plan.json'
with plan_file.open('x',encoding='utf-8') as stream:
    stream.write(inspected.stdout)
plan_file.chmod(0o600)
lock = target.with_name(target.name+'.pilot-owner.lock')
# Operator abort checkpoint: a rejected digest must not enroll or change the
# copy, and the original remains available. This does NOT restore a stale
# source after the new private ledger has accepted authoritative mutations.
wrong_digest = ('0' if document['digest'][0] != '0' else '1')+document['digest'][1:]
aborted = enrollment_cli('apply','--plan',str(plan_file),
    '--expected-digest',wrong_digest,'--writers-stopped','--backup-verified')
assert aborted.returncode == 1 and not aborted.stdout and not lock.exists()
assert target.read_bytes() == before_inspection and source.read_bytes() == original
applied = enrollment_cli('apply','--plan',str(plan_file),
    '--expected-digest',document['digest'],'--writers-stopped','--backup-verified')
assert applied.returncode == 0 and not applied.stderr
enrolled = json.loads(applied.stdout)
assert enrolled['runtime_activated'] is False and source.read_bytes() == original
assert enrolled['prior_snapshot'] == document['plan']['snapshot']
enrolled_bytes, lock_bytes = target.read_bytes(), lock.read_bytes()
retry = enrollment_cli('apply','--plan',str(plan_file),
    '--expected-digest',document['digest'],'--writers-stopped','--backup-verified')
assert retry.returncode == 1 and not retry.stdout
assert target.read_bytes() == enrolled_bytes and lock.read_bytes() == lock_bytes
assert source.read_bytes() == original
settings['schema_version'] = 2
settings['storage'] = {'layout':'protected-ledger-v1'}
(root / 'settings.json').write_text(json.dumps(settings))
saved = json.loads((root / '.settings-smoke.json').read_text())
saved.update(decision=decision,source_sha256=hashlib.sha256(original).hexdigest(),
    settings_sha256=hashlib.sha256((root/'settings.json').read_bytes()).hexdigest(),
    database_device=enrolled['database_device'],database_inode=enrolled['database_inode'])
(private / 'fixture.json').write_text(json.dumps(saved))
print(json.dumps({'protected_seed':'passed','enrollment_cli':'passed',
    'abort_before_enrollment':'unchanged','reenrollment':'refused'}))
'''

# Reuse the existing complete three-identity HTTP/MCP and ingress assertions,
# adding protected-record and notes checks rather than relaxing that contract.
def _replace_once(source, anchor, replacement):
    if source.count(anchor) != 1:
        raise ValueError('Protected smoke probe contract anchor changed')
    return source.replace(anchor,replacement,1)


PROBE = _replace_once(SETTINGS_PROBE,"Path('/var/lib/hub/.settings-smoke.json')",
                      "Path('/var/lib/hub-private/fixture.json')")
PROBE = _replace_once(PROBE,"                        await call(session,'hub_overview')", r'''
                        overview = await call(session,'hub_overview')
                        assert saved['decision'] in overview['decisions']
                        assert overview['autonomy'] == {'configured':False,'state':'disabled','running':False,'receipts':[]}
                        assert overview['runs'] == []
                        assert all(a['chat_readiness']=='unverified' for a in overview['agents'])
                        note = {'project':'engineering-hub','path':'protected-smoke.md',
                            'title':'Protected fixture','content':'Preserved authored note','expected_version':0}
                        if agent == 'hermes' and sys.argv[2] == '0':
                            await call(session,'knowledge_put',note)
                        doc = await call(session,'knowledge_get',{'project':note['project'],'path':note['path']})
                        assert doc['content'] == note['content'] and doc['version'] == 1
''')
PROBE = _replace_once(PROBE,"    listeners = {}", r'''
    import hashlib
    ledger = Path('/var/lib/hub-private/hub.sqlite3').stat()
    assert (ledger.st_dev,ledger.st_ino) == (saved['database_device'],saved['database_inode'])
    assert hashlib.sha256(Path('/var/lib/hub/settings.json').read_bytes()).hexdigest() == saved['settings_sha256']
    assert not Path('/var/lib/hub/hub.sqlite3').exists()
    assert not Path('/var/lib/hub/private').exists()
    listeners = {}
''')

VERIFY = r'''
import hashlib,json,sqlite3
from pathlib import Path
root=Path('/var/lib/hub')
saved=json.loads((root/'private/fixture.json').read_text())
assert hashlib.sha256((root/'hub.sqlite3').read_bytes()).hexdigest()==saved['source_sha256']
assert hashlib.sha256((root/'settings.json').read_bytes()).hexdigest()==saved['settings_sha256']
with sqlite3.connect('file:/var/lib/hub/private/hub.sqlite3?mode=ro',uri=True) as db:
    row=db.execute('SELECT device,inode FROM hub_pilot_owner WHERE id=1').fetchone()
assert row==(saved['database_device'],saved['database_inode'])
print(json.dumps({'preservation':'passed'}))
'''


def _artifact(image, commit):
    if not isinstance(image,str) or not re.fullmatch(
        r'ghcr\.io/ledgetoptechnologies/engineering-hub-control:(?:build-)[a-z0-9-]+|'
        r'ghcr\.io/ledgetoptechnologies/engineering-hub-control@sha256:[a-f0-9]{64}',image):
        raise ValueError('Expected explicit release artifact')
    if not isinstance(commit,str) or not re.fullmatch('[a-f0-9]{40}',commit):
        raise ValueError('Expected exact source commit')


def smoke(image, commit):
    _artifact(image,commit)
    # DOCKER_CONTEXT takes precedence over DOCKER_HOST. Never combine them or
    # infer the effective daemon from only one of two competing selectors.
    if 'DOCKER_HOST' in os.environ:
        raise ValueError('Unset ambient DOCKER_HOST before release smoke')
    context=json.loads(run('docker','context','inspect','--format','{{json .}}'))
    name=context['Name']
    endpoint=context['Endpoints']['docker']['Host']
    if (not isinstance(name,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name)
        or not isinstance(endpoint,str)
        or not (endpoint.startswith('unix:///') or endpoint.startswith('npipe:////./pipe/'))):
        raise ValueError('Only an explicitly pinned local Docker context is permitted')
    # The imported ownership/cleanup helpers spawn Docker too: pin their
    # context for the entire fixture lifetime, including exceptional cleanup.
    prior=os.environ.get('DOCKER_CONTEXT')
    os.environ['DOCKER_CONTEXT']=name
    try:
        return _smoke(image,commit)
    finally:
        if prior is None:
            os.environ.pop('DOCKER_CONTEXT',None)
        else:
            os.environ['DOCKER_CONTEXT']=prior


def _smoke(image, commit):
    image_id = run('docker','image','inspect','--format','{{.Id}}',image)
    if not re.fullmatch('sha256:[a-f0-9]{64}',image_id):
        raise RuntimeError('Immutable local image required')
    owner = uuid.uuid4().hex
    volume = 'hub-protected-state-'+owner
    names, reports = [], []

    def command(name):
        return ['docker','run','--pull','never','--name',name,'--label','hub.release-owner='+owner,
            '--network','none','--user','568:568','--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--memory','512m','--memory-swap','512m',
            '--cpus','1','--pids-limit','128','--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777']

    def fixture(script, suffix):
        name = 'hub-protected-'+owner+'-'+suffix
        names.append(name)
        output = run(*command(name),'--mount','type=volume,source='+volume+',target=/var/lib/hub',
                     '--entrypoint','python',image_id,'-I','-B','-c',script)
        stop_owned(name,owner)
        names.remove(name)
        return json.loads(output.splitlines()[-1])

    try:
        run('docker','volume','create','--label','hub.release-owner='+owner,volume)
        inspected = json.loads(run('docker','volume','inspect','--format','{{json .}}',volume))
        mountpoint = inspected['Mountpoint']
        if (inspected['Name'] != volume or inspected['Labels'].get('hub.release-owner') != owner
            or inspected['Driver'] != 'local' or inspected.get('Options')
            or mountpoint != '/var/lib/docker/volumes/'+volume+'/_data'
            or str(PurePosixPath(mountpoint)) != mountpoint):
            raise RuntimeError('Disposable volume identity mismatch')
        seed = fixture(SEED,'seed')
        assert seed == {'protected_seed':'passed','enrollment_cli':'passed',
                        'abort_before_enrollment':'unchanged','reenrollment':'refused'}
        expected = {'/var/lib/hub/settings.json':(mountpoint+'/settings.json',False),
                    '/var/lib/hub/agent-message-bus':(mountpoint+'/agent-message-bus',True),
                    '/var/lib/hub-private':(mountpoint+'/private',True)}
        for iteration in range(3):
            name = 'hub-protected-'+owner+'-'+str(iteration)
            names.append(name)
            args = command(name)
            args.insert(2,'-d')
            for destination,(source,writable) in expected.items():
                args.extend(['--mount','type=bind,source='+source+',target='+destination+('' if writable else ',readonly')])
            for agent in ('HERMES','CODEX','CLAUDE','ADMIN'):
                args.extend(['-e',agent+'_TOKEN=synthetic-protected-'+agent.lower()+'-only'])
            run(*args,'--entrypoint','python',image_id,'-I','-B','-m',
                'engineering_hub.owned_inactive_hub','--config','/var/lib/hub/settings.json')
            actual=json.loads(run('docker','container','inspect','--format','{{json .}}',name))
            assert actual['Image']==image_id and actual['Config']['User']=='568:568'
            assert actual['Config']['Labels']['hub.release-owner']==owner
            profile=actual['HostConfig']
            assert profile['NetworkMode']=='none' and not profile.get('PortBindings')
            assert profile['ReadonlyRootfs'] and not profile['Privileged'] and not profile.get('Binds')
            assert profile['Memory']==profile['MemorySwap']==512*1024*1024
            assert profile['NanoCpus']==1_000_000_000 and profile['PidsLimit']==128
            assert profile['CapDrop']==['ALL'] and 'no-new-privileges:true' in profile['SecurityOpt']
            assert len(actual['Mounts'])==3
            assert {m['Destination']:(m['Source'],m['RW']) for m in actual['Mounts']}==expected
            assert all(m['Type']=='bind' for m in actual['Mounts'])
            report=json.loads(run('docker','exec',name,'python','-I','-B','-c',PROBE,commit,str(iteration),timeout=60))
            assert report['settings_profile']=='passed' and report['build_commit']==commit
            reports.append(report)
            run('docker','kill','--signal','SIGTERM',name)
            assert run('docker','wait',name,timeout=40)=='0'
            stop_owned(name,owner)
            names.remove(name)
            assert fixture(VERIFY,'verify-'+str(iteration))['preservation']=='passed'
        return {'protected_profile':'passed','source_commit':commit,'resolved_image_id':image_id,
            'stages':reports,'recreations':2,'selective_maps':3,'runtime':'inactive',
            'enrollment_cli':seed,
            'network':'none','truenas':'not_deployed','human_sign_in':'not_tested','agent_wake':'not_tested'}
    finally:
        for name in names:
            stop_owned(name,owner)
        remove_owned_volume(volume,owner)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--commit',required=True)
    args=parser.parse_args()
    print(json.dumps(smoke(args.image,args.commit)))
