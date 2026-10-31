"""Exact same-process local fixture cleanup; unknown outcomes remain held.

Requires the trusted exclusive Windows parent and its durable task journal.
The separate journal volume remains as evidence; no host-wide custody is claimed.
"""
import json
import re
from scripts.hub_local_windows_storage import WindowsTask,OperatorStorage,Held,LABEL
from scripts.hub_local_windows_operator import OperatorCompletion
from scripts.hub_local_windows_initialization import _storage


def cleanup(task,storage,completion):
    if (type(task) is not WindowsTask or type(storage) is not OperatorStorage or type(completion) is not OperatorCompletion
        or getattr(task,'_operator_completion',None) is not completion or task._storage is not storage
        or completion.task is not task or completion.storage is not storage
        or task._operator_binding!=(storage,completion.request_sha256,completion.image_id)):
        raise Held('Cleanup evidence unavailable')
    with task._seed_lock:
        with task._cleanup_lock:
            if task._cleanup_started: raise Held('Cleanup already attempted; preserve resources')
            task._cleanup_started=True
        return _cleanup_locked(task,storage,completion)


def _cleanup_locked(task,storage,completion):
    def absent(keeper=True):
        task.inspect_daemon()
        containers=task.call('container','ls','--all','--quiet','--no-trunc').splitlines()
        if (any(not re.fullmatch(r'[a-f0-9]{64}',x) for x in containers)
            or completion.operator_id in containers or completion.coordinator_id in containers): raise ValueError
        if task.call('container','ls','--all','--quiet','--no-trunc','--filter','label='+LABEL+'='+completion.inner_task_id): raise ValueError
        if task.call('volume','ls','--quiet','--filter','label='+LABEL+'='+completion.inner_task_id): raise ValueError
        outer=task.call('container','ls','--all','--quiet','--no-trunc','--filter','label='+LABEL+'='+task.task_id).splitlines()
        if outer!=([storage.scratch_keeper_id] if keeper else []): raise ValueError
        volumes=task.call('volume','ls','--quiet','--filter','label='+LABEL+'='+task.task_id).splitlines()
        expected={storage.journal_name}
        if keeper: expected|={storage.source_name,storage.scratch_name}
        else: expected|=remaining
        if set(volumes)!=expected or len(volumes)!=len(expected): raise ValueError
    remaining={storage.source_name,storage.scratch_name}
    try:
        absent(); _storage(task,storage)
        users=task.call('container','ls','--all','--quiet','--no-trunc','--filter','volume='+storage.source_name).splitlines()
        if users: raise ValueError
        users=task.call('container','ls','--all','--quiet','--no-trunc','--filter','volume='+storage.scratch_name).splitlines()
        if users!=[storage.scratch_keeper_id]: raise ValueError
        before=json.loads(task.call('container','inspect',storage.scratch_keeper_id))[0]
        absent(); _storage(task,storage); task.record('scratch-keeper-stop-intent',id=storage.scratch_keeper_id)
        if task.call('container','stop','--time','5',storage.scratch_keeper_id)!=storage.scratch_keeper_id: raise ValueError
        task.record('scratch-keeper-stopped',id=storage.scratch_keeper_id)
        after=json.loads(task.call('container','inspect',storage.scratch_keeper_id))[0]; s=after['State']
        if (after['Id']!=storage.scratch_keeper_id or any(after[k]!=before[k] for k in ('Name','Config','HostConfig','Mounts'))
            or s.get('Status')!='exited' or s.get('Running') is not False or type(s.get('Pid')) is not int or s['Pid']!=0
            or any(s.get(k) is not False for k in ('Paused','Restarting','Dead'))): raise ValueError
        absent(); task.record('scratch-keeper-remove-intent',id=storage.scratch_keeper_id)
        if task.call('container','rm',storage.scratch_keeper_id)!=storage.scratch_keeper_id: raise ValueError
        task.record('scratch-keeper-removed',id=storage.scratch_keeper_id); absent(False)
        if storage.scratch_keeper_id in task.call('container','ls','--all','--quiet','--no-trunc').splitlines(): raise ValueError
        for name,mount,opts in ((storage.source_name,storage.source_mount,{}),(storage.scratch_name,storage.scratch_mount,{'type':'tmpfs','device':'tmpfs','o':'size=536870912,mode=0700,nr_inodes=4096'})):
            absent(False); task.inspect_daemon()
            rows=json.loads(task.call('volume','inspect',name)); v=rows[0]
            if (len(rows)!=1 or v.get('Name')!=name or v.get('Mountpoint')!=mount or mount!=task.docker_root+'/volumes/'+name+'/_data'
                or v.get('Labels')!={LABEL:task.task_id} or v.get('Driver')!='local' or v.get('Scope')!='local' or (v.get('Options') or {})!=opts): raise ValueError
            if task.call('container','ls','--all','--quiet','--no-trunc','--filter','volume='+name): raise ValueError
            task.record('storage-volume-remove-intent',name=name)
            if task.call('volume','rm',name)!=name: raise ValueError
            task.record('storage-volume-removed',name=name)
            remaining.remove(name)
            task.inspect_daemon()
            if name in task.call('volume','ls','--format','{{.Name}}').splitlines(): raise ValueError
        task.record('storage-cleaned-journal-retained',journal_volume=storage.journal_name)
        task._operator_completion=None
        return {'status':'cleaned_journal_retained','journal_volume':storage.journal_name}
    except Exception:
        try: task.record('storage-cleanup-held')
        except Exception: pass
        raise Held('Storage cleanup uncertain; preserve remaining resources') from None
