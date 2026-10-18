"""Fail-closed human Access assertions; never trusts proxy email headers.

Keys come only from the operator-pinned Access team origin. This module does
not configure Cloudflare policies or authenticate machine/MCP identities.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey


class AccessDenied(Exception):
    def __init__(self):
        super().__init__("Cloudflare Access identity denied")


class AccessUnavailable(Exception):
    def __init__(self):
        super().__init__("Cloudflare Access verification unavailable")


_ISSUER = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com\Z")
_EMAIL = re.compile(r"[a-z0-9.!#$%&'+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}\Z")
_KID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


@dataclass(frozen=True)
class CloudflareAccessConfig:
    issuer: str
    audience: str
    allowed_emails: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.issuer, str) or not _ISSUER.fullmatch(self.issuer):
            raise ValueError("Access issuer must be an exact HTTPS team origin")
        if (not isinstance(self.audience, str) or not self.audience
                or len(self.audience) > 256 or not self.audience.isascii()
                or any(c.isspace() or ord(c) < 33 for c in self.audience)):
            raise ValueError("Access application audience required")
        if type(self.allowed_emails) is not tuple or not self.allowed_emails:
            raise ValueError("Exact Access email allowlist required")
        normalized = []
        for email in self.allowed_emails:
            if not isinstance(email, str) or len(email) > 254 or not _EMAIL.fullmatch(email.lower()):
                raise ValueError("Access allowlist requires exact email addresses")
            normalized.append(email.lower())
        if len(set(normalized)) != len(normalized):
            raise ValueError("Duplicate Access email identity")
        object.__setattr__(self, "allowed_emails", tuple(normalized))


@dataclass(frozen=True)
class CloudflareIdentity:
    subject: str
    email: str
    actor: str
    expires_at: int


# In-process capability only: no HTTP header, boolean state flag or serialized
# value can impersonate this marker. It is intentionally not public API.
_VERIFIED_ACCESS_KEY = object()
_VERIFIED_ACCESS_MARKER = object()


def _record_verified_access(scope, config, identity):
    if type(config) is not CloudflareAccessConfig or type(identity) is not CloudflareIdentity:
        raise ValueError("Verified Access identity and configuration required")
    assertions = [value for name, value in scope.get("headers", [])
                  if name.lower() == b"cf-access-jwt-assertion"]
    if len(assertions) != 1:
        raise ValueError("One verified Access assertion required")
    scope[_VERIFIED_ACCESS_KEY] = (
        _VERIFIED_ACCESS_MARKER, config, identity, hashlib.sha256(assertions[0]).digest(),
    )


def _verified_access(scope, config):
    proof = scope.get(_VERIFIED_ACCESS_KEY)
    if (type(proof) is not tuple or len(proof) != 4
            or proof[0] is not _VERIFIED_ACCESS_MARKER
            or type(config) is not CloudflareAccessConfig
            or type(proof[1]) is not CloudflareAccessConfig or proof[1] != config
            or type(proof[2]) is not CloudflareIdentity or type(proof[3]) is not bytes):
        return None
    assertions = [value for name, value in scope.get("headers", [])
                  if name.lower() == b"cf-access-jwt-assertion"]
    if (len(assertions) != 1 or proof[3] != hashlib.sha256(assertions[0]).digest()
            or proof[2].expires_at <= int(time.time())):
        return None
    return proof[2]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-JSON numeric constant")


def _token_json(segment):
    decoded = base64.b64decode(segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True)
    value = json.loads(decoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if type(value) is not dict:
        raise ValueError("JWT object required")
    return value


class CloudflareAccessVerifier:
    CACHE_SECONDS = 300
    REFRESH_COOLDOWN_SECONDS = 30
    MAX_RESPONSE_BYTES = 128 * 1024
    MAX_TOKEN_BYTES = 16 * 1024

    def __init__(self, config: CloudflareAccessConfig, *, transport=None):
        if type(config) is not CloudflareAccessConfig:
            raise ValueError("Validated Access configuration required")
        self.config = config
        self._transport = transport
        self._keys: dict[str, RSAPublicKey] = {}
        self._expires = 0.0
        self._next_refresh = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self):
        # Reserve cooldown before I/O, including timeout/cancellation failures.
        self._next_refresh = time.monotonic() + self.REFRESH_COOLDOWN_SECONDS
        try:
            async with asyncio.timeout(10):
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=httpx.Timeout(5),
                    trust_env=False, follow_redirects=False,
                ) as client:
                    async with client.stream("GET", self.config.issuer + "/cdn-cgi/access/certs") as response:
                        if response.status_code != 200:
                            raise ValueError("Key endpoint unavailable")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > self.MAX_RESPONSE_BYTES:
                                raise ValueError("Key response too large")
                            data.extend(chunk)
            document = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            keys = document.get("keys") if isinstance(document, dict) else None
            if type(keys) is not list or not 1 <= len(keys) <= 16:
                raise ValueError("Invalid signing keys")
            parsed = {}
            for key in keys:
                if (type(key) is not dict or key.get("kty") != "RSA"
                        or key.get("alg", "RS256") != "RS256"
                        or key.get("use", "sig") != "sig"
                        or "d" in key or "oth" in key
                        or ("key_ops" in key and key["key_ops"] != ["verify"])):
                    raise ValueError("Invalid signing key")
                kid = key.get("kid")
                if not isinstance(kid, str) or not _KID.fullmatch(kid) or kid in parsed:
                    raise ValueError("Invalid signing key identifier")
                public = jwt.algorithms.RSAAlgorithm.from_jwk(key)
                if not isinstance(public, RSAPublicKey) or public.key_size < 2048:
                    raise ValueError("Invalid RSA signing key")
                parsed[kid] = public
            self._keys = parsed
            self._expires = time.monotonic() + self.CACHE_SECONDS
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, jwt.PyJWTError):
            raise AccessUnavailable() from None

    async def _key(self, kid):
        async with self._lock:
            now = time.monotonic()
            if now < self._expires and kid in self._keys:
                return self._keys[kid]
            if now >= self._next_refresh:
                await self._refresh()
            if time.monotonic() >= self._expires:
                raise AccessUnavailable()
            if kid not in self._keys:
                raise AccessDenied()
            return self._keys[kid]

    async def verify(self, token: str) -> CloudflareIdentity:
        if (not isinstance(token, str) or not token or not token.isascii()
                or len(token) > self.MAX_TOKEN_BYTES
                or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token)):
            raise AccessDenied()
        try:
            header_segment, claims_segment, _ = token.split(".")
            header = _token_json(header_segment)
            # Reject duplicate claim/header names before any key-endpoint I/O.
            # These are not identities until jwt.decode verifies the signature.
            _token_json(claims_segment)
            kid = header.get("kid")
            if (header.get("alg") != "RS256" or not isinstance(kid, str)
                    or not _KID.fullmatch(kid) or "crit" in header
                    or "jku" in header or "x5u" in header or "jwk" in header):
                raise AccessDenied()
        except (jwt.PyJWTError, ValueError, TypeError):
            raise AccessDenied() from None
        key = await self._key(kid)
        try:
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.config.audience,
                issuer=self.config.issuer,
                options={"require": ["iss", "aud", "exp", "iat", "sub", "email", "type"]},
            )
            for name in ("exp", "iat", "nbf"):
                if name in claims and (type(claims[name]) is not int or not 0 <= claims[name] <= 2**53):
                    raise AccessDenied()
            if claims["exp"] <= claims["iat"] or claims.get("nbf", claims["iat"]) > claims["exp"]:
                raise AccessDenied()
            if claims["aud"] not in (self.config.audience, [self.config.audience]):
                raise AccessDenied()
            subject, email = claims["sub"], claims["email"]
            if (claims["type"] != "app" or not isinstance(subject, str)
                    or not subject or len(subject) > 256 or not subject.isascii()
                    or any(ord(c) < 33 or ord(c) == 127 for c in subject)
                    or not isinstance(email, str) or email.lower() not in self.config.allowed_emails):
                raise AccessDenied()
        except (jwt.PyJWTError, TypeError, ValueError, KeyError):
            raise AccessDenied() from None
        # Audience binding prevents identities being recycled across EH apps.
        binding = "\0".join((self.config.issuer, self.config.audience, subject))
        actor = "human:cf:" + hashlib.sha256(binding.encode()).hexdigest()
        return CloudflareIdentity(subject, email.lower(), actor, claims["exp"])


def install_cloudflare_access(app, config: CloudflareAccessConfig | None, *, verifier=None):
    """Annotate human Hub API requests, leaving route authorization fail-closed.

    Assertion failures never become agent credentials or a bypass for existing
    local-session checks. Public assets and independent bearer MCP stay untouched.
    """
    checker = verifier or (CloudflareAccessVerifier(config) if config is not None else None)

    @app.middleware("http")
    async def access_assertion(request, call_next):
        path = request.url.path
        if (checker is not None and (path == "/api/hub/v1" or path.startswith("/api/hub/v1/"))
                and not request.headers.getlist("authorization")):
            verified = _verified_access(request.scope, config)
            # Identity state alone is not verification evidence. Clear stale or
            # injected state before a normal assertion check.
            request.scope.setdefault("state", {}).pop("cloudflare_identity", None)
            request.scope["state"].pop("cloudflare_auth_error", None)
            if verified is not None:
                request.state.cloudflare_identity = verified
                return await call_next(request)
            assertions = request.headers.getlist("cf-access-jwt-assertion")
            if assertions:
                if len(assertions) != 1:
                    request.state.cloudflare_auth_error = 401
                else:
                    try:
                        request.state.cloudflare_identity = await checker.verify(assertions[0])
                    except AccessDenied:
                        request.state.cloudflare_auth_error = 401
                    except Exception:
                        # Includes a verifier/transport defect: never expose claims,
                        # raw upstream messages or tracebacks through human routes.
                        # CancelledError is BaseException and propagates normally.
                        request.state.cloudflare_auth_error = 503
        return await call_next(request)
