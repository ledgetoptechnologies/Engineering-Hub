from pathlib import Path


def test_proxy_example_is_inert_and_narrow():
    source = Path('deployment/staging-observer/nginx-http.conf').read_text()
    assert '"~^GET:/v1/runtime$" 1;' in source
    assert 'default 0;' in source and 'if ($eh_observer_allowed = 0) { return 404; }' in source
    assert 'allow __APPROVED_NAS_IP__;' in source and 'deny all;' in source
    assert 'proxy_pass http://127.0.0.1:__OBSERVER_PORT__;' in source
    assert 'proxy_pass_request_headers off;' in source and 'proxy_pass_request_body off;' in source
    assert 'proxy_set_header Authorization $http_authorization;' in source
    assert 'proxy_next_upstream off;' in source and 'access_log off;' in source
    assert 'ssl_certificate_key __CERTIFICATE_KEY_PATH__;' in source
