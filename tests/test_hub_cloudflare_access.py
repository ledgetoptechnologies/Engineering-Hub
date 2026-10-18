import asyncio
import base64
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes

from engineering_hub.cloudflare_access import (
    AccessDenied, AccessUnavailable, CloudflareAccessConfig, CloudflareAccessVerifier,
    CloudflareIdentity, install_cloudflare_access,
)

ISSUER = "https://engineering-test.cloudflareaccess.com"
AUD = "hub-test-audience"
EMAILS = ("beau@example.com", "beau@other.example.com")


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def jwk(key, kid="first"):
    return {**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
            "kid": kid, "alg": "RS256", "use": "sig"}


def token(key, *, kid="first", changes=None, remove=None, headers=None):
    now = int(time.time())
    claims = dict(iss=ISSUER, aud=[AUD], exp=now + 600, iat=now - 1,
                  sub="human-subject", email=EMAILS[0], type="app")
    claims.update(changes or {})
    if remove:
        claims.pop(remove)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid, **(headers or {})})


def verifier(keys_document, *, config=None, status=200):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=keys_document)

    v = CloudflareAccessVerifier(config or CloudflareAccessConfig(ISSUER, AUD, EMAILS),
                                 transport=httpx.MockTransport(handle))
    return v, calls


@pytest.mark.asyncio
async def test_valid_signed_human_and_exact_fixed_key_url(keys):
    v, calls = verifier({"keys": [jwk(keys[0])]})
    identity = await v.verify(token(keys[0], changes={"email": EMAILS[1].upper()}))
    assert identity.email == EMAILS[1]
    assert identity.subject == "human-subject"
    assert identity.actor.startswith("human:cf:") and len(identity.actor) == 73
    assert str(calls[0].url) == ISSUER + "/cdn-cgi/access/certs"
    assert "authorization" not in calls[0].headers
    await v.verify(token(keys[0]))
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [
    ("iss", "https://evil.cloudflareaccess.com"), ("aud", "other-app"),
    ("aud", [AUD, "other-app"]), ("type", "service"), ("email", "outsider@example.com"),
    ("email", ""), ("email", None), ("sub", ""), ("sub", 123),
    ("exp", 1), ("iat", 2**53 + 1), ("iat", True), ("iat", "123"),
    ("iat", 1.5), ("nbf", "0"), ("nbf", True), ("exp", "9999999999"),
    ("iat", int(time.time()) + 3600), ("nbf", int(time.time()) + 3600),
])
@pytest.mark.asyncio
async def test_denied_signed_claims(keys, field, value):
    v, _ = verifier({"keys": [jwk(keys[0])]})
    with pytest.raises(AccessDenied, match="identity denied"):
        await v.verify(token(keys[0], changes={field: value}))


@pytest.mark.parametrize("field", ["iss", "aud", "exp", "iat", "sub", "email", "type"])
@pytest.mark.asyncio
async def test_required_claims(keys, field):
    v, _ = verifier({"keys": [jwk(keys[0])]})
    with pytest.raises(AccessDenied):
        await v.verify(token(keys[0], remove=field))


@pytest.mark.asyncio
async def test_wrong_signature(keys):
    v, _ = verifier({"keys": [jwk(keys[0])]})
    with pytest.raises(AccessDenied):
        await v.verify(token(keys[1]))


@pytest.mark.parametrize("bad", [None, "", "a" * 16385, "a.b.c", "a.b.", "a.b.c\n", "é.b.c"])
@pytest.mark.asyncio
async def test_malformed_tokens_do_not_fetch(bad):
    v, calls = verifier({"keys": []})
    with pytest.raises(AccessDenied):
        await v.verify(bad)
    assert not calls


@pytest.mark.parametrize("headers", [
    {"jku": "https://evil.example/keys"}, {"x5u": "https://evil.example/cert"},
    {"crit": ["test"]}, {"jwk": {}}, {"kid": "../evil"},
])
@pytest.mark.asyncio
async def test_token_key_urls_and_critical_headers_denied_before_io(keys, headers):
    v, calls = verifier({"keys": [jwk(keys[0])]})
    with pytest.raises(AccessDenied):
        await v.verify(token(keys[0], headers=headers))
    assert not calls


