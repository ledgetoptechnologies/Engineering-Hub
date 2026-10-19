# Explicit host-executor service composition

`hub_container_executor.service.serve_owned_host_executor` supplies the owned
listener lifecycle for the existing `PilotASGI` and `HostAdapter`. It is a
callable installation seam, not an installed service, configuration loader,
Docker discovery mechanism or completed pilot acceptance runner. Ordinary Hub
startup and the current MCP do not select it.

## Operator contract

The trusted operator launcher calls the asynchronous function with:

- A synchronous `resource_factory` returning a new synchronous context manager.
  Successful entry yields an exact, dedicated `HostAdapter`, not a subclass or
  borrowed adapter. Required dependencies are explicit: record and metadata
  stores, provenance verifier, backend, baseline provider, state provisioner,
  versioned test-secret resolver and driver-identity verifier.
- An explicitly resolved private executor `bearer_token`, not a command-line
  token or environment-selected discovery mechanism. Do not reuse live MCP
  data or tokens for a disposable application fixture.
- An explicit `port`, with literal `host='127.0.0.1'` or `host='::1'` only.
  Port zero is supported when deliberately supplied for isolated tests. No LAN
  binding, hostname resolution or remote Docker endpoint is selected here.
- `supervisor_stop_grace_seconds` at least the existing 1120-second source
  requirement. Proposed installed grace is 1200 seconds; the launcher assertion
  is not evidence that an actual supervisor honors it.
- Optional `stop_event`, `on_ready(bound_address)` and `install_signals` for
  explicit ownership by the enclosing launcher. Readiness can be asynchronous;
  it reports listener startup, not driver authentication or machine readiness.

The socket is bound before the resource factory runs. Bind failure cannot open
the adapter ledger or acquire its resources. The service validates the exact
adapter type and required provider presence; those checks do **not** authenticate
the providers, prove independent enrollment or establish fresh exclusive use.
The real build, driver, baseline and current-host admission authorities remain
mandatory operator dependencies. No permissive replacement is supplied.

Factory construction and context entry own their partial failures. Once an
adapter is accepted, the service owns closing it. The provider context must not
destroy its dependencies while the service is running. Context exit occurs on
an executor thread after adapter close; it must support that threading contract
and attempt cleanup of all owned providers even if a close fails. A trusted
`ExitStack` is suitable for recording provider closers in dependency order.
The context may idempotently close the adapter as its own defensive cleanup,
but must not return another service's adapter or resources. The service cannot
prove arbitrary factory cleanup correctness from its return type.

## Shutdown and recovery

The service reuses the existing listener and cancellation-drain helpers rather
than introducing another dispatcher. Stop closes new request admission before
awaiting shutdown. HTTP requests already admitted, including accepted synchronous
adapter work, retain their original lifetime. No wall-clock timeout is used to
kill an accepted container effect or mark it terminal.

Repeated cancellation retains the same ownership operation through HTTP drain,
adapter close and provider-context exit. Adapter close runs off the event loop
before provider cleanup. The listener stops accepting during drain: Uvicorn can
close the bound socket before outstanding requests finish. Port closure or a
successful new bind is therefore **not** proof of quiescence or ledger ownership
release. The outer binding context finishes after resource cleanup, while the
adapter/provider ownership remains retained until that cleanup is terminal.
Cleanup exceptions are
sanitized, and a context cannot suppress a service failure and report success.
Neither startup nor teardown reconciles, replays, unpauses or adopts old commands.
The controller's original persisted command/run inspection rules remain in
force. This does not remove the host's existing explicit POST `/reconcile`
operation, which inspects original operations without replay. No reconciliation
is called automatically by the service. Lease expiry does not prove that an
effect stopped.

## Concrete local resource assembly

