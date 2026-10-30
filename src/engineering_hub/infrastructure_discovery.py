"""Pure normalization of an already fetched active-target list.

The caller supplies trusted inventory aliases and enforces transport budgets. Host
labels are display/grouping hints, never authority or proof of machine identity.
Only exact aliases associate inventory; absent and ambiguous matches remain visible.
Duplicate (job, instance) pairs are discarded entirely. Valid unique rows are
sorted before the 200-row cap, making truncation independent of input order.
The omitted count is the number of valid unique rows beyond that cap. Invalid
rows and aliases are ignored; warnings are a sorted, closed set of codes.
"""

import hashlib
import json
import re

MAX_TARGETS = 200
# Separate decoded-input cap lets discovery truncate outputs without accepting
# an unbounded target array. Transport byte limits still belong to the reader.
MAX_INPUT_TARGETS = 4096
MAX_INVENTORY_ENTRIES = 4096
_EXPORTERS = {
    'node': 'node', 'node_exporter': 'node',
    'windows': 'windows', 'windows_exporter': 'windows',
    'nvidia': 'nvidia', 'nvidia_exporter': 'nvidia', 'nvidia-gpu': 'nvidia',
    'dcgm': 'nvidia', 'dcgm_exporter': 'nvidia',
}
_SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')


def _plain(value, maximum):
    if (type(value) is not str or not 0 < len(value) <= maximum
            or value.strip() != value
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        return False
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def normalize_targets(targets, inventory):
    """Return {rows, warnings, omitted_count} for active Prometheus targets.

    Inputs are lists: targets contain a labels dict; inventory entries contain a
    slug device_id and a list of exact hostnames. Invalid top-level inputs are
    treated as empty. Invalid individual inventory entries or aliases are ignored.
    Association is matched, unregistered, or ambiguous; probe_id is always
    None because discovery does not establish a registry probe mapping. Exporter
    inference uses only the exact documented job names, otherwise it is None.
    No target fields other than bounded labels are copied to the output.
    """
    warnings = set()
    aliases = {}
    if type(targets) is list and len(targets) > MAX_INPUT_TARGETS:
        raise ValueError('discovery_target_limit')
    if type(inventory) is list and len(inventory) > MAX_INVENTORY_ENTRIES:
        raise ValueError('discovery_inventory_limit')
    if type(inventory) is list:
        for item in inventory:
            if (type(item) is not dict or type(item.get('device_id')) is not str
                    or not _SLUG.fullmatch(item['device_id'])
                    or type(item.get('hostnames')) is not list
                    or len(item['hostnames']) > 32):
                continue
            for hostname in item['hostnames']:
                if _plain(hostname, 64):
                    aliases.setdefault(hostname, set()).add(item['device_id'])

    candidates = {}
    if type(targets) is not list:
        targets = []
        warnings.add('invalid_target')
    for target in targets:
        labels = target.get('labels') if type(target) is dict else None
        if (type(labels) is not dict or not _plain(labels.get('job'), 128)
                or not _plain(labels.get('instance'), 128)):
            warnings.add('invalid_target')
            continue
        key = (labels['job'], labels['instance'])
        # None is a permanent duplicate marker; do not retain duplicate lists.
        candidates[key] = None if key in candidates else labels

    rows = []
    for (job, instance), labels in sorted(candidates.items()):
        if labels is None:
            warnings.add('invalid_target')
            continue
        safe_labels = {key: labels[key] for key in ('host', 'role', 'env')
                       if _plain(labels.get(key), 64)}
        host = safe_labels.get('host')
        if host is None:
            canonical = json.dumps([job, instance], ensure_ascii=True, separators=(',', ':'))
            host_key = 'target:' + _hash(canonical)
        else:
            host_key = 'host:' + _hash(host)
        matches = aliases.get(host, set()) if host is not None else set()
        association = 'matched' if len(matches) == 1 else 'ambiguous' if matches else 'unregistered'
        if association == 'ambiguous':
            warnings.add('ambiguous_inventory')
        exporter = _EXPORTERS.get(job)
        if exporter is None:
            warnings.add('unknown_exporter')
        rows.append({'host_key': host_key, 'host_label': host, 'association': association,
                     'device_id': next(iter(matches)) if len(matches) == 1 else None,
                     'probe_id': None, 'job': job, 'instance': instance,
                     'exporter': exporter, 'labels': safe_labels})
    omitted = max(0, len(rows) - MAX_TARGETS)
    if omitted:
        warnings.add('targets_truncated')
    return {'rows': rows[:MAX_TARGETS], 'warnings': sorted(warnings), 'omitted_count': omitted}
