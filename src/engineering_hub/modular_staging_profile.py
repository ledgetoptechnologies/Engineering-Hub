"""Closed, nonsecret modular-staging profile identity; no execution authority."""
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation


SCHEMA = 'modular-staging-profile-v1'
DOMAIN = b'engineering-hub:modular-staging-profile:v1\x00'
FIELDS = frozenset({'schema', 'image_id', 'build_argv', 'test_argv', 'timeout_seconds',
                    'memory_mb', 'pids', 'output_bytes', 'log_bytes', 'workspace_bytes', 'cpu_millis'})


def canonical_profile(value):
    """Return a detached closed profile. Caller must not include secret values."""
    if type(value) is not dict or set(value) != FIELDS or value.get('schema') != SCHEMA:
        raise ValueError('invalid profile schema')
    if type(value['image_id']) is not str or re.fullmatch(r'sha256:[0-9a-f]{64}', value['image_id']) is None:
        raise ValueError('invalid image identity')
    for key in ('build_argv', 'test_argv'):
        argv = value[key]
        if (type(argv) is not list or not 0 < len(argv) <= 32
                or any(type(x) is not str or not 0 < len(x) <= 2048 or '\x00' in x for x in argv)):
            raise ValueError('invalid arguments')
    limits = {'timeout_seconds': (1, 900), 'memory_mb': (32, 1024), 'pids': (8, 128),
              'output_bytes': (1024, 64 * 1024 * 1024), 'log_bytes': (1024, 1024 * 1024),
              'cpu_millis': (1, 1000), 'workspace_bytes': (65536, 1024 * 1024 * 1024)}
    for key, (low, high) in limits.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError('invalid limits')
    if (value['log_bytes'] > value['output_bytes'] // 4
            or value['workspace_bytes'] > value['memory_mb'] * 1024 * 1024 // 2):
        raise ValueError('invalid limits')
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


def profile_bytes(value):
    return json.dumps(canonical_profile(value), sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def profile_digest(value):
    return 'sha256:' + hashlib.sha256(DOMAIN + profile_bytes(value)).hexdigest()


def from_worker_recipe(value):
    """Convert exact effective worker recipe; unsupported extras fail closed."""
    required = FIELDS - {'schema', 'cpu_millis'}
    if type(value) is not dict or set(value) != required | {'cpus'}:
        raise ValueError('unsupported worker recipe')
    cpu = value['cpus']
    if type(cpu) not in (int, float):
        raise ValueError('invalid CPU')
    try:
        milli = Decimal(str(cpu)) * 1000
        if not milli.is_finite() or milli != milli.to_integral_value():
            raise ValueError('noncanonical CPU')
        result = {key: value[key] for key in required}
        result.update(schema=SCHEMA, cpu_millis=int(milli))
        return canonical_profile(result)
    except (InvalidOperation, OverflowError):
        raise ValueError('invalid CPU') from None
