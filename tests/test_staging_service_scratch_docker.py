"""Synthetic scratch/namespace facts only; no quota, Docker or host acceptance."""
import copy
import io
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch
import uuid

import pytest

from hub_staging_service.core import Rejected, recipe
from hub_staging_service.docker import DockerBackend, _SCRATCH_PROBE, _SCRATCH_CLEANUP
from hub_staging_service.scratch import ScratchLease, ScratchSlot
from tests.test_staging_service_docker import profile


@pytest.fixture
def env(monkeypatch):
    command = str(uuid.uuid4())
    paths = {kind: Path('/worker/resources') / str(uuid.uuid4())
             for kind in ('workspace', 'build', 'test_run', 'preview')}
    slot = ScratchSlot(PurePosixPath('/scratch-slot'), str(uuid.uuid4()), '7:9',
        2*1024**3, 131072, 512*1024**2, 4096, 10001, 10001, 210000, 310000)
    record = {'name': 'job-' + command, 'resource_ids': {k: p.name for k,p in paths.items()},
              'directory_identity': [1801, 1234]}
    lease = ScratchLease(slot, None, command, str(uuid.uuid4()), record)
    facts = {'mount_id': 17, 'mount_root': '/', 'mount_device': '7:9', 'filesystem': 'ext4',
             'capacity_bytes': 2*1024**3, 'capacity_inodes': 131072, 'block_size': 4096,
             'blocks': 524288, 'filesystem_id': 123456, 'root_identity': [1801,999],
             'directory_identity': [1801,1234]}
    verifies = []
    def verify(self, *, admission=True):
        assert self is lease
        verifies.append(admission)
        return dict(facts)
    monkeypatch.setattr(ScratchLease, 'verify', verify)
    monkeypatch.setattr('os.getuid', lambda: 10001, raising=False)
    monkeypatch.setattr('os.getgid', lambda: 10001, raising=False)
    mounts = [{'Type':'bind','Source':str(lease.path),'Destination':'/ci-scratch','RW':True,'Propagation':'rprivate'}]
    probe = {'uid':10001,'gid':10001,'owner_uid':0,'owner_gid':0,'mode':0o777,
             'device':1801,'inode':1234,'block_size':4096,'blocks':524288,
             'capacity_inodes':131072,'free_bytes':1024**3,'free_inodes':65536,'filesystem_id':123456,
             'mountinfo':f'22 15 7:9 /{lease.path.name} /ci-scratch rw,nosuid,nodev - ext4 /dev/loop9 rw\n',
             'uid_map':'0 10001 1\n1 200000 65536\n','gid_map':'0 10001 1\n1 300000 65536\n'}
    backend = DockerBackend('unix:///run/user/10001/docker.sock')
    backend._scratch_engine = (lease, 'engine-A')
    monkeypatch.setattr(backend, '_verify_daemon', lambda: 'engine-A')
    return SimpleNamespace(command=command, paths=paths, lease=lease, facts=facts,
        mounts=mounts, probe=probe, backend=backend, verifies=verifies, profile=recipe(profile()))


def checked_probe(env):
    def output(parts, **kwargs):
        if parts[0] == 'inspect': return json.dumps(env.mounts).encode()
        assert parts[0] == 'exec' and parts[2:] == ('/usr/local/bin/python','-I','-B','-c',_SCRATCH_PROBE)
        return json.dumps(env.probe).encode()
    return output


def test_only_captured_lease_binds_private_nonrecursive_scratch(env):
    _, argv = env.backend.argv(env.paths, env.profile, env.command, 'build', scratch_lease=env.lease)
    assert '--network=none' in argv and '--cap-drop=ALL' in argv
    assert argv[argv.index('--user')+1] == '10001:10001'
    assert 'type=bind,src='+str(env.lease.path)+',dst=/ci-scratch,bind-propagation=rprivate,bind-recursive=disabled' in argv
    assert '/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777' in argv
    assert any(item.startswith('/workspace:rw,exec,nosuid,nodev,') for item in argv)
    with pytest.raises(Rejected):
        env.backend.argv(env.paths, env.profile, env.command, 'build', scratch_lease={'path':'/etc'})
    with pytest.raises(Rejected):
        env.backend.argv(env.paths, env.profile, str(uuid.uuid4()), 'build', scratch_lease=env.lease)


