"""Inert NGINX template contract checks, not nginx -t or deployed-route proof."""
from pathlib import Path
import re

import pytest


TEMPLATE = Path(__file__).resolve().parents[1] / 'docker/nginx-executor-https.conf.template'
TEXT = TEMPLATE.read_text(encoding='utf-8')
BODY = '\n'.join(line.split('#', 1)[0] for line in TEXT.splitlines())
UUID = '00000000-0000-4000-8000-000000000001'
ROOT = '/v1/container-pilot/commands'


def admitted(method, uri):
    # These patterns deliberately use the common anchored PCRE/Python subset.
    # Testing the literal template avoids inventing a separate route allowlist.
    patterns = re.findall(r'"~(\^.*\$)"\s+1;', BODY)
    assert len(patterns) == 3
    return any(re.fullmatch(pattern, method + ':' + uri) for pattern in patterns)


@pytest.mark.parametrize('method,path', [('POST', ROOT), ('GET', ROOT + '/' + UUID),
    ('POST', ROOT + '/' + UUID + '/reconcile')])
def test_only_expected_routes_admitted(method, path):
    assert admitted(method, path)


@pytest.mark.parametrize('method,path', [('GET', ROOT), ('HEAD', ROOT + '/' + UUID),
    ('OPTIONS', ROOT), ('PUT', ROOT), ('post', ROOT), ('GET', ROOT + '/bad-uuid'),
    ('POST', ROOT + '/' + UUID), ('GET', ROOT + '/' + UUID + '/reconcile'),
    ('POST', ROOT + '/'), ('POST', ROOT + '?x=1'), ('GET', ROOT + '/' + UUID + '?'),
    ('POST', '/v1/%63ontainer-pilot/commands'), ('POST', '/v1//container-pilot/commands'),
    ('POST', '/v1/other/../container-pilot/commands'), ('GET', '/health'), ('POST', '/mcp'),
    ('GET', ROOT + '/ABCDEF00-0000-4000-8000-000000000001')])
def test_aliases_other_methods_and_query_strings_denied(method, path):
    assert not admitted(method, path)


def test_template_is_inert_explicit_source_and_uds_without_credentials_or_broad_trust():
    placeholders = set(re.findall(r'__[A-Z_]+__', BODY))
    assert placeholders == {'__EXECUTOR_UDS_PATH__', '__STAGING_BIND_IP__', '__EXECUTOR_DNS_NAME__',
        '__CERTIFICATE_CHAIN_PATH__', '__CERTIFICATE_KEY_PATH__', '__APPROVED_NAS_IP__'}
    assert 'map "$request_method:$request_uri" $eh_executor_route' in BODY
    assert 'default 0;' in BODY
    assert 'allow __APPROVED_NAS_IP__;' in BODY and 'deny all;' in BODY
    assert 'server unix:__EXECUTOR_UDS_PATH__;' in BODY
    assert 'proxy_pass http://eh_private_executor;' in BODY
    assert BODY.count('proxy_pass ') == 1
    assert 'proxy_pass_request_headers on;' in BODY
    assert 'proxy_set_header Authorization' not in BODY
    assert 'Bearer ' not in BODY and 'real_ip' not in BODY and 'auth_request' not in BODY
    assert 'proxy_next_upstream off;' in BODY and 'proxy_next_upstream_tries 1;' in BODY
    assert 'proxy_ignore_client_abort on;' in BODY
    assert 'proxy_ignore_headers X-Accel-Redirect X-Accel-Buffering;' in BODY
    assert 'ssl_protocols TLSv1.2 TLSv1.3;' in BODY
    assert 'client_max_body_size 8k;' in BODY and 'client_body_buffer_size 8k;' in BODY
    assert 'proxy_request_buffering on;' in BODY
    assert 'proxy_max_temp_file_size 0;' in BODY
    for directive in ['proxy_connect_timeout 2s;', 'proxy_send_timeout 5s;', 'proxy_read_timeout 130s;',
                      'client_header_timeout 5s;', 'client_body_timeout 2s;', 'send_timeout 10s;',
                      'proxy_buffer_size 4k;', 'proxy_buffers 4 4k;', 'proxy_busy_buffers_size 8k;',
                      'access_log off;', 'proxy_cache off;', 'proxy_store off;']:
        assert directive in BODY
