"""One-shot synchronous HTTPS transport, not executor identity authority.

Only explicitly enrolled literal-IP origins, CA bytes and dedicated executor
credentials are used. The signed result still requires CurrentExecutorObserver.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from .current_executor_observation import MAX_OBSERVATION_BYTES, _strict
from .unified_pilot_startup import read_protected_file

PATH = "/v1/container-pilot/current-observation"
MAX_HEADER_BYTES = 8192
MAX_HEADER_LINES = 64
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_HEADER = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[\x20-\x7e\t]*\r\n\Z")


class CurrentExecutorTransportError(RuntimeError):
    """Sanitized transport denial, without endpoints, credentials or wire data."""


def _require(condition):
    if not condition:
        raise CurrentExecutorTransportError("Current executor transport unavailable")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    _require(math.isfinite(remaining) and remaining > 0)
    return remaining


class _DeadlineRaw(io.RawIOBase):
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def readable(self):
        return True

    def readinto(self, target):
        self.sock.settimeout(_remaining(self.deadline))
        size = self.sock.recv_into(target)
        _remaining(self.deadline)
        return size


class _DeadlineReader(io.BufferedReader):
    def __init__(self, sock, deadline):
        super().__init__(_DeadlineRaw(sock, deadline), buffer_size=1024)
        self.header_bytes = self.header_lines = 0
        self.in_headers = True

    def readline(self, size=-1):
        # HTTPResponse remains the status/header parser. Bound its raw input,
        # disallow obsolete folded/malformed fields before email parsing.
        line = super().readline(min(size if size >= 0 else MAX_HEADER_BYTES + 1,
                                    MAX_HEADER_BYTES + 1))
        if self.in_headers:
            self.header_bytes += len(line)
            self.header_lines += 1
            _require(self.header_bytes <= MAX_HEADER_BYTES
                     and self.header_lines <= MAX_HEADER_LINES
                     and line.endswith(b"\r\n"))
            if self.header_lines > 1 and line != b"\r\n":
                _require(_HEADER.fullmatch(line) is not None)
        return line


class _ResponseSocket:
    def __init__(self, reader):
        self.reader = reader

    def makefile(self, mode):
        _require(mode == "rb")
        return self.reader


class _Response(http.client.HTTPResponse):
    def _read_status(self):
        version, status, reason = super()._read_status()
        # Do not process informational responses or follow redirects.
        _require(status == 200 and version in {"HTTP/1.0", "HTTP/1.1"})
        return version, status, reason


class CurrentExecutorHTTPSTransport:
    """Fresh socket per call, no retry, proxy, discovery, redirect or event loop.

    The caller must independently enroll the dedicated executor credential;
    this class cannot prove that arbitrary input bytes are not an agent token.
    Protected files are captured once at construction, not reopened during TLS.
    """
    def __init__(self, origin, *, ca_file, ca_sha256,
                 credential_file, credential_sha256):
        try:
            _require(type(origin) is str and origin.isascii()
                     and "\\" not in origin
                     and all(32 < ord(c) < 127 for c in origin))
            parsed = urlsplit(origin)
            _require(parsed.scheme == "https" and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.path and not parsed.query and not parsed.fragment
                     and origin == "https://" + parsed.netloc)
            address = ipaddress.ip_address(parsed.hostname)
            _require(getattr(address, "scope_id", None) is None)
            port = parsed.port if parsed.port is not None else 443
            _require(0 < port <= 65535)
            _require(all(type(pin) is str and _SHA.fullmatch(pin)
                         for pin in (ca_sha256, credential_sha256)))
            _require(Path(ca_file) != Path(credential_file))
            ca = read_protected_file(Path(ca_file), maximum_bytes=1024 * 1024, private=False)
            credential = read_protected_file(Path(credential_file), maximum_bytes=4096)
            _require(hmac.compare_digest("sha256:" + hashlib.sha256(ca).hexdigest(), ca_sha256)
                     and hmac.compare_digest("sha256:" + hashlib.sha256(credential).hexdigest(),
                                             credential_sha256)
                     and credential and all(33 <= c <= 126 for c in credential))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_verify_locations(cadata=ca.decode("ascii"))
            self._family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            self._address, self._port = str(address), port
            self._host_header = parsed.netloc
            self.__credential = credential
            self._context = context
            self._pid = os.getpid()
        except Exception:
            raise CurrentExecutorTransportError("Invalid current executor HTTPS configuration") from None

    def __repr__(self):
        return "CurrentExecutorHTTPSTransport(credential=<redacted>)"

    def observe_current_launch(self, *, nonce, timeout_ms):
        """Return canonical envelope bytes; do not trust their identity here."""
        raw_sock = tls_sock = reader = response = None
        try:
            _require(self._pid == os.getpid() and type(nonce) is str and _NONCE.fullmatch(nonce)
                     and type(timeout_ms) is int and 1 <= timeout_ms <= 10000)
            deadline = time.monotonic() + timeout_ms / 1000
            body = json.dumps({"nonce": nonce}, separators=(",", ":")).encode("ascii")
            request = (b"POST " + PATH.encode("ascii") + b" HTTP/1.1\r\nHost: "
                       + self._host_header.encode("ascii") + b"\r\nAuthorization: Bearer "
                       + self.__credential + b"\r\nAccept: application/json\r\n"
                       b"Content-Type: application/json\r\nConnection: close\r\nContent-Length: "
                       + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
            # No getaddrinfo: even a literal address is passed directly to the
            # selected family socket. No alternative address retries exist.
            raw_sock = socket.socket(self._family, socket.SOCK_STREAM)
            raw_sock.settimeout(_remaining(deadline))
            raw_sock.connect((self._address, self._port))
            raw_sock.settimeout(_remaining(deadline))
            tls_sock = self._context.wrap_socket(raw_sock, server_hostname=self._address)
            tls_sock.settimeout(_remaining(deadline))
            tls_sock.sendall(request)
            _remaining(deadline)
            reader = _DeadlineReader(tls_sock, deadline)
            response = _Response(_ResponseSocket(reader), method="POST")
            response.begin()
            reader.in_headers = False
            fields = response.getheaders()
            lowered = [name.lower() for name, _ in fields]
            _require(len(lowered) == len(set(lowered)))
            headers = {name.lower(): value for name, value in fields}
            _require("transfer-encoding" not in headers and "content-encoding" not in headers
                     and headers.get("content-type", "").lower() == "application/json"
                     and "content-length" in headers)
            length = headers["content-length"]
            _require(re.fullmatch(r"[1-9][0-9]{0,3}", length) is not None)
            length = int(length)
            _require(length <= MAX_OBSERVATION_BYTES)
            # Read through the deadline wrapper rather than HTTPResponse.read,
            # which closes its file at Content-Length and hides trailing bytes.
            result = reader.read(length)
            _require(len(result) == length and reader.read(1) == b"")
            value = _strict(result)
            _require(type(value) is dict and set(value) == {"payload", "signature"})
            _remaining(deadline)
            return result
        except Exception:
            raise CurrentExecutorTransportError("Current executor transport unavailable") from None
        finally:
            if response is not None:
                response.close()
            elif reader is not None:
                reader.close()
            if tls_sock is not None:
                tls_sock.close()
            if raw_sock is not None:
                raw_sock.close()
