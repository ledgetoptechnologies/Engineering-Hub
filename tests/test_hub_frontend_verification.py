"""Real JWT verification through composed front doors with local mocked JWKS."""
import time
from dataclasses import replace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.cloudflare_access import CloudflareAccessVerifier, CloudflareIdentity
from engineering_hub import cloudflare_access as access
from engineering_hub.auth import AuthConfig
from engineering_hub.frontends import HubFrontends

BASE = "/api/hub/v1"
ORIGIN = "https://hub.test"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = {**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
              "kid": "frontdoor", "alg": "RS256", "use": "sig"}
    fetches, checks = [], []

    def serve_keys(request):
        fetches.append(str(request.url))
        return httpx.Response(200, json={"keys": [public]})

    class Verifier(CloudflareAccessVerifier):
        async def verify(self, token):
            checks.append(self)
            return await super().verify(token)

    def factory(config):
        return Verifier(config, transport=httpx.MockTransport(serve_keys))

    monkeypatch.setattr("engineering_hub.ingress.CloudflareAccessVerifier", factory)
    monkeypatch.setattr("engineering_hub.cloudflare_access.CloudflareAccessVerifier", factory)
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("private dashboard")
    (ui / "bundle.js").write_text("private bundle")
    config = AuthConfig(ORIGIN, human_auth_mode="cloudflare_access",
                        cloudflare_issuer="https://hub-test.cloudflareaccess.com",
                        cloudflare_audience="hub-audience", cloudflare_allowed_emails=("beau@example.com",))
    core = create_app(Settings(tmp_path / "hub.db", AuthSettings("h", "c"), config, ui))
    pair = HubFrontends(core)
    now = int(time.time())
    claims = {"iss": config.cloudflare_issuer, "aud": [config.cloudflare_audience],
              "iat": now - 1, "exp": now + 600, "sub": "beau-subject", "email": "beau@example.com", "type": "app"}
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "frontdoor"})
    forged = jwt.encode(claims, "not-a-real-signature-key" * 2, algorithm="HS256", headers={"kid": "frontdoor"})
    return pair, config, token, forged, fetches, checks


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                             follow_redirects=False)


@pytest.mark.asyncio
async def test_browser_one_verification_each_request_and_only_one_jwks_fetch(rig):
    pair, config, token, forged, fetches, checks = rig
    signed = {"CF-Access-Jwt-Assertion": token}
    async with pair.lifespan(), client(pair.browser) as browser:
        assert (await browser.get(BASE + "/auth/login", headers=signed)).status_code == 303
        assert len(checks) == 1 and len(fetches) == 1
        me = (await browser.get(BASE + "/auth/me", headers=signed)).json()
        assert me["authenticated"]
        assert (await browser.get(BASE + "/overview", headers=signed)).status_code == 200
        assert (await browser.get("/", headers=signed)).text == "private dashboard"
        assert (await browser.get("/bundle.js", headers=signed)).text == "private bundle"
        assert len(checks) == 5 and len(fetches) == 1
        assert len({id(checker) for checker in checks}) == 1  # Outer verifier only.
        assert (await browser.get("/bundle.js", headers={"CF-Access-Jwt-Assertion": forged})).status_code == 401
        assert len(checks) == 6
        payload = {"project": "treedar", "title": "task", "client_request_id": "proof-test"}
        assert (await browser.post(BASE + "/tasks", headers=signed, json=payload)).status_code == 403
        assert (await browser.post(BASE + "/tasks", headers={**signed, "Origin": ORIGIN,
                                   "X-Hub-CSRF": me["csrf_token"]}, json=payload)).status_code == 201


