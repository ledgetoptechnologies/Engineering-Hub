"""Authenticated optional ASGI factory; no module-level app or listener."""
from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import math
import os
import re
import threading
import time
from contextvars import copy_context
from uuid import UUID

from engineering_hub.container_pilot_client import ContainerPilotRequest, ContainerPilotResponse
from engineering_hub.owned_tasks import finish_owned
from scripts.inspect_hub_image_identity import IdentityError, strict_json

MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 16 * 1024
COMMANDS = "/v1/container-pilot/commands"
CURRENT_OBSERVATION = "/v1/container-pilot/current-observation"
_COMMAND_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class PilotASGI:
    def __init__(self, adapter, bearer_token: str, *, request_body_timeout_seconds: float = 2.0,
                 current_observation_transport=None, current_observation_timeout_ms=2000):
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
        observe = getattr(current_observation_transport, 'observe_current_launch', None)
        if current_observation_transport is not None and (
                not callable(observe) or inspect.iscoroutinefunction(observe)
                or inspect.isasyncgenfunction(observe)
                or inspect.iscoroutinefunction(getattr(observe, '__call__', None))
                or inspect.isasyncgenfunction(getattr(observe, '__call__', None))):
            raise ValueError('synchronous current observation transport required')
        if type(current_observation_timeout_ms) is not int or not 100 <= current_observation_timeout_ms <= 10000:
            raise ValueError('bounded current observation deadline required')
        self._observe_current = observe
        self._observation_timeout_ms = current_observation_timeout_ms
        self._observation_operation = threading.Lock()

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

        if method == 'POST' and path == CURRENT_OBSERVATION and self._observe_current is not None:
            await self._observation(receive, send, headers)
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

    async def _observation(self, receive, send, headers):
        # The wire supplies only a nonce, never identity selectors or budgets.
        deadline = time.monotonic() + self._observation_timeout_ms / 1000
        content_types = [value for key, value in headers if key.lower() == b'content-type']
        if len(content_types) != 1 or content_types[0].split(b';', 1)[0].strip().lower() != b'application/json':
            await self._error(send, 400, 'invalid_payload')
            return
        try:
            raw = await self._read_body(receive, headers, allow_missing_length=False,
                deadline=deadline, byte_limit=256)
            value = strict_json(raw)
            if (type(value) is not dict or set(value) != {'nonce'}
                    or type(value['nonce']) is not str
                    or not re.fullmatch('[0-9a-f]{64}', value['nonce'])):
                raise ValueError()
        except TimeoutError:
            await self._error(send, 408, 'request_timeout')
            return
        except OverflowError:
            await self._error(send, 413, 'invalid_payload')
            return
        except (IdentityError, ValueError, TypeError, UnicodeError, RecursionError):
            await self._error(send, 400, 'invalid_payload')
            return
        if os.getpid() != self._pid or not self._observation_operation.acquire(blocking=False):
            await self._error(send, 503, 'observation_unavailable')
            return
        try:
            def exchange():
                remaining = int((deadline - time.monotonic()) * 1000)
                if remaining < 1:
                    raise TimeoutError()
                response = self._observe_current(nonce=value['nonce'], timeout_ms=remaining)
                # A badly declared trusted callback cannot create an abandoned
                # coroutine or turn response claims into local identity trust.
                if inspect.iscoroutine(response) and inspect.getcoroutinestate(response) == inspect.CORO_CREATED:
                    response.close()
                if type(response) is not bytes or not 0 < len(response) <= 8192:
                    raise ValueError()
                from .launch_transport import _receipt_frame
                _receipt_frame(response)
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                return response
            context = copy_context()
            future = asyncio.get_running_loop().run_in_executor(None, context.run, exchange)
            raw = await finish_owned(future)
            # Worker completion does not bound delayed event-loop resumption.
            # Never report a late success after the original request budget.
            if time.monotonic() >= deadline:
                raise TimeoutError()
        except Exception:
            await self._error(send, 503, 'observation_unavailable')
            return
        finally:
            self._observation_operation.release()
        await send({'type': 'http.response.start', 'status': 200, 'headers': [
            (b'content-type', b'application/json'),
            (b'content-length', str(len(raw)).encode('ascii')), (b'cache-control', b'no-store'),
            (b'connection', b'close'),
            (b'x-content-type-options', b'nosniff')]})
        await send({'type': 'http.response.body', 'body': raw})

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

    async def _read_body(self, receive, headers, *, allow_missing_length: bool,
                         deadline=None, byte_limit=MAX_REQUEST_BYTES):
        body_deadline = time.monotonic() + self._request_body_timeout_seconds
        deadline = body_deadline if deadline is None else min(deadline, body_deadline)
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
            if expected > byte_limit:
                raise OverflowError
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
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
            if len(body) + len(chunk) > byte_limit:
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


def create_app(adapter, *, bearer_token: str, request_body_timeout_seconds: float = 2.0,
               current_observation_transport=None, current_observation_timeout_ms=2000) -> PilotASGI:
    """Return an ASGI callable without binding a socket or starting a task."""
    return PilotASGI(
        adapter, bearer_token, request_body_timeout_seconds=request_body_timeout_seconds,
        current_observation_transport=current_observation_transport,
        current_observation_timeout_ms=current_observation_timeout_ms,
    )
