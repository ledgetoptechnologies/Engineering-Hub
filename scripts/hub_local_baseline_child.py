"""Fixed, synthetic-only baseline child IPC for the local acceptance fixture.

The trusted parent owns and selects the local broker listener and outer
lifecycle. The path checks do not authenticate that listener as a broker. No
command, executable or secret is accepted from this request. This entrypoint
is not an enrollment authority or a real-daemon runner.
"""
import base64
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import sys
import time

_ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(_ROOT / 'src'), str(_ROOT)]
from engineering_hub.container_pilot_manifest import PilotManifest
import engineering_hub.pilot_ownership as _owner_module
import hub_container_executor.baseline_observer as _observer_module
from hub_container_executor.daemon_backend import DockerEngineBackend, DockerEngineBinding
from hub_container_executor.operation_journal import SQLiteOperationJournal
from hub_container_executor.baseline_snapshot import BaselineSnapshotError
from scripts.hub_local_baseline_bootstrap import bootstrap_baseline
import scripts.hub_local_baseline_bootstrap as _bootstrap_module
from scripts.hub_local_test_oci import _Store, _SyntheticTrust


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def run(request):
    if (type(request) is not dict or set(request) != {'manifest', 'owner_db', 'socket', 'generation_root',
            'journal', 'owner', 'record', 'blobs', 'ids', 'startup_wait_ms'} or
            any(type(request[k]) is not str for k in ('manifest','owner_db','socket','generation_root','journal','owner','record')) or
            type(request['blobs']) is not dict or len(request['blobs']) != 2 or
            any(type(k) is not str or type(v) is not str for k,v in request['blobs'].items()) or
            type(request['ids']) is not list or len(request['ids']) != 6 or
            any(type(value) is not str for value in request['ids']) or
            type(request['startup_wait_ms']) is not int or not 0 <= request['startup_wait_ms'] <= 30000):
        raise ValueError('invalid bootstrap request')
    owner_db, sock, journal, generation = (Path(request[k]) for k in ('owner_db','socket','journal','generation_root'))
    if (any(not path.is_absolute() or '..' in path.parts or str(path) != str(path.resolve())
            for path in (owner_db, sock, journal, generation)) or
            owner_db.parent != sock.parent or journal.parent != sock.parent or sock.name != 'bootstrap.sock' or
            len({owner_db, sock, journal}) != 3):
        raise ValueError('invalid bootstrap paths')
    parent = os.lstat(sock.parent); endpoint = os.lstat(sock)
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700 or
            not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.getuid() or stat.S_IMODE(endpoint.st_mode) != 0o600):
        raise ValueError('bootstrap listener unavailable')
    manifest = PilotManifest.from_bytes(request['manifest'].encode())
    raw = base64.b64decode(request['record'], validate=True)
    blobs = {key: base64.b64decode(value, validate=True) for key,value in request['blobs'].items()}
    if len(raw) > 65536 or any(len(value) > 1024 * 1024 for value in blobs.values()):
        raise ValueError('bootstrap metadata exceeds bound')
    if (Path(_owner_module.__file__).resolve() != _ROOT/'src/engineering_hub/pilot_ownership.py' or
            Path(_observer_module.__file__).resolve() != _ROOT/'hub_container_executor/baseline_observer.py' or
            Path(_bootstrap_module.__file__).resolve() != _ROOT/'scripts/hub_local_baseline_bootstrap.py'):
        raise ValueError('bootstrap source mismatch')
    operation_journal = SQLiteOperationJournal(journal,create=True,allow_ephemeral_test_storage=True)
    backend = None
    try:
        binding = DockerEngineBinding(manifest.runner_binding.runner_id,manifest.runner_binding.daemon_identity,
            manifest.runner_binding.platform,str(sock),'1.41',str(generation))
        backend = DockerEngineBackend(binding,operation_journal=operation_journal)
        class Secrets:
            def resolve_test_secret(self,slot,version):
                return 'synthetic-' + slot.removeprefix('test-')
        result = bootstrap_baseline(owner_database=owner_db,backend=backend,manifest=manifest,
            record_store=_Store({manifest.rollback.provenance.attestation_digest:raw}),metadata_store=_Store(blobs),
            verifier=_SyntheticTrust(raw),secret_resolver=Secrets(),owner_id=request['owner'],generation_root=str(generation),
            source_generation_id='baseline-g1',frozen_leaf='frozen.sqlite3',create_id=request['ids'][0],
            start_id=request['ids'][1],observe_id=request['ids'][2],maximum_bytes=2*1024*1024,
            deadline_monotonic=time.monotonic()+120,quiescent=True,preflight_probe_id=request['ids'][3],
            stop_id=request['ids'][4],restart_id=request['ids'][5],initialize_empty=True,
            startup_wait_ms=request['startup_wait_ms'])
        return {'manifest':result.manifest.canonical_bytes().decode(),'snapshot':asdict(result.snapshot),
            'receipt':base64.b64encode(result.draft.receipt_bytes).decode(),
            'receipt_digest':result.draft.receipt_digest,'profile_digest':result.draft.runtime_profile_digest,
            'container_id':result.container_id}
    finally:
        try:
            if backend is not None: backend.close()
        finally:
            operation_journal.close()


def main():
    try:
        raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        if not 0 < len(raw) <= 4 * 1024 * 1024: raise ValueError('invalid request size')
        result = run(json.loads(raw, object_pairs_hook=_strict_pairs))
        print(json.dumps(result,separators=(',',':')))
        return 0
    except BaselineSnapshotError as exc:
        allowed={'baseline_inventory_changed','private_snapshot_file_required',
                 'foreign_baseline_entry','baseline_snapshot_failed'}
        code=exc.args[0] if len(exc.args)==1 and type(exc.args[0]) is str and exc.args[0] in allowed else 'unavailable'
        print(json.dumps({'error':'local_baseline_unavailable','phase':'snapshot','code':code},separators=(',',':')))
        return 2
    except Exception:
        print('{"error":"local_baseline_unavailable"}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
