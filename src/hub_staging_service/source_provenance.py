"""Bounded proof that a staging ZIP is the exact tree named by a Git commit.

This verifies Git object identity and the archived bytes, paths, and executable
modes. It does not prove repository membership, branch policy, authorship, or
review. Callers must bind the verified commit to their own reviewed task.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
import stat
import zipfile

SCHEME = "git-sha1-tree-v1"
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_COMMIT_OBJECT = 64 * 1024
MAX_PATH_BYTES = 4096
MAX_PATH_DEPTH = 64
_OID = re.compile(r"^[0-9a-f]{40}$")


class SourceProvenanceError(ValueError):
    """Fixed diagnostic for invalid or unsupported Git source evidence."""


def _fail(code: str) -> None:
    raise SourceProvenanceError(code)


def _object_id(kind: bytes, content: bytes) -> str:
    return hashlib.sha1(kind + b" " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()


def _path(name: str) -> tuple[str, ...]:
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        _fail("unsafe_source_path")
    parts = tuple(name.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        _fail("unsafe_source_path")
    try:
        encoded = name.encode("utf-8", "strict")
    except UnicodeError:
        _fail("unsafe_source_path")
    if len(encoded) > MAX_PATH_BYTES or len(parts) > MAX_PATH_DEPTH:
        _fail("source_path_limit")
    return parts


def _archive_tree(raw: bytes) -> tuple[str, int, int]:
    if type(raw) is not bytes or len(raw) > MAX_ARCHIVE:
        _fail("source_archive_limit")
    root: dict[str, object] = {}
    folded: set[str] = set()
    folded_parts: dict[str, str] = {}
    total = 0
    count = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ENTRIES:
                _fail("source_entry_limit")
            for info in infos:
                if (info.is_dir() or info.orig_filename != info.filename or info.create_system != 3
                        or info.flag_bits & 1 or info.file_size < 0 or info.compress_size < 0):
                    _fail("unsupported_source_archive_entry")
                parts = _path(info.filename)
                folded_name = "/".join(parts).casefold()
                if folded_name in folded:
                    _fail("duplicate_source_path")
                folded.add(folded_name)
                for index in range(1, len(parts) + 1):
                    prefix = "/".join(parts[:index])
                    previous = folded_parts.setdefault(prefix.casefold(), prefix)
                    if previous != prefix:
                        _fail("case_colliding_source_path")
                mode = info.external_attr >> 16
                if mode not in (stat.S_IFREG | 0o644, stat.S_IFREG | 0o755):
                    _fail("unsupported_source_mode")
                total += info.file_size
                if total > MAX_EXPANDED:
                    _fail("source_expansion_limit")

                node = root
                for part in parts[:-1]:
                    existing = node.get(part)
                    if existing is None:
                        existing = {}
                        node[part] = existing
                    if type(existing) is not dict:
                        _fail("source_path_collision")
                    node = existing
                leaf = parts[-1]
                if leaf in node:
                    _fail("source_path_collision")

                blob_hash = hashlib.sha1(b"blob " + str(info.file_size).encode("ascii") + b"\0")
                read = 0
                with archive.open(info, "r") as stream:
                    while True:
                        chunk = stream.read(65536)
                        if not chunk:
                            break
                        read += len(chunk)
                        if read > info.file_size:
                            _fail("source_size_drift")
                        blob_hash.update(chunk)
                if read != info.file_size:
                    _fail("source_size_drift")
                node[leaf] = ("file", b"100755" if mode & 0o111 else b"100644", blob_hash.digest())
                count += 1
    except SourceProvenanceError:
        raise
    except (OSError, RuntimeError, EOFError, zipfile.BadZipFile, NotImplementedError):
        _fail("invalid_source_archive")

    if count != len(folded):
        _fail("source_entry_limit")

    def tree_oid(node: dict[str, object]) -> str:
        rows: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            name_bytes = name.encode("utf-8", "strict")
            if type(value) is dict:
                oid = bytes.fromhex(tree_oid(value))
                mode = b"40000"
                key = name_bytes + b"/"
            else:
                _, mode, oid = value
                key = name_bytes + b"\0"
            rows.append((key, mode + b" " + name_bytes + b"\0" + oid))
        rows.sort(key=lambda row: row[0])
        return _object_id(b"tree", b"".join(row for _, row in rows))

    return tree_oid(root), total, count


def _commit_tree(commit_object: bytes, expected_commit: str) -> str:
    if (type(commit_object) is not bytes or not commit_object or len(commit_object) > MAX_COMMIT_OBJECT
            or not _OID.fullmatch(expected_commit)
            or _object_id(b"commit", commit_object) != expected_commit):
        _fail("source_commit_object_mismatch")
    headers, separator, _message = commit_object.partition(b"\n\n")
    if not separator:
        _fail("invalid_source_commit_object")
    trees = [line[5:] for line in headers.split(b"\n") if line.startswith(b"tree ")]
    if len(trees) != 1:
        _fail("invalid_source_commit_tree")
    try:
        tree = trees[0].decode("ascii")
    except UnicodeError:
        _fail("invalid_source_commit_tree")
    if not _OID.fullmatch(tree):
        _fail("invalid_source_commit_tree")
    return tree


def verify(raw: bytes, proof_base64: str, expected_commit: str) -> dict[str, object]:
    """Verify a raw Git commit object against the exact ZIP tree and return receipt."""
    if (type(proof_base64) is not str or len(proof_base64) > ((MAX_COMMIT_OBJECT + 2) // 3) * 4
            or not proof_base64.isascii()):
        _fail("invalid_source_commit_proof")
    try:
        commit_object = base64.b64decode(proof_base64, validate=True)
    except (ValueError, TypeError):
        _fail("invalid_source_commit_proof")
    if len(commit_object) > MAX_COMMIT_OBJECT:
        _fail("invalid_source_commit_proof")
    commit_tree = _commit_tree(commit_object, expected_commit)
    archive_tree, expanded, entries = _archive_tree(raw)
    if archive_tree != commit_tree:
        _fail("source_tree_mismatch")
    return {
        "scheme": SCHEME,
        "commit_oid": expected_commit,
        "tree_oid": commit_tree,
        "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "commit_object_sha256": hashlib.sha256(commit_object).hexdigest(),
        "expanded_bytes": expanded,
        "entries": entries,
    }


def verify_tree_binding(raw: bytes, binding: object) -> dict[str, object]:
    """Recheck stored source bytes against a previously verified immutable binding."""
    if (type(binding) is not dict or set(binding) != {
            "scheme", "commit_oid", "tree_oid", "archive_sha256", "commit_object_sha256",
            "expanded_bytes", "entries"}
            or binding.get("scheme") != SCHEME or not _OID.fullmatch(binding.get("commit_oid", ""))
            or not _OID.fullmatch(binding.get("tree_oid", ""))):
        _fail("invalid_source_binding")
    tree, expanded, entries = _archive_tree(raw)
    if (tree != binding["tree_oid"] or hashlib.sha256(raw).hexdigest() != binding["archive_sha256"]
            or expanded != binding["expanded_bytes"] or entries != binding["entries"]):
        _fail("source_binding_mismatch")
    return binding
