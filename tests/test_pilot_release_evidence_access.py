from engineering_hub.auth import AuthConfig
from engineering_hub.pilot_release_evidence import release_evidence_owner_allowed
from engineering_hub.private_gateway import browser_route_allowed


_EVIDENCE_PATH = '/api/hub/v1/container-pilot/runs/00000000-0000-4000-8000-000000000001/release-evidence'


def test_release_evidence_cloudflare_reader_must_match_configured_owner():
    config = AuthConfig(
        'https://hub.invalid',
        human_auth_mode='cloudflare_access',
        cloudflare_issuer='https://team.cloudflareaccess.com',
        cloudflare_audience='hub',
        cloudflare_allowed_emails=('owner@example.com', 'reviewer@example.com'),
    )
    owner = 'human:cf:owner'
    reviewer = 'human:cf:reviewer'

    assert release_evidence_owner_allowed(owner, config, 'human:cf:owner') is True
    assert release_evidence_owner_allowed(reviewer, config, 'human:cf:owner') is False
    assert release_evidence_owner_allowed(owner, config, None) is False


def test_release_evidence_github_reader_must_match_configured_owner():
    config = AuthConfig('https://hub.invalid', beau_github_id=42)

    assert release_evidence_owner_allowed('human:42', config, None) is True
    assert release_evidence_owner_allowed('human:43', config, None) is False
    assert release_evidence_owner_allowed('human:42', AuthConfig('https://hub.invalid'), None) is False


def test_release_evidence_rejects_non_actor_principal_shapes():
    config = AuthConfig('https://hub.invalid', beau_github_id=42)

    assert release_evidence_owner_allowed({'github_id': 42}, config, None) is False
    assert release_evidence_owner_allowed(None, config, None) is False


def test_release_evidence_browser_gateway_allows_only_exact_read_route():
    assert browser_route_allowed('GET', _EVIDENCE_PATH) is True
    assert browser_route_allowed('HEAD', _EVIDENCE_PATH) is True
    for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        assert browser_route_allowed(method, _EVIDENCE_PATH) is False
    for path in (
        _EVIDENCE_PATH + '/recover',
        _EVIDENCE_PATH.replace('/00000000-0000-4000-8000-000000000001/', '/not-a-uuid/'),
    ):
        assert browser_route_allowed('GET', path) is False
