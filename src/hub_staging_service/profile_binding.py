"""Inert building block for a future profile-bound worker protocol.

The experimental worker admission path calls this module and executes its
detached snapshot. Independent Hub and worker interlocks remain disabled.
Transport authentication and reviewed Hub admission are separate requirements;
this value check does not grant authority.
"""
import json
import re

from .modular_staging_profile import from_worker_recipe, profile_digest

_NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
_PROJECT = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')
_DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')


def checked_recipe_snapshot(project, recipe_name, expected, effective_recipe):
    """Validate the closed reviewed tuple against a detached effective recipe.

    This validates a value; it neither authorizes a worker request nor proves
    that the detached value was used for execution. The caller must enforce
    both properties across the durable operation and terminal receipt.
    """
    if (type(project) is not str or _PROJECT.fullmatch(project) is None
            or type(recipe_name) is not str or _NAME.fullmatch(recipe_name) is None
            or type(expected) is not dict
            or set(expected) != {'project', 'recipe', 'generation', 'digest'}
            or expected['project'] != project or expected['recipe'] != recipe_name
            or type(expected['generation']) is not int or not 1 <= expected['generation'] < 2**31
            or type(expected['digest']) is not str or _DIGEST.fullmatch(expected['digest']) is None):
        raise ValueError('invalid reviewed profile tuple')
    try:
        # Reject unsupported runtime fields rather than dropping them. JSON
        # detaches nested argv before the exact detached copy is hashed.
        from_worker_recipe(effective_recipe)
        snapshot = json.loads(json.dumps(effective_recipe, allow_nan=False))
        canonical = from_worker_recipe(snapshot)
        if profile_digest(canonical) != expected['digest']:
            raise ValueError('reviewed profile mismatch')
        return snapshot
    except (TypeError, ValueError, OverflowError):
        raise ValueError('reviewed profile mismatch') from None
