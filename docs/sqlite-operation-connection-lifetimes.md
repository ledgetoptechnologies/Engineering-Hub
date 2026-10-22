# Repository-owned SQLite connection lifetimes

SQLite connection context managers commit or roll back; they do not close the
connection. MessageRepository, KnowledgeRepository and SemanticIndex now close
their operation-owned connections after the existing transaction context exits.
Factory setup failures also close handles that were never returned. The bus
health probe closes the raw connection it owns.

Public raw `connect()` methods remain caller-owned. A borrowed connection passed
to `MessageRepository.get()` remains open for its caller. Schema, locking,
transactions, CAS/version checks, endpoints and authentication are unchanged.

## Evidence and limits

Hermes matched six unclosed-connection warning identities to these factories on
Python 3.14. A bounded baseline f479488 test reproduced the problem; it was not
introduced by the optional generation-drain feature. Root also reproduced the
MessageRepository initialization warning with Python 3.13 and tracemalloc before
the fix, then observed no warning for the same synthetic diagnostic afterward.

The focused isolated Linux Python 3.12 run passed 90 tests in 9.09 seconds with
ResourceWarning and PytestUnraisableExceptionWarning treated as errors. New tests
track actual connection closure, commit/rollback, initialization/setup failures,
borrowed ownership, knowledge CAS, semantic indexing/search/deletion and the real
ASGI health route. Semantic tests use a fake embedder and no network/model fetch.
Independent source review accepted the patch. Full-suite and independent Python
3.14 verification of this follow-up must be recorded separately.

This does not prove every external caller of a public raw connection closes it.
Test-owned connections and other unrelated warning sources are not silently
suppressed. No live database, service, credential or deployment is changed by
this source patch; actual NAS and restart acceptance remain separate gates.