@pytest.mark.asyncio
async def test_combined_core_still_verifies_its_own_requests_and_rejects_cookie_with_bad_jwt(rig):
    pair, config, token, forged, fetches, checks = rig
    async with pair.lifespan(), client(pair.core) as browser:
        signed = {"CF-Access-Jwt-Assertion": token}
        assert (await browser.get(BASE + "/auth/login", headers=signed)).status_code == 303
        assert (await browser.get(BASE + "/overview", headers=signed)).status_code == 200
        assert len(checks) == 2 and len(fetches) == 1
        assert (await browser.get(BASE + "/overview", headers={"CF-Access-Jwt-Assertion": forged})).status_code == 401
        assert (await browser.get(BASE + "/overview", headers={"CF-Access-Authenticated-User-Email": "beau@example.com",
                                                               "X-Cloudflare-Verified": "true"})).status_code == 401
        assert (await browser.get(BASE + "/overview")).status_code == 401
        # Combined legacy static behavior is deliberately unchanged; separated
        # browser mode, not this private core, provides the asset boundary.
        assert (await browser.get("/")).status_code == 200


class ScopeOverlay:
    def __init__(self, app, overlay):
        self.app, self.overlay = app, overlay

    async def __call__(self, scope, receive, send):
        self.overlay(scope)
        await self.app(scope, receive, send)


@pytest.mark.asyncio
@pytest.mark.parametrize("fake", [True, "verified", (object(), None, None, b"digest")])
async def test_forged_scope_proof_and_identity_state_never_skip_verification(rig, fake):
    pair, config, token, forged, fetches, checks = rig

    def inject(scope):
        scope[access._VERIFIED_ACCESS_KEY] = fake
        scope.setdefault("state", {})["cloudflare_identity"] = CloudflareIdentity(
            "beau-subject", "beau@example.com", "human:cf:" + "a" * 64, int(time.time()) + 600)

    async with pair.lifespan(), client(pair.core) as login:
        assert (await login.get(BASE + "/auth/login", headers={"CF-Access-Jwt-Assertion": token})).status_code == 303
        async with client(ScopeOverlay(pair.core, inject)) as browser:
            browser.cookies.update(login.cookies)
            before = len(checks)
            assert (await browser.get(BASE + "/overview", headers={"CF-Access-Jwt-Assertion": forged})).status_code == 401
            assert len(checks) == before + 1
            assert (await browser.get(BASE + "/overview")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["audience", "issuer", "email_allowlist", "changed_assertion", "duplicate_assertion"])
async def test_wrong_configuration_or_assertion_cannot_reuse_genuine_inprocess_proof(rig, variant):
    pair, config, token, forged, fetches, checks = rig
    # Obtain the exact signed identity, not an email-header surrogate.
    identity = await pair.browser.ingress.checker.verify(token)
    trusted_config = config.cloudflare_config
    if variant == "audience":
        trusted_config = replace(trusted_config, audience="ops-audience")
    elif variant == "issuer":
        trusted_config = replace(trusted_config, issuer="https://different.cloudflareaccess.com")
    elif variant == "email_allowlist":
        trusted_config = replace(trusted_config, allowed_emails=("other@example.com",))

    def inject(scope):
        original_headers = scope["headers"]
        # Mimic proof from another trusted adapter/configuration. The core may
        # consume it only when config and the actual current assertion match.
        scope["headers"] = [(b"cf-access-jwt-assertion", token.encode())]
        access._record_verified_access(scope, trusted_config, identity)
        scope["headers"] = original_headers

    async with pair.lifespan(), client(pair.core) as login:
        assert (await login.get(BASE + "/auth/login", headers={"CF-Access-Jwt-Assertion": token})).status_code == 303
        async with client(ScopeOverlay(pair.core, inject)) as browser:
            browser.cookies.update(login.cookies)
            if variant in {"audience", "issuer", "email_allowlist"}:
                before = len(checks)
                valid = await browser.get(BASE + "/overview", headers={"CF-Access-Jwt-Assertion": token})
                assert valid.status_code == 200
                assert len(checks) == before + 1  # Wrong-config proof cannot skip even a valid JWT.
            before = len(checks)
            headers = [("CF-Access-Jwt-Assertion", forged)]
            if variant == "duplicate_assertion":
                headers = [("CF-Access-Jwt-Assertion", token)] * 2
            response = await browser.get(BASE + "/overview", headers=headers)
            assert response.status_code == 401
            assert len(checks) == before + (0 if variant == "duplicate_assertion" else 1)
