"""Human-only GitHub login, isolated from the message bus agent identities."""
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


@dataclass(frozen=True)
class AuthConfig:
    base_url: str
    github_client_id: str = ""
    github_client_secret: str = ""
    beau_github_id: int | None = None
    cookie_name: str = "hub_session"

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or url.path not in ("", "/") or url.scheme not in ("http", "https")):
            raise ValueError("Hub base URL must be an absolute origin")
        if url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Hub base URL requires HTTPS except on loopback")
        if self.beau_github_id is not None and (type(self.beau_github_id) is not int or self.beau_github_id <= 0):
            raise ValueError("Allowlisted GitHub identity must be a positive numeric ID")

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

    def __init__(self, path: str | Path, *, session_ttl=28800, state_ttl=600, clock=time.time):
        self.path = str(path)
        self.session_ttl = session_ttl
        self.state_ttl = state_ttl
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS auth_sessions (token_hash TEXT PRIMARY KEY, github_id INTEGER NOT NULL, csrf_token TEXT NOT NULL, expires REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS auth_states (state_hash TEXT PRIMARY KEY, verifier TEXT NOT NULL, expires REAL NOT NULL)")

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
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
            db.execute("BEGIN IMMEDIATE")
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
        with self._db() as db:
            row = db.execute("SELECT github_id, csrf_token, expires FROM auth_sessions WHERE token_hash = ?", (_hash(token),)).fetchone()
        if not row or row[2] <= self.clock():
            return None
        return {"github_id": row[0], "csrf_token": row[1], "actor": f"human:{row[0]}"}

    def delete_session(self, token):
        if token:
            with self._db() as db:
                db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_hash(token),))


def require_human(request: Request, config: AuthConfig, store: SessionStore, mutation=False):
    session = store.session_for(request.cookies.get(config.cookie_name))
    if session is None:
        raise HTTPException(401, "Human sign-in required")
    if config.beau_github_id is None or session["github_id"] != config.beau_github_id:
        raise HTTPException(403, "Human identity is not authorized")
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
    async def login():
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
        session = auth_store.session_for(request.cookies.get(config.cookie_name))
        valid = session is not None and config.beau_github_id is not None and session["github_id"] == config.beau_github_id
        return JSONResponse({"authenticated": valid, "actor": session["actor"] if valid else None,
                             "csrf_token": session["csrf_token"] if valid else None,
                             "github_id": session["github_id"] if valid else None}, headers={"Cache-Control": "no-store"})

    @router.post("/logout")
    async def logout(request: Request):
        require_human(request, config, auth_store, mutation=True)
        auth_store.delete_session(request.cookies.get(config.cookie_name))
        response = JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(config.cookie_name, **cookie_options)
        return response

    return router
