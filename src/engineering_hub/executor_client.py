"""Fail-closed service client for staging fixture executors; never retries commands.

This module does not enable dispatch. A caller must durably retain CommandRequest
before submission and inspect/reconcile the same identity after UnknownOutcome.
The bearer is a dedicated executor service secret, not a message-bus agent token.
"""
from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class _WireModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class CommandRequest(_WireModel):
    command_id: str
    target_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}:staging$")
    action: Literal["deploy_fixture", "smoke_fixture", "rollback_fixture"]
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    policy_revision: int = Field(gt=0)
    fencing_generation: int = Field(gt=0, le=2**63 - 1)

    @field_validator("command_id")
    @classmethod
    def canonical_command_id(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError):
            raise ValueError("invalid command identity") from None
        if str(parsed) != value:
            raise ValueError("invalid command identity")
        return value


class CommandResponse(CommandRequest):
    state: Literal["pending", "running", "succeeded", "failed", "held"]
    reason: str | None = Field(max_length=2000)
    observed: dict[str, Any]
    replayed: bool = False


class HealthResponse(_WireModel):
    ok: bool


class _PauseResponse(_WireModel):
    paused: bool


class ExecutorClientError(Exception):
    """Safe public error: never includes URLs, response bodies or credentials."""


class UnknownOutcome(ExecutorClientError):
    def __init__(self, command_id: str):
        self.command_id = command_id
        super().__init__("executor outcome unknown; inspect original command before retry")


class HeldProtocolError(ExecutorClientError):
    state = "held"

    def __init__(self, command_id: str):
        self.command_id = command_id
        super().__init__("executor protocol verification failed; command held")


class ExecutorUnavailable(ExecutorClientError):
    def __init__(self):
        super().__init__("executor service unavailable")


class ExecutorClient:
    """One authenticated service connection with explicit, bounded operations."""

    def __init__(self, base_url: str, service_token: str, *,
                 timeout_seconds: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        try:
            parsed = urlsplit(base_url)
            port = parsed.port  # Reject malformed ports without reflecting URL.
            del port
        except (ValueError, TypeError):
            raise ValueError("invalid executor origin") from None
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("invalid executor origin")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("executor requires HTTPS except on loopback")
        if (not isinstance(service_token, str) or not service_token
                or any(not 33 <= ord(character) <= 126 for character in service_token)):
            raise ValueError("invalid executor credential")
        if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 60:
            raise ValueError("executor timeout must be positive and bounded")
        self.__token = service_token
        self.__client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {service_token}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0)),
            follow_redirects=False, trust_env=False, transport=transport,
        )

    async def __aenter__(self):
        await self.__client.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.__client.__aexit__(*args)

    async def aclose(self):
        await self.__client.aclose()

    def __repr__(self):
        return "ExecutorClient(credential=<redacted>)"

    async def _request(self, method: str, path: str, *, command_id: str | None = None,
                       payload: dict | None = None) -> dict:
        try:
            response = await self.__client.request(method, path, json=payload)
        except httpx.RequestError:
            if command_id is not None:
                raise UnknownOutcome(command_id) from None
            raise ExecutorUnavailable() from None
        # Never follow redirects or reflect error payloads. A service may have
        # accepted work before a proxy returned an error: do not resubmit.
        if not 200 <= response.status_code < 300:
            if command_id is not None:
                raise UnknownOutcome(command_id) from None
            raise ExecutorUnavailable() from None
        # Never return a service secret even if a compromised/misconfigured
        # executor reflects the Authorization header into an otherwise valid body.
        if len(response.content) > 1024 * 1024 or self.__token in response.text:
            if command_id is not None:
                raise HeldProtocolError(command_id) from None
            raise ExecutorUnavailable() from None
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            if self.__token in json.dumps(data, ensure_ascii=False):
                raise ValueError
            return data
        except (ValueError, UnicodeError):
            if command_id is not None:
                raise HeldProtocolError(command_id) from None
            raise ExecutorUnavailable() from None

    async def _command(self, method: str, path: str, expected: CommandRequest,
                       *, submit: bool = False) -> CommandResponse:
        if not isinstance(expected, CommandRequest) or isinstance(expected, CommandResponse):
            raise TypeError("expected a command request")
        data = await self._request(method, path, command_id=expected.command_id,
                                   payload=expected.model_dump() if submit else None)
        try:
            result = CommandResponse.model_validate(data)
        except ValidationError:
            raise HeldProtocolError(expected.command_id) from None
        echoed = result.model_dump(include=set(CommandRequest.model_fields))
        if echoed != expected.model_dump():
            raise HeldProtocolError(expected.command_id)
        return result

    async def submit_command(self, command: CommandRequest) -> CommandResponse:
        return await self._command("POST", "/v1/commands", command, submit=True)

    async def get_command(self, command: CommandRequest) -> CommandResponse:
        return await self._command("GET", f"/v1/commands/{command.command_id}", command)

    async def reconcile_command(self, command: CommandRequest) -> CommandResponse:
        """Inspect the original identity; never submits/restarts/retries work."""
        return await self._command("POST", f"/v1/commands/{command.command_id}/reconcile", command)

    async def pause(self, paused: bool) -> bool:
        if type(paused) is not bool:
            raise ValueError("pause requires a boolean")
        data = await self._request("POST", "/v1/pause", payload={"paused": paused})
        try:
            result = _PauseResponse.model_validate(data)
        except ValidationError:
            raise ExecutorUnavailable() from None
        if result.paused is not paused:
            raise ExecutorUnavailable()
        return result.paused

    async def health(self) -> HealthResponse:
        data = await self._request("GET", "/health")
        try:
            return HealthResponse.model_validate(data)
        except ValidationError:
            raise ExecutorUnavailable() from None
