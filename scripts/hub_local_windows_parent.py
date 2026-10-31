"""Explicit Windows parent for one synthetic local fixture; never staging.

The local operator/daemon and source inputs must be trusted and exclusively
controlled. Any ambiguous effect is held. The parent journal is fsynced but
Windows directory-entry durability across power loss is not established.
"""
import json
import os
from pathlib import Path
import sys
import uuid
import argparse
import re
import stat

from scripts.hub_local_windows_storage import WindowsTask, Held, provision_storage
from scripts.hub_local_windows_initialization import initialize, SEED, CLI, _file, _stable_file_observation, validate_daemon_paths
from scripts.hub_local_windows_operator import run_operator, OPERATOR_IMAGE
from scripts.hub_local_windows_cleanup import cleanup


def _preflight(config):
    from engineering_hub.container_pilot_manifest import PilotManifest
    from scripts.hub_local_fixture_metadata import decode_fixture as _fixture
    from scripts.hub_local_test_oci import verify_fixture_image
    from scripts.hub_local_test_launcher import _check_resource_envelope
    inner=config['inner']
    if (config['helper_image_id']!=OPERATOR_IMAGE or config['operator_image_id']!=OPERATOR_IMAGE
        or inner['helper_image_id']!=OPERATOR_IMAGE or inner['coordinator_image_id']!=OPERATOR_IMAGE
        or type(config['daemon_id']) is not str or not re.fullmatch(r'[A-Za-z0-9:._-]{12,128}',config['daemon_id'])
        or type(inner['source_digest']) is not str or not re.fullmatch(r'sha256:[a-f0-9]{64}',inner['source_digest'])
        or type(inner['startup_wait_ms']) is not int or not 0<=inner['startup_wait_ms']<=30000
        or type(inner['timeout_seconds']) is not int or not 1<=inner['timeout_seconds']<=600): raise ValueError('Invalid local fixture pins')
    validate_daemon_paths(tuple(config['daemon_paths']))
    script=str(Path(__file__).resolve().with_name('hub_local_operator_seed.py'))
    _file(script,SEED,131072); _file(config['archive'],config['archive_sha256'],512*1024*1024); _file(config['client'],CLI,128*1024*1024)
    if type(inner['initial_manifest']) is not str: raise ValueError('Invalid manifest')
    manifest=PilotManifest.from_bytes(inner['initial_manifest'].encode('ascii'))
    if manifest.canonical_bytes()!=inner['initial_manifest'].encode('ascii') or any(p.startup_grace_ms for p in manifest.probes.rollback): raise ValueError('Invalid manifest')
    _check_resource_envelope(manifest)
    r=manifest.runtime.resources
    if (3*r.memory_bytes+1073741824+67108864+134217728+536870912+67108864+536870912+536870912+67108864>4*1024**3
        or 3*r.cpu_millicores+1000+100+250+250>2500 or 3*r.pids_limit+256+16+32+128>600): raise ValueError('Resource envelope')
    images=[]
    for role in ('candidate','rollback'):
        image,*_=verify_fixture_image(manifest,role,_fixture(inner[role])); images.append(image)
    return script,images


def _daemon_preflight(task,images,helper_image):
    from scripts.hub_local_test_oci import discover_cached_image
    task.inspect_daemon()
    if (task.call('image','inspect','--format','{{.Id}}',helper_image)!=helper_image
        or json.loads(task.call('image','inspect','--format','{{json (index .Config "Volumes")}}',helper_image)) not in (None,{})):
        raise ValueError('Helper image unavailable')
    for image in images:
        discover_cached_image(image,daemon_id=task.daemon_id,run=lambda args:task.call(*args[3:]))
    task.inspect_daemon()