def test_actual_kernel_fact_contract_and_uid_maps_checked_before_source(env, monkeypatch):
    monkeypatch.setattr(env.backend, '_checked_output', checked_probe(env))
    env.backend._verify_scratch_mount('owned', env.lease, env.paths, env.command)
    assert env.verifies == [True, True]


@pytest.mark.parametrize('change', ['readonly','foreign-source','nested','noexec','missing-nodev','wrong-root',
    'device','inode','filesystem-id','capacity','free-bytes','free-inodes','uid','mapping','owner','malformed-map'])
def test_unverified_child_mount_capacity_or_identity_denies_source(env, monkeypatch, change):
    if change == 'readonly': env.mounts[0]['RW'] = False
    elif change == 'foreign-source': env.mounts[0]['Source'] = '/other'
    elif change == 'nested': env.mounts.append(dict(env.mounts[0], Destination='/ci-scratch/nested'))
    elif change == 'noexec': env.probe['mountinfo'] = env.probe['mountinfo'].replace('rw,nosuid,nodev','rw,nosuid,nodev,noexec')
    elif change == 'missing-nodev': env.probe['mountinfo'] = env.probe['mountinfo'].replace('rw,nosuid,nodev','rw,nosuid')
    elif change == 'wrong-root': env.probe['mountinfo'] = env.probe['mountinfo'].replace('/job-', '/other-')
    elif change == 'device': env.probe['device'] += 1
    elif change == 'inode': env.probe['inode'] += 1
    elif change == 'filesystem-id': env.probe['filesystem_id'] += 1
    elif change == 'capacity': env.probe['blocks'] += 1
    elif change == 'free-bytes': env.probe['free_bytes'] = 0
    elif change == 'free-inodes': env.probe['free_inodes'] = 0
    elif change == 'uid': env.probe['uid'] = 0
    elif change == 'mapping': env.probe['uid_map'] = '0 0 4294967295\n'
    elif change == 'owner': env.probe['owner_uid'] = 10001
    else: env.probe['uid_map'] = '0 10001 1\n0 200000 65536\n'
    monkeypatch.setattr(env.backend, '_checked_output', checked_probe(env))
    with pytest.raises(Rejected): env.backend._verify_scratch_mount('owned', env.lease, env.paths, env.command)
    assert not env.lease.backend_absent


def test_scratch_guard_fails_before_source_copy_or_command_popen(env, monkeypatch):
    env.probe['inode'] += 1
    monkeypatch.setattr(env.backend, '_checked_output', checked_probe(env))
    monkeypatch.setattr(env.backend, '_verify_kernel_limits', lambda *a: None)
    monkeypatch.setattr(env.backend, '_verify_job_mounts', lambda *a: None)
    monkeypatch.setattr('hub_staging_service.docker.subprocess.run', lambda *a,**k: subprocess.CompletedProcess(a,0))
    popen = Mock(side_effect=AssertionError('SOURCE MUST NOT RUN'))
    monkeypatch.setattr('hub_staging_service.docker.subprocess.Popen', popen)
    with pytest.raises(Rejected):
        env.backend._stage_inner(env.paths,env.profile,env.command,'build',scratch_lease=env.lease)
    popen.assert_not_called()


HOST_MOUNTS = (b'1 0 0:1 / / rw - tmpfs tmpfs rw\n'
               b'2 1 7:9 / /scratch-slot rw,nosuid,nodev - ext4 /dev/loop9 rw\n')


