"""Human sign-in providers, isolated from message bus agent identities."""
from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .repository import HubError, HubRepository
from .cloudflare_access import CloudflareAccessConfig, CloudflareIdentity


@dataclass(frozen=True)
class AuthConfig:
    base_url: str
    github_client_id: str = ""
    github_client_secret: str = ""
    beau_github_id: int | None = None
    cookie_name: str = "hub_session"
    human_auth_mode: str = "github"
    cloudflare_issuer: str = ""
    cloudflare_audience: str = ""
    cloudflare_allowed_emails: tuple[str, ...] = ()
    alternate_origins: tuple[str, ...] = ()

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or url.path not in ("", "/") or url.scheme not in ("http", "https")):
            raise ValueError("Hub base URL must be an absolute origin")
        if url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Hub base URL requires HTTPS except on loopback")
        if self.beau_github_id is not None and (type(self.beau_github_id) is not int or self.beau_github_id <= 0):
            raise ValueError("Allowlisted GitHub identity must be a positive numeric ID")
        if self.human_auth_mode not in {"github", "cloudflare_access"}:
            raise ValueError("Unknown human authentication mode")
        if self.human_auth_mode == "cloudflare_access" and not self.secure:
            raise ValueError("Cloudflare Access requires HTTPS")
        _ = self.cloudflare_config  # Validate complete configuration before serving.
        if type(self.alternate_origins) is not tuple or len(self.alternate_origins) > 4:
            raise ValueError("At most four explicit alternate HTTPS origins are allowed")
        for alternate in self.alternate_origins:
            parsed = urlsplit(alternate)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment or alternate != "https://" + parsed.netloc
                    or parsed.netloc != parsed.netloc.lower() or alternate == self.origin):
                raise ValueError("Alternate Hub origins must be distinct exact HTTPS origins")
        if len(set(self.alternate_origins)) != len(self.alternate_origins):
            raise ValueError("Duplicate alternate Hub origin")

    @property
    def cloudflare_config(self):
        if not (self.cloudflare_issuer and self.cloudflare_audience and self.cloudflare_allowed_emails):
            return None
        return CloudflareAccessConfig(self.cloudflare_issuer, self.cloudflare_audience,
                                      self.cloudflare_allowed_emails)

    @property
    def origin(self):
        return self.base_url.rstrip("/")

    @property
    def secure(self):
        return urlsplit(self.base_url).scheme == "https"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SessionStore:
    """Persist only hashed bearer session IDs; OAuth state is one-use and short-lived."""

    def __init__(self, path: str | Path, *, session_ttl=28800, state_ttl=600, clock=time.time, repository=None):
        self.path = str(path)
        if repository is not None and (
            type(repository) is not HubRepository
            or Path(repository.path).absolute() != Path(self.path).absolute()
        ):
            raise HubError('pilot_ownership_required', 409)
        self.repository = repository
        self.session_ttl = session_ttl
        self.state_ttl = state_ttl
        self.clock = clock
        if self.repository is None:
            self._assert_unenrolled()
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS auth_sessions (token_hash TEXT PRIMARY KEY, github_id INTEGER NOT NULL, csrf_token TEXT NOT NULL, expires REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS auth_states (state_hash TEXT PRIMARY KEY, verifier TEXT NOT NULL, expires REAL NOT NULL)")

    @contextmanager
    def _db(self):
        if self.repository is not None:
            with self.repository._transaction() as db:
                yield db
            return
        # Probe before opening writable: even SQLite journal recovery would be
        # an unauthorized write to an owner-enrolled ledger.
        self._assert_unenrolled()
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                self._assert_unenrolled(connection)
                yield connection
                self._assert_unenrolled(connection)
        finally:
            connection.close()

    def _assert_unenrolled(self, connection=None):
        if connection is None:
            if not Path(self.path).exists():
                return
            with self._read_db() as db:
                self._assert_unenrolled(db)
        elif connection.execute("SELECT 1 FROM sqlite_master WHERE name='hub_pilot_owner' COLLATE NOCASE").fetchone():
            # Any marker, including malformed tables/views, is a deny signal.
            raise HubError('pilot_ownership_required', 409)

    @contextmanager
    def _read_db(self):
        if self.repository is not None:
            with self.repository._connection() as db:
                yield db
            return
        connection = sqlite3.connect(Path(self.path).absolute().as_uri() + '?mode=ro', uri=True, timeout=10)
        try:
            yield connection
        finally:
            connection.close()

    def create_state(self):
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with self._db() as db:
            db.execute("DELETE FROM auth_states WHERE expires <= ?", (self.clock(),))
            db.execute("INSERT INTO auth_states VALUES (?, ?, ?)", (_hash(state), verifier, self.clock() + self.state_ttl))
        return {"state": state, "verifier": verifier, "challenge": challenge}

    def consume_state(self, state):
        with self._db() as db:
            row = db.execute("SELECT verifier, expires FROM auth_states WHERE state_hash = ?", (_hash(state),)).fetchone()
            db.execute("DELETE FROM auth_states WHERE state_hash = ?", (_hash(state),))
        return row[0] if row and row[1] > self.clock() else None

    def create_session(self, github_id: int):
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires <= ?", (self.clock(),))
            db.execute("INSERT INTO auth_sessions VALUES (?, ?, ?, ?)", (_hash(token), github_id, csrf, self.clock() + self.session_ttl))
        return {"cookie_token": token, "csrf_token": csrf}

    def session_for(self, token):
        if not token:
            return None
        with self._read_db() as db:
            row = db.execute("SELECT github_id, csrf_token, expires FROM auth_sessions WHERE token_hash = ?", (_hash(token),)).fetchone()
        if not row or row[2] <= self.clock():
            return None
        return {"github_id": row[0], "csrf_token": row[1], "actor": f"human:{row[0]}"}

    def delete_session(self, token):
        if token:
            with self._db() as db:
                db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_hash(token),))

    @staticmethod
    def _cloudflare_binding(config, identity):
        # Pin the local session to this application and verified identity. Never
        # turn the Access assertion itself into a reusable Hub session bearer.
        return _hash("\n".join((config.cloudflare_issuer, config.cloudflare_audience,
                                identity.subject, identity.email)))

    @staticmethod
    def _cloudflare_identity_key(config, identity):
        return _hash("\n".join((config.cloudflare_issuer, config.cloudflare_audience, identity.email)))

    def create_cloudflare_session(self, config, identity, *, previous_token=None):
        if type(identity) is not CloudflareIdentity or identity.expires_at <= self.clock():
            raise HTTPException(401, "Human sign-in required")
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        expires = min(self.clock() + self.session_ttl, identity.expires_at)
        with self._db() as db:
            # Lazy, additive schema: default GitHub installs are unchanged.
            db.execute("CREATE TABLE IF NOT EXISTS auth_access_sessions (token_hash TEXT PRIMARY KEY, binding TEXT NOT NULL, actor TEXT NOT NULL, csrf_token TEXT NOT NULL, expires REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS auth_access_identities (identity_key TEXT PRIMARY KEY, binding TEXT NOT NULL, actor TEXT NOT NULL)")
            key = self._cloudflare_identity_key(config, identity)
            binding = self._cloudflare_binding(config, identity)
            enrolled = db.execute("SELECT binding, actor FROM auth_access_identities WHERE identity_key = ?", (key,)).fetchone()
            if enrolled is not None and (not secrets.compare_digest(enrolled[0], binding) or enrolled[1] != identity.actor):
                raise HTTPException(403, "Human identity requires operator review")
            if enrolled is None:
                # Only a cryptographically verified, explicitly allowlisted
                # identity can reach this transaction. Never repin on logout,
                # expiry or a later matching-email/different-subject assertion.
                db.execute("INSERT INTO auth_access_identities VALUES (?, ?, ?)", (key, binding, identity.actor))
            db.execute("DELETE FROM auth_access_sessions WHERE expires <= ?", (self.clock(),))
            if previous_token:
                db.execute("DELETE FROM auth_access_sessions WHERE token_hash = ?", (_hash(previous_token),))
            db.execute("INSERT INTO auth_access_sessions VALUES (?, ?, ?, ?, ?)",
                       (_hash(token), binding, identity.actor, csrf, expires))
        return {"cookie_token": token, "csrf_token": csrf, "expires": expires}

    def cloudflare_session_for(self, token, config, identity):
        if not token or type(identity) is not CloudflareIdentity or identity.expires_at <= self.clock():
            return None
        with self._read_db() as db:
            if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('auth_access_sessions', 'auth_access_identities')").fetchone()[0] != 2:
                return None
            enrolled = db.execute("SELECT binding, actor FROM auth_access_identities WHERE identity_key = ?",
                                  (self._cloudflare_identity_key(config, identity),)).fetchone()
            row = db.execute("SELECT binding, actor, csrf_token, expires FROM auth_access_sessions WHERE token_hash = ?",
                             (_hash(token),)).fetchone()
        if (not enrolled or enrolled[1] != identity.actor
                or not secrets.compare_digest(enrolled[0], self._cloudflare_binding(config, identity))
                or not row or row[3] <= self.clock() or row[1] != identity.actor
                or not secrets.compare_digest(row[0], self._cloudflare_binding(config, identity))):
            return None
        return {"actor": row[1], "csrf_token": row[2], "github_id": None,
                "email": identity.email, "auth_provider": "cloudflare_access"}

    def delete_cloudflare_session(self, token):
        if token:
            with self._db() as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_access_sessions'").fetchone():
                    db.execute("DELETE FROM auth_access_sessions WHERE token_hash = ?", (_hash(token),))


