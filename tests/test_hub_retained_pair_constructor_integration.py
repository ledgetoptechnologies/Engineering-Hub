"""Real owner/journal/supervisor constructor; executor and trust inputs synthetic."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
pytest.importorskip('fcntl')
from hub_container_executor import launch_supervisor as launch, retained_oauth_pair as pair
from hub_container_executor import configuration_commitment
from tests.test_executor_launch_profile import supervised_local_oauth_profile
from tests.test_hub_container_executor_launch_supervisor import (
    synthetic_issuer,short_root,root_journal,v3_constructor,disjoint_policy)


@pytest.mark.parametrize('wrong_pin',[False,True])
def test_real_pair_owner_constructs_real_supervisor_only_after_journal_reservation(
        synthetic_issuer,short_root,root_journal,monkeypatch,wrong_pin):
    executor,_,_=synthetic_issuer
    sha=lambda raw:'sha256:'+hashlib.sha256(raw).hexdigest()
    session=b'synthetic-session';admission=b'synthetic-admission';runtime=b'synthetic-runtime'
    enrollment={'synthetic':'broker'}
    broker_raw=json.dumps(enrollment,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
    with v3_constructor(short_root,root_journal) as (data,kwargs,requests):
        data['schema']='coordinator_launch_supervisor_v4';data['admission_raw_digest']=sha(admission)
        data['profile']=supervised_local_oauth_profile();data['profile']['daemon_policy']=disjoint_policy()
        next(m for m in data['profile']['mounts'] if m['role']=='local_issuer_socket')['source']=data['socket_path']
        signed=launch.ExecutorLaunchProfile.model_validate(data['profile'])
        data['release']['launch_profile_digest']=signed.digest()
        raw=launch._canonical(data);Path(kwargs['enrollment_file']).write_bytes(raw)
        executor.profile=SimpleNamespace(role=None,executor_endpoint='protected-uds-v1',gate_ledger_channel='pair-outcome-listener-v1')
        executor._daemon_pin=launch._DaemonPin(**data['daemon'],disjoint=True)
        runtime_path=short_root/'pair-runtime';runtime_path.write_bytes(runtime);runtime_path.chmod(0o600)
        policy={'schema':'engineering-hub.retained-oauth-pair.v2','pair_id':str(uuid4()),
            'journal_file':str(short_root/'pair.sqlite3'),'manifest_digest':'sha256:'+'a'*64,
            'executor_launch_id':executor._launch_id,'coordinator_profile_digest':signed.digest(),
            'session_digest':sha(session),'admission_digest':sha(admission),'broker_enrollment_digest':sha(broker_raw),
            'broker_runtime_policy':str(runtime_path),'broker_runtime_digest':sha(runtime),
            'coordinator_supervisor_digest':sha(b'wrong') if wrong_pin else sha(raw)}
        policy_path=short_root/'pair-policy';policy_path.write_bytes(launch._canonical(policy));policy_path.chmod(0o600)
        seen=[]
        monkeypatch.setattr(pair,'read_enrollment',lambda _:enrollment)
        monkeypatch.setattr(pair.RetainedOAuthPair,'_validate_executor',lambda self:seen.append('executor-checked'))
        monkeypatch.setattr(pair.RetainedOAuthPair,'_bind_endpoint',lambda self:seen.append('endpoint-checked'))
        monkeypatch.setattr(configuration_commitment,'supervised_local_coordinator_commitment',lambda *a,**kw:seen.append('commitment'))
        monkeypatch.setattr(launch.LaunchSupervisor,'_pin_trusted_local_inputs',lambda self:seen.append('capture'))
        owned=pair.RetainedOAuthPair(executor=executor,pair_policy=str(policy_path),config_raw=b'cfg',session_raw=session,
                                     admission_raw=admission,broker_enrollment_fd=8,broker_listener=object())
        try:
            with owned._journal.transaction() as db:
                assert db.execute('SELECT state FROM launch').fetchone()==('create_reserved',)
            args={k:kwargs[k] for k in ('enrollment_file','signing_key_file','journal_file','held_ranges_file')}
            if wrong_pin:
                with pytest.raises(pair.RetainedPairError):owned.create_coordinator(**args)
                assert owned._state=='held' and not Path(kwargs['journal_file']).exists() and requests==[]
            else:
                owned.create_coordinator(**args)
                assert owned._state=='prepared' and type(owned._coordinator) is launch.LaunchSupervisor
                assert owned._coordinator._retained_pair is owned and Path(kwargs['journal_file']).is_file()
                assert seen.index('capture')<seen.index('commitment') and requests==[]
        finally:
            if owned._coordinator is not None:owned._coordinator.close()
            owned._journal.close()