def run(config):
    fields={'daemon_id','docker_root','base_dir','helper_image_id','operator_image_id',
            'archive','archive_sha256','client','daemon_paths','inner','timeout_seconds'}
    inner_fields={'helper_image_id','coordinator_image_id','initial_manifest','candidate','rollback',
                  'source_digest','startup_wait_ms','timeout_seconds'}
    if (type(config) is not dict or set(config)!=fields or type(config['inner']) is not dict
        or set(config['inner'])!=inner_fields or type(config['daemon_paths']) is not list
        or len(config['daemon_paths'])!=3 or type(config['timeout_seconds']) is not int
        or not 1<=config['timeout_seconds']<=600): raise ValueError('Invalid local fixture request')
    base=Path(config['base_dir'])
    if not base.is_absolute() or not base.is_dir() or base.is_symlink() or base.resolve()!=base:
        raise ValueError('Invalid private parent directory')
    script,images=_preflight(config)
    task_id,source_id,scratch_id,journal_id,inner_id=(str(uuid.uuid4()) for _ in range(5))
    if len({task_id,source_id,scratch_id,journal_id,inner_id})!=5: raise ValueError('Invalid task identity')
    task=WindowsTask(task_id=task_id,daemon_id=config['daemon_id'],docker_root=config['docker_root'],
                     directory=str(base/('local-operator-'+task_id)))
    try:
        _daemon_preflight(task,images,config['helper_image_id'])
        storage=provision_storage(task,helper_image_id=config['helper_image_id'],
            source_task_id=source_id,scratch_task_id=scratch_id,journal_task_id=journal_id)
        digest=initialize(task,storage,script=script,script_sha256=SEED,archive=config['archive'],
            archive_sha256=config['archive_sha256'],client=config['client'],helper_image_id=config['helper_image_id'],
            daemon_paths=tuple(config['daemon_paths']))
        if config['inner']['source_digest']!=digest: raise ValueError('Pinned source tree mismatch')
        request={**config['inner'],'schema':'engineering-hub.local-launch-request.v2','task_id':inner_id,
            'daemon_id':config['daemon_id'],'source_root':storage.source_mount+'/checkout',
            'private_dir':storage.scratch_mount,'journal_dir':storage.journal_mount,'socket_group':0}
        completion=run_operator(task,storage,image_id=config['operator_image_id'],request=request,
                                timeout_seconds=config['timeout_seconds'])
        result=cleanup(task,storage,completion)
        return {'status':result['status'],'task_id':task_id,'journal':str(task.journal_path),'journal_volume':result['journal_volume']}
    except Exception:
        try: task.record('parent-held')
        except Exception: pass
        raise Held('Local fixture held; preserve task '+task_id+' and original journal') from None
    finally:
        try: task.close()
        except Exception: raise Held('Local fixture held; preserve task '+task_id+' and original journal') from None


def main(argv=None):
    try:
        parser=argparse.ArgumentParser(); parser.add_argument('--request-file',required=True); args=parser.parse_args(argv)
        named=os.stat(args.request_file,follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode) or getattr(named,'st_file_attributes',0)&0x400 or named.st_nlink!=1 or named.st_size>8*1024*1024: raise ValueError
        flags=os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)|getattr(os,'O_NOINHERIT',0)
        with os.fdopen(os.open(args.request_file,flags),'rb') as f:
            before=os.fstat(f.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or getattr(before,'st_file_attributes',0)&0x400: raise ValueError
            raw=f.read(8*1024*1024+1); after=os.fstat(f.fileno())
        final=os.stat(args.request_file,follow_symlinks=False)
        if (len(raw)>8*1024*1024 or len(raw)!=before.st_size or not _stable_file_observation(named,before,after,final)
            or getattr(final,'st_file_attributes',0)&0x400): raise ValueError
        def pairs(values):
            result={}
            for key,value in values:
                if key in result: raise ValueError('Duplicate key')
                result[key]=value
            return result
        config=json.loads(raw,object_pairs_hook=pairs)
        result=run(config)
        print(json.dumps(result,sort_keys=True,separators=(',',':')))
        return 0
    except Held as error:
        print(str(error),file=sys.stderr); return 2
    except Exception:
        print('Local fixture preflight unavailable',file=sys.stderr); return 1


if __name__=='__main__': raise SystemExit(main())
