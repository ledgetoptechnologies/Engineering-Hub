"""Optional standalone inventory composition; never edits Hub authentication.

Operator-owned schema1 settings example (no implicit owner or agent grant)::

    {"schema_version":1,"enabled":true,"principals":[
      {"kind":"human","identity":"human:1",
       "capabilities":["read","device_write","maintenance_write"]},
      {"kind":"agent","identity":"hermes",
       "capabilities":["read","maintenance_write","probe_write"]}]}

Only the fixed state_root/infrastructure/inventory.sqlite3 is used. Optional
Prometheus settings select one literal IP/port and explicit target mappings for
an authenticated on-demand refresh tool; never a caller URL, scheduler or listener. Missing/disabled
settings cause no database/directory writes unless the application explicitly
supplies its configured owner for built-in defaults. An existing operator file
always takes precedence, including an explicit disable. Existing inventory schemas must
match this standalone implementation; migrations require explicit review.
Operators must not rename live state paths. SQLite's Unix VFS may canonicalize
proc-FD paths; live identity guards fail closed on named-path drift instead of
claiming that journal sidecars remain anchored through renames. These checks
are not protection against hostile root racing within an individual syscall.
"""
from dataclasses import dataclass
import json
import logging
import os
import re
from pathlib import Path
import sqlite3
import stat
import time

from .infrastructure import CAPABILITIES, HUMAN_ONLY, InfrastructurePrincipal, InfrastructureRegistry
from .infrastructure_service import InfrastructureService
from .repository import AGENTS, HubRepository


logger = logging.getLogger(__name__)

MAX_CONFIG_BYTES = 16384
# Existing private Prometheus endpoint, independently observed serving active
# targets on 2026-10-30 UTC. An explicit operator origin always overrides it.
DEFAULT_PROMETHEUS_ADDRESS = '192.168.50.80'
DEFAULT_PROMETHEUS_PORT = 30104


def _need(value):
    if not value:
        raise ValueError('Invalid optional infrastructure configuration')


def _identity(info):
    return info.st_dev,info.st_ino


def _directory(path):
    """Open every existing ancestor without following symlinks."""
    _need(path.is_absolute() and '..' not in path.parts)
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _unique(pairs):
    result={}
    for key,value in pairs:
        _need(key not in result)
        result[key]=value
    return result


def _settings(path):
    path=Path(path).absolute()
    if os.name=='posix':
        parent=_directory(path.parent)
        try:fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
        finally:os.close(parent)
    else:
        _need(not path.is_symlink())
        fd=os.open(path,os.O_RDONLY)
    try:
        info=os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=MAX_CONFIG_BYTES)
        if os.name=='posix':
            _need(info.st_uid in (0,os.getuid()) and not stat.S_IMODE(info.st_mode)&0o022)
        raw=os.read(fd,MAX_CONFIG_BYTES+1)
        _need(len(raw)<=MAX_CONFIG_BYTES and len(raw)==info.st_size)
        after=os.fstat(fd)
        _need((after.st_size,after.st_mtime_ns,after.st_ctime_ns)==
              (info.st_size,info.st_mtime_ns,info.st_ctime_ns))
        try:current=path.stat(follow_symlinks=False)
        except FileNotFoundError:raise ValueError('Configuration changed during read') from None
        _need(_identity(current)==_identity(info))
    finally:os.close(fd)
    value=json.loads(raw,object_pairs_hook=_unique,
                     parse_constant=lambda value:(_ for _ in ()).throw(ValueError()))
    required={'schema_version','enabled','principals'}
    _need(type(value) is dict and required<=set(value)<=required|{'prometheus'})
    _need(type(value['schema_version']) is int and value['schema_version']==1 and type(value['enabled']) is bool)
    _need(type(value['principals']) is list and len(value['principals'])<=128)
    grants={}
    for item in value['principals']:
        _need(type(item) is dict and set(item)=={'kind','identity','capabilities'})
        _need(type(item['kind']) is str and item['kind'] in ('human','agent') and type(item['identity']) is str)
        HubRepository._actor(item['identity'])
        _need((item['identity'] in AGENTS)==(item['kind']=='agent'))
        actions=item['capabilities']
        _need(type(actions) is list and len(actions)<=len(CAPABILITIES) and all(type(action) is str for action in actions))
        _need(len(set(actions))==len(actions) and set(actions)<=CAPABILITIES)
        _need(not (set(actions)-{'read'}) or 'read' in actions)
        _need(item['kind']=='human' or not set(actions)&HUMAN_ONLY)
        principal=InfrastructurePrincipal(item['kind'],item['identity'])
        _need(principal not in grants)
        grants[principal]=frozenset(actions)
    prometheus=None
    if 'prometheus' in value:
        from .infrastructure_prometheus import PrometheusOrigin, TargetMapping, MAX_TARGETS
        source=value['prometheus']
        _need(type(source) is dict and {'address','port'} <= set(source) <= {'address','port','mappings'})
        origin=PrometheusOrigin(source['address'],source['port'])
        rows=source.get('mappings', [])
        _need(type(rows) is list and len(rows)<=MAX_TARGETS)
        mappings=[]
        for row in rows:
            keys={'job','instance','device_id','probe_id'}
            _need(type(row) is dict and keys<=set(row)<=keys|{'source_label','exporter'})
            mappings.append(TargetMapping(**row))
        _need(len({(item.job,item.instance) for item in mappings})==len(mappings))
        _need(len({(item.device_id,item.probe_id) for item in mappings})==len(mappings))
        _need(all(sum(other.device_id==item.device_id for other in mappings)<=16 for item in mappings))
        prometheus=(origin,tuple(mappings))
    return value['enabled'],grants,prometheus