def setup_absence(env, monkeypatch, *, source=None, unstable=False, host=HOST_MOUNTS):
    calls = []
    monkeypatch.setattr(env.backend, '_cleanup_scratch', lambda *args: calls.append(('trusted-cleanup',)))
    monkeypatch.setattr(env.backend, '_container_absent', lambda name: calls.append(('absent',name)))
    identity='a'*64
    scans=[0]
    def output(parts, **kwargs):
        if parts[:3] == ('volume','ls','--quiet'): return b''
        if parts[0] == 'ps':
            scans[0] += 1
            return (identity+'\n'+('b'*64+'\n' if unstable and scans[0]>1 else '')).encode()
        assert parts[0]=='inspect' and parts[-1]==identity
        mounts=[] if source is None else [{'Type':'bind','Source':source}]
        return json.dumps(mounts).encode()+b'\n'
    monkeypatch.setattr(env.backend, '_checked_output', output)
    monkeypatch.setattr('builtins.open', mock_open(read_data=host))
    return calls,scans


def test_cleanup_proof_requires_stable_engine_visible_mounts_and_original_lease(env, monkeypatch):
    calls,scans=setup_absence(env,monkeypatch)
    env.backend._confirm_scratch_absent(env.paths,env.profile,env.command,env.lease)
    assert env.lease.backend_absent and sum(item[0]=='absent' for item in calls)==14 and scans[0]==4
    assert sum(item[0]=='trusted-cleanup' for item in calls)==1
    assert env.verifies and all(admission is False for admission in env.verifies)
    assert env.lease.absence_receipt()['scope']=='owned_containers_engine_binds_worker_visible_mounts'


@pytest.mark.parametrize('reference', ['exact','parent','child','unstable','visible-alias','visible-child'])
def test_any_reference_or_uncertainty_keeps_original_lease(env,monkeypatch,reference):
    source={'exact':str(env.lease.path),'parent':'/scratch-slot','child':str(env.lease.path)+'/child'}.get(reference)
    host=HOST_MOUNTS
    if reference=='visible-alias': host+=b'3 1 7:9 / /alias rw,nosuid,nodev - ext4 /dev/loop9 rw\n'
    elif reference=='visible-child': host+=(f'3 1 0:4 / {env.lease.path}/child rw - tmpfs tmpfs rw\n').encode()
    setup_absence(env,monkeypatch,source=source,unstable=reference=='unstable',host=host)
    with pytest.raises(Rejected):
        env.backend._confirm_scratch_absent(env.paths,env.profile,env.command,env.lease)
    assert not env.lease.backend_absent


def test_failed_stage_still_needs_absence_proof_but_never_releases_lease(env,monkeypatch):
    env.backend._scratch_engine = None
    monkeypatch.setattr('hub_staging_service.docker.os',SimpleNamespace(name='posix',getuid=lambda:10001,getgid=lambda:10001))
    monkeypatch.setattr(env.backend,'_run_stages',Mock(side_effect=Rejected('stage_test_failed')))
    proof=Mock(); monkeypatch.setattr(env.backend,'_confirm_scratch_absent',proof)
    monkeypatch.setattr(env.backend,'_runtime_absent',Mock())
    # A bare error has no frozen-export receipt, so it remains held.
    with pytest.raises(Rejected,match='execution_uncertain'):
        env.backend.run(env.paths,env.profile,env.command,scratch_lease=env.lease)
    proof.assert_called_once_with(env.paths,env.profile,env.command,env.lease)
    assert not env.lease.backend_absent


def test_optional_absence_keeps_legacy_backend_callshape(env,monkeypatch):
    env.backend._scratch_engine=None
    monkeypatch.setattr(env.backend,'_runtime_absent',Mock())
    monkeypatch.setattr('hub_staging_service.docker.os',SimpleNamespace(name='posix',getuid=lambda:10001,getgid=lambda:10001))
    stages=Mock(return_value={'stages':{},'preview_ready':True})
    monkeypatch.setattr(env.backend,'_run_stages',stages)
    proof=Mock(side_effect=AssertionError('scratch absent'))
    monkeypatch.setattr(env.backend,'_confirm_scratch_absent',proof)
    assert env.backend.run(env.paths,env.profile,env.command)['preview_ready']
    stages.assert_called_once_with(env.paths,env.profile,env.command)
    proof.assert_not_called()


