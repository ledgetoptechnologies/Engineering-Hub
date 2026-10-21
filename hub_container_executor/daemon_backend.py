"""Concrete, operator-pinned Docker Engine backend; no implicit daemon discovery.

The HTTP surface is bound to one explicitly configured Unix socket and API
version. All transport effects occur only when a typed method is called. Fake
transports are accepted solely through the explicit ``test_transport`` keyword.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

import httpx

from engineering_hub.container_pilot_manifest import (
    MIN_CGROUPNS_ENGINE_API, PROBE_PROFILE, Platform, Probe,
)

from .backend import (
    ActualImageSelection,
    ContainerObservation,
    ContainerPlan,
    EffectiveProfile,
    ImageDefaults,
    OperationObservation,
    ProbeObservation,
    RunnerObservation,
)
from .build_record import VerifiedImage

API_RE = re.compile(r"1\.(?:0|[1-9][0-9]{0,2})\Z")
OPAQUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
EXEC_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_CONFIG_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
_MAX_JSON = 1024 * 1024
_MAX_ARCHIVE = 2 * 1024**3
_MAX_ARCHIVE_METADATA = 16 * 1024**2
_MAX_METADATA_ENTRY = 1024 * 1024
_MAX_TAR_ENTRIES = 20_000
_MAX_INDEX_DEPTH = 8
_MAX_DESCRIPTORS = 64
_MAX_PROBES = 3
_MAX_EXEC_OUTPUT = 64 * 1024
_FIXED_TMPFS_SIZE_MAX = 256 * 1024**2


class ArchiveMetadataError(ValueError):
    """Malformed or unsupported local daemon archive metadata."""


def _parse_metadata_json(raw):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ArchiveMetadataError("duplicate metadata key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ArchiveMetadataError("non-finite metadata number")

    try:
        return json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ArchiveMetadataError("invalid metadata JSON") from None


_PROBE_PROFILE = PROBE_PROFILE
_PROBE_SCRIPT = r'''import html.parser,json,os,re,sys,time,urllib.error,urllib.parse,urllib.request
for _name in ("CODEX_TOKEN","HERMES_TOKEN","CLAUDE_TOKEN"):
    os.environ.pop(_name,None)
_KIND,_PATH,_EXPECTED,_EXPECTATION,_MAX_BYTES,_TIMEOUT,_SOURCE_COMMIT=sys.argv[1:]
if not re.fullmatch(r"[0-9a-f]{40}",_SOURCE_COMMIT): raise SystemExit(2)
_EXPECTED=int(_EXPECTED); _MAX_BYTES=int(_MAX_BYTES); _TIMEOUT=int(_TIMEOUT)
_DEADLINE=time.monotonic()+_TIMEOUT/1000
_BASE="http://127.0.0.1:8770"
_OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def _fetch(path):
    if (not path.startswith("/") or path.startswith("//") or "\\\\" in path
        or any(part in (".","..") for part in path.split("/"))):
        return 0,b""
    remaining=_DEADLINE-time.monotonic()
    if remaining<=0:
        return 0,b""
    request=urllib.request.Request(_BASE+path,headers={"Accept":"application/json,text/html,*/*"},method="GET")
    try:
        response=_OPENER.open(request,timeout=max(.001,remaining))
    except urllib.error.HTTPError as error:
        response=error
    except Exception:
        return 0,b""
    try:
        body=response.read(_MAX_BYTES+1)
        return response.getcode(),body
    except Exception:
        return 0,b""
    finally:
        try: response.close()
        except Exception: pass

def _run():
    if _KIND=="health_json":
        status,body=_fetch(_PATH)
        try: value=json.loads(body.decode("utf-8"))
        except Exception: value=None
        ok=(status==_EXPECTED and len(body)<=_MAX_BYTES and type(value) is dict
            and set(value)=={"ok","service","build_commit"} and value["ok"] is True
            and value["service"]=="engineering-hub-control" and value["build_commit"]==_SOURCE_COMMIT)
        return status,ok
    if _KIND=="auth_denial":
        status,body=_fetch(_PATH)
        return status,(status==401 and len(body)<=_MAX_BYTES)
    if _KIND!="application_assets":
        return 0,False
    status,body=_fetch(_PATH)
    if status!=_EXPECTED or len(body)>_MAX_BYTES:
        return status,False
    try: document=body.decode("utf-8")
    except UnicodeError: return status,False
    class _Assets(html.parser.HTMLParser):
        def __init__(self):
            super().__init__(); self.title=False; self.title_text=[]; self.paths=[]
        def handle_starttag(self,tag,attrs):
            values=dict(attrs)
            if tag=="title": self.title=True
            if tag=="script" and "src" in values: self.paths.append(values["src"])
            if tag=="link" and "href" in values:
                rel=set((values.get("rel") or "").lower().split())
                if rel.intersection({"stylesheet","icon","manifest"}): self.paths.append(values["href"])
        def handle_endtag(self,tag):
            if tag=="title": self.title=False
        def handle_data(self,data):
            if self.title: self.title_text.append(data)
    parser=_Assets()
    try: parser.feed(document); parser.close()
    except Exception: return status,False
    title="".join(parser.title_text)
    if "Engineering Hub" not in title or not parser.paths or len(parser.paths)>16:
        return status,False
    for path in parser.paths:
        parsed=urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return status,False
        asset_status,asset_body=_fetch(parsed.path)
        if asset_status!=200 or len(asset_body)>_MAX_BYTES:
            return status,False
    return status,True

try:
    _status,_ok=_run()
    print(json.dumps({"expectation_id":_EXPECTATION,"ok":bool(_ok),"status":int(_status)},separators=(",",":")))
    raise SystemExit(0 if _ok else 1)
except Exception:
    raise SystemExit(2)
'''


class DaemonBackendError(RuntimeError):
    """Safe backend error; never includes daemon bodies, paths, or environment."""


@dataclass(frozen=True)
class DockerEngineBinding:
    """Operator-enrolled local daemon binding; every authority field is explicit."""

    runner_id: str
    daemon_identity: str
    platform: Platform
    unix_socket_path: str
    api_version: str
    generation_root: str

    def __post_init__(self):
        if (type(self.runner_id) is not str or not OPAQUE_RE.fullmatch(self.runner_id)
                or type(self.daemon_identity) is not str
                or not OPAQUE_RE.fullmatch(self.daemon_identity)):
            raise ValueError("invalid operator daemon identity binding")
        if type(self.platform) is not Platform:
            raise TypeError("explicit shared Platform binding required")
        if type(self.api_version) is not str or not API_RE.fullmatch(self.api_version):
            raise ValueError("explicit Docker API version required")
        if tuple(int(part) for part in self.api_version.split(".")) < MIN_CGROUPNS_ENGINE_API:
            raise ValueError("Docker API version does not support private cgroup namespaces")
        for value in (self.unix_socket_path, self.generation_root):
            if (type(value) is not str or not os.path.isabs(value)
                    or ".." in Path(value).parts or "\x00" in value):
                raise ValueError("absolute operator path required")
        if self.unix_socket_path == self.generation_root:
            raise ValueError("daemon socket and generation root must differ")


class TrustedPrivateOperationJournal(Protocol):
    """Optional operator-owned durable journal; never inferred from daemon labels.

    Implementations must be private, durable and independent of container labels.
    Methods receive no secret values or request bodies.
    """

    def begin(self, operation_id: str, kind: str) -> None: ...

    def completed(self, operation_id: str, kind: str,
                  container_id: str | None = None) -> None: ...

    def inspect(self, operation_id: str) -> OperationObservation | None: ...


class _ChunkReader:
    """Small bounded reader over HTTPX raw chunks; no archive extraction."""

    def __init__(self, chunks, limit: int, deadline: float | None = None):
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._limit = limit
        self._deadline = deadline
        self._total = 0
        self._eof = False

    def read(self, size=-1):
        if size < 0:
            size = self._limit - self._total + 1
        while not self._eof and len(self._buffer) < size:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise TimeoutError("archive deadline exceeded")
            try:
                chunk = next(self._chunks)
            except StopIteration:
                self._eof = True
                break
            self._total += len(chunk)
            if self._total > self._limit:
                raise ArchiveMetadataError("Archive byte limit exceeded")
            self._buffer.extend(chunk)
        amount = min(size, len(self._buffer))
        result = bytes(self._buffer[:amount])
        del self._buffer[:amount]
        return result


def _read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining:
        part = stream.read(remaining)
        if not part:
            raise ArchiveMetadataError("Truncated archive")
        chunks.append(part)
        remaining -= len(part)
    return b"".join(chunks)


def _archive_metadata(stream):
    """Read bounded OCI metadata from tar, skipping layer payloads in place."""
    entries = {}
    seen = set()
    metadata_total = 0
    for _ in range(_MAX_TAR_ENTRIES):
        header = _read_exact(stream, 512)
        if header == bytes(512):
            if _read_exact(stream, 512) != bytes(512):
                raise ArchiveMetadataError("Invalid tar terminator")
            while True:
                part = stream.read(65536)
                if not part:
                    return entries
                if any(part):
                    raise ArchiveMetadataError("Invalid trailing archive data")
        try:
            checksum = int(header[148:156].strip(b"\0 "), 8)
            size = int(header[124:136].strip(b"\0 ") or b"0", 8)
            name = header[:100].split(b"\0", 1)[0].decode("utf-8")
            prefix = header[345:500].split(b"\0", 1)[0].decode("utf-8")
        except (ValueError, UnicodeError):
            raise ArchiveMetadataError("Unsupported tar header") from None
        if checksum != sum(header[:148]) + 8 * 32 + sum(header[156:]):
            raise ArchiveMetadataError("Invalid tar checksum")
        if prefix:
            name = prefix + "/" + name
        name = name.removeprefix("./").rstrip("/")
        if (not name or name.startswith("/") or "\\" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))):
            raise ArchiveMetadataError("Unsafe archive path")
        if name in seen:
            raise ArchiveMetadataError("Duplicate archive entry")
        seen.add(name)
        kind = header[156:157]
        if kind not in {b"0", b"\0", b"5"} or size < 0:
            raise ArchiveMetadataError("Unsupported tar entry")
        if kind == b"5" and size:
            raise ArchiveMetadataError("Invalid directory entry")
        is_metadata = (name in {"oci-layout", "index.json", "manifest.json"}
                       or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name) is not None)
        if is_metadata and size <= _MAX_METADATA_ENTRY:
            metadata_total += size
            if metadata_total > _MAX_ARCHIVE_METADATA:
                raise ArchiveMetadataError("Metadata byte budget exceeded")
            entries[name] = _read_exact(stream, size)
        else:
            if (name.endswith(".json") or name in {"oci-layout", "index.json"}) and size > _MAX_METADATA_ENTRY:
                raise ArchiveMetadataError("Metadata entry exceeds limit")
            remaining = size
            while remaining:
                amount = min(remaining, 64 * 1024)
                _read_exact(stream, amount)
                remaining -= amount
        _read_exact(stream, (-size) % 512)
    raise ArchiveMetadataError("Archive entry limit exceeded")


def _strict_json(raw):
    try:
        value = _parse_metadata_json(raw)
        # Docker metadata contains integer fields only in every field we use.
        return value
    except (ArchiveMetadataError, RecursionError):
        raise DaemonBackendError("daemon metadata unavailable") from None


def _raw_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _archive_image(entries, image: VerifiedImage):
    """Return selected original config only after rechecking the full OCI chain."""
    layout_raw = entries.get("oci-layout")
    index_raw = entries.get("index.json")
    if layout_raw is None or index_raw is None:
        raise DaemonBackendError("daemon image archive lacks supported OCI metadata")
    layout = _strict_json(layout_raw)
    index = _strict_json(index_raw)
    if (not isinstance(layout, dict) or set(layout) != {"imageLayoutVersion"}
            or layout.get("imageLayoutVersion") != "1.0.0"):
        raise DaemonBackendError("daemon image archive layout is unsupported")
    if (not isinstance(index, dict) or set(index) - {"schemaVersion", "mediaType", "manifests"}
            or type(index.get("schemaVersion")) is not int or index.get("schemaVersion") != 2
            or index.get("mediaType") not in (None, *_INDEX_TYPES)
            or not isinstance(index.get("manifests"), list)):
        raise DaemonBackendError("daemon image archive index is invalid")
    descriptors = index["manifests"]
    if len(descriptors) != 1 or not isinstance(descriptors[0], dict):
        raise DaemonBackendError("daemon image archive selection is ambiguous")
    root_descriptor = descriptors[0]
    if set(root_descriptor) - {"mediaType", "digest", "size", "annotations"}:
        raise DaemonBackendError("daemon image archive descriptor is unsupported")
    if (root_descriptor.get("digest") != image.runnable_reference
            or root_descriptor.get("mediaType") != image.runnable_media_type):
        raise DaemonBackendError("daemon image archive root identity mismatch")
    root_size = root_descriptor.get("size")
    if type(root_size) is not int or root_size < 1 or root_size > _MAX_METADATA_ENTRY:
        raise DaemonBackendError("daemon image archive root size is invalid")

    visited = set()
    candidates = []
    descriptor_count = 0

    def load(identity, media_type, size=None):
        if type(identity) is not str or not DIGEST_RE.fullmatch(identity):
            raise DaemonBackendError("daemon image descriptor identity is invalid")
        if media_type not in _INDEX_TYPES | _MANIFEST_TYPES | _CONFIG_TYPES:
            raise DaemonBackendError("daemon image descriptor media type is unsupported")
        raw = entries.get("blobs/sha256/" + identity[7:])
        if (raw is None or _raw_digest(raw) != identity
                or (size is not None and len(raw) != size)):
            raise DaemonBackendError("daemon image metadata bytes are unavailable")
        return raw

    def visit(identity, media_type, size, depth, ancestry, declared_platform=None,
              from_index=False):
        nonlocal descriptor_count
        if identity in ancestry:
            raise DaemonBackendError("daemon image descriptor cycle")
        raw = load(identity, media_type, size)
        document = _strict_json(raw)
        if media_type in _INDEX_TYPES:
            if (depth > _MAX_INDEX_DEPTH or not isinstance(document, dict)
                    or set(document) - {"schemaVersion", "mediaType", "manifests"}
                    or type(document.get("schemaVersion")) is not int
                    or document.get("schemaVersion") != 2
                    or document.get("mediaType", media_type) != media_type
                    or not isinstance(document.get("manifests"), list)):
                raise DaemonBackendError("daemon image index is unsupported")
            next_ancestry = ancestry | {identity}
            for child in document["manifests"]:
                descriptor_count += 1
                if descriptor_count > _MAX_DESCRIPTORS or not isinstance(child, dict):
                    raise DaemonBackendError("daemon image descriptor budget exceeded")
                media = child.get("mediaType")
                keys = {"mediaType", "digest", "size"}
                if "platform" in child:
                    keys.add("platform")
                if set(child) != keys:
                    raise DaemonBackendError("daemon image descriptor shape is invalid")
                child_id = child.get("digest")
                child_size = child.get("size")
                if type(child_size) is not int or not 1 <= child_size <= _MAX_METADATA_ENTRY:
                    raise DaemonBackendError("daemon image descriptor size is invalid")
                child_platform = child.get("platform")
                if child_platform is not None:
                    if (not isinstance(child_platform, dict)
                            or set(child_platform) - {"os", "architecture", "variant"}
                            or not isinstance(child_platform.get("os"), str)
                            or not isinstance(child_platform.get("architecture"), str)
                            or ("variant" in child_platform
                                and not isinstance(child_platform["variant"], str))):
                        raise DaemonBackendError("daemon image platform descriptor is invalid")
                if media in _INDEX_TYPES:
                    if child_platform is not None:
                        raise DaemonBackendError("nested index platform selection is unsupported")
                elif media in _MANIFEST_TYPES:
                    if child_platform is None:
                        raise DaemonBackendError("platform manifest omits its platform")
                else:
                    raise DaemonBackendError("unsupported runnable image descriptor")
                visit(child_id, media, child_size, depth + 1, next_ancestry,
                      child_platform, from_index=True)
            return
        if media_type not in _MANIFEST_TYPES:
            raise DaemonBackendError("image config is not a runnable descriptor")
        if (not isinstance(document, dict)
                or set(document) - {"schemaVersion", "mediaType", "config", "layers"}
                or type(document.get("schemaVersion")) is not int
                or document.get("schemaVersion") != 2
                or document.get("mediaType", media_type) != media_type
                or not isinstance(document.get("layers"), list)):
            raise DaemonBackendError("daemon image manifest is unsupported")
        if from_index and declared_platform is None:
            raise DaemonBackendError("platform manifest selection is missing")
        config_descriptor = document.get("config")
        if (not isinstance(config_descriptor, dict)
                or set(config_descriptor) != {"mediaType", "digest", "size"}):
            raise DaemonBackendError("daemon image config descriptor is invalid")
        config_media = config_descriptor.get("mediaType")
        config_size = config_descriptor.get("size")
        if type(config_size) is not int or not 1 <= config_size <= _MAX_METADATA_ENTRY:
            raise DaemonBackendError("daemon image config size is invalid")
        config_id = config_descriptor.get("digest")
        config_raw = load(config_id, config_media, config_size)
        config = _strict_json(config_raw)
        if (not isinstance(config, dict) or not isinstance(config.get("os"), str)
                or not isinstance(config.get("architecture"), str)
                or (config.get("variant") is not None and not isinstance(config.get("variant"), str))):
            raise DaemonBackendError("daemon image config platform is invalid")
        if from_index:
            declared_variant = declared_platform.get("variant")
            if (declared_platform.get("os") != config.get("os")
                    or declared_platform.get("architecture") != config.get("architecture")
                    or declared_variant != config.get("variant")):
                raise DaemonBackendError("daemon descriptor and config platform differ")
        target_match = (
            config.get("os") == image.platform.os
            and config.get("architecture") == image.platform.architecture
            and (config.get("variant") == image.platform.variant
                 if image.platform.architecture == "arm64"
                 else config.get("variant") in (None, ""))
        )
        if from_index:
            target_match = target_match and (
                declared_platform.get("os") == image.platform.os
                and declared_platform.get("architecture") == image.platform.architecture
                and (declared_platform.get("variant") == image.platform.variant
                     if image.platform.architecture == "arm64"
                     else declared_platform.get("variant") in (None, ""))
            )
        defaults = config.get("config") or {}
        if not isinstance(defaults, dict):
            raise DaemonBackendError("daemon image defaults are invalid")
        if target_match:
            candidates.append((identity, config_id, config, defaults))

    visit(image.runnable_reference, image.runnable_media_type, root_size, 0,
          frozenset(), from_index=False)
    if len(candidates) != 1:
        raise DaemonBackendError("daemon image platform selection is absent or ambiguous")
    manifest_id, config_id, config, defaults = candidates[0]
    if (manifest_id != image.platform_manifest_digest
            or config_id != image.image_config_id):
        raise DaemonBackendError("daemon image selection differs from verified metadata")
    return config, defaults


def _strings(value, label):
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(type(item) is not str for item in value):
        raise DaemonBackendError("daemon image defaults are invalid")
    return tuple(value)


class DockerEngineBackend:
    """Explicitly constructed Docker Engine HTTP backend on one enrolled Unix socket."""

    def __init__(self, binding: DockerEngineBinding, *,
                 operation_journal: TrustedPrivateOperationJournal | None = None,
                 test_transport: httpx.MockTransport | None = None):
        if type(binding) is not DockerEngineBinding:
            raise TypeError("explicit DockerEngineBinding required")
        if test_transport is not None and type(test_transport) is not httpx.MockTransport:
            raise TypeError("only an explicit httpx.MockTransport test transport is accepted")
        self.binding = binding
        self.operation_journal = operation_journal
        self._using_test_transport = test_transport is not None
        transport = (test_transport if test_transport is not None
                     else httpx.HTTPTransport(uds=binding.unix_socket_path))
        self._client = httpx.Client(
            base_url="http://docker",
            transport=transport,
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
        )
        self._closed = False
        self._images: dict[str, tuple[VerifiedImage, dict, ImageDefaults]] = {}
        self._container_images: dict[str, VerifiedImage] = {}
        self._container_profiles: dict[str, EffectiveProfile] = {}
        self._container_generations: dict[str, str] = {}

    def close(self):
        if not self._closed:
            self._closed = True
            self._client.close()

    def _deadline(self, timeout_ms):
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 120_000:
            raise DaemonBackendError("invalid daemon timeout")
        return time.monotonic() + timeout_ms / 1000

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DaemonBackendError("daemon operation deadline exceeded")
        return remaining

    def _timeout(self, deadline):
        remaining = self._remaining(deadline)
        # A read timeout is not cancellation; callers retain their durable intent.
        return httpx.Timeout(remaining, connect=remaining, read=remaining,
                             write=remaining, pool=remaining)

    def _ensure_socket_binding(self):
        if self._using_test_transport:
            return
        try:
            info = os.lstat(self.binding.unix_socket_path)
        except OSError:
            raise DaemonBackendError("enrolled daemon socket unavailable") from None
        if not stat.S_ISSOCK(info.st_mode):
            raise DaemonBackendError("enrolled daemon socket is not a socket")

    def _response_bytes(self, method, route, deadline, *, expected_statuses,
                        json_body=None, params=None, body_limit=_MAX_JSON):
        if self._closed:
            raise DaemonBackendError("daemon backend is closed")
        self._ensure_socket_binding()
        try:
            with self._client.stream(
                method, route, params=params, json=json_body,
                timeout=self._timeout(deadline),
            ) as response:
                if response.status_code not in expected_statuses:
                    raise DaemonBackendError("daemon response was not accepted")
                if response.status_code == 404:
                    return 404, b""
                encoding = response.headers.get("content-encoding", "identity").lower()
                if encoding not in {"", "identity"}:
                    raise DaemonBackendError("compressed daemon response is unsupported")
                raw_length = response.headers.get("content-length")
                if raw_length is not None:
                    if not raw_length.isdecimal() or int(raw_length) > body_limit:
                        raise DaemonBackendError("daemon response exceeds its byte limit")
                chunks = []
                total = 0
                source = ((response.content,) if response.is_stream_consumed
                          else response.iter_raw(chunk_size=64 * 1024))
                for chunk in source:
                    self._remaining(deadline)
                    total += len(chunk)
                    if total > body_limit:
                        raise DaemonBackendError("daemon response exceeds its byte limit")
                    chunks.append(chunk)
                self._remaining(deadline)
                return response.status_code, b"".join(chunks)
        except DaemonBackendError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TimeoutError):
            # HTTPX exceptions can include response/request details; never reflect them.
            raise DaemonBackendError("daemon request outcome is unknown") from None

    def _read_response(self, method, route, deadline, *, expected_status,
                       json_body=None, params=None, body_limit=_MAX_JSON):
        _status, raw = self._response_bytes(
            method, route, deadline, expected_statuses={expected_status},
            json_body=json_body, params=params, body_limit=body_limit,
        )
        return raw

    def _archive_response(self, route, deadline):
        if self._closed:
            raise DaemonBackendError("daemon backend is closed")
        self._ensure_socket_binding()
        try:
            with self._client.stream("GET", route, timeout=self._timeout(deadline)) as response:
                if response.status_code != 200:
                    raise DaemonBackendError("daemon archive request was not accepted")
                encoding = response.headers.get("content-encoding", "identity").lower()
                if encoding not in {"", "identity"}:
                    raise DaemonBackendError("compressed daemon archive is unsupported")
                raw_length = response.headers.get("content-length")
                if raw_length is not None and (not raw_length.isdecimal()
                                                or int(raw_length) > _MAX_ARCHIVE):
                    raise DaemonBackendError("daemon archive exceeds its byte limit")
                source = ((response.content,) if response.is_stream_consumed
                          else response.iter_raw(chunk_size=64 * 1024))
                reader = _ChunkReader(source, _MAX_ARCHIVE, deadline)
                entries = _archive_metadata(reader)
                self._remaining(deadline)
                return entries
        except DaemonBackendError:
            raise
        except (ArchiveMetadataError, httpx.HTTPError, OSError, ValueError, TimeoutError, RecursionError):
            raise DaemonBackendError("daemon archive outcome is unknown or malformed") from None

    def _json(self, method, route, deadline, *, expected_status, json_body=None,
              params=None, limit=_MAX_JSON):
        raw = self._read_response(method, route, deadline,
                                  expected_status=expected_status,
                                  json_body=json_body, params=params,
                                  body_limit=limit)
        result = _strict_json(raw)
        self._remaining(deadline)
        return result

    def _route(self, suffix):
        return f"/v{self.binding.api_version}{suffix}"

    def inspect_runner(self, timeout_ms: int) -> RunnerObservation:
        deadline = self._deadline(timeout_ms)
        info = self._json("GET", self._route("/info"), deadline, expected_status=200)
        if not isinstance(info, dict):
            raise DaemonBackendError("daemon runner metadata is invalid")
        os_name = info.get("OSType")
        architecture = info.get("Architecture")
        arch_aliases = {"amd64": {"amd64", "x86_64"},
                        "arm64": {"arm64", "aarch64"}}
        expected = self.binding.platform
        if (info.get("ID") != self.binding.daemon_identity or os_name != expected.os
                or type(architecture) is not str
                or architecture not in arch_aliases.get(expected.architecture, set())):
            raise DaemonBackendError("daemon runner identity does not match enrollment")
        self._remaining(deadline)
        return RunnerObservation(self.binding.runner_id, self.binding.daemon_identity,
                                 self.binding.platform)

    @staticmethod
    def _image_id(image):
        return image.runnable_reference

    def _image_route(self, image, action):
        if not DIGEST_RE.fullmatch(image.runnable_reference):
            raise DaemonBackendError("only immutable runnable image digests are supported")
        segment = quote(image.runnable_reference, safe="")
        return self._route(f"/images/{segment}/{action}")

    def inspect_image(self, image: VerifiedImage, timeout_ms: int) -> ImageDefaults:
        if type(image) is not VerifiedImage:
            raise TypeError("exact VerifiedImage required")
        deadline = self._deadline(timeout_ms)
        image_json = self._json("GET", self._image_route(image, "json"), deadline,
                                expected_status=200)
        if not isinstance(image_json, dict):
            raise DaemonBackendError("daemon image metadata is invalid")
        image_id = image_json.get("Id")
        allowed_ids = {image.runnable_reference, image.platform_manifest_digest,
                       image.image_config_id}
        if image_id not in allowed_ids:
            raise DaemonBackendError("daemon image identity is outside verified chain")
        entries = self._archive_response(self._image_route(image, "get"), deadline)
        config, defaults = _archive_image(entries, image)
        self._remaining(deadline)
        if (config.get("os") != image.platform.os
                or config.get("architecture") != image.platform.architecture):
            raise DaemonBackendError("daemon image platform differs from verified selection")
        daemon_config = image_json.get("Config")
        if not isinstance(daemon_config, dict):
            raise DaemonBackendError("daemon image defaults are unavailable")
        entrypoint = _strings(defaults.get("Entrypoint"), "entrypoint")
        command = _strings(defaults.get("Cmd"), "command")
        environment = _strings(defaults.get("Env"), "environment")
        if (entrypoint != _strings(daemon_config.get("Entrypoint"), "entrypoint")
                or command != _strings(daemon_config.get("Cmd"), "command")
                or environment != _strings(daemon_config.get("Env"), "environment")):
            raise DaemonBackendError("daemon image defaults differ from original config")
        defaults_result = ImageDefaults(image.image_config_id, image.platform,
                                        entrypoint, command, environment)
        for identifier in allowed_ids:
            existing = self._images.get(identifier)
            if existing is not None and existing[0] != image:
                raise DaemonBackendError("daemon image identity is ambiguous across verified records")
        for identifier in allowed_ids:
            self._images[identifier] = (image, config, defaults_result)
        self._images[image.runnable_reference] = (image, config, defaults_result)
        self._remaining(deadline)
        return defaults_result

    def _remaining_ms(self, deadline):
        return max(1, int(self._remaining(deadline) * 1000))

    def _assert_runner(self, deadline):
        self.inspect_runner(self._remaining_ms(deadline))
        self._remaining(deadline)

    def _operation_id(self, operation_id):
        try:
            if type(operation_id) is not str or str(UUID(operation_id)) != operation_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise DaemonBackendError("invalid durable operation identity") from None

    def _journal_begin(self, operation_id, kind):
        self._operation_id(operation_id)
        if self.operation_journal is None:
            return
        try:
            self.operation_journal.begin(operation_id, kind)
        except Exception:
            raise DaemonBackendError("private operation journal unavailable") from None

    def _journal_complete(self, operation_id, kind, container_id=None):
        if self.operation_journal is None:
            return
        try:
            self.operation_journal.completed(operation_id, kind, container_id)
        except Exception:
            # The engine may already have changed state; leave the intent uncertain.
            raise DaemonBackendError("private operation receipt unavailable") from None

    def inspect_operation(self, operation_id: str, timeout_ms: int) -> OperationObservation:
        deadline = self._deadline(timeout_ms)
        self._operation_id(operation_id)
        journal = self.operation_journal
        if journal is None:
            return OperationObservation(operation_id, "unknown", False, None)
        try:
            self._remaining(deadline)
            observed = journal.inspect(operation_id)
            self._remaining(deadline)
        except Exception:
            return OperationObservation(operation_id, "unknown", False, None)
        if (type(observed) is not OperationObservation
                or type(observed.operation_id) is not str
                or observed.operation_id != operation_id
                or type(observed.state) is not str
                or observed.state not in {"pending", "unknown", "completed", "terminal_failed"}
                or type(observed.terminal) is not bool
                or (observed.container_id is not None
                    and (type(observed.container_id) is not str
                         or not CONTAINER_ID_RE.fullmatch(observed.container_id)))
                or (observed.state == "terminal_failed" and observed.terminal is not True)
                or (observed.state in {"pending", "unknown"} and observed.terminal is not False)
                or (observed.state == "completed" and observed.terminal is not True)):
            return OperationObservation(operation_id, "unknown", False, None)
        return observed

    @staticmethod
    def _generation_id(value):
        if type(value) is not str or not OPAQUE_RE.fullmatch(value):
            raise DaemonBackendError("invalid state generation identity")
        return value

    def _resolve_generation(self, generation_id, deadline):
        generation_id = self._generation_id(generation_id)
        root = Path(self.binding.generation_root)
        source = root / generation_id
        try:
            root_info = os.lstat(root)
            source_info = os.lstat(source)
            if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
                    or root_info.st_uid != os.geteuid() or root_info.st_mode & 0o022
                    or not stat.S_ISDIR(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode)
                    or source_info.st_uid != os.geteuid() or source_info.st_mode & 0o022
                    or root.resolve(strict=True) != root
                    or source.resolve(strict=True) != source
                    or source.parent != root):
                raise ValueError
            # Generation trees are private state, never arbitrary host paths or
            # socket/device mounts. Reject links and special files without following them.
            seen = 0
            def walk_error(_error):
                raise OSError("generation enumeration failed")
            for base, dirs, files in os.walk(source, topdown=True, onerror=walk_error,
                                             followlinks=False):
                self._remaining(deadline)
                for name in (*dirs, *files):
                    seen += 1
                    if seen > 200_000:
                        raise ValueError
                    info = os.lstat(Path(base) / name)
                    if (stat.S_ISLNK(info.st_mode)
                            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                        raise ValueError
            return source
        except DaemonBackendError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise DaemonBackendError("state generation is not safely enrolled") from None

    @staticmethod
    def _profile_environment(profile, defaults):
        original = defaults.get("Env") or []
        values = {}
        for entry in original:
            if type(entry) is not str or "=" not in entry:
                raise DaemonBackendError("image default environment is invalid")
            key, value = entry.split("=", 1)
            if not key or key in values or "\x00" in key or "\x00" in value:
                raise DaemonBackendError("image default environment is ambiguous")
            values[key] = value
        reserved = {"HUB_BASE_URL", "CODEX_TOKEN", "HERMES_TOKEN", "CLAUDE_TOKEN"}
        # The published Hub image documents this single fixed data path. Admit
        # that exact image default only; the closed final environment below
        # still injects/checks the same path and cannot inherit an alternative.
        if (reserved.intersection(values)
                or ("HUB_DB" in values and values["HUB_DB"] != "/var/lib/hub/hub.sqlite3")):
            raise DaemonBackendError("image defaults contain reserved runtime keys")
        observed = profile.environment
        if (type(observed) is not tuple or any(type(item) is not tuple or len(item) != 2
                                               or type(item[0]) is not str or type(item[1]) is not str
                                               for item in observed)):
            raise DaemonBackendError("effective runtime environment is invalid")
        profile_environment = dict(observed)
        if len(profile_environment) != len(observed):
            raise DaemonBackendError("effective runtime environment has duplicate keys")
        expected = dict(values)
        expected.update({"HUB_BASE_URL": "http://127.0.0.1:8770",
                         "HUB_DB": "/var/lib/hub/hub.sqlite3"})
        for name in ("CODEX_TOKEN", "HERMES_TOKEN", "CLAUDE_TOKEN"):
            value = profile_environment.get(name)
            if (type(value) is not str or not value or len(value) > 4096
                    or any(ord(character) < 32 or ord(character) == 127 for character in value)):
                raise DaemonBackendError("test-only runtime secret is invalid")
            expected[name] = value
        if tuple(sorted(observed)) != tuple(sorted(expected.items())):
            raise DaemonBackendError("effective runtime environment is not closed")
        return tuple(sorted(expected.items()))

    def _validate_profile(self, plan, actual_config, actual_defaults):
        return self._validate_effective_profile(plan.profile, actual_config, actual_defaults)

    def _validate_effective_profile(self, profile, actual_config, actual_defaults):
        if type(profile) is not EffectiveProfile:
            raise DaemonBackendError("typed effective profile required")
        if (type(profile.uid) is not int or profile.uid != 10001
                or type(profile.gid) is not int or profile.gid != 10001
                or profile.supplementary_groups != ()
                or profile.read_only_root is not True
                or profile.cap_drop != ("ALL",)
                or profile.no_new_privileges is not True
                or profile.privileged is not False
                or profile.pid_mode != "private" or profile.ipc_mode != "private"
                or profile.cgroupns_mode != "private"
                or profile.network_mode != "none" or profile.published_ports != ()
                or profile.restart_policy != "no"
                or profile.state_destination != "/var/lib/hub"
                or profile.state_mode != "rw"):
            raise DaemonBackendError("runtime profile is outside the closed pilot policy")
        if (type(profile.memory_bytes) is not int or not 128 * 1024**2 <= profile.memory_bytes <= 2 * 1024**3
                or type(profile.cpu_millicores) is not int or not 100 <= profile.cpu_millicores <= 2000
                or type(profile.pids_limit) is not int or not 16 <= profile.pids_limit <= 256):
            raise DaemonBackendError("runtime resource profile is outside bounds")
        if (type(profile.tmpfs) is not tuple or len(profile.tmpfs) != 1
                or type(profile.tmpfs[0]) is not tuple or len(profile.tmpfs[0]) != 5):
            raise DaemonBackendError("runtime tmpfs profile is invalid")
        tmp_path, tmp_size, tmp_mode, noexec, nosuid = profile.tmpfs[0]
        if (tmp_path != "/tmp" or type(tmp_size) is not int
                or not 1024**2 <= tmp_size <= _FIXED_TMPFS_SIZE_MAX
                or tmp_size > profile.memory_bytes or tmp_mode != 0o1777
                or noexec is not True or nosuid is not True):
            raise DaemonBackendError("runtime tmpfs profile is outside bounds")
        generation_id = self._generation_id(profile.state_generation_id)
        expected_mounts = (("state_generation", "/var/lib/hub", "rw", generation_id),
                           ("tmpfs", "/tmp", "rw", f"size={tmp_size}",
                            f"mode={tmp_mode:o}", "noexec", "nosuid"))
        if profile.mounts != expected_mounts:
            raise DaemonBackendError("runtime mounts are outside the closed profile")
        if actual_config.get("Volumes") not in (None, {}):
            raise DaemonBackendError("image declares unsupported volume mounts")
        entrypoint = _strings(actual_defaults.get("Entrypoint"), "entrypoint")
        command = _strings(actual_defaults.get("Cmd"), "command")
        if (profile.entrypoint != entrypoint or profile.command != command
                or not (entrypoint or command)
                or any("\x00" in item for item in (*entrypoint, *command))):
            raise DaemonBackendError("runtime must preserve image-default entrypoint and command")
        environment = self._profile_environment(profile, actual_defaults)
        return generation_id, environment

    def _static_profile_check(self, plan):
        if type(plan) is not ContainerPlan or type(plan.image) is not VerifiedImage:
            raise DaemonBackendError("typed container plan required")
        if (plan.pull is not False or plan.immutable_reference != plan.image.runnable_reference
                or not DIGEST_RE.fullmatch(plan.immutable_reference)
                or plan.image.runnable_reference == plan.image.image_config_id):
            raise DaemonBackendError("container plan must use a verified runnable digest")
        if (plan.image.runnable_media_type not in _INDEX_TYPES | _MANIFEST_TYPES
                or not DIGEST_RE.fullmatch(plan.image.platform_manifest_digest)
                or not DIGEST_RE.fullmatch(plan.image.image_config_id)):
            raise DaemonBackendError("container plan image chain is invalid")
        if (type(plan.resource_name) is not str or len(plan.resource_name) > 127
                or not re.fullmatch(r"hub-pilot-[a-z0-9][a-z0-9-]{0,39}-[0-9a-f-]{12}-(candidate|rollback)",
                                    plan.resource_name)
                or plan.image.role not in {"candidate", "rollback"}):
            raise DaemonBackendError("container resource identity is invalid")
        profile = plan.profile
        if type(profile) is not EffectiveProfile:
            raise DaemonBackendError("typed effective profile required")
        # Check all fixed security fields before any daemon write or state resolution.
        if (profile.uid != 10001 or profile.gid != 10001
                or profile.supplementary_groups != () or profile.read_only_root is not True
                or profile.cap_drop != ("ALL",) or profile.no_new_privileges is not True
                or profile.privileged is not False or profile.pid_mode != "private"
                or profile.ipc_mode != "private" or profile.network_mode != "none"
                or profile.cgroupns_mode != "private"
                or profile.published_ports != () or profile.restart_policy != "no"):
            raise DaemonBackendError("runtime profile is outside the closed pilot policy")

    def _create_body(self, plan, source, environment):
        profile = plan.profile
        tmp_size, tmp_mode = profile.tmpfs[0][1], profile.tmpfs[0][2]
        return {
            "Image": plan.immutable_reference,
            "User": "10001:10001",
            "Env": [f"{key}={value}" for key, value in environment],
            "HostConfig": {
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Privileged": False,
                "CgroupnsMode": profile.cgroupns_mode,
                # Private PID and IPC are Docker's defaults: omit host/container modes.
                "NetworkMode": "none",
                "PortBindings": {},
                "PublishAllPorts": False,
                "Memory": profile.memory_bytes,
                "MemorySwap": profile.memory_bytes,
                "NanoCpus": profile.cpu_millicores * 1_000_000,
                "PidsLimit": profile.pids_limit,
                "Tmpfs": {"/tmp": f"rw,noexec,nosuid,size={tmp_size},mode={tmp_mode:o}"},
                "Mounts": [{"Type": "bind", "Source": str(source),
                            "Target": "/var/lib/hub", "ReadOnly": False}],
                "RestartPolicy": {"Name": "no"},
                "AutoRemove": False,
            },
        }

    def create_container(self, plan: ContainerPlan, operation_id: str, timeout_ms: int) -> str:
        deadline = self._deadline(timeout_ms)
        self._operation_id(operation_id)
        self._static_profile_check(plan)
        source = self._resolve_generation(plan.profile.state_generation_id, deadline)
        # Read-only identity and original archive metadata are rechecked using one
        # deadline immediately before the durable-intent-protected create request.
        self._assert_runner(deadline)
        defaults = self.inspect_image(plan.image, self._remaining_ms(deadline))
        cached = self._images.get(plan.image.runnable_reference)
        if cached is None:
            raise DaemonBackendError("verified daemon image metadata is unavailable")
        generation_id, environment = self._validate_profile(
            plan, cached[1].get("config") or {},
            {"Entrypoint": defaults.entrypoint, "Cmd": defaults.command,
             "Env": defaults.environment,
             "Volumes": (cached[1].get("config") or {}).get("Volumes")},
        )
        self._remaining(deadline)
        self._journal_begin(operation_id, "create_" + plan.image.role)
        name_route = self._route("/containers/" + quote(plan.resource_name, safe="") + "/json")
        status, _ = self._response_bytes("GET", name_route, deadline,
                                         expected_statuses={200, 404}, body_limit=_MAX_JSON)
        self._remaining(deadline)
        if status != 404:
            # Name collisions, including exact labels/IDs, are ambiguous and never reused.
            raise DaemonBackendError("container resource name is already occupied")
        self._assert_runner(deadline)
        body = self._create_body(plan, source, environment)
        route = self._route("/containers/create")
        raw = self._read_response("POST", route, deadline, expected_status=201,
                                  json_body=body, params={"name": plan.resource_name})
        result = _strict_json(raw)
        self._remaining(deadline)
        if (not isinstance(result, dict) or type(result.get("Id")) is not str
                or not CONTAINER_ID_RE.fullmatch(result["Id"])
                or ("Warnings" in result and (not isinstance(result["Warnings"], list)
                                               or any(type(warning) is not str
                                                      for warning in result["Warnings"])) )):
            raise DaemonBackendError("daemon create response is invalid")
        container_id = result["Id"]
        self._journal_complete(operation_id, "create_" + plan.image.role, container_id)
        self._assert_runner(deadline)
        self._container_images[container_id] = plan.image
        self._container_profiles[container_id] = plan.profile
        self._container_generations[container_id] = generation_id
        self._remaining(deadline)
        actual = self.inspect_container(container_id, self._remaining_ms(deadline))
        if (type(actual) is not ContainerObservation or actual.container_id != container_id
                or actual.selected_image != ActualImageSelection(
                    plan.image.runnable_reference, plan.image.platform_manifest_digest,
                    plan.image.image_config_id, plan.image.platform)
                or actual.profile != plan.profile
                or actual.state_generation_id != generation_id or actual.status != "created"):
            raise DaemonBackendError("created container effective profile differs from preflight")
        self._remaining(deadline)
        return container_id

    def _safe_port_bindings(self, value):
        if value in (None, {}):
            return True
        if not isinstance(value, dict):
            return False
        for bindings in value.values():
            if bindings is None or bindings == []:
                continue
            return False
        return True

    def _container_observation(self, container_id, data, deadline):
        if not isinstance(data, dict) or data.get("Id") != container_id:
            raise DaemonBackendError("daemon container identity is invalid")
        actual_image_id = data.get("Image")
        cached = self._images.get(actual_image_id)
        if cached is None:
            image = self._container_images.get(container_id)
            if image is None or actual_image_id not in {
                    image.runnable_reference, image.platform_manifest_digest, image.image_config_id}:
                raise DaemonBackendError("container image identity is outside verified chain")
            cache = self._images.get(image.runnable_reference)
            if cache is None:
                raise DaemonBackendError("container original image metadata is unavailable")
            cached = cache
        image, config, defaults_result = cached
        if image.platform.architecture == "arm64" and (
                image.runnable_reference != image.platform_manifest_digest
                and actual_image_id == image.runnable_reference):
            raise DaemonBackendError("arm64 container selection lacks a platform-specific identity")
        if self._container_images.get(container_id, image) != image:
            raise DaemonBackendError("container image identity conflicts with its create plan")
        config_info = data.get("Config")
        host = data.get("HostConfig")
        if not isinstance(config_info, dict) or not isinstance(host, dict):
            raise DaemonBackendError("daemon container profile metadata is missing")
        if config_info.get("Image") != image.runnable_reference:
            raise DaemonBackendError("container immutable image reference changed")
        if config_info.get("User") != "10001:10001":
            raise DaemonBackendError("container user differs from closed profile")
        actual_defaults = config.get("config") or {}
        env_value = config_info.get("Env")
        if not isinstance(env_value, list) or any(type(item) is not str or "=" not in item
                                                  for item in env_value):
            raise DaemonBackendError("container environment is invalid")
        env = {}
        for item in env_value:
            name, value = item.split("=", 1)
            if not name or name in env or "\x00" in name or "\x00" in value:
                raise DaemonBackendError("container environment is ambiguous")
            env[name] = value
        try:
            entrypoint = tuple(config_info.get("Entrypoint") or ())
            command = tuple(config_info.get("Cmd") or ())
        except TypeError:
            raise DaemonBackendError("container command defaults are invalid") from None
        image_entrypoint = _strings(actual_defaults.get("Entrypoint"), "entrypoint")
        image_command = _strings(actual_defaults.get("Cmd"), "command")
        if entrypoint != image_entrypoint or command != image_command:
            raise DaemonBackendError("container command differs from image defaults")
        if config_info.get("Volumes") not in (None, {}):
            raise DaemonBackendError("container declares unexpected volumes")
        generation_id = self._container_generations.get(container_id)
        state = data.get("State")
        if not isinstance(state, dict):
            raise DaemonBackendError("daemon container state metadata is invalid")
        status = state.get("Status")
        if type(status) is not str or status not in {"created", "running", "exited"}:
            raise DaemonBackendError("daemon container state is unsupported")
        # Pinned Moby 568f755 daemon/volumes_unix.go keeps HostConfig.Tmpfs
        # separate from MountPoints, including while running. Inspect.Mounts
        # therefore need not contain /tmp. The fully checked HostConfig below
        # is daemon configuration evidence, not independent kernel mount proof.
        never_started = (state.get("Status") == "created"
                         and state.get("Running") is False
                         and type(state.get("Pid")) is int and state["Pid"] == 0
                         and state.get("StartedAt") == "0001-01-01T00:00:00Z")
        mounts = data.get("Mounts")
        host_mounts = host.get("Mounts")
        if (not isinstance(mounts, list) or len(mounts) not in (1, 2)
                or (len(mounts) == 1 and status == "created" and not never_started)
                or not isinstance(host_mounts, list) or len(host_mounts) != 1):
            raise DaemonBackendError("container has additional or missing mounts")
        bind_mounts = [item for item in mounts if isinstance(item, dict)
                       and item.get("Type") == "bind"]
        tmp_mounts = [item for item in mounts if isinstance(item, dict)
                      and item.get("Type") == "tmpfs"]
        if (len(bind_mounts) != 1 or len(tmp_mounts) != len(mounts) - 1
                or len(tmp_mounts) not in (0, 1)):
            raise DaemonBackendError("container mount types are unsupported")
        bind = bind_mounts[0]
        source_value = bind.get("Source")
        state_destination = bind.get("Destination")
        if (type(source_value) is not str or state_destination != "/var/lib/hub"
                or bind.get("RW") is not True or bind.get("Mode") not in ("", "rw")
                or bind.get("Propagation") != "rprivate"):
            raise DaemonBackendError("container state mount is unsafe")
        source = Path(source_value)
        if generation_id is None:
            try:
                generation_id = source.name
                self._generation_id(generation_id)
            except DaemonBackendError:
                raise DaemonBackendError("container generation identity is unavailable") from None
        enrolled_source = self._resolve_generation(generation_id, deadline)
        if source != enrolled_source:
            raise DaemonBackendError("container state mount is outside enrolled root")
        configured_mount = host_mounts[0]
        # Pinned Moby mount.Mount.ReadOnly is bool with json:",omitempty";
        # absent means false. Present null/0/strings are never that default.
        if (not isinstance(configured_mount, dict) or configured_mount.get("Type") != "bind"
                or set(configured_mount) - {"Type", "Source", "Target", "ReadOnly"}
                or configured_mount.get("Source") != str(enrolled_source)
                or configured_mount.get("Target") != "/var/lib/hub"
                or ("ReadOnly" in configured_mount and configured_mount["ReadOnly"] is not False)):
            raise DaemonBackendError("container host bind configuration differs")
        tmp_mount = tmp_mounts[0] if tmp_mounts else None
        if tmp_mount is not None and (tmp_mount.get("Destination") != "/tmp" or tmp_mount.get("RW") is not True
                or tmp_mount.get("Source") not in ("", None)):
            raise DaemonBackendError("container scratch mount is unsafe")
        tmp_options = host.get("Tmpfs")
        if not isinstance(tmp_options, dict) or set(tmp_options) != {"/tmp"}:
            raise DaemonBackendError("container tmpfs configuration is unsupported")
        option_value = tmp_options.get("/tmp")
        if type(option_value) is not str:
            raise DaemonBackendError("container tmpfs options are invalid")
        option_tokens = option_value.split(",")
        option_set = set(option_tokens)
        if (len(option_set) != len(option_tokens)
                or not {"rw", "noexec", "nosuid", "mode=1777"}.issubset(option_set)
                or len([token for token in option_tokens if token.startswith("size=")]) != 1
                or option_set - {"rw", "noexec", "nosuid", "nodev", "mode=1777",
                                 next((token for token in option_tokens
                                       if token.startswith("size=")), "")}):
            raise DaemonBackendError("container tmpfs options differ from policy")
        size_token = next(token for token in option_tokens if token.startswith("size="))
        try:
            tmp_size = int(size_token[5:])
        except ValueError:
            raise DaemonBackendError("container tmpfs size is invalid") from None
        mount_mode = tmp_mount.get("Mode") if tmp_mount is not None else ""
        if type(mount_mode) is not str:
            raise DaemonBackendError("container scratch mount mode is unavailable")
        if not 1024**2 <= tmp_size <= _FIXED_TMPFS_SIZE_MAX:
            raise DaemonBackendError("container scratch mount differs from policy")
        # Engine reports an empty top-level Mode for tmpfs. The security
        # options are authoritative in HostConfig.Tmpfs, checked above.
        if mount_mode:
            mount_mode_tokens = mount_mode.split(",")
            mount_mode_set = set(mount_mode_tokens)
            required_mount_mode = {"rw", "noexec", "nosuid", size_token}
            if (len(mount_mode_set) != len(mount_mode_tokens)
                    or not required_mount_mode.issubset(mount_mode_set)
                    or mount_mode_set - required_mount_mode - {"nodev", "mode=1777"}):
                raise DaemonBackendError("container scratch mount differs from policy")
        tmp_profile = (("/tmp", tmp_size, 0o1777, True, True),)
        expected_env_names = {item.split("=", 1)[0] for item in defaults_result.environment}
        expected_env_names.update({"HUB_BASE_URL", "HUB_DB", "CODEX_TOKEN", "HERMES_TOKEN", "CLAUDE_TOKEN"})
        if set(env) != expected_env_names:
            raise DaemonBackendError("container effective environment has extra or missing keys")
        if (env.get("HUB_BASE_URL") != "http://127.0.0.1:8770"
                or env.get("HUB_DB") != "/var/lib/hub/hub.sqlite3"):
            raise DaemonBackendError("container runtime environment differs from policy")
        for name in ("CODEX_TOKEN", "HERMES_TOKEN", "CLAUDE_TOKEN"):
            value = env.get(name)
            if (type(value) is not str or not value or len(value) > 4096
                    or any(ord(character) < 32 or ord(character) == 127 for character in value)):
                raise DaemonBackendError("container test secret is invalid")
        restart = host.get("RestartPolicy")
        retry_count = restart.get("MaximumRetryCount", 0) if isinstance(restart, dict) else None
        if (not isinstance(restart, dict)
                or set(restart) - {"Name", "MaximumRetryCount"}
                or restart.get("Name") != "no"
                or type(retry_count) is not int or retry_count != 0):
            raise DaemonBackendError("container restart policy differs")
        if (type(host.get("ReadonlyRootfs")) is not bool or host.get("ReadonlyRootfs") is not True
                or host.get("CapDrop") != ["ALL"] or host.get("CapAdd") not in (None, [])
                or host.get("SecurityOpt") != ["no-new-privileges:true"]
                or type(host.get("Privileged")) is not bool or host.get("Privileged") is not False
                or host.get("PidMode") not in ("", "private")
                or host.get("IpcMode") not in ("", "private")
                or host.get("CgroupnsMode") != "private"
                or host.get("NetworkMode") != "none"
                or not self._safe_port_bindings(host.get("PortBindings"))
                or host.get("PublishAllPorts") is True
                or type(host.get("Memory")) is not int
                or not 128 * 1024**2 <= host.get("Memory") <= 2 * 1024**3
                or host.get("MemorySwap") != host.get("Memory")
                or type(host.get("NanoCpus")) is not int
                or host.get("NanoCpus") % 1_000_000 != 0
                or not 100_000_000 <= host.get("NanoCpus") <= 2_000_000_000
                or type(host.get("PidsLimit")) is not int
                or not 16 <= host.get("PidsLimit") <= 256
                or host.get("AutoRemove") is not False):
            raise DaemonBackendError("container host security profile differs")
        for key in ("GroupAdd", "Binds", "Devices", "DeviceRequests", "DeviceCgroupRules",
                    "ExtraHosts", "Links", "VolumesFrom"):
            if host.get(key) not in (None, []):
                raise DaemonBackendError("container has unsupported host configuration")
        network_settings = data.get("NetworkSettings") or {}
        if not isinstance(network_settings, dict):
            raise DaemonBackendError("container network inspection is invalid")
        networks = network_settings.get("Networks") or {}
        if not isinstance(networks, dict) or set(networks) - {"none"}:
            raise DaemonBackendError("container has an additional network attachment")
        actual_ports = network_settings.get("Ports")
        if not self._safe_port_bindings(actual_ports):
            raise DaemonBackendError("container has published host ports")
        profile = EffectiveProfile(
            uid=10001, gid=10001, supplementary_groups=(), read_only_root=True,
            cap_drop=("ALL",), no_new_privileges=True, privileged=False,
            pid_mode="private", ipc_mode="private", network_mode="none",
            cgroupns_mode=host["CgroupnsMode"],
            published_ports=(), memory_bytes=host["Memory"],
            cpu_millicores=host["NanoCpus"] // 1_000_000,
            pids_limit=host["PidsLimit"], tmpfs=tmp_profile,
            mounts=(("state_generation", "/var/lib/hub", "rw", generation_id),
                    ("tmpfs", "/tmp", "rw", f"size={tmp_size}",
                     "mode=1777", "noexec", "nosuid")),
            state_destination="/var/lib/hub", state_mode="rw",
            state_generation_id=generation_id,
            environment=tuple(sorted(env.items())),
            entrypoint=entrypoint, command=command, restart_policy="no",
        )
        validated_generation, _validated_environment = self._validate_effective_profile(
            profile, actual_defaults, actual_defaults
        )
        if validated_generation != generation_id:
            raise DaemonBackendError("container state generation differs from its profile")
        expected_image = ActualImageSelection(
            image.runnable_reference, image.platform_manifest_digest,
            image.image_config_id, image.platform,
        )
        self._container_profiles.setdefault(container_id, profile)
        self._container_generations.setdefault(container_id, generation_id)
        return ContainerObservation(container_id, expected_image, profile,
                                    generation_id, status)

    def inspect_container(self, container_id: str, timeout_ms: int) -> ContainerObservation | None:
        if type(container_id) is not str or not CONTAINER_ID_RE.fullmatch(container_id):
            raise DaemonBackendError("invalid container identity")
        deadline = self._deadline(timeout_ms)
        route = self._route("/containers/" + container_id + "/json")
        status, raw = self._response_bytes("GET", route, deadline,
                                           expected_statuses={200, 404}, body_limit=_MAX_JSON)
        if status == 404:
            return None
        data = _strict_json(raw)
        observation = self._container_observation(container_id, data, deadline)
        self._remaining(deadline)
        return observation

    def _known_container(self, container_id, expected_status, deadline):
        if type(container_id) is not str or not CONTAINER_ID_RE.fullmatch(container_id):
            raise DaemonBackendError("invalid container identity")
        observation = self.inspect_container(container_id, self._remaining_ms(deadline))
        if (type(observation) is not ContainerObservation
                or observation.status != expected_status):
            raise DaemonBackendError("container is absent or has an ambiguous state")
        expected_profile = self._container_profiles.get(container_id)
        if expected_profile is not None and observation.profile != expected_profile:
            raise DaemonBackendError("container effective profile changed")
        return observation

    def start_container(self, container_id: str, operation_id: str, timeout_ms: int) -> None:
        deadline = self._deadline(timeout_ms)
        self._operation_id(operation_id)
        before = self._known_container(container_id, "created", deadline)
        self._journal_begin(operation_id, "start")
        self._assert_runner(deadline)
        route = self._route("/containers/" + container_id + "/start")
        self._read_response("POST", route, deadline, expected_status=204)
        self._journal_complete(operation_id, "start", container_id)
        self._assert_runner(deadline)
        after = self.inspect_container(container_id, self._remaining_ms(deadline))
        if (type(after) is not ContainerObservation or after.status != "running"
                or after.selected_image != before.selected_image or after.profile != before.profile):
            raise DaemonBackendError("started container state is not verified")
        self._remaining(deadline)

    def stop_container(self, container_id: str, operation_id: str, timeout_ms: int) -> None:
        deadline = self._deadline(timeout_ms)
        self._operation_id(operation_id)
        before = self._known_container(container_id, "running", deadline)
        self._journal_begin(operation_id, "stop")
        self._assert_runner(deadline)
        route = self._route("/containers/" + container_id + "/stop")
        self._read_response("POST", route, deadline, expected_status=204,
                            params={"t": "5"})
        self._journal_complete(operation_id, "stop", container_id)
        self._assert_runner(deadline)
        after = self.inspect_container(container_id, self._remaining_ms(deadline))
        if (type(after) is not ContainerObservation or after.status != "exited"
                or after.selected_image != before.selected_image or after.profile != before.profile):
            raise DaemonBackendError("stopped container state is not verified")
        self._remaining(deadline)

    def _exec_frame(self, raw):
        if type(raw) is not bytes or len(raw) > _MAX_EXEC_OUTPUT:
            raise DaemonBackendError("compiled probe output exceeds its byte limit")
        output = bytearray()
        offset = 0
        while offset < len(raw):
            if len(raw) - offset < 8:
                raise DaemonBackendError("compiled probe stream is truncated")
            stream_type = raw[offset]
            if raw[offset + 1:offset + 4] != bytes(3) or stream_type not in {1, 2}:
                raise DaemonBackendError("compiled probe stream framing is invalid")
            size = int.from_bytes(raw[offset + 4:offset + 8], "big")
            offset += 8
            if size > _MAX_EXEC_OUTPUT or offset + size > len(raw):
                raise DaemonBackendError("compiled probe frame exceeds its byte limit")
            payload = raw[offset:offset + size]
            offset += size
            if stream_type != 1:
                # Stderr stays private; no daemon/process diagnostic is reflected.
                raise DaemonBackendError("compiled probe emitted stderr")
            output.extend(payload)
            if len(output) > _MAX_EXEC_OUTPUT:
                raise DaemonBackendError("compiled probe output exceeds its byte limit")
        return bytes(output)

    def _one_probe(self, container_id, probe, budget_ms, deadline, source_commit):
        probe_deadline = min(deadline, time.monotonic() + budget_ms / 1000)
        self._remaining(probe_deadline)
        create_body = {
            "AttachStdin": False,
            "AttachStdout": True,
            "AttachStderr": False,
            "Tty": False,
            "User": "10001:10001",
            "Cmd": ["/usr/local/bin/python3", "-I", "-c", _PROBE_SCRIPT,
                    probe.kind, probe.path, str(probe.expected_status),
                    probe.expectation_id, str(probe.max_response_bytes), str(budget_ms), source_commit],
        }
        self._assert_runner(probe_deadline)
        raw_create = self._read_response(
            "POST", self._route("/containers/" + container_id + "/exec"),
            probe_deadline, expected_status=201, json_body=create_body,
        )
        created = _strict_json(raw_create)
        if (not isinstance(created, dict) or type(created.get("Id")) is not str
                or not EXEC_ID_RE.fullmatch(created["Id"])):
            raise DaemonBackendError("compiled probe execution identity is invalid")
        exec_id = created["Id"]
        self._assert_runner(probe_deadline)
        raw_stream = self._read_response(
            "POST", self._route("/exec/" + exec_id + "/start"),
            probe_deadline, expected_status=200,
            json_body={"Detach": False, "Tty": False}, body_limit=_MAX_EXEC_OUTPUT,
        )
        stdout = self._exec_frame(raw_stream)
        self._assert_runner(probe_deadline)
        inspected = self._json(
            "GET", self._route("/exec/" + exec_id + "/json"),
            probe_deadline, expected_status=200, limit=_MAX_JSON,
        )
        if (not isinstance(inspected, dict) or inspected.get("ID") != exec_id
                or type(inspected.get("Running")) is not bool
                or inspected.get("Running") is not False
                or type(inspected.get("ExitCode")) is not int
                or inspected["ExitCode"] not in {0, 1}):
            raise DaemonBackendError("compiled probe completion is not established")
        report = _strict_json(stdout)
        if (not isinstance(report, dict) or set(report) != {"expectation_id", "ok", "status"}
                or report.get("expectation_id") != probe.expectation_id
                or type(report.get("ok")) is not bool
                or type(report.get("status")) is not int
                or not 0 <= report["status"] <= 599):
            raise DaemonBackendError("compiled probe evidence is invalid")
        if inspected["ExitCode"] == 0 and report["ok"] is True:
            if report["status"] != probe.expected_status:
                raise DaemonBackendError("compiled probe status evidence differs")
            return True
        if inspected["ExitCode"] == 1 and report["ok"] is False:
            return False
        raise DaemonBackendError("compiled probe process outcome is ambiguous")

    def run_compiled_probes(self, container_id: str, probes: tuple[Probe, ...],
                            operation_id: str, timeout_ms: tuple[int, ...]) -> ProbeObservation:
        self._operation_id(operation_id)
        if (type(probes) is not tuple or len(probes) != _MAX_PROBES
                or any(type(probe) is not Probe for probe in probes)
                or tuple((probe.kind, probe.path, probe.expected_status, probe.expectation_id)
                         for probe in probes) != _PROBE_PROFILE
                or any(probe.method != "GET" for probe in probes)):
            raise DaemonBackendError("only the shared compiled probe profile is supported")
        if (type(timeout_ms) is not tuple or len(timeout_ms) != _MAX_PROBES
                or any(type(value) is not int or not 1 <= value <= 10_000
                       for value in timeout_ms)
                or any(value > probe.timeout_ms for value, probe in zip(timeout_ms, probes, strict=True))
                or sum(timeout_ms) > 120_000):
            raise DaemonBackendError("compiled probe budgets are invalid")
        deadline = time.monotonic() + sum(timeout_ms) / 1000
        observation = self._known_container(container_id, "running", deadline)
        if (type(observation) is not ContainerObservation
                or observation.container_id != container_id
                or not observation.profile.read_only_root
                or observation.profile.network_mode != "none"
                or observation.profile.uid != 10001 or observation.profile.gid != 10001):
            raise DaemonBackendError("probe target is not admitted under the closed profile")
        selection = observation.selected_image
        cached = (self._images.get(selection.image_config_id)
                  if type(selection) is ActualImageSelection else None)
        image = cached[0] if type(cached) is tuple and len(cached) == 3 else None
        if (type(image) is not VerifiedImage or image.role not in {"candidate", "rollback"}
                or selection != ActualImageSelection(image.runnable_reference,
                    image.platform_manifest_digest, image.image_config_id, image.platform)
                or type(image.source_commit) is not str
                or not re.fullmatch(r"[0-9a-f]{40}", image.source_commit)
                or self._container_images.get(container_id, image) != image):
            raise DaemonBackendError("probe target lacks a verified source-image binding")
        self._remaining(deadline)
        self._journal_begin(operation_id, "probe")
        health_ok = True
        for probe, budget in zip(probes, timeout_ms, strict=True):
            self._remaining(deadline)
            healthy = self._one_probe(container_id, probe, budget, deadline, image.source_commit)
            health_ok = health_ok and healthy
            self._remaining(deadline)
        self._journal_complete(operation_id, "probe", container_id)
        self._remaining(deadline)
        profile_bytes = json.dumps(
            [probe.model_dump(mode="json") for probe in probes],
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        return ProbeObservation(
            container_id=container_id,
            image_config_id=observation.selected_image.image_config_id,
            profile_digest="sha256:" + hashlib.sha256(profile_bytes).hexdigest(),
            health="healthy" if health_ok else "unhealthy",
            evidence_verified=True,
        )
