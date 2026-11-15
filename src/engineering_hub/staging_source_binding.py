"""Shared validation of persisted modular staging source bindings."""
import re

UNBOUND_HOLD = 'source_binding_unavailable'


def valid_source_binding(item):
    binding = item.get('source_binding')
    return (type(binding) is dict and set(binding) == {
                'scheme', 'commit_oid', 'tree_oid', 'archive_sha256',
                'commit_object_sha256', 'expanded_bytes', 'entries'}
            and binding.get('scheme') == 'git-sha1-tree-v1'
            and type(binding.get('commit_oid')) is str
            and re.fullmatch(r'[a-f0-9]{40}', binding['commit_oid']) is not None
            and binding['commit_oid'] == item.get('source_commit')
            and type(binding.get('tree_oid')) is str
            and re.fullmatch(r'[a-f0-9]{40}', binding['tree_oid']) is not None
            and type(binding.get('archive_sha256')) is str
            and re.fullmatch(r'[a-f0-9]{64}', binding['archive_sha256']) is not None
            and binding['archive_sha256'] == item.get('source_digest')
            and type(binding.get('commit_object_sha256')) is str
            and re.fullmatch(r'[a-f0-9]{64}', binding['commit_object_sha256']) is not None
            and type(binding.get('expanded_bytes')) is int and 0 <= binding['expanded_bytes'] <= 64 * 1024**2
            and type(binding.get('entries')) is int and 1 <= binding['entries'] <= 5000)