@pytest.mark.parametrize('floor_exhausted', [False, True])
def test_cleanup_keeps_all_identity_guards_but_allows_exhausted_floor(env,monkeypatch,floor_exhausted):
    if floor_exhausted: env.probe.update(free_bytes=0,free_inodes=0)
    monkeypatch.setattr(env.backend,'_checked_output',checked_probe(env))
    env.backend._verify_scratch_mount('owned',env.lease,env.paths,env.command,admission=False)
    assert env.verifies == [False,False]
    env.probe['inode'] += 1
    with pytest.raises(Rejected):
        env.backend._verify_scratch_mount('owned',env.lease,env.paths,env.command,admission=False)


def cleanup_harness(env,monkeypatch,*,failure=False):
    events=[]
    monkeypatch.setattr(env.backend,'_container_absent',lambda n:events.append(('absent',n)))
    monkeypatch.setattr(env.backend,'_stop_owned',lambda *a:events.append(('stop',a)))
    monkeypatch.setattr(env.backend,'_verify_kernel_limits',lambda n,p:events.append(('kernel',p)))
    monkeypatch.setattr(env.backend,'_verify_tmpfs_mount',lambda *a,**k:events.append(('tmp',a,k)))
    monkeypatch.setattr(env.backend,'_verify_scratch_mount',lambda *a,**k:events.append(('scratch',k)))
    def run(argv,**kwargs):
        events.append(('create',argv,kwargs));return subprocess.CompletedProcess(argv,0)
    monkeypatch.setattr('hub_staging_service.docker.subprocess.run',run)
    def output(parts,**kwargs):
        events.append(('exec',parts,kwargs))
        if failure:raise Rejected('resource_limits_unverified')
        return json.dumps({'empty':True,'removed':8,'device':env.lease.device,'inode':env.lease.inode}).encode()
    monkeypatch.setattr(env.backend,'_checked_output',output)
    return events


def test_trusted_cleanup_container_is_same_uid_bounded_and_has_only_exact_bind(env,monkeypatch):
    events=cleanup_harness(env,monkeypatch)
    env.backend._cleanup_scratch(env.paths,env.profile,env.command,env.lease)
    created=next(event for event in events if event[0]=='create')
    argv=created[1]
    assert argv[argv.index('--user')+1]=='10001:10001'
    assert '--network=none' in argv and '--cap-drop=ALL' in argv and '--read-only' in argv
    assert argv.count('--mount')==1
    assert str(env.lease.path) in argv[argv.index('--mount')+1]
    assert '/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777' in argv
    # The trusted CLI pins its Unix socket before `run`; no socket is mounted
    # into the helper and none of its container options expose one.
    assert not any('docker.sock' in value or 'dst=/source' in value or 'privileged' in value
                   for value in argv[argv.index('run')+1:])
    policy=next(event[1] for event in events if event[0]=='kernel')
    assert policy['cpus']==0.25 and policy['memory_mb']==128 and policy['pids']==32
    assert next(event for event in events if event[0]=='scratch')[1]=={'admission':False}
    execution=next(event for event in events if event[0]=='exec')
    assert execution[1][2:7]==('/usr/local/bin/python','-I','-B','-c',_SCRATCH_CLEANUP)
    assert execution[2]['timeout']==60 and execution[2]['limit']==4096
    assert events[-2][0]=='stop' and events[-1][0]=='absent'
    assert not env.lease.backend_absent  # Only the later all-reference proof may confirm.


def test_uncertain_cleanup_stops_only_owned_helper_and_never_confirms_lease(env,monkeypatch):
    events=cleanup_harness(env,monkeypatch,failure=True)
    with pytest.raises(Rejected):env.backend._cleanup_scratch(env.paths,env.profile,env.command,env.lease)
    assert events[-1][0]=='stop' and not env.lease.backend_absent


