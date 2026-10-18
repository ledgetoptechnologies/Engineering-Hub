"""Build identity baked into the installed package, never runtime configuration.

This is an identity claim, not an attestation: the pilot must still verify the
image's immutable provenance. Missing or malformed metadata is unknown.
"""
from importlib import resources
import json
import re


_RESOURCE = "_build_identity.json"
_MAX_BYTES = 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate build metadata key")
        result[key] = value
    return result


def read_build_commit() -> str | None:
    """Read only the fixed package resource, failing closed to unknown."""
    try:
        with resources.files("engineering_hub").joinpath(_RESOURCE).open("rb") as source:
            raw = source.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (type(data) is not dict or set(data) != {"schema_version", "build_commit"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1):
            return None
        commit = data["build_commit"]
        if type(commit) is str and re.fullmatch(r"[0-9a-f]{40}", commit):
            return commit
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        pass
    return None
