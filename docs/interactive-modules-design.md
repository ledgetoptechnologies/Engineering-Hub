# Agent-defined interactions and bounded modules

Status: requirements/design, not a deployed code runner. Beau explicitly asked
for agents to supply reusable interactive tools and small services through EH,
without a Docker container per interaction. Native labeling and richer decisions
are the first slice, not completion of this requirement.

## User experience

An agent attaches an interaction to a project/topic and an inbox item. Beau
opens it in EH, sees the relevant maps/photos/context, supplies answers or makes
a selection, and can later find both the original material and the result.
No project-specific route, species choice or task workflow is hardcoded into
the interaction framework. Participants share the same durable records rather
than relying on chat memory or a provider-specific artifact.

## Two complementary execution modes

1. Native, data-defined components: forms, label sets, tables, images, maps and
   comparison panels. Agents submit schema/data; trusted EH components render
   them. Most human-input workflows need no custom runtime.
2. Versioned code modules for interactions native components cannot express:
   bounded handlers and, where necessary, custom sandboxed widgets. A single
   reusable isolated module host runs these, not one Docker container per module.
   EH presents their UI and brokers explicit capabilities. It does not import
   agent Python/JavaScript into the privileged Hub writer process.

The runtime location must be chosen from actual equipment and verified isolation;
the staging PC is a candidate, not an enrolled service. One separately constrained
runtime service is different from provisioning a container for every widget.
No new server, Docker service or Cloudflare deployment is authorized by this file.

## Contract to freeze with Claude

- Manifest: stable module ID, project/topic placement, version/source digest,
  input/output schemas, entrypoints, proposer, independent review and budgets.
- Lifecycle: draft, validating, active, disabled/failed; immutable versions,
  explicit instance identity, bounded concurrency and cancellation. A module
  failure must not take down ordinary notes, messages or decisions.
- Capabilities: namespaced state with CAS and bounded dataset reads, requested
  by the manifest and intersected with host policy. No ambient EH tokens,
  session cookies, database path, filesystem, shell, Docker socket or outbound
  networking. Human identity comes from EH, never module input.
- Results: typed submissions tied to module version, input revision, project,
  human identity and time. Retain history/export and show stale/incomplete
  states honestly. Module output is data, never deployment permission.
- Browser: native rendering by default. Custom code must use a separately
  constrained origin/opaque sandbox and a validated, instance-bound message
  bridge; never trusted-origin innerHTML or arbitrary script tags. The trusted
  EH shell presents the canonical decision and saves the human's authenticated
  response. Agent code cannot approve its own request.
- Limits: source/data/output sizes, CPU/fuel, memory, wall time, requests,
  storage and instance counts. Reject imports or requested capabilities that
  cannot be enforced. No arbitrary package installation or always-on services
  implicitly created by module registration.

## Runtime research boundary

The Cloudflare Sandbox SDK guidance is primarily for hosted container workloads;
it is not automatically a fit for the existing self-hosted NAS/staging setup.
No SDK package or cloud runtime has been added for this design.

Bare `workerd` is not a complete hostile-code security boundary. Its upstream
README requires additional secure isolation for possibly malicious code. Wasm
runtime capability/fuel controls are candidates but also need exact version,
host-call, resource-exhaustion and escape-resistance assessment. Do not substitute
a timeout around eval/exec, Node vm or a Python subprocess for that verification.

References:
- https://github.com/cloudflare/workerd/blob/main/README.md
- https://developers.cloudflare.com/workers/local-development/
- https://docs.wasmtime.dev/security.html
- https://docs.wasmtime.dev/examples-interrupting-wasm.html

## Acceptance still required

Prove one generic module with two different project datasets; human input durable
across refresh/restart; concurrent CAS conflict; exact source version attribution;
bounded infinite loop, memory/output bomb, filesystem/network/secret-access
attempts denied; disabled module cannot invoke; stale widgets cannot submit into
a replacement instance; no cross-project read/write; results cannot fabricate a
human approval. Source mocks alone are not installed-runtime isolation evidence.

The current native labeling/context work must preserve existing live auth,
ports, tokens, data and Compose. Runtime activation, public exposure or new
permissions require an independently reviewed, appropriately authorized handoff.