def human_session(request: Request, config: AuthConfig, store: SessionStore):
    if request.headers.get("authorization"):
        return None  # Agent/service bearers never become human sessions.
    if config.human_auth_mode == "cloudflare_access":
        return store.cloudflare_session_for(request.cookies.get(config.cookie_name), config,
                                            getattr(request.state, "cloudflare_identity", None))
    session = store.session_for(request.cookies.get(config.cookie_name))
    if session is None or config.beau_github_id is None or session["github_id"] != config.beau_github_id:
        return None
    return session


def require_human(request: Request, config: AuthConfig, store: SessionStore, mutation=False):
    if config.human_auth_mode == "github":
        session = None if request.headers.get("authorization") else store.session_for(request.cookies.get(config.cookie_name))
        if session is not None and (config.beau_github_id is None or session["github_id"] != config.beau_github_id):
            raise HTTPException(403, "Human identity is not authorized")
    else:
        session = human_session(request, config, store)
        if session is None and getattr(request.state, "cloudflare_auth_error", None) == 503:
            raise HTTPException(503, "Human authentication is unavailable")
    if session is None:
        raise HTTPException(401, "Human sign-in required")
    if mutation:
        supplied = request.headers.get("x-hub-csrf", "")
        if request.headers.get("origin") != config.origin or not secrets.compare_digest(supplied.encode(), session["csrf_token"].encode()):
            raise HTTPException(403, "Request verification failed")
    return session["actor"]