@pytest.mark.asyncio
async def test_hs256_and_unsigned_denied_before_io():
    v, calls = verifier({"keys": []})
    for signed in (jwt.encode({"sub": "person"}, "x" * 64, algorithm="HS256", headers={"kid": "first"}),
                   jwt.encode({"sub": "person"}, None, algorithm="none", headers={"kid": "first"})):
        with pytest.raises(AccessDenied):
            await v.verify(signed)
    assert not calls


@pytest.mark.parametrize("issuer", [
    "http://team.cloudflareaccess.com", "https://team.cloudflareaccess.com/",
    "https://team.cloudflareaccess.com:443", "https://user@team.cloudflareaccess.com",
    "https://team.cloudflareaccess.com.evil.test", "https://127.0.0.1", "",
])
def test_invalid_issuer(issuer):
    with pytest.raises(ValueError):
        CloudflareAccessConfig(issuer, AUD, EMAILS)


@pytest.mark.parametrize("emails", [(), ("*@example.com",), ("example.com",), ("a@b.com ",),
                                    ("a@b.com", "A@B.COM"), ["a@b.com"]])
def test_exact_email_config(emails):
    with pytest.raises(ValueError):
        CloudflareAccessConfig(ISSUER, AUD, emails)
    assert CloudflareAccessConfig(ISSUER, AUD, ("A@B.COM",)).allowed_emails == ("a@b.com",)


@pytest.mark.asyncio
async def test_serialized_cache_rotation_unknown_kid_and_cooldown(keys, monkeypatch):
    tick = [100.0]
    monkeypatch.setattr("engineering_hub.cloudflare_access.time.monotonic", lambda: tick[0])
    document = {"keys": [jwk(keys[0])]}
    v, calls = verifier(document)
    await asyncio.gather(*(v.verify(token(keys[0])) for _ in range(10)))
    assert len(calls) == 1
    for _ in range(3):
        with pytest.raises(AccessDenied):
            await v.verify(token(keys[1], kid="second"))
    assert len(calls) == 1
    tick[0] += 31
    document["keys"] = [jwk(keys[1], "second")]
    await v.verify(token(keys[1], kid="second"))
    assert len(calls) == 2
    with pytest.raises(AccessDenied):
        await v.verify(token(keys[0]))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_outage_no_stale_keys_and_failed_refresh_cooldown(keys, monkeypatch):
    tick = [100.0]
    monkeypatch.setattr("engineering_hub.cloudflare_access.time.monotonic", lambda: tick[0])
    count = [0]

    def handle(request):
        count[0] += 1
        if count[0] == 1:
            return httpx.Response(200, json={"keys": [jwk(keys[0])]})
        raise httpx.ConnectError("secret detail must not escape")

    v = CloudflareAccessVerifier(CloudflareAccessConfig(ISSUER, AUD, EMAILS),
                                transport=httpx.MockTransport(handle))
    signed = token(keys[0])
    await v.verify(signed)
    tick[0] += 301
    for _ in range(3):
        with pytest.raises(AccessUnavailable) as error:
            await v.verify(signed)
        assert "secret" not in str(error.value)
    assert count[0] == 2
    tick[0] += 31
    with pytest.raises(AccessUnavailable):
        await v.verify(signed)
    assert count[0] == 3


@pytest.mark.parametrize("case", ["duplicate", "too_many", "weak", "wrong_alg", "wrong_use", "private", "empty"])
@pytest.mark.asyncio
async def test_invalid_jwks_fail_closed(keys, case):
    key = jwk(keys[0])
    document = {"keys": [key]}
    if case == "duplicate":
        document["keys"] *= 2
    elif case == "too_many":
        document["keys"] = [{**key, "kid": str(i)} for i in range(17)]
    elif case == "weak":
        document["keys"] = [jwk(rsa.generate_private_key(public_exponent=65537, key_size=1024))]
    elif case == "wrong_alg":
        key["alg"] = "HS256"
    elif case == "wrong_use":
        key["use"] = "enc"
    elif case == "private":
        key["d"] = "private"
    else:
        document["keys"] = []
    v, _ = verifier(document)
    with pytest.raises(AccessUnavailable):
        await v.verify(token(keys[0]))


@pytest.mark.parametrize("status,body", [(302, b""), (500, b""), (200, b"x" * 131073),
                                       (200, b'{"keys":[],"keys":[]}'), (200, b"not-json")],
                         ids=["redirect", "server-error", "oversized", "duplicate-json", "invalid-json"])