def _schema(db):
    return {row[0]:(row[1],row[2]) for row in db.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def _owned_schema():
    registry=InfrastructureRegistry(sqlite3.connect(':memory:'))
    try:return _schema(registry.db)
    finally:registry.close()


def _file_info(info):
    # Owner-only, not exactly 0600: NFSv4-ACL datasets (e.g. TrueNAS) can
    # report an owner-only file as 0700 even after fchmod(0600). Execute on a
    # data file grants nothing; any group/other bit still refuses.
    mode=stat.S_IMODE(info.st_mode)
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.getuid()
          and not mode&0o077 and mode&0o600==0o600)


_SIDECARS=('inventory.sqlite3-wal','inventory.sqlite3-shm','inventory.sqlite3-journal')
_PRESERVED='inventory.sqlite3.empty-'
MAX_PRESERVED=3


def _preserve_empty(root_fd,name,fd):
    """Move a zero-byte, sidecar-free, owner-only leftover aside; never open it.

    A first start that stopped before writing its schema leaves exactly this.
    Its inode is kept under a new name (nothing is written, truncated or
    deleted) so the ordinary first-start path can create a fresh database.
    If anything about it changes while moving, it is put back and startup
    fails closed. Returns False for any non-empty file.
    """
    info=os.fstat(fd)
    if info.st_size!=0:
        return False
    _file_info(info)
    _need(sum(item.startswith(_PRESERVED) for item in os.listdir(root_fd))<MAX_PRESERVED)
    target=_PRESERVED+str(time.time_ns())
    try:os.stat(target,dir_fd=root_fd,follow_symlinks=False)
    except FileNotFoundError:pass
    else:raise ValueError('Preserved name collision')
    _need(_identity(os.stat(name,dir_fd=root_fd,follow_symlinks=False))==_identity(info))
    os.rename(name,target,src_dir_fd=root_fd,dst_dir_fd=root_fd)
    os.fsync(root_fd)
    moved=os.stat(target,dir_fd=root_fd,follow_symlinks=False)
    if _identity(moved)!=_identity(info) or moved.st_size!=0 or os.fstat(fd).st_size!=0:
        try:os.stat(name,dir_fd=root_fd,follow_symlinks=False)
        except FileNotFoundError:
            os.rename(target,name,src_dir_fd=root_fd,dst_dir_fd=root_fd);os.fsync(root_fd)
        raise ValueError('Leftover changed while preserving it')
    return True