def test_preexisting_cleanup_helper_is_never_adopted_or_removed(env,monkeypatch):
    monkeypatch.setattr(env.backend,'_container_absent',Mock(side_effect=Rejected('execution_uncertain')))
    start=Mock();stop=Mock()
    monkeypatch.setattr('hub_staging_service.docker.subprocess.run',start)
    monkeypatch.setattr(env.backend,'_stop_owned',stop)
    with pytest.raises(Rejected):env.backend._cleanup_scratch(env.paths,env.profile,env.command,env.lease)
    start.assert_not_called();stop.assert_not_called()
    assert not env.lease.backend_absent


@pytest.mark.parametrize('reference', ['exact','parent','same-device','block-alias','symlink','plugin','unknown-type','unparseable','unstable'])
def test_detached_or_opaque_volume_references_keep_lease(env,monkeypatch,reference):
    scans=[0]
    device={'exact':str(env.lease.path),'parent':'/scratch-slot','same-device':'/alias',
            'block-alias':'/dev/alias','symlink':'/symlink','unparseable':'nfs://opaque'}.get(reference)
    def output(parts,**kwargs):
        if parts[:3]==('volume','ls','--quiet'):
            scans[0]+=1
            return b'volume-a\nvolume-b\n' if reference=='unstable' and scans[0]>1 else b'volume-a\n'
        return json.dumps(['volume-a','plugin' if reference=='plugin' else 'local',
            '/daemon/volumes/a',device,'ext4' if reference=='unknown-type' else None]).encode()+b'\n'
    monkeypatch.setattr(env.backend,'_checked_output',output)
    original_os=__import__('hub_staging_service.docker',fromlist=['os']).os
    def fake_stat(value,**kwargs):
        same=value in ('/alias','/dev/alias')
        return SimpleNamespace(st_mode=stat.S_IFLNK if value=='/symlink' else
            stat.S_IFBLK if value=='/dev/alias' else stat.S_IFDIR,
            st_dev=env.lease.device if same and value!='/dev/alias' else 999,
            st_rdev=env.lease.device if value=='/dev/alias' else 0)
    monkeypatch.setattr('hub_staging_service.docker.os',SimpleNamespace(**{**vars(original_os),'stat':fake_stat}))
    with pytest.raises(Rejected):env.backend._scratch_volumes_absent(env.lease)
    assert not env.lease.backend_absent


def test_disjoint_default_local_and_tmpfs_volumes_are_bounded_metadata_only(env,monkeypatch):
    calls=[]
    def output(parts,**kwargs):
        calls.append(parts)
        if parts[:3]==('volume','ls','--quiet'):return b'volume-a\nvolume-b\n'
        return (json.dumps(['volume-a','local','/daemon/a',None,None])+'\n'+
                json.dumps(['volume-b','local','/daemon/b','tmpfs','tmpfs'])+'\n').encode()
    monkeypatch.setattr(env.backend,'_checked_output',output)
    original_os=__import__('hub_staging_service.docker',fromlist=['os']).os
    monkeypatch.setattr('hub_staging_service.docker.os',SimpleNamespace(**{**vars(original_os),
        'stat':lambda *a,**k:SimpleNamespace(st_mode=stat.S_IFDIR,st_dev=999,st_rdev=0)}))
    env.backend._scratch_volumes_absent(env.lease)
    assert len(calls)==3 and '.Options.o' not in str(calls) and '.Env' not in str(calls)


