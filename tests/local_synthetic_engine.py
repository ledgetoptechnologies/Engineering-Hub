"""Task-owned synthetic Engine UDS for offline acceptance tests only."""
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import sqlite3
import stat
import threading
from urllib.parse import quote
import uuid

from hub_container_executor.adapter import HostAdapter
from hub_container_executor.runtime_profile import derive_runtime_profile
from tests.test_hub_container_executor_daemon_backend import container_inspect, docker_config


class SyntheticEngine:
    """No daemon socket discovery: caller supplies one fresh task-owned path."""
    def __init__(self, *, socket_path, fake_root, volume, owner, images, archives,
                 manifest, secrets, baseline_db, dynamic_names=False, baseline_uid_gid=None):
        self.path, self.fake_root, self.volume, self.owner = str(socket_path), Path(fake_root), volume, owner
        self.images, self.archives, self.manifest, self.secrets = images, archives, manifest, secrets
        if baseline_uid_gid is not None and (type(baseline_uid_gid) is not tuple or baseline_uid_gid != (10001,10001)):
            raise ValueError('synthetic baseline owner must be explicit')
        self.baseline_db, self.dynamic_names, self.baseline_uid_gid = Path(baseline_db), dynamic_names, baseline_uid_gid
        self.profiles, self.names, self.by_name, self.containers, self.execs = {}, {}, {}, {}, {}
        self.by_ref = {image.runnable_reference:role for role,image in images.items()}
        self.generations = self.baseline_db.parent.parent
        self.server = self.thread = self.identity = None

    def start(self):
        if self.server is not None or os.path.lexists(self.path): raise RuntimeError('synthetic socket already exists')
        engine=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def reply(self,status,value,binary=False):
                raw=value if binary else json.dumps(value).encode()
                self.send_response(status); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                path=self.path
                if path.endswith('/info'):
                    return self.reply(200,{'ID':'fixture-daemon','OSType':'linux','Architecture':'amd64',
                                           'DockerRootDir':str(engine.fake_root)})
                if '/volumes/' in path:
                    return self.reply(200,{'Name':engine.volume.name,'Driver':'local','Scope':'local',
                        'Mountpoint':engine.volume.mountpoint,'Labels':{'engineering-hub.local-test-owner':engine.owner},
                        'Options':dict(engine.volume.options)})
                for ref,role in engine.by_ref.items():
                    base='/v1.41/images/'+quote(ref,safe='')
                    if path==base+'/json': return self.reply(200,docker_config(engine.images[role]))
                    if path==base+'/get': return self.reply(200,engine.archives[role],True)
                for name in engine.names.values():
                    if path=='/v1.41/containers/'+name+'/json': return self.reply(404,{})
                for identity,item in engine.containers.items():
                    if path=='/v1.41/containers/'+identity+'/json':
                        role=item['role']; profile=engine.profiles[role]; image=engine.images['candidate' if role=='candidate' else 'rollback']
                        value=container_inspect(image,profile,engine.generations/profile.state_generation_id,
                                                status=item['status'],container_id=identity)
                        value['Config']['Labels']={'engineering-hub.local-test-owner':engine.owner}
                        value['HostConfig']['PublishAllPorts']=False
                        value['HostConfig']['LogConfig']={'Type':'local','Config':{'max-size':'1m','max-file':'2'}}
                        value['HostConfig']['Mounts'][0].pop('ReadOnly')
                        value['Mounts']=value['Mounts'][:1]; value['Mounts'][0]['Mode']=''
                        value['State'].update(Running=item['status']=='running',Pid=123 if item['status']=='running' else 0,
                            Paused=False,Restarting=False,Dead=False,
                            StartedAt='2026-10-21T00:00:00Z' if item['status']=='running' else '0001-01-01T00:00:00Z')
                        return self.reply(200,value)
                for identity,item in engine.execs.items():
                    if path=='/v1.41/exec/'+identity+'/json':
                        return self.reply(200,{'ID':identity,'Running':False,'ExitCode':1 if item['failed'] else 0})
                self.reply(404,{})
            def do_POST(self):
                path=self.path; length=int(self.headers.get('Content-Length','0'))
                body=json.loads(self.rfile.read(length)) if length else None
                if path.startswith('/v1.41/containers/create?name='):
                    name=path.split('=',1)[1]; role=engine.by_name.get(name)
                    if role is None and engine.dynamic_names:
                        role='candidate' if name.endswith('-candidate') else 'rollback' if name.endswith('-rollback') else None
                        assert role is not None
                        generation=Path(body['HostConfig']['Mounts'][0]['Source']).name
                        engine.profiles[role]=derive_runtime_profile(engine.manifest,HostAdapter._defaults_from_record(engine.images[role]),
                                                                     generation,engine.secrets)
                        engine.names[role]=name; engine.by_name[name]=role
                    assert role is not None
                    assert body['Labels']=={'engineering-hub.local-test-owner':engine.owner}
                    assert body['HostConfig']['LogConfig']=={'Type':'local','Config':{'max-size':'1m','max-file':'2'}}
                    identity={'baseline':'e','candidate':'c','rollback':'d'}[role]*64
                    engine.containers[identity]={'role':role,'status':'created'}
                    return self.reply(201,{'Id':identity,'Warnings':[]})
                for identity,item in list(engine.containers.items()):
                    if path=='/v1.41/containers/'+identity+'/start':
                        if item['role']=='baseline' and not engine.baseline_db.exists():
                            db=sqlite3.connect(engine.baseline_db)
                            try:
                                db.execute('CREATE TABLE synthetic_fixture(v TEXT)')
                                db.execute("INSERT INTO synthetic_fixture VALUES('baseline')"); db.commit()
                            finally: db.close()
                            fd=os.open(engine.baseline_db,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
                            try:
                                info=os.fstat(fd)
                                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise RuntimeError('synthetic baseline changed')
                                if engine.baseline_uid_gid is not None: os.fchown(fd,*engine.baseline_uid_gid)
                                os.fchmod(fd,0o600)
                            finally: os.close(fd)
                        item['status']='running'; self.send_response(204); self.end_headers(); return
                    if path=='/v1.41/containers/'+identity+'/stop?t=5':
                        item['status']='exited'; self.send_response(204); self.end_headers(); return
                    if path=='/v1.41/containers/'+identity+'/exec':
                        cmd=body['Cmd']; exec_id=uuid.uuid4().hex+uuid.uuid4().hex
                        failed=item['role']=='candidate' and cmd[4]=='health_json'
                        engine.execs[exec_id]={'failed':failed,'expectation':cmd[7],
                                               'status':503 if failed else int(cmd[6])}
                        return self.reply(201,{'Id':exec_id})
                for identity,item in engine.execs.items():
                    if path=='/v1.41/exec/'+identity+'/start':
                        raw=json.dumps({'expectation_id':item['expectation'],'ok':not item['failed'],
                                        'status':item['status']}).encode()
                        return self.reply(200,b'\x01\x00\x00\x00'+len(raw).to_bytes(4,'big')+raw,True)
                self.reply(404,{})
            def do_DELETE(self):
                identity=self.path.split('/containers/',1)[1].split('?',1)[0]
                if identity not in engine.containers or engine.containers[identity]['status']!='exited': return self.reply(409,{})
                del engine.containers[identity]; self.send_response(204); self.end_headers()
        try:
            self.server=socketserver.UnixStreamServer(self.path,Handler)
            self.identity=os.lstat(self.path)
            self.thread=threading.Thread(target=self.server.serve_forever)
            self.thread.start()
        except BaseException:
            if self.server is not None:
                self.server.server_close(); self.server=None
            if self.identity is not None:
                current=os.lstat(self.path)
                if (current.st_dev,current.st_ino)==(self.identity.st_dev,self.identity.st_ino): os.unlink(self.path)
            raise

    def close(self):
        if self.server is None: return
        try: self.server.shutdown()
        finally:
            try: self.thread.join(timeout=5)
            finally: self.server.server_close()
        if self.thread.is_alive(): raise RuntimeError('synthetic engine did not drain')
        current=os.lstat(self.path)
        if (current.st_dev,current.st_ino)!=(self.identity.st_dev,self.identity.st_ino):
            raise RuntimeError('synthetic socket changed')
        os.unlink(self.path)
        self.server=None
