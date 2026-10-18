# Frozen legacy archive test resources

The six Python/SQL resources in this directory are unchanged copies from
`src/agent_message_bus/` at Engineering-Hub commit
`2001ee7cf0b24cce12b7340904476d06c36593dc` (the dereferenced `v0.6.0` tag).
They are test-only schema producers, not runtime modules or migration code.
Their original SHA-256 values remain enforced in
`tests/test_hub_legacy_archive.py`; changing the live package must not change
these resources or relax those expected digests.

Tests build synthetic legacy data by loading these modules under a unique
namespace. No credentials, original messages, datasets, Git checkout or network
are needed. Tests must never import these resources into a running Hub service.