@pytest.mark.asyncio
async def test_bounded_key_endpoint(keys, status, body):
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(status, content=body, headers={"location": "https://evil.test/keys"})

    v = CloudflareAccessVerifier(CloudflareAccessConfig(ISSUER, AUD, EMAILS),
                                transport=httpx.MockTransport(handle))
    with pytest.raises(AccessUnavailable):
        await v.verify(token(keys[0]))
    assert seen == [ISSUER + "/cdn-cgi/access/certs"]


@pytest.mark.asyncio
async def test_actor_bound_to_app_audience(keys):
    first, _ = verifier({"keys": [jwk(keys[0])]})
    second, _ = verifier({"keys": [jwk(keys[0])]},
                         config=CloudflareAccessConfig(ISSUER, "other", EMAILS))
    a = await first.verify(token(keys[0]))
    b = await second.verify(token(keys[0], changes={"aud": ["other"]}))
    assert a.actor != b.actor


@pytest.mark.asyncio
async def test_middleware_scope_duplicates_machine_auth_and_missing_assertions(keys):
    from fastapi import FastAPI, Request

    app = FastAPI()
    v, calls = verifier({"keys": [jwk(keys[0])]})
    install_cloudflare_access(app, v.config, verifier=v)

    @app.get("/{path:path}")
    async def inspect(request: Request):
        identity = getattr(request.state, "cloudflare_identity", None)
        return {"email": identity.email if identity else None,
                "error": getattr(request.state, "cloudflare_auth_error", None)}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://hub.example") as client:
        assertion = token(keys[0])
        assert (await client.get("/api/hub/v1/auth/me", headers={"cf-access-jwt-assertion": assertion})).json() == {
            "email": EMAILS[0], "error": None}
        assert (await client.get("/api/hub/v1/auth/me", headers=[
            ("cf-access-jwt-assertion", assertion), ("cf-access-jwt-assertion", assertion)])).json() == {
                "email": None, "error": 401}
        for path, headers in [
            ("/api/hub/v1/auth/me", {}),
            ("/api/hub/v1/auth/me", {"authorization": "Bearer machine", "cf-access-jwt-assertion": assertion}),
            ("/api/hub/v10/auth/me", {"cf-access-jwt-assertion": assertion}),
            ("/mcp", {"cf-access-jwt-assertion": assertion}),
        ]:
            assert (await client.get(path, headers=headers)).json() == {"email": None, "error": None}
        assert (await client.get("/api/hub/v1", headers={"cf-access-jwt-assertion": "bad"})).json() == {
            "email": None, "error": 401}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_middleware_disabled_and_unavailable_sanitized():
    from fastapi import FastAPI, Request

    class BrokenVerifier:
        async def verify(self, token):
            raise RuntimeError("secret upstream value")

    for config, checker, expected in [
        (None, None, None),
        (CloudflareAccessConfig(ISSUER, AUD, EMAILS), BrokenVerifier(), 503),
    ]:
        app = FastAPI()
        install_cloudflare_access(app, config, verifier=checker)

        @app.get("/api/hub/v1/auth/me")
        async def inspect(request: Request):
            return {"error": getattr(request.state, "cloudflare_auth_error", None)}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://hub.example") as client:
            response = await client.get("/api/hub/v1/auth/me", headers={"cf-access-jwt-assertion": "bad"})
            assert response.json() == {"error": expected}
            assert "secret" not in response.text


@pytest.mark.parametrize("header,claims", [
    ('{"alg":"HS256","alg":"RS256","kid":"first"}', '{}'),
    ('{"alg":"RS256","kid":"first"}', '{"aud":"evil","aud":"hub-test-audience"}'),
    ('{"alg":"RS256","kid":"first"}', '{"iat":NaN}'),
])
@pytest.mark.asyncio
async def test_signed_duplicate_json_and_non_json_constants_denied_before_io(keys, header, claims):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    message = encode(header.encode()) + "." + encode(claims.encode())
    signature = keys[0].sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    signed = message + "." + encode(signature)
    v, calls = verifier({"keys": [jwk(keys[0])]})
    with pytest.raises(AccessDenied):
        await v.verify(signed)
    assert not calls