`hub_container_executor.local_resources.local_host_adapter_resources` is the
explicit synchronous resource context for this listener. A trusted launcher may
bind its operator-reviewed arguments using `functools.partial` and pass that
zero-argument factory to `serve_owned_host_executor`. This is source composition,
not a configuration or enrollment endpoint. No ordinary Hub entrypoint selects
it automatically.

It assembles the real local content stores, versioned test-secret resolver,
operator-enrolled build verifier, protected baseline reader, frozen SQLite state
provisioner, private operation journal, Docker Engine backend and host adapter.
Both candidate and rollback records are checked against the explicitly supplied
build enrollments and their OCI metadata before writable state is opened.
Enrollment must already have been independently approved; the helper must not
derive an approval from the record it is checking.

The operator supplies the exact manifest and daemon binding; pre-provisioned
private paths; independent build enrollments; baseline receipt and runtime-profile
digest pins; immutable test-secret bindings; explicit journal/reservation
create-or-reopen choices; and a caller-owned driver-identity verifier. There is
no default verifier, test transport, temporary-storage override, environment
discovery, permission repair or directory provisioning. A callable verifier
interface is not evidence that its implementation is trustworthy.

The daemon binding's generation root is the only generation root. Dedicated
ledger and journal parent directories, content/receipt/secret roots and state
reservation storage must be mutually disjoint from application generations and
each other. This stricter composition contract keeps application writes away
from executor keys, receipts, journals and approval material. It does not retrofit
the same restriction into unrelated existing callers of the underlying classes.
Current UID/GID must be 10001 in the process's user namespace; this does not prove
the initial-host identity or establish a usable rootless bind-mount mapping.

Construction reads protected local metadata but sends no Docker requests or
health probes and starts no jobs. Historical receipt verification is not fresh
health evidence; the state provider's actual backup and baseline-file byte checks
still occur at operation admission. Journal/reservation creation and adapter
ledger opening are real local writes, not a dry run or one transaction across
files. The adapter ledger retains its existing guarded create/reopen semantics.
Partial state is preserved for operator inspection after failure, not deleted,
automatically adopted or retried.

All acquired resources register cleanup immediately in dependency order. Exit
closes the adapter before its backend/journal/state/read-only providers and
attempts remaining cleanup even if an individual close fails. The supplied
driver verifier remains caller-owned. When used by the owned listener, this
context exits only after accepted HTTP and raw executor work has drained.

## Installation remains separate

The current production Hub Dockerfile installs the `src` distribution and the
dashboard. It does **not** copy or install the root-level `hub_container_executor`
package. The validation Dockerfile explicitly copies that package, which is why
source tests can import it. A passing validation image is not a shipped executor
artifact; a separate, reviewed executor packaging/launcher composition is still
required. Do not patch the live MCP image or mount a mutable checkout to hide
this installation gap.

The smallest next installation path is an independently approved executor release,
not another permissive configuration layer. A concrete runtime verifier must
measure the actual imported implementation against an independently protected
release enrollment in a boundary the executor cannot rewrite. Approval cannot be
derived from the same mutable checkout, expected manifest commit or Docker backend
echo being checked. The launcher must also exclude alternative import/code-overlay
paths. The [signed launch-receipt consumer](executor-launch-identity.md) now provides
the bounded identity interface for an independently approved immutable executor
artifact. It still requires a real authenticated direct issuer exchange and does
not install that launcher or establish actual process/container correlation.

Use the [pilot preparation proposal](staging-pilot-preparation-2026-10-19.md)
for the proposed topology and unresolved host inputs. This function supplies no
JSON/module-name loader, public enrollment endpoint, installed driver-identity
provider, target-readiness authority, systemd unit, rootless UID mapping,
credential provisioning, startup registration or PC-reboot verification.

Linux integration tests for this seam use real loopback HTTP, the actual
`ContainerPilotClient`, `HostAdapter` and private SQLite command ledger, but
**synthetic providers and Docker effects**. They must not be reported as an
actual Docker deployment, independently attested build, TrueNAS acceptance or
successful pilot on the staging PC.
