"""Canonical modular staging work action identity."""
import hashlib
import json
import re


def staging_action_key(project, recipe):
    encoded = json.dumps([project, recipe], sort_keys=True, separators=(',', ':'),
                         allow_nan=False).encode()
    return 'staging-service.' + hashlib.sha256(encoded).hexdigest()[:24]


def legacy_staging_action(key, target=None):
    # Old configured actions used the full SHA-256. A modular-prefix row with
    # a staging submission binding also fails closed if its key is malformed.
    return (isinstance(key, str) and key.startswith('modular.') and
            (re.fullmatch(r'modular\.[0-9a-f]{64}', key) is not None or
             target is not None and target.submission_id is not None))
