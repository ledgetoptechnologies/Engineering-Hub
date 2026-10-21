"""Pure fixed-boundary NGINX bundle renderer, not installer or activation.
All paths and identities are proposals until independent live preflight.
This function never reads files, keys, sockets, credentials or the host state.
"""
import hashlib
import json
from pathlib import PurePosixPath
from uuid import UUID

TLS_ROOT = '/var/lib/eh-staging-https-20261021-v1'
TEST_ROOT = '/var/lib/eh-pilot-volume-test-v1'
NGINX_DIRECTORY = TLS_ROOT + '/nginx-runtime-v1/extracted/usr/sbin'
NGINX_SHA256 = 'a24def283d640db0bcad3c81b0630a3444b3079f6c50c956c9bd39330eaca544'
SERVER_CERT_SHA256 = 'fbe0f4980ad4ef24aa1e87284a5fcf094488bca853f66deab2920a1653d6a282'
HISTORICAL_GENERATIONS = {
    '0afbc5d9-55ab-4f02-bd00-f7c490988f11',
    '102ca430-4aec-435c-b6b5-30446cc39450',
    '5550ee4d-2ce0-4ed3-8aca-d7dcca8bcdbf',
}
HTTP_TEMPLATE = "# INERT TEMPLATE: include only in a dedicated nginx http {} context after\n# independent operator approval and replacement of every __PLACEHOLDER__.\n# No certificate, credential, socket permission or service is provisioned here.\n\n# Match the ORIGINAL URI, including any arguments, not nginx's normalized $uri.\n# These deliberately case-sensitive regexes admit exactly three wire routes.\nmap \"$request_method:$request_uri\" $eh_executor_route {\n    default 0;\n    \"~^POST:/v1/container-pilot/commands$\" 1;\n    \"~^GET:/v1/container-pilot/commands/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\" 1;\n    \"~^POST:/v1/container-pilot/commands/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/reconcile$\" 1;\n}\n\nupstream eh_private_executor {\n    server unix:__EXECUTOR_UDS_PATH__;\n}\n\nserver {\n    listen __STAGING_BIND_IP__:9443 ssl;\n    server_name __EXECUTOR_DNS_NAME__;\n    ssl_certificate __CERTIFICATE_CHAIN_PATH__;\n    ssl_certificate_key __CERTIFICATE_KEY_PATH__;\n    ssl_protocols TLSv1.2 TLSv1.3;\n    ssl_session_tickets off;\n\n    # Actual TCP peer only. Never trust X-Forwarded-For/CF-* as source identity.\n    allow __APPROVED_NAS_IP__;\n    deny all;\n    access_log off;\n    server_tokens off;\n    client_header_timeout 5s;\n    client_body_timeout 2s;\n    client_max_body_size 8k;\n    client_body_buffer_size 8k;\n    large_client_header_buffers 2 8k;\n    keepalive_timeout 5s;\n    send_timeout 10s;\n\n    location / {\n        if ($eh_executor_route = 0) { return 404; }\n        if ($http_transfer_encoding != \"\") { return 400; }\n        proxy_pass http://eh_private_executor;\n        proxy_http_version 1.1;\n        proxy_set_header Host localhost;\n        proxy_set_header Connection \"\";\n        # Authorization remains caller-supplied; nginx may itself reject\n        # duplicates before proxying. Never configure/inject a bearer here.\n        proxy_pass_request_headers on;\n        proxy_pass_request_body on;\n        proxy_request_buffering on;\n        proxy_buffering on;\n        proxy_buffer_size 4k;\n        proxy_buffers 4 4k;\n        proxy_busy_buffers_size 8k;\n        proxy_max_temp_file_size 0;\n        proxy_cache off;\n        proxy_store off;\n        proxy_redirect off;\n        proxy_intercept_errors off;\n        proxy_ignore_headers X-Accel-Redirect X-Accel-Buffering;\n        proxy_next_upstream off;\n        proxy_next_upstream_tries 1;\n        proxy_connect_timeout 2s;\n        proxy_send_timeout 5s;\n        proxy_read_timeout 130s;\n        proxy_ignore_client_abort on;\n    }\n}\n"
PASSWD = ('root:x:0:0:root:/root:/bin/sh\n'
          'ehproxy:!:210000:210000:Engineering Hub private executor proxy:/nonexistent:/usr/sbin/nologin\n')
GROUP = 'root:x:0:\nehproxy:!:210000:\n'


def require(value):
    if not value:
        raise ValueError('Explicit fresh fixed-boundary proxy inputs required')


def public_digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def plain(value):
    require(type(value) is str and value
            and all(32 < ord(c) < 127 for c in value)
            and not any(c in value for c in ('\\', '"', "'", ';', '$', '%', '#')))
    return value


