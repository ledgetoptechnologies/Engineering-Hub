# Software staging checkpoint — 2026-10-18

Beau selected software workflows first. Bulk ODM/OpenSplat/TreeDAR processing
workers remain separate future work. This is **not** a report that the remote
staging installation is ready.

## Exact source/artifact verification

Reviewed source checkpoint: `a377e26e68140bf9abc5dd598914721bd535e3ba`, on
`work/hub-pilot-integration-codex`, not main. A clean `git archive` of that commit
was used as the complete Docker build context, excluding unrelated untracked
work and local datasets. No new production image was published or installed.

- Local validation image index:
  `sha256:402c3aa41620d043232e46d197e10f2f79b39c912408b63beb26ccfe5420890b`.
- Selected linux/amd64 manifest:
  `sha256:4feb828b8d7ee53232dffd5829742a47eb89a9f253422b105fac65d0a8cbdf8d`.
- Image config:
  `sha256:8d60fc69c9537cf92954d428dbf26bb7cd34aa2b0f3e3591e35520ae4f6aa914`.
- Full exact-image Linux suite: **2648 passed, 3 skipped, 3 warnings**, 93.48s.
  The skipped owner-change tests separately passed **3/3**, 0.31s, against the
  same image with only CHOWN and synthetic fixtures. Known warnings: Starlette,
  an unclosed asyncio test event loop, and deliberate multithreaded fork.
- Actual HTTP coordination smoke on that image passed real 401 handling,
  non-preemptive production priority with synthetic approval, machine exclusion,
  instance fencing, expiry hold and exact native SSE queue invalidation. This
  test's coordination-only actions did not execute anything.

## Actual local Engine validation

An explicit Windows driver connected to the existing local Docker Desktop Linux
Engine, using its pinned daemon identity and the preloaded exact image above.
The original local OCI archive bytes established the index/selected manifest/
config chain. Task reviews and operator enrollment were **synthetic fixtures**,
not a verified CI attestation or human production approval. No real token or
live Hub database was used. No host Docker socket was mounted into a container.

The first actual test invocation exited 4 because the test conftest creates a
data directory next to the read-only test tree. The backend correctly recorded a
verified failed receipt and released that job; it did not claim success. The
exact original stopped container was inspected and removed without force. Its
small synthetic intent/receipt databases were retained.

The driver was corrected to copy the fixed test tree into disposable `/tmp`
before invoking the fixed tests. Runtime isolation was not relaxed. The corrected
actual local acceptance passed:

- Fixed queue/API/bootstrap tests executed and exited zero.
- A separate fixed command exited 7 and produced a failed result, not success.
- Original image, effective sandbox profile, exit and quiescence were inspected.
- Trusted receipts survived reopening; a fabricated evidence ID was rejected.
- Both verified terminal jobs released their machine reservations.
- Unapproved production stayed queued; no deployment run or task promotion.
- Only the two exact receipt-bound, independently verified stopped containers
  were removed without force. A final local listing found none remaining.

The worker used **Windows test-only receipt storage**. Linux permissions were
tested separately in the exact-image suite; the Windows run does not establish
Linux private storage, remote staging, trusted launcher identity, reboot recovery,
application deployment/rollback or production execution acceptance.

## Remaining first-use gap

The new HTTP queue and local validation worker are not yet an authenticated
Hub-to-staging-PC execution service. Do not place the Hub database on SMB or let
agent assertions create trusted receipts. The remote adapter, its bounded
execution/receipt contract, private provisioning and installed lifecycle still
need implementation and actual-host acceptance. The default production verifier
and executable completion/reconciliation provider remain disabled.

After peer review, the configured bootstrap was further hardened to deny claims
entirely until a trusted server-side exact-worker/action/machine/target policy
is installed. Merely enabling the queue cannot wedge the machine by reserving
an unexecutable job. Optional action filters only narrow that policy. The local
acceptance harness explicitly enrolls its precise synthetic target and bound
receipt provider; this is not remote executor credential provisioning.

The staged agent API base remains `http://192.168.50.80:8770/api/hub/v1/work`,
and the Hub MCP listener is `http://192.168.50.80:8770/mcp/`; these new queue
capabilities are **candidate source**, not live installed endpoints. The old MCP
on 8765 is unchanged. Beau retains TrueNAS management/installation ownership.