@pytest.mark.skipif(os.name!='posix' or not hasattr(os,'O_PATH'),reason='POSIX fd-safe cleanup primitive')
def test_fixed_cleanup_script_removes_owned_private_dirs_without_following_links(tmp_path,monkeypatch,capsys):
    if os.getuid()==0:pytest.skip('Trusted cleanup requires a real nonroot test UID')
    root=tmp_path/'job';root.mkdir(mode=0o777);root.chmod(0o777)
    private=root/'private';private.mkdir(mode=0o700);(private/'file').write_text('scratch')
    denied=root/'denied';denied.mkdir();(denied/'file').write_text('scratch');denied.chmod(0)
    outside=tmp_path/'outside';outside.mkdir();(outside/'retained').write_text('keep')
    (root/'link').symlink_to(outside,target_is_directory=True)
    identity=root.stat();mode=stat.S_IMODE(identity.st_mode)
    original_open=os.open
    def opened(value,*args,**kwargs):
        return original_open(str(root) if value=='/ci-scratch' else value,*args,**kwargs)
    monkeypatch.setattr(os,'open',opened)
    monkeypatch.setattr(sys,'argv',['cleanup',str(identity.st_dev),str(identity.st_ino),
        str(os.getuid()),str(os.getgid()),'100000'])
    exec(compile(_SCRATCH_CLEANUP,'<fixed-image-owned-cleanup>','exec'),{})
    report=json.loads(capsys.readouterr().out)
    assert report['empty'] and report['removed']==5
    assert not list(root.iterdir()) and root.stat().st_ino==identity.st_ino
    assert stat.S_IMODE(root.stat().st_mode)==mode==0o777
    assert (outside/'retained').read_text()=='keep'


def test_cli_static_scratch_schema_is_checked_without_service_or_quota_probe(tmp_path,monkeypatch):
    from hub_staging_service.__main__ import main
    from tests.test_staging_service_http import StorageConfigTests
    config=StorageConfigTests().configuration(tmp_path)
    config['scratch_slot']={'path':'/scratch-slot','filesystem_uuid':str(uuid.uuid4()),'mount_device':'7:9',
        'ceiling_bytes':2*1024**3,'ceiling_inodes':131072,'free_floor_bytes':512*1024**2,
        'free_floor_inodes':4096,'container_uid':10001,'container_gid':10001,'mapped_uid':210000,'mapped_gid':310000}
    file=tmp_path/'config.json';file.write_text(json.dumps(config))
    monkeypatch.setattr(sys,'argv',['worker','--config',str(file),'--check-config'])
    monkeypatch.setattr('hub_staging_service.__main__.credentials',lambda _: 'synthetic-test-token')
    service=Mock();monkeypatch.setattr('hub_staging_service.__main__.Service',service)
    with patch.object(ScratchSlot,'verify',side_effect=AssertionError('NO QUOTA PROBE')):
        main()
    service.assert_not_called()
    config['scratch_slot']['source_path']='/etc';file.write_text(json.dumps(config))
    with pytest.raises(Rejected):main()
    service.assert_not_called()


@pytest.mark.parametrize('info', [
    {'ID':'engine-A','SecurityOptions':[],'CgroupVersion':'2','CgroupDriver':'systemd'},
    {'SecurityOptions':['name=rootless'],'CgroupVersion':'2','CgroupDriver':'systemd'},
])
def test_failed_initial_engine_admission_never_enters_cleanup_or_source(env,monkeypatch,info):
    env.backend._scratch_engine=None
    monkeypatch.setattr('hub_staging_service.docker.os',SimpleNamespace(name='posix',getuid=lambda:10001,getgid=lambda:10001))
    monkeypatch.setattr(env.backend,'_verify_daemon',DockerBackend._verify_daemon.__get__(env.backend))
    output=Mock(return_value=json.dumps(info).encode());monkeypatch.setattr(env.backend,'_checked_output',output)
    stages=Mock();cleanup=Mock();confirm=Mock()
    monkeypatch.setattr(env.backend,'_run_stages',stages)
    monkeypatch.setattr(env.backend,'_cleanup_scratch',cleanup)
    monkeypatch.setattr(env.lease,'confirm_backend_absent',confirm)
    with pytest.raises(Rejected,match='rootless_cgroup_policy_unverified'):
        env.backend.run(env.paths,env.profile,env.command,scratch_lease=env.lease)
    stages.assert_not_called();cleanup.assert_not_called();confirm.assert_not_called()
    assert output.call_args_list[0].args[0][0]=='info' and output.call_count==1
    assert env.backend._scratch_engine is None


