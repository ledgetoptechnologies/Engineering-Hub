"""Public decision projection; dispatch lineage is coordination-private."""


def public_decision(value):
    if isinstance(value, dict):
        value = dict(value)
        value.pop('origin_dispatch_id', None)
        value.pop('origin_dispatch_revision', None)
    return value
