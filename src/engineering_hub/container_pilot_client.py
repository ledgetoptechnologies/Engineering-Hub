"""Draft, opt-in container pilot wire client; no deployment capability is enabled.

Persist the complete request before submission. An uncertain POST must be
inspected using its original identity, never retried automatically. Returned
evidence is a typed adapter assertion, not independent proof of Docker effects.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class _WireModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ContainerPilotRequest(_WireModel):
    schema_version: Literal["container_pilot_v1"]
    command_id: str
    run_id: str
    target_id: Literal["engineering-hub:staging"]
    action: Literal["deploy_container", "smoke_container", "rollback_container", "smoke_container_rollback"]
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    policy_revision: int = Field(gt=0)
    fencing_generation: int = Field(gt=0, le=2**63 - 1)

    @field_validator("command_id", "run_id")
    @classmethod
    def canonical_identity(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError):
            raise ValueError("invalid pilot identity") from None
        if str(parsed) != value:
            raise ValueError("invalid pilot identity")
        return value


class ContainerPilotEvidence(_WireModel):
    container_id: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    image_config_id: str | None = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    health: Literal["unknown", "healthy", "unhealthy"]
    evidence_verified: bool


class ContainerPilotResponse(ContainerPilotRequest):
    state: Literal["pending", "running", "succeeded", "failed", "held"]
    reason: Literal[
        "inspect_required", "paused", "target_busy", "stale_fence", "identity_conflict",
        "manifest_mismatch", "policy_mismatch", "image_unavailable", "daemon_unavailable",
        "container_conflict", "evidence_mismatch", "health_failed", "effect_failed",
        "recovery_required",
    ] | None
    evidence: ContainerPilotEvidence
    replayed: bool


class ContainerPilotClientError(Exception):
    """Safe public error without endpoint, response or credential reflection."""


class UnknownOutcome(ContainerPilotClientError):
    def __init__(self, command_id: str):
        self.command_id = command_id
        super().__init__("container pilot outcome unknown; inspect original command")


class HeldProtocolError(ContainerPilotClientError):
    state = "held"

    def __init__(self, command_id: str):
        self.command_id = command_id
        super().__init__("container pilot protocol verification failed; command held")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _validate_credential_and_timeout(service_token, timeout_seconds, maximum):
    if (not isinstance(service_token, str) or not service_token
            or any(not 33 <= ord(character) <= 126 for character in service_token)):
        raise ValueError("invalid container pilot credential")
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= maximum:
        raise ValueError("container pilot timeout must be positive and bounded")


class ContainerPilotClient:
    """Explicit bounded operations, separate from the existing fixture executor."""

    RESPONSE_BYTE_LIMIT = 16 * 1024
    # The host may use the manifest's complete 120-second command budget.
    # Transport setup and the bounded response need their own finite margin.
    TRANSPORT_MARGIN_SECONDS = 5.0
    MAX_TIMEOUT_SECONDS = 125.0

    def __init__(self, base_url: str, service_token: str, *, timeout_seconds: float = 125.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        if (not isinstance(base_url, str) or "\\" in base_url
                or any(ord(character) <= 32 or ord(character) == 127 for character in base_url)):
            raise ValueError("invalid container pilot origin")
        try:
            parsed = urlsplit(base_url)
            parsed.port  # Validate malformed ports without printing the origin.
        except (ValueError, TypeError):
            raise ValueError("invalid container pilot origin") from None
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("invalid container pilot origin")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError("container pilot requires HTTPS except literal loopback")
        _validate_credential_and_timeout(service_token, timeout_seconds, self.MAX_TIMEOUT_SECONDS)
        self.__token = service_token
        self.__timeout_seconds = timeout_seconds
        self.__client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {service_token}", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0)),
            follow_redirects=False, trust_env=False, transport=transport,
        )

    @classmethod
    def for_verified_https(cls, origin, service_token, ca_pem, *, timeout_seconds=125.0):
        """Inert dedicated HTTPS client using only explicitly captured CA bytes.

        No environment CA/proxy/key-log discovery, redirects or transport retries.
        The caller must independently provision and pin those CA bytes and the
        endpoint hostname. Trust in a CA is not host/executor enrollment proof.
        """
        try:
            parsed = urlsplit(origin)
            if (type(origin) is not str or parsed.scheme != 'https' or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path or parsed.query
                    or parsed.fragment or origin != 'https://' + parsed.netloc
                    or not origin.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in origin)
                    or '\\' in origin):
                raise ValueError()
            parsed.port
            _validate_credential_and_timeout(service_token, timeout_seconds, cls.MAX_TIMEOUT_SECONDS)
            if type(ca_pem) is not bytes or not 0 < len(ca_pem) <= 1024 * 1024:
                raise ValueError()
            # create_default_context() can honor SSLKEYLOGFILE. A bare client
            # context does not discover environment key logging or default CAs.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_verify_locations(cadata=ca_pem.decode('ascii'))
        except Exception:
            raise ValueError('Invalid verified executor HTTPS configuration') from None
        transport = httpx.AsyncHTTPTransport(verify=context, trust_env=False, retries=0)
        return cls(origin, service_token, timeout_seconds=timeout_seconds, transport=transport)

    @classmethod
    def for_private_unix(cls, path, service_token, *, timeout_seconds=125.0):
        """Create an inert dedicated Linux UDS client; caller owns ``aclose``.

        Requires a current-UID mode0700 parent, mode0600 socket and trusted
        no-follow ancestors. Path checks are not kernel peer authentication or
        isolation from same-UID/root mutation after validation. This is a local
        connection, not a remote transport or endpoint-discovery mechanism.
        There is no TCP fallback, environment proxy, retry or injected callback.
        The loopback URL is only the HTTP origin over the explicit Unix socket;
        the existing bearer, response identity and whole deadline remain in use.
        """
        from .private_browser_socket import (
            _path, _private_parent, _socket_entry, _check_parent,
        )
        # Deny bad credentials/budgets before allocating the owned transport.
        _validate_credential_and_timeout(service_token, timeout_seconds, cls.MAX_TIMEOUT_SECONDS)
        fd = None
        try:
            target = _path(path)
            fd, parent_identity = _private_parent(target)
            entry = _socket_entry(target, fd)
            _check_parent(target, fd, parent_identity)
            _socket_entry(target, fd, entry)
            transport = httpx.AsyncHTTPTransport(uds=str(target), retries=0, trust_env=False)
            return cls("http://127.0.0.1", service_token,
                       timeout_seconds=timeout_seconds, transport=transport)
        except Exception:
            raise ValueError("invalid private container pilot socket") from None
        finally:
            if fd is not None:
                os.close(fd)

    def __repr__(self):
        return "ContainerPilotClient(credential=<redacted>)"

    @property
    def timeout_seconds(self) -> float:
        """Whole-operation deadline, including connection and response transport."""
        return self.__timeout_seconds

    async def __aenter__(self):
        await self.__client.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.__client.__aexit__(*args)

    async def aclose(self):
        await self.__client.aclose()

    async def _command(self, method: str, path: str, expected: ContainerPilotRequest,
                       *, submit: bool = False) -> ContainerPilotResponse:
        if type(expected) is not ContainerPilotRequest:
            raise TypeError("expected a container pilot request")
        kwargs = {"json": expected.model_dump()} if submit else {}
        try:
            # HTTPX read timeouts bound each chunk; this also bounds a slow-drip
            # response across the complete operation, not just idle periods.
            async with asyncio.timeout(self.__timeout_seconds):
                async with self.__client.stream(method, path, **kwargs) as response:
                    if not 200 <= response.status_code < 300:
                        raise UnknownOutcome(expected.command_id)
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > self.RESPONSE_BYTE_LIMIT:
                            raise HeldProtocolError(expected.command_id)
                        content.extend(chunk)
        except (httpx.RequestError, TimeoutError):
            raise UnknownOutcome(expected.command_id) from None
        try:
            text = bytes(content).decode("utf-8")
            if self.__token in text:
                raise ValueError
            data = json.loads(text, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
            if self.__token in json.dumps(data, ensure_ascii=False):
                raise ValueError
            result = ContainerPilotResponse.model_validate(data)
        except (ValueError, UnicodeError, RecursionError, ValidationError):
            raise HeldProtocolError(expected.command_id) from None
        if result.model_dump(include=set(ContainerPilotRequest.model_fields)) != expected.model_dump():
            raise HeldProtocolError(expected.command_id)
        return result

    async def submit_command(self, command: ContainerPilotRequest) -> ContainerPilotResponse:
        return await self._command("POST", "/v1/container-pilot/commands", command, submit=True)

    async def get_command(self, command: ContainerPilotRequest) -> ContainerPilotResponse:
        return await self._command("GET", f"/v1/container-pilot/commands/{command.command_id}", command)

    async def reconcile_command(self, command: ContainerPilotRequest) -> ContainerPilotResponse:
        """Bodyless inspection only; never restart an exited workload or replay an effect."""
        return await self._command("POST", f"/v1/container-pilot/commands/{command.command_id}/reconcile", command)
