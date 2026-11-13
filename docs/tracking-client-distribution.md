# Tracking client distribution preparation

This document and the source preparation utility are **not** an installed or
released client. The existing unauthenticated agent-session helper and its v1
manifest are unchanged. No route, session, identity or permission is created.

The client-only context contains just the tracking client, its shared attention
profile/MCP transport helpers, the existing pairing/profile module and their
package initializers. It deliberately excludes the Hub server, database,
fastembed, model and UI. It uses Python 3.12 and exact direct versions of MCP
and httpx; those packages have transitive dependencies, including platform
wheels. The separately resolved `uv.lock` and exported hash-pinned
`requirements.txt` include 30 dependency packages and preserve Windows/Linux
environment markers. They do not contain the wheel files and do not establish
which target wheels are locally available. A reviewed platform-qualified
wheelhouse is required before installation. Windows and Linux wheel availability,
native ABI, offline install and behavior remain to be verified per target.

On a trusted, task-owned build machine, obtain the exact reviewed full Git
commit, independently verify its objects and a canonical `git archive` against
that commit, then run `scripts/prepare_tracking_client.py --revision <full-id>
--output-directory <new-private-directory>`. The tool stages a fixed allowlist
without downloading, executing, installing or activating it, and records the
archive digest and staged file hashes in the package's provenance JSON. The
caller must control the parent directory and handle retention of the output.
The archive comment, self-reported commit and local provenance are not a
signature and do not establish the origin or trustworthiness of the archive.

Before a release, independently check the context against the reviewed source,
build the wheel in an isolated environment with a hash-verified build backend,
independently verify the lock and exported requirements, bind the resulting
wheel and dependency lock to the reviewed commit,
and verify every installed client file and dependency wheel. Use a separately
trusted release digest or signature: a manifest and archive obtained from the
same untrusted server do not independently authenticate each other. No release
signing, publication or automatic installation is implemented here.

Qualify a dedicated private virtual environment on each actual supported OS,
architecture and Python version. Supply an independently verified, complete
wheelhouse and hash-pinned requirements; install offline with wheel-only,
hash-required settings. Do not modify global Python or another agent's
environment. Test the complete explicitly invoked client against an isolated
Hub: session identity, current project grants, loss and retry of idempotent
writes, separate-family or human review, revocation, session expiry, error
redaction, redirects and proxy rejection. Verify Windows DPAPI works under the
same Windows user as the existing profile and Linux profile permissions. Do not
copy secrets, overwrite existing profiles or grant access as a shortcut.

Only after exact staging acceptance and the normal release gates can an owner
install a verified artifact on each agent host and explicitly grant the minimum
project capabilities. The client neither starts agents nor automatically reads
new requests or turns. A disconnected host, absent grant, stale session or
uninvoked client must not be reported as task tracking working everywhere.