class GitHubIdentityProvider:
    def __init__(self, config: AuthConfig):
        self.config = config

    async def exchange(self, code, verifier):
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = await client.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"}, data={
                "client_id": self.config.github_client_id, "client_secret": self.config.github_client_secret,
                "code": code, "code_verifier": verifier,
                "redirect_uri": self.config.origin + "/api/hub/v1/auth/callback",
            })
            response.raise_for_status()
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("Identity exchange failed")
            identity = await client.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            identity.raise_for_status()
            github_id = identity.json().get("id")
            if type(github_id) is not int or github_id <= 0:
                raise ValueError("Invalid identity")
            return github_id


def build_auth_router(settings, auth_store: SessionStore, github_client=None):
    config = settings if isinstance(settings, AuthConfig) else settings.auth_config
    provider = github_client or GitHubIdentityProvider(config)
    router = APIRouter(prefix="/api/hub/v1/auth", tags=["human-auth"])
    state_cookie = config.cookie_name + "_oauth"
    cookie_options = {"httponly": True, "secure": config.secure, "samesite": "lax", "path": "/"}

    @router.get("/login")
    async def login(request: Request):
        if config.human_auth_mode == "cloudflare_access":
            if config.cloudflare_config is None:
                raise HTTPException(503, "Human sign-in is not configured")
            identity = getattr(request.state, "cloudflare_identity", None)
            if request.headers.get("authorization") or type(identity) is not CloudflareIdentity:
                raise HTTPException(getattr(request.state, "cloudflare_auth_error", 401), "Human sign-in required")
            session = auth_store.create_cloudflare_session(config, identity,
                previous_token=request.cookies.get(config.cookie_name))
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(config.cookie_name, session["cookie_token"],
                                max_age=max(0, int(session["expires"] - auth_store.clock())), **cookie_options)
            response.headers["Cache-Control"] = "no-store"
            return response
        if not config.github_client_id or not config.github_client_secret or config.beau_github_id is None:
            raise HTTPException(503, "Human sign-in is not configured")
        pending = auth_store.create_state()
        response = RedirectResponse("https://github.com/login/oauth/authorize?" + urlencode({
            "client_id": config.github_client_id, "redirect_uri": config.origin + "/api/hub/v1/auth/callback",
            "state": pending["state"], "code_challenge": pending["challenge"], "code_challenge_method": "S256",
        }), status_code=302)
        response.set_cookie(state_cookie, pending["state"], max_age=auth_store.state_ttl, **cookie_options)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/callback")
    async def callback(request: Request):
        if config.human_auth_mode != "github":
            raise HTTPException(404, "Not found")
        state = request.query_params.get("state", "")
        browser_state = request.cookies.get(state_cookie, "")
        if not state or not browser_state or not secrets.compare_digest(state.encode(), browser_state.encode()):
            raise HTTPException(400, "Sign-in verification failed")
        verifier = auth_store.consume_state(state)
        code = request.query_params.get("code", "")
        if not verifier or not code or request.query_params.get("error"):
            raise HTTPException(400, "Sign-in verification failed")
        if not config.github_client_id or not config.github_client_secret or config.beau_github_id is None:
            raise HTTPException(503, "Human sign-in is not configured")
        try:
            github_id = await provider.exchange(code, verifier)
        except Exception:
            raise HTTPException(502, "Identity provider request failed") from None
        if type(github_id) is not int or github_id != config.beau_github_id:
            raise HTTPException(403, "Human identity is not authorized")
        auth_store.delete_session(request.cookies.get(config.cookie_name))
        session = auth_store.create_session(github_id)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(config.cookie_name, session["cookie_token"], max_age=auth_store.session_ttl, **cookie_options)
        response.delete_cookie(state_cookie, **cookie_options)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/me")
    async def me(request: Request):
        session = human_session(request, config, auth_store)
        valid = session is not None
        data = {"authenticated": valid, "actor": session["actor"] if valid else None,
                             "csrf_token": session["csrf_token"] if valid else None,
                             "github_id": session["github_id"] if valid else None}
        if config.human_auth_mode == "cloudflare_access":
            data.update(auth_provider="cloudflare_access", email=session["email"] if valid else None)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @router.post("/logout")
    async def logout(request: Request):
        require_human(request, config, auth_store, mutation=True)
        if config.human_auth_mode == "cloudflare_access":
            auth_store.delete_cloudflare_session(request.cookies.get(config.cookie_name))
        else:
            auth_store.delete_session(request.cookies.get(config.cookie_name))
        response = JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(config.cookie_name, **cookie_options)
        return response

    return router