@dataclass
class InfrastructureModule:
    service: object = None
    collector: object = None
    metrics: object = None
    prometheus_origin: object = None
    state: str = 'disabled'
    _directory_fd: object = None
    _registry: object = None
    _closed: bool = False

    def close(self):
        if self._closed:
            return
        self._closed=True
        self.collector=None
        self.metrics=None
        self.prometheus_origin=None
        service,self.service=self.service,None
        registry,self._registry=self._registry,None
        try:
            if registry is not None:registry.close()
            elif service is not None:service.registry.close()
        finally:
            if self._directory_fd is not None:
                os.close(self._directory_fd);self._directory_fd=None


def compose_infrastructure(path,*,state_root,on_change=None,default_owner=None):
    """Load optional settings; closed failures expose no paths or raw errors."""
    module=InfrastructureModule()
    if path is None:
        logger.warning('Infrastructure startup: config_not_selected')
        return module
    used_default = False
    try:
        enabled,grants,prometheus=_settings(path)
    except FileNotFoundError:
        if default_owner is None:
            logger.warning('Infrastructure startup: config_missing_no_default_owner')
            return module
        # Explicit app-owned defaults remove manual settings-file setup. An
        # existing file, including enabled:false, always retains precedence.
        if (type(default_owner) is not str or re.fullmatch(
                r'human:(?:[1-9][0-9]{0,19}|cf:[0-9a-f]{64})', default_owner) is None):
            module.state='invalid'
            logger.warning('Infrastructure startup: invalid_default_owner')
            return module
        enabled, prometheus = True, None
        used_default = True
        grants = {InfrastructurePrincipal('human', default_owner): CAPABILITIES}
        grants.update({InfrastructurePrincipal('agent', agent): frozenset({'read'}) for agent in AGENTS})
    except Exception:
        module.state='invalid'
        logger.warning('Infrastructure startup: config_unreadable_or_invalid')
        return module
    if not enabled:
        logger.warning('Infrastructure startup: explicitly_disabled')
        return module
    db=None;root_fd=None;file_fd=None
    stage='state_directory'
    try:
        _need(os.name=='posix')
        import fcntl
        selected=Path(state_root).absolute()
        parent=_directory(selected)
        try:
            stage='state_directory_create'
            try:
                os.mkdir('infrastructure',0o700,dir_fd=parent)
            except FileExistsError:
                pass
            else:
                stage='state_directory_chmod'
                os.chmod('infrastructure',0o700,dir_fd=parent,follow_symlinks=False)
            stage='state_directory_open'
            root_fd=os.open('infrastructure',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            selected_info=os.fstat(parent)
        finally:os.close(parent)
        info=os.fstat(root_fd)
        stage='state_directory_owner'
        _need(info.st_uid==os.getuid())
        stage='state_directory_mode'
        _need(stat.S_IMODE(info.st_mode)==0o700)
        stage='state_lock'
        fcntl.flock(root_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stage='state_directory_identity'
        directory=selected/'infrastructure'
        _need(_identity(directory.stat(follow_symlinks=False))==_identity(info))
        stage='database_admission'
        for name in _SIDECARS:
            try:os.stat(name,dir_fd=root_fd,follow_symlinks=False)
            except FileNotFoundError:continue
            raise ValueError('Inventory recovery requires review')
        name='inventory.sqlite3'
        preserved_empty=False
        try:
            file_fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=root_fd)
            existing=True
        except FileNotFoundError:
            existing=False
        if existing:
            stage='database_empty_preserve'
            if _preserve_empty(root_fd,name,file_fd):
                os.close(file_fd);file_fd=None
                existing=False;preserved_empty=True
            stage='database_admission'
        if not existing:
            file_fd=os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root_fd)
            os.fchmod(file_fd,0o600);os.fsync(file_fd);os.fsync(root_fd)
        stage='database_file'
        before=os.fstat(file_fd);_file_info(before)
        captured_fd=root_fd
        def directory_identity(value):
            return (value.st_dev,value.st_ino,value.st_uid,value.st_gid,
                    stat.S_IMODE(value.st_mode))
        selected_capture=directory_identity(selected_info)
        directory_capture=directory_identity(info)
        file_capture=(before.st_dev,before.st_ino,before.st_uid,before.st_gid,
                      stat.S_IMODE(before.st_mode),before.st_nlink)
        def admission():
            if module._closed or module.state=='invalid':
                return False
            named_parent=None;named_root=None
            try:
                named_parent=_directory(selected)
                _need(directory_identity(os.fstat(named_parent))==selected_capture)
                named_root=os.open('infrastructure',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=named_parent)
                _need(directory_identity(os.fstat(named_root))==directory_capture)
                _need(directory_identity(os.fstat(captured_fd))==directory_capture)
                named_file=os.stat(name,dir_fd=named_root,follow_symlinks=False)
                _file_info(named_file)
                _need((named_file.st_dev,named_file.st_ino,named_file.st_uid,named_file.st_gid,
                       stat.S_IMODE(named_file.st_mode),named_file.st_nlink)==file_capture)
                return True
            except Exception:
                module.state='invalid';module.service=None
                return False
            finally:
                if named_root is not None:os.close(named_root)
                if named_parent is not None:os.close(named_parent)
        # The held FD supplies a stable object reference, not a promise about
        # SQLite VFS sidecar paths. Named identity gates are checked at every
        # registry read/write boundary and permanently veto path replacement.
        sqlite_location=Path('/proc/self/fd')/str(root_fd)/name
        stage='database_schema'
        if existing:
            # immutable prevents SQLite from creating WAL/SHM files while
            # inspecting an unknown database. Sidecars above are never adopted.
            readonly=sqlite3.connect(sqlite_location.as_uri()+'?mode=ro&immutable=1',uri=True)
            try:_need(_schema(readonly)==_owned_schema())
            finally:readonly.close()
        _need(_identity(os.stat(name,dir_fd=root_fd,follow_symlinks=False))==_identity(before))
        _need(admission())
        stage='database_open'
        for sidecar in _SIDECARS:
            # Re-checked just before SQLite may replay one; never adopted.
            try:os.stat(sidecar,dir_fd=root_fd,follow_symlinks=False)
            except FileNotFoundError:continue
            raise ValueError('Inventory recovery requires review')
        db=sqlite3.connect(sqlite_location.as_uri()+'?mode=rw',uri=True,check_same_thread=False)
        registry=InfrastructureRegistry(db,capabilities=grants,admission_guard=admission)
        _need(_schema(db)==_owned_schema())
        _need(_identity(os.stat(name,dir_fd=root_fd,follow_symlinks=False))==_identity(before))
        stage='service_setup'
        service=InfrastructureService(registry,on_change=on_change)
        if prometheus is not None:
            from .infrastructure_prometheus import PrometheusCollector
            from .infrastructure_metrics import MetricsReader
            module.prometheus_origin=prometheus[0]
            # Explicit mappings remain available for the legacy ledger refresh;
            # read-only discovery needs only the fixed origin.
            if prometheus[1]:
                module.collector=PrometheusCollector(service,*prometheus)
                module.metrics=MetricsReader(service,*prometheus)
        elif used_default:
            from .infrastructure_prometheus import PrometheusOrigin
            module.prometheus_origin=PrometheusOrigin(DEFAULT_PROMETHEUS_ADDRESS,DEFAULT_PROMETHEUS_PORT)
        module.service,module.state,module._directory_fd=service,'ready',root_fd
        module._registry=registry
        if preserved_empty:
            logger.warning('Infrastructure startup: empty_database_preserved')
        logger.info('Infrastructure startup: ready')
        root_fd=None;db=None
        return module
    except Exception:
        module.state='invalid'
        logger.warning('Infrastructure startup: %s_failed', stage)
        return module
    finally:
        if db is not None:db.close()
        if file_fd is not None:os.close(file_fd)
        if root_fd is not None:os.close(root_fd)
