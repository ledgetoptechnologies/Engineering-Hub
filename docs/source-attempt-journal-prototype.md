# Experimental source-attempt journal

This disabled worker-side prototype records one immutable source identity as
`uncertain` before a source deposit. It never automatically retries that attempt.
A local post-effect observation can change the retained state to `historical_observation`,
which is evidence only: neither state establishes a worker epoch, an external
fence, authority to release a hold, or proof that absence means no effect.

The journal has a fixed 4,096-record limit and refuses new attempts when full.
There is no deletion, compaction, or archival endpoint. Operators must retain
the original journal; recovery from capacity exhaustion is not yet designed.
The v1 source path checks the retained journal within its effect transaction
and refuses an ID reserved by an attempt, even when this feature is later
disabled. Unrelated v1 source IDs continue through the legacy path. This is
not a security boundary against old binaries, other database writers, or
in-process callers. The HTTP endpoints rely on the current worker-wide peer
and bearer admission, not project- or installation-scoped credentials.
Existing v1 receipt retries take a SQLite writer transaction when the attempt
table exists; the old read-only retry path is retained when it does not. If a
separate writer creates that table concurrently with the old read-only retry,
ordering is not guaranteed. Such writers are outside the single-owner worker
assumption and require the separately unimplemented external fence.

The prototype uses the existing source writer after persisting the intent. Its
file fsync and SQLite commit do not establish power-loss safety or durable
directory entries; external writers, host replacement, ledger restoration,
same-account interference, and old binaries remain outside its guarantees.
The record therefore cannot authorize automatic source replay, release, or
dispatch, even after a `historical_observation` response. Source cleanup may
remove a source after the observation; a separate evidence query is necessary
to inspect its current presence. A trusted original-worker identity,
independent rollback boundary, receiver drain and authorization contract remain
necessary before any production use.
