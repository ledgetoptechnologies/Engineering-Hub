import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import COMMAND_FIELDS, ExecutorError, HubExecutor


MAX_BODY_BYTES = 8192
COMMAND_PATH = re.compile(r"^/v1/commands/([0-9a-f-]{36})$")
RECONCILE_PATH = re.compile(r"^/v1/commands/([0-9a-f-]{36})/reconcile$")


class _ExecutorHandler(BaseHTTPRequestHandler):
    executor: HubExecutor
    expected_authorization: str

    def log_message(self, format, *args):
        return

    def _send(self, status, value):
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status, code):
        self._send(status, {"error": {"code": code}})

    def _authorized(self):
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(
            supplied.encode("utf-8"), self.expected_authorization.encode("utf-8")
        )

    def _read_json(self, expected_fields):
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdecimal() or int(raw_length) > MAX_BODY_BYTES:
            raise ValueError("invalid_payload")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("invalid_payload")
        raw = self.rfile.read(int(raw_length))

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("invalid_payload")
                value[key] = item
            return value

        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise ValueError("invalid_payload")
        return value

    def _dispatch(self, payload):
        try:
            result = self.executor.execute_command(payload)
        except ExecutorError as error:
            self._error(error.status, error.code)
            return
        except Exception:
            self._error(500, "internal_error")
            return
        status = 200 if result.get("replayed") else 201
        self._send(status, result)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
            return
        if not self._authorized():
            self._error(401, "unauthorized")
            return
        match = COMMAND_PATH.fullmatch(self.path)
        if match:
            try:
                result = self.executor.get_command(match.group(1))
            except ExecutorError as error:
                self._error(error.status, error.code)
                return
            if result is None:
                self._error(404, "command_not_found")
            else:
                self._send(200, result)
            return
        self._error(404, "command_not_found")

    def do_POST(self):
        if not self._authorized():
            self._error(401, "unauthorized")
            return
        if self.path == "/v1/pause":
            try:
                payload = self._read_json({"paused"})
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._error(400, "invalid_payload")
                return
            if type(payload["paused"]) is not bool:
                self._error(400, "invalid_payload")
                return
            try:
                result = self.executor.set_paused(payload["paused"])
            except ExecutorError as error:
                self._error(error.status, error.code)
                return
            except Exception:
                self._error(500, "internal_error")
                return
            self._send(200, result)
            return

        reconcile_match = RECONCILE_PATH.fullmatch(self.path)
        if reconcile_match:
            if self.headers.get("Content-Length", "0") not in {"", "0"}:
                self._error(400, "invalid_payload")
                return
            try:
                result = self.executor.reconcile_command(reconcile_match.group(1))
            except ExecutorError as error:
                self._error(error.status, error.code)
                return
            except Exception:
                self._error(500, "internal_error")
                return
            self._send(200, result)
            return

        if self.path != "/v1/commands":
            self._error(404, "command_not_found")
            return
        try:
            payload = self._read_json(COMMAND_FIELDS)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._error(400, "invalid_payload")
            return
        self._dispatch(payload)

    def _unsupported_method(self):
        if not self._authorized():
            self._error(401, "unauthorized")
        else:
            self._error(404, "command_not_found")

    def do_PUT(self):
        self._unsupported_method()

    def do_PATCH(self):
        self._unsupported_method()

    def do_DELETE(self):
        self._unsupported_method()

    def do_OPTIONS(self):
        self._unsupported_method()

    def do_HEAD(self):
        self._unsupported_method()


def make_server(executor, token, *, host="127.0.0.1", port=0):
    if not isinstance(token, str) or not token:
        raise ValueError("configured bearer token is required")
    handler = type(
        "BoundExecutorHandler",
        (_ExecutorHandler,),
        {"executor": executor, "expected_authorization": f"Bearer {token}"},
    )
    return ThreadingHTTPServer((host, port), handler)
