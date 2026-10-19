"""Authenticated optional ASGI factory; no module-level app or listener."""
from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import re
import threading
from contextvars import copy_context
from uuid import UUID

from engineering_hub.container_pilot_client import ContainerPilotRequest, ContainerPilotResponse
from engineering_hub.owned_tasks import finish_owned
from scripts.inspect_hub_image_identity import IdentityError, strict_json

MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 16 * 1024
COMMANDS = "/v1/container-pilot/commands"
_COMMAND_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class PilotASGI:
    def __init__(self, adapter, bearer_token: str, *, request_body_timeout_seconds: float = 2.0):
        if (not isinstance(bearer_token, str) or not bearer_token
                or any(not 33 <= ord(char) <= 126 for char in bearer_token)):
            raise ValueError("configured container-pilot bearer credential is required")
        if (isinstance(request_body_timeout_seconds, bool)
                or not isinstance(request_body_timeout_seconds, (int, float))
                or not 0 < request_body_timeout_seconds <= 60
                or not math.isfinite(request_body_timeout_seconds)):
            raise ValueError("request body deadline must be finite and in (0, 60] seconds")
        self._adapter = adapter
        self._authorization = ("Bearer " + bearer_token).encode("ascii")
        self._request_body_timeout_seconds = float(request_body_timeout_seconds)
        self._pid = os.getpid()
        # No waiting admission queue and no executor work during construction.
        # The non-loop-bound gate also protects explicit cross-loop callers.
        self._operation = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return
        method = scope.get("method", "")
        path = scope.get("path", "")
        if scope.get("query_string", b""):
            await self._error(send, 404, "command_not_found")
            return
        headers = scope.get("headers", [])
        auth_values = [value for key, value in headers if key.lower() == b"authorization"]
        if len(auth_values) != 1 or not hmac.compare_digest(auth_values[0], self._authorization):
            await self._error(send, 401, "unauthorized")
            return

        if method == "POST" and path == COMMANDS:
            await self._submit(receive, send, headers)
            return
        if method == "GET" and path.startswith(COMMANDS + "/"):
            command_id = path[len(COMMANDS) + 1:]
            if not self._valid_id(command_id):
                await self._error(send, 400, "invalid_payload")
                return
            try:
                body = await self._read_body(receive, headers, allow_missing_length=True)
            except TimeoutError:
                await self._error(send, 408, "request_timeout")
                return
            except OverflowError:
                await self._error(send, 413, "invalid_payload")
                return
            except ValueError:
                await self._error(send, 400, "invalid_payload")
                return
            if body:
                await self._error(send, 400, "invalid_payload")
                return
            accepted, response = await self._invoke(send, self._adapter.get, command_id)
            if not accepted:
                return
            if response is None:
                await self._error(send, 404, "command_not_found")
            else:
                await self._response(send, 200, response)
            return
        suffix = "/reconcile"
        if method == "POST" and path.startswith(COMMANDS + "/") and path.endswith(suffix):
            command_id = path[len(COMMANDS) + 1:-len(suffix)]
            if not self._valid_id(command_id):
                await self._error(send, 400, "invalid_payload")
                return
            try:
                body = await self._read_body(receive, headers, allow_missing_length=True)
            except TimeoutError:
                await self._error(send, 408, "request_timeout")
                return
            except OverflowError:
                await self._error(send, 413, "invalid_payload")
                return
            except ValueError:
                await self._error(send, 400, "invalid_payload")
                return
            if body:
                await self._error(send, 400, "invalid_payload")
                return
            accepted, response = await self._invoke(send, self._adapter.reconcile, command_id)
            if not accepted:
                return
            if response is None:
                await self._error(send, 404, "command_not_found")
            else:
                await self._response(send, 200, response)
            return
        await self._error(send, 404, "command_not_found")

    async def _submit(self, receive, send, headers):
        content_types = [value for key, value in headers if key.lower() == b"content-type"]
        if (len(content_types) != 1
                or content_types[0].decode("latin-1").split(";", 1)[0].strip().lower() != "application/json"):
            await self._error(send, 400, "invalid_payload")
            return
        try:
            raw = await self._read_body(receive, headers, allow_missing_length=False)
            value = strict_json(raw)
            if not isinstance(value, dict) or set(value) != set(ContainerPilotRequest.model_fields):
                raise ValueError
            request = ContainerPilotRequest.model_validate(value)
        except TimeoutError:
            await self._error(send, 408, "request_timeout")
            return
        except OverflowError:
            await self._error(send, 413, "invalid_payload")
            return
        except (IdentityError, ValueError, TypeError, UnicodeError, RecursionError):
            await self._error(send, 400, "invalid_payload")
            return
        accepted, response = await self._invoke(send, self._adapter.submit, request)
        if not accepted:
            return
        await self._response(send, 200, response)

    async def _invoke(self, send, method, argument):
        # Never touch a mutex inherited from another process. A fork must build
        # its own explicitly enrolled adapter/app, not adopt accepted work.
        if os.getpid() != self._pid:
            await self._error(send, 503, "executor_unavailable")
            return False, None
        if not self._operation.acquire(blocking=False):
            await self._error(send, 503, "command_busy")
            return False, None
        try:
            # Use the raw executor Future, not an abandonable to_thread Task:
            # finish_owned shields accepted work through repeated cancellation.
            # Keep the gate until the underlying call has actually terminated.
            context = copy_context()
            future = asyncio.get_running_loop().run_in_executor(None, context.run, method, argument)
            return True, await finish_owned(future)
        finally:
            self._operation.release()

    async def _read_body(self, receive, headers, *, allow_missing_length: bool):
        deadline = asyncio.get_running_loop().time() + self._request_body_timeout_seconds
        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        transfers = [value for key, value in headers if key.lower() == b"transfer-encoding"]
        if transfers or len(lengths) > 1 or (not lengths and not allow_missing_length):
            raise ValueError("invalid request framing")
        expected = None
        if lengths:
            try:
                text = lengths[0].decode("ascii")
                if not text.isdecimal():
                    raise ValueError
                expected = int(text)
            except (ValueError, UnicodeError):
                raise ValueError("invalid request framing") from None
            if expected > MAX_REQUEST_BYTES:
                raise OverflowError
        body = bytearray()
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("request body deadline expired")
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError:
                raise TimeoutError("request body deadline expired") from None
            if message.get("type") == "http.disconnect":
                raise ValueError("request disconnected")
            if message.get("type") != "http.request":
                raise ValueError("invalid ASGI request event")
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                raise ValueError("invalid body")
            if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                raise OverflowError
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        if expected is not None and len(body) != expected:
            raise ValueError("content length mismatch")
        return bytes(body)

    @staticmethod
    def _valid_id(value: str) -> bool:
        if not _COMMAND_ID.fullmatch(value):
            return False
        try:
            return str(UUID(value)) == value
        except ValueError:
            return False

    @staticmethod
    async def _response(send, status: int, response: ContainerPilotResponse):
        raw = json.dumps(response.model_dump(mode="json"), sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > MAX_RESPONSE_BYTES:
            await PilotASGI._error(send, 500, "internal_error")
            return
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(raw)).encode("ascii")),
                                (b"cache-control", b"no-store"),
                                (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": raw})

    @staticmethod
    async def _error(send, status: int, code: str):
        raw = json.dumps({"error": {"code": code}}, separators=(",", ":")).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(raw)).encode("ascii")),
                                (b"cache-control", b"no-store"),
                                (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": raw})


def create_app(adapter, *, bearer_token: str, request_body_timeout_seconds: float = 2.0) -> PilotASGI:
    """Return an ASGI callable without binding a socket or starting a task."""
    return PilotASGI(
        adapter, bearer_token, request_body_timeout_seconds=request_body_timeout_seconds
    )
