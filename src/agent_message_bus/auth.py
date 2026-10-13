from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class AuthSettings:
    hermes_token: str
    codex_token: str
    admin_token: str | None = None
    allowed_ips: frozenset[str] = frozenset()
    # Appended after legacy positional arguments to keep the constructor compatible.
    claude_token: str | None = None

    def __post_init__(self) -> None:
        for name, token in (("HERMES_TOKEN", self.hermes_token), ("CODEX_TOKEN", self.codex_token)):
            if not token:
                raise ValueError(f"{name} must be configured")
        configured = [(name, token) for name, token in (
            ("HERMES_TOKEN", self.hermes_token), ("CODEX_TOKEN", self.codex_token),
            ("ADMIN_TOKEN", self.admin_token), ("CLAUDE_TOKEN", self.claude_token),
        ) if token]
        for index, (name, token) in enumerate(configured):
            token_hash = hashlib.sha256(token.encode()).digest()
            if any(hmac.compare_digest(token_hash, hashlib.sha256(other.encode()).digest())
                   for _, other in configured[index + 1:]):
                raise ValueError(f"configured authentication tokens must be distinct ({name})")

    @classmethod
    def from_env(cls) -> "AuthSettings":
        return cls(
            hermes_token=os.environ.get("HERMES_TOKEN", ""),
            codex_token=os.environ.get("CODEX_TOKEN", ""),
            admin_token=os.environ.get("ADMIN_TOKEN") or None,
            allowed_ips=frozenset(x.strip() for x in os.environ.get("AGENT_BUS_ALLOWED_IPS", "").split(",") if x.strip()),
            claude_token=os.environ.get("CLAUDE_TOKEN") or None,
        )

    def identity_for(self, token: str) -> str | None:
        token_hash = hashlib.sha256(token.encode()).digest()
        for identity, configured in (("hermes", self.hermes_token), ("codex", self.codex_token),
                                     ("admin", self.admin_token), ("claude", self.claude_token)):
            if configured and hmac.compare_digest(token_hash, hashlib.sha256(configured.encode()).digest()):
                return identity
        return None

    def authenticate(self, request: Request, *, admin: bool = False) -> str:
        if self.allowed_ips and (request.client is None or request.client.host not in self.allowed_ips):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="source IP is not allowed")
        value = request.headers.get("authorization", "")
        scheme, _, token = value.partition(" ")
        identity = self.identity_for(token) if scheme.lower() == "bearer" and token else None
        if identity is None or (admin and identity != "admin") or (not admin and identity == "admin"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        request.state.agent_identity = identity
        return identity

    def agent(self, request: Request) -> str:
        identity = self.authenticate(request)
        if identity not in {"hermes", "codex", "claude"}:
            raise HTTPException(status_code=401, detail="agent token required")
        return identity

    def admin(self, request: Request) -> str:
        return self.authenticate(request, admin=True)