def render_proxy_bundle(*, generation_id, api_parent, socket_leaf,
                        staging_bind_ip, approved_nas_ip, worker_uid):
    """Render a deployable draft; shape validation never approves host readiness.

    A NEW generation is required. These deliberately narrow inputs cannot name
    an arbitrary upstream, account, certificate, listener or access policy.
    """
    plain(generation_id)
    parsed = UUID(generation_id)
    require(str(parsed) == generation_id and parsed.version == 4
            and generation_id not in HISTORICAL_GENERATIONS)
    plain(api_parent)
    expected_parent = (TEST_ROOT + '/campaigns/hub-pilot-cli-' + generation_id
                       + '/executor-state/api')
    require(api_parent == expected_parent and str(PurePosixPath(api_parent)) == api_parent
            and '..' not in PurePosixPath(api_parent).parts)
    require(socket_leaf == 'executor.sock' and type(socket_leaf) is str
            and staging_bind_ip == '192.168.50.90' and type(staging_bind_ip) is str
            and approved_nas_ip == '192.168.50.80' and type(approved_nas_ip) is str
            and type(worker_uid) is int and worker_uid == 210000)

    stem = 'eh-executor-proxy-' + parsed.hex
    unit_name = stem + '.service'
    run = '/run/' + stem
    bundle = TLS_ROOT + '/proxy-' + generation_id
    credentials = '/run/credentials/' + unit_name
    replacements = {
        '__EXECUTOR_UDS_PATH__': run + '/uds/' + socket_leaf,
        '__STAGING_BIND_IP__': staging_bind_ip,
        '__EXECUTOR_DNS_NAME__': staging_bind_ip,
        '__CERTIFICATE_CHAIN_PATH__': credentials + '/server.pem',
        '__CERTIFICATE_KEY_PATH__': credentials + '/server.key',
        '__APPROVED_NAS_IP__': approved_nas_ip,
    }
    http = HTTP_TEMPLATE
    for name, value in replacements.items():
        require(http.count(name) == 1)
        http = http.replace(name, value)
    require(not any(name in http for name in replacements))
    config = (
        '# SOURCE DRAFT: requires explicit independent activation.\n'
        'user ehproxy ehproxy;\nworker_processes 1;\ndaemon off;\nmaster_process on;\n'
        'error_log stderr notice;\n'
        + 'pid ' + run + '/nginx.pid;\nlock_file ' + run + '/nginx.lock;\n'
        'events { worker_connections 32; }\n'
        'http {\n'
        + ''.join('    ' + row + '\n' for row in (
            'client_body_temp_path ' + run + '/client-body;',
            'proxy_temp_path ' + run + '/proxy;',
            'fastcgi_temp_path ' + run + '/fastcgi;',
            'uwsgi_temp_path ' + run + '/uwsgi;',
            'scgi_temp_path ' + run + '/scgi;'))
        + ''.join('    ' + line + '\n' for line in http.splitlines())
        + '}\n'
    )
    unit = f"""# MANUAL DRAFT ONLY: no [Install], autostart, restart or timeout kill.
# Type=exec is NOT readiness. Prove TLS/ACL/UDS/identity before admitting work.
[Unit]
Description=Engineering Hub bounded-generation executor HTTPS proxy {generation_id}

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
LoadCredential=server.pem:{TLS_ROOT}/server.pem
LoadCredential=server.key:{TLS_ROOT}/server.key
ExecStart=/usr/bin/env -- {run}/bin/nginx -e stderr -p {run}/ -c {run}/config/nginx.conf
StandardOutput=journal
StandardError=journal
"""
    notes = f"""# Manual executor proxy draft: not activated

Generation: {generation_id}. Bundle host parent: {bundle}.
Unit name: {unit_name}. This renderer does not create any of those paths.

The only forwarding listener is {staging_bind_ip}:9443. The ACL admits only
the actual TCP peer {approved_nas_ip}; it does not trust forwarding headers.
The TLS port is not a firewall or mutual-TLS boundary. Other peers may negotiate
TLS but cannot forward an allowed API route. No NAS connection is proven here.

## Mandatory independent preflight before any manual start

- Independently approve this exact fresh generation, manifest, executor source
  and config. Prior stopped/held generation receipts are not reusable authority.
- Verify boot, daemon, launcher/consumer PID-start/pidfd identities, successful
  direct receipt, remaining receipt lifetime and actual API readiness.
- Prove {api_parent} is the enrolled current socket parent (no symlinks),
  UID/GID210000, mode0700; exact executor.sock must be UID/GID210000 mode0600.
  Record stable parent/socket device and inode and actual current namespace
  mapping. Do not chmod/chown existing sockets or broaden account permissions.
- Prove all root-owned binary/config/TLS source ancestors are non-writable by
  untrusted users. Verify the pinned binary and server-certificate hashes and
  the previously authenticated Ubuntu package/dependency provenance. Inspect
  server.key metadata (root0600/single-link); never print private key bytes.
- Verify new root-private bundle files, new absent unit/runtime paths, no host
  account/group collision for210000, bind-port availability, and unchanged
  original16 workloads. Check common1CPU/3.25GiB/zero-swap/256PID limits and
  current headroom for the additional64MiB/100mCPU/16PID proxy.
- Manual materialization/publication and starting are separate owner-authorized
  effects. Do not install in /etc/systemd/system, enable a unit, create a timer,
  reload an unrelated service or infer authority from canonical rendering.

## Validate the actual composed service, not a namespace impersonation

Run the reviewed actual NGINX configuration test with the SAME sandbox/mounts
and protected credentials before admitting a listener. Recheck rendered -T
locally without exposing credentials. No inherited includes/config or debug logs.
The source /var/lib trees are deliberately hidden: binary/config/API sources
are mapped read-only to explicit /run aliases. Existing v5 validated the binary
alias pattern, NOT this complete API-parent mount. The actual systemd mountinfo,
same source/target identities, read-only aliases, worker traversal/UDS access,
root master, worker UID/GID210000, dropped worker capabilities and credential
open-denial must all be measured before declaring readiness.
Type=exec alone proves none of these. Never fix failure by exposing /var/lib.

Verify TLS CA/IP identity, actual NAS source and negative workstation source,
caller bearer handling, exact routes and no retry through the real route.
A simulated NAS source is not actual NAS proof. Initial missing-bearer inspection
can establish a real upstream401 without a command. Dedicated TEST-bearer
inspection may establish a nonexistent-command404; neither is job approval.
Actual TrueNAS/Hub-container access remains an operator step, not this bundle.

## Lifetime, drain and stop

No timer, RuntimeMaxSec or restart loop is configured. An external reviewed
operator owns the bounded session and receipt lifetime; this is not an always-on
executor installation. After closing new Hub/queue admission, allow accepted
work to finish while retaining proxy and executor. Only then, using the observed
unit InvocationID/MainPID/start/pidfd, explicitly QUIT this OWN proxy master.
The unit uses KillMode=mixed and KillSignal=SIGQUIT, with1200seconds stop grace
and SendSIGKILL=no. No configured control action targets the executor: no Engine
socket, issuer key, database, bearer or helper stop endpoint is supplied.
This unit does not create PID isolation between same-UID processes. Check workers,
connections and own runtime have actually terminated before stopping the owned
executor. A timeout leaves unknown state for manual inspection, not retry or kill.
Do not derive safety from inactivity timeouts or reuse an old unit/process name.

The root master loads the key; workers necessarily inherit TLS key material in
memory. Filesystem credential denial is not key-memory isolation. Trusted root
and same-UID administrators remain outside this isolation claim.

References: https://nginx.org/en/docs/control.html and
https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html .
"""
    files = {'nginx.conf': config.encode(), unit_name: unit.encode(),
             'passwd': PASSWD.encode(), 'group': GROUP.encode(), 'OPERATOR.md': notes.encode()}
    manifest = {
        'schema': 'engineering-hub.executor-proxy-rendered-draft.v1',
        'status': 'rendered_not_activated_not_enrolled',
        'generation_id': generation_id, 'api_parent': api_parent, 'socket_leaf': socket_leaf,
        'staging_bind_ip': staging_bind_ip, 'approved_nas_ip': approved_nas_ip,
        'worker_uid': worker_uid, 'worker_gid': worker_uid, 'unit_name': unit_name,
        'expected_bundle_parent': bundle,
        'template_repository_path': 'docker/nginx-executor-https.conf.template',
        'template_canonical_lf_sha256': public_digest(HTTP_TEMPLATE.encode()),
        'expected_runtime_binary_sha256': 'sha256:' + NGINX_SHA256,
        'expected_server_certificate_sha256': 'sha256:' + SERVER_CERT_SHA256,
        'runtime_identity_status': 'must_be_measured_no_live_claim',
        'files': {name: {'sha256': public_digest(raw), 'size_bytes': len(raw)}
                  for name, raw in files.items()},
        'tls_private_key_contents_included': False, 'bearer_included': False,
        'activation': False, 'host_state_read': False, 'nas_route_verified': False,
        'boot_autostart': False, 'production_approval': False,
    }
    files['bundle-manifest.json'] = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    return files


def main(argv=None):
    """Render public proposed file text to stdout; never materialize or activate."""
    import argparse
    import sys

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            self.print_usage(sys.stderr)
            self.exit(2, 'Explicit fresh fixed-boundary proxy inputs required\n')

    parser = Parser(description='Render a bounded manual proxy draft; no activation.',
                    allow_abbrev=False)
    parser.add_argument('--generation-id', required=True)
    parser.add_argument('--api-parent', required=True)
    args = parser.parse_args(argv)
    try:
        files = render_proxy_bundle(generation_id=args.generation_id,
            api_parent=args.api_parent, socket_leaf='executor.sock',
            staging_bind_ip='192.168.50.90', approved_nas_ip='192.168.50.80',
            worker_uid=210000)
    except (ValueError, TypeError, AttributeError):
        print('Explicit fresh fixed-boundary proxy inputs required', file=sys.stderr)
        return 2
    print(json.dumps({'status': 'rendered_not_activated',
        'files': {name: raw.decode('utf-8') for name, raw in files.items()}},
        sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
