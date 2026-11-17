"""Inert, loopback-only executor proxy renderer. No files or processes created.

The coordinator's UID cannot traverse the rootless executor's private API
directory. A separately owned root NGINX master drops its worker to the measured
host UID; only that API parent and TLS material enter its sandbox. Rendering
neither approves that UID mapping nor establishes readiness or admission.
"""
import hashlib
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit
from uuid import UUID

from scripts.render_executor_proxy_bundle import HTTP_TEMPLATE, NGINX_DIRECTORY

ROOT = '/var/lib/eh-pilot-volume-test-v1'


def require(value):
    if not value:
        raise ValueError('explicit_loopback_executor_proxy_binding_required')


def safe_path(value):
    require(type(value) is str and value.startswith('/') and len(value) <= 1024
            and re.fullmatch(r'[A-Za-z0-9_./-]+', value) is not None
            and PurePosixPath(value).as_posix() == value
            and '..' not in PurePosixPath(value).parts)
    return value


def render(*, campaign_root, generation_id, executor_origin, api_parent,
           certificate, private_key, worker_uid):
    safe_path(campaign_root)
    campaign = PurePosixPath(campaign_root)
    require(campaign.parent == PurePosixPath(ROOT)
            and re.fullmatch(r'campaign-real-accept-[a-z0-9-]{1,48}', campaign.name))
    require(type(generation_id) is str)
    generation = UUID(generation_id)
    require(str(generation) == generation_id and generation.version == 4)
    fixture = campaign_root + '/hub-pilot-executor-' + generation_id
    require(safe_path(api_parent) == fixture + '/executor-state/api')
    require(type(worker_uid) is int and worker_uid == 210000)
    require(type(executor_origin) is str)
    parsed = urlsplit(executor_origin)
    port = parsed.port
    require(type(port) is int and 1024 <= port <= 65535
            and port not in (8765, 8770, 8771)
            and executor_origin == 'https://127.0.0.1:' + str(port))
    for value in (certificate, private_key):
        safe_path(value)
        require(campaign in PurePosixPath(value).parents)
    require(certificate != private_key)
    stem = 'eh-pilot-loopback-' + generation.hex
    run = '/run/' + stem
    unit_name = stem + '.service'
    bundle = campaign_root + '/loopback-proxy-' + generation_id
    credentials = '/run/credentials/' + unit_name
    replacements = {
        '__EXECUTOR_UDS_PATH__': run + '/uds/executor.sock',
        '__STAGING_BIND_IP__': '127.0.0.1', '__EXECUTOR_DNS_NAME__': '127.0.0.1',
        '__CERTIFICATE_CHAIN_PATH__': credentials + '/server.pem',
        '__CERTIFICATE_KEY_PATH__': credentials + '/server.key',
        '__APPROVED_NAS_IP__': '127.0.0.1',
    }
    http = HTTP_TEMPLATE
    for name, value in replacements.items():
        require(http.count(name) == 1)
        http = http.replace(name, value)
    require(http.count('listen 127.0.0.1:9443 ssl;') == 1)
    http = http.replace('listen 127.0.0.1:9443 ssl;', f'listen 127.0.0.1:{port} ssl;')
    config = ('# INERT loopback TEST proxy; manual independent activation only.\n'
        'user ehproxy ehproxy;\nworker_processes 1;\ndaemon off;\nmaster_process on;\n'
        'error_log stderr notice;\npid ' + run + '/nginx.pid;\n'
        'events { worker_connections 32; }\nhttp {\n'
        + ''.join('    ' + name + ' ' + run + '/' + name + ';\n' for name in (
            'client_body_temp_path', 'proxy_temp_path', 'fastcgi_temp_path',
            'uwsgi_temp_path', 'scgi_temp_path'))
        + ''.join('    ' + line + '\n' for line in http.splitlines()) + '}\n')
    unit = f'''# MANUAL ONLY: no Install section, restart, boot activation or automatic start.
[Unit]
Description=Bounded TEST executor loopback HTTPS proxy {generation_id}
[Service]
Type=exec
User=root
Group=root
Slice=user-10001.slice
UMask=0077
MemoryMax=64M
MemorySwapMax=0
CPUQuota=10%
TasksMax=16
LimitCORE=0
TimeoutStartSec=30
TimeoutStopSec=1200
KillMode=mixed
KillSignal=SIGQUIT
SendSIGKILL=no
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_CHOWN CAP_SETUID CAP_SETGID
AmbientCapabilities=CAP_SETUID
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
PrivateNetwork=no
ProtectProc=invisible
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
SystemCallFilter=@system-service
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET
RuntimeDirectory={stem}
RuntimeDirectoryMode=0711
BindReadOnlyPaths={bundle}/passwd:/etc/passwd
BindReadOnlyPaths={bundle}/group:/etc/group
BindReadOnlyPaths={bundle}/nginx.conf:{run}/config/nginx.conf
BindReadOnlyPaths={NGINX_DIRECTORY}:{run}/bin
BindReadOnlyPaths={api_parent}:{run}/uds
InaccessiblePaths=/var/lib -/run/user -/run/docker.sock -/var/run/docker.sock -/etc/shadow -/etc/gshadow -/etc/ssl/private -/etc/ssh -/mnt -/media -/srv
LoadCredential=server.pem:{certificate}
LoadCredential=server.key:{private_key}
ExecStart=/usr/bin/env -- {run}/bin/nginx -e stderr -p {run}/ -c {run}/config/nginx.conf
StandardOutput=journal
StandardError=journal
'''
    return {'schema': 'engineering-hub.loopback-executor-proxy.v1',
        'state': 'rendered_not_activated', 'executor_origin': executor_origin,
        'executor_fixture_root': fixture, 'api_parent': api_parent,
        'worker_uid': worker_uid, 'unit_name': unit_name, 'bundle_parent': bundle,
        'http': http,
        'files': {'nginx.conf': config, 'proxy.service': unit,
            'passwd': 'root:x:0:0:root:/root:/bin/sh\nehproxy:!:210000:210000:Private executor proxy:/nonexistent:/usr/sbin/nologin\n',
            'group': 'root:x:0:\nehproxy:!:210000:\n'},
        'http_template_sha256': hashlib.sha256(HTTP_TEMPLATE.encode()).hexdigest()}
