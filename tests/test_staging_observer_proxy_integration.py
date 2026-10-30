"""Opt-in isolated NGINX contract test; never pulls images or publishes ports."""
import base64
import os
from pathlib import Path
import shutil
import re
import sys
import subprocess
import uuid

import pytest

IMAGE = 'engineering-hub-backend-qa:node24-vitest-nginx-20261030-v1'

# Executed only in a disposable networkless container. TLS material and logs
# exist solely in its temporary filesystem and use synthetic markers.
CONTAINER_TEST = r'''
import base64, http.server, pathlib, select, socket, ssl, subprocess, tempfile, threading, time
root = pathlib.Path(tempfile.mkdtemp())
subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
 '-keyout',str(root/'key'),'-out',str(root/'cert'),'-days','1',
 '-subj','/CN=127.0.0.1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
seen=[]
class Handler(http.server.BaseHTTPRequestHandler):
 # Avoid buffered header parsing consuming body bytes before the socket check.
 rbufsize = 0
 def do_GET(self):
  # The proxy closes each upstream connection, so any readable bytes here
  # are request body data, not another request on a keepalive connection.
  readable, _, _ = select.select([self.connection], [], [], 0.2)
  body = self.connection.recv(4096, socket.MSG_PEEK) if readable else b''
  seen.append((self.headers.get_all('Authorization'), self.headers.get('X-Synthetic'),
   self.headers.get('Content-Length'), self.headers.get('Transfer-Encoding'), body))
  self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
 def log_message(self,*args): pass
server=http.server.HTTPServer(('127.0.0.1',18781),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
template=base64.b64decode(TEMPLATE).decode()
for key,value in {'__STAGING_BIND_IP__':'127.0.0.1','__TLS_PORT__':'19443',
 '__CERTIFICATE_CHAIN_PATH__':str(root/'cert'),'__CERTIFICATE_KEY_PATH__':str(root/'key'),
 '__APPROVED_NAS_IP__':'127.0.0.1','__OBSERVER_PORT__':'18781'}.items():
 template=template.replace(key,value)
(root/'observer.conf').write_text(template)
(root/'nginx.conf').write_text('daemon off; pid '+str(root/'pid')+'; error_log /dev/null info; '
 'events { worker_connections 32; } http { client_body_temp_path '+str(root/'body')+
 '; proxy_temp_path '+str(root/'proxy')+'; fastcgi_temp_path '+str(root/'fastcgi')+
 '; uwsgi_temp_path '+str(root/'uwsgi')+'; scgi_temp_path '+str(root/'scgi')+
 '; include '+str(root/'observer.conf')+'; }')
logs=(root/'stderr').open('wb')
process=subprocess.Popen(['nginx','-c',str(root/'nginx.conf')],stdout=logs,stderr=logs)
def request(method,path,headers='',source='127.0.0.1',body=b''):
 raw=(method+' '+path+' HTTP/1.1\r\nHost: localhost\r\n'+headers+'Connection: close\r\n\r\n').encode()+body
 with ssl._create_unverified_context().wrap_socket(socket.create_connection(
  ('127.0.0.1',19443),timeout=3,source_address=(source,0)),server_hostname='127.0.0.1') as connection:
  connection.sendall(raw); output=b''
  while True:
   chunk=connection.recv(65536)
   if not chunk: break
   output+=chunk
  return int(output.split(b' ',2)[1])
try:
 for _ in range(60):
  try: socket.create_connection(('127.0.0.1',19443),timeout=.1).close(); break
  except OSError: time.sleep(.05)
 else: raise AssertionError('nginx did not start')
 bearer='Authorization: Bearer synthetic-bearer-marker\r\n'
 assert request('GET','/v1/runtime',bearer)==200
 assert request('GET','/v1/runtime',bearer+'X-Synthetic: marker\r\nContent-Length: 4\r\n',body=b'test')==200
 assert request('GET','/v1/runtime',bearer+'Transfer-Encoding: chunked\r\n',body=b'0\r\n\r\n')==400
 assert request('GET','/v1/runtime',bearer+'Transfer-Encoding: chunked\r\n',body=b'4\r\ntest\r\n0\r\n\r\n')==400
 assert request('GET','/v1/runtime',bearer+'Authorization: Bearer synthetic-other-marker\r\n')==400
 assert request('GET','/v1/runtime', 'Authorization: Bearer synthetic-other-marker\r\n'+bearer)==400
 for method,path in [('GET','/v1/runtime?synthetic-query-marker'),('GET','/V1/runtime'),
  ('GET','/v1/%72untime'),('POST','/v1/runtime')]:
  assert request(method,path,bearer)==404
 assert request('GET','/v1/runtime','X-Forwarded-For: 127.0.0.1\r\n', '127.0.0.2')==403
 assert request('GET','/synthetic-uri-marker?synthetic-query-marker','Bad Header: x\r\n')==400
 assert seen == [(['Bearer synthetic-bearer-marker'], None, None, None, b'')] * 2
 server.shutdown();server.server_close()
 assert request('GET','/v1/runtime',bearer)==502
finally:
 process.terminate();process.wait(timeout=5);logs.close()
 assert (root/'stderr').read_bytes()==b''
 assert not list(root.glob('*.log'))
print('isolated observer proxy adversarial checks passed')
'''


@pytest.mark.skipif(os.environ.get('EH_TEST_NGINX_PROXY') != '1', reason='explicit opt-in required')
def test_isolated_nginx_observer_proxy():
    docker = shutil.which('docker')
    if docker is None:
        pytest.skip('Docker CLI unavailable')
    try:
        inspect = subprocess.run([docker, 'image', 'inspect', '--format', '{{.Id}}', IMAGE],
                                 capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip('Docker runtime unavailable')
    if inspect.returncode:
        pytest.skip('Docker runtime or cached NGINX image unavailable')
    image_id = inspect.stdout.strip()
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', image_id), 'invalid cached image identity'
    print('NGINX test image:', image_id, flush=True)
    template = Path('deployment/staging-observer/nginx-http.conf').read_bytes()
    script = 'TEMPLATE = ' + repr(base64.b64encode(template).decode()) + '\n' + CONTAINER_TEST
    name = 'eh-observer-proxy-test-' + uuid.uuid4().hex
    try:
        result = subprocess.run([docker, 'run', '--rm', '-i', '--name', name, '--pull=never', '--network', 'none',
            '--memory', '128m', '--cpus', '0.5', '--pids-limit', '32', '--entrypoint', 'python3', image_id, '-'],
            input=script, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert 'adversarial checks passed' in result.stdout
    finally:
        original = sys.exception()
        problem = None
        try:
            removal = subprocess.run([docker, 'rm', '-f', name], capture_output=True, text=True, timeout=10)
            check = subprocess.run([docker, 'container', 'inspect', name], capture_output=True, text=True, timeout=10)
            if removal.returncode and 'No such container' not in removal.stderr and 'No such object' not in removal.stderr:
                problem = 'container removal command failed'
            if check.returncode == 0:
                problem = 'task-owned container still exists after cleanup'
            elif 'No such object' not in check.stderr and 'No such container' not in check.stderr:
                problem = 'could not verify task-owned container absence'
        except (OSError, subprocess.TimeoutExpired):
            problem = 'could not verify task-owned container absence'
        if problem:
            if original is not None:
                original.add_note('Cleanup failure: ' + problem)
            else:
                pytest.fail('Cleanup failure: ' + problem)
