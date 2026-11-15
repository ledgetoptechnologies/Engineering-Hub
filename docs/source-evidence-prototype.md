# Source observation prototype

`GET /v2/source-evidence/{source_id}?project=...&digest=...&binding=...` is
disabled by a source-code interlock. The binding is the caller's separately
trusted, exact original seven-field binding serialized as JSON. It uses the
existing worker bearer and peer checks. This
credential is worker-wide; it is not a project-scoped end-user authorization.
No Hub route, task, source retry, cleanup, queue transition, or hold release is
connected to this observation.

`present` means the currently opened worker ledger has a matching source row,
an incarnation-matched persisted Git binding exactly equal to the separately
supplied original, and a matching descriptor-read ZIP whose digest and Git tree
verify. `journal_match_tree_checked` means journal equality plus tree checking;
it is not a new commit-object verification. The original commit-object bytes are
not retained, so its hash and commit OID rely on the separately trusted original
binding and earlier admission. A returned observation is neither an authenticated worker/ledger
epoch nor independent evidence that this is the original worker or host.

`conflict` reports a current row with the requested ID but different project or
digest. `unknown` includes missing, expired, orphaned, replaced, corrupt,
unavailable and unsupported states. Neither status establishes whether a
previous POST committed or whether an in-flight writer can still commit. It is
never permission to repost, release, delete, or declare no effect. The worker's
same-process lock does not exclude another process or host rollback. A durable
identity, retention/tombstone, independent writer/rollback boundary and
reconciliation protocol remain prerequisites before automatic use.

The POSIX implementation reads via nofollow descriptor relative to the opened
source directory, validates stable descriptor/path metadata and uses no journal
mutation. Unsupported platforms return `unknown`. This does not protect against
a privileged writer or establish exclusive host custody.