def real_engine(env,monkeypatch):
    selected=['engine-A']
    monkeypatch.setattr(env.backend,'_verify_daemon',DockerBackend._verify_daemon.__get__(env.backend))
    def output(parts,**kwargs):
        assert parts[0]=='info'
        return json.dumps({'ID':selected[0],'SecurityOptions':['name=rootless'],
            'CgroupVersion':'2','CgroupDriver':'systemd'}).encode()
    monkeypatch.setattr(env.backend,'_checked_output',output)
    return selected


def test_engine_retarget_between_stages_denies_second_source_effect_and_cleanup(env,monkeypatch):
    env.backend._scratch_engine=None
    selected=real_engine(env,monkeypatch)
    monkeypatch.setattr('hub_staging_service.docker.os',SimpleNamespace(name='posix',getuid=lambda:10001,getgid=lambda:10001))
    monkeypatch.setattr(env.backend,'_container_absent',Mock())
    monkeypatch.setattr(env.backend,'_volume_absent',Mock())
    create=Mock();monkeypatch.setattr(env.backend,'_create_volume',create)
    monkeypatch.setattr(env.backend,'_stop_owned',Mock());monkeypatch.setattr(env.backend,'_remove_volume',Mock())
    executed=[]
    def inner(paths,profile,command,stage,**kwargs):
        executed.append(stage);selected[0]='engine-B';return {}
    monkeypatch.setattr(env.backend,'_stage_inner',inner)
    def stages(paths,profile,command,**kwargs):
        env.backend._stage(paths,profile,command,'build',**kwargs)
        env.backend._stage(paths,profile,command,'test',**kwargs)
    monkeypatch.setattr(env.backend,'_run_stages',stages)
    helper=Mock();monkeypatch.setattr(env.backend,'_cleanup_scratch',helper)
    with pytest.raises(Rejected,match='execution_uncertain'):
        env.backend.run(env.paths,env.profile,env.command,scratch_lease=env.lease)
    assert executed==['build'] and create.call_count==2
    helper.assert_not_called();assert not env.lease.backend_absent
    assert env.backend._scratch_engine is None


def test_engine_retarget_after_inventory_blocks_helper_and_absence_confirmation(env,monkeypatch):
    selected=real_engine(env,monkeypatch)
    def scanned(*args):selected[0]='engine-B'
    monkeypatch.setattr(env.backend,'_scratch_references_absent',scanned)
    run=Mock();monkeypatch.setattr('hub_staging_service.docker.subprocess.run',run)
    with pytest.raises(Rejected,match='execution_uncertain'):
        env.backend._confirm_scratch_absent(env.paths,env.profile,env.command,env.lease)
    run.assert_not_called();assert not env.lease.backend_absent


def test_fixed_scratch_flag_is_absent_without_lease(env):
    assert 'EH_CI_SCRATCH=1' not in env.backend.argv(env.paths,env.profile,env.command,'build')[1]
    args=env.backend.argv(env.paths,env.profile,env.command,'build',scratch_lease=env.lease)[1]
    assert args.count('EH_CI_SCRATCH=1')==1 and args[args.index('EH_CI_SCRATCH=1')-1]=='--env'


def test_fresh_cleanup_policy_failure_cannot_launch_helper(env,monkeypatch):
    monkeypatch.setattr(env.backend,'_verify_daemon',Mock(side_effect=Rejected('rootless_cgroup_policy_unverified')))
    started=Mock();monkeypatch.setattr('hub_staging_service.docker.subprocess.run',started)
    checked=Mock();monkeypatch.setattr(env.backend,'_checked_output',checked)
    with pytest.raises(Rejected,match='rootless_cgroup_policy_unverified'):
        env.backend._confirm_scratch_absent(env.paths,env.profile,env.command,env.lease)
    started.assert_not_called();checked.assert_not_called();assert not env.lease.backend_absent


def test_bound_engine_retarget_blocks_cli_before_subprocess(env,monkeypatch):
    selected=real_engine(env,monkeypatch);selected[0]='engine-B'
    with pytest.raises(Rejected,match='execution_uncertain'):
        env.backend.command('exec','owned','/usr/local/bin/python','-I','-B')
