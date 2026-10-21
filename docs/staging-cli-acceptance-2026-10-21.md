# Fresh installed TEST launcher acceptance

Status: actual staging startup, receipt exchange, GET-only authentication and
exact-owned graceful shutdown **passed** on 2026-10-21. This is not an application
deployment, permanent owner service, NAS connection or reboot acceptance.

## Bound artifacts and execution

- Host source: `3d558fdecfd5a245ee4b59b6e0c115fc4d58283e`.
- Host wheel: SHA-256
  `3a903d12daa3b467d7cf2798f7c45d2d94ce7cfd9a751f5a2b9d1bcb21f1966f`,
  122990 bytes. It was imported from a sealed memfd after protected runtime
  validation; it did not replace the existing dependency installation.
- Consumer/dependency source: `d5656bfd9271a1b1c831294f544c909d33652b3b`;
  core source: `cce316a326c8db1d681ad7e591f45dd4cc60d3c0`.
- Fresh generation: `0afbc5d9-55ab-4f02-bd00-f7c490988f11`.
- Owning invocation: `bff7c72132964bf0bfb39ee47861810b`.
- Native launch: `856fcc95-88e0-4653-9a38-76a828003a27`.
- Exact consumer:
  `bab410f1b6ff6e61cd8f5c2b3cc6461915aff16a1f55abd32454dbc4c596c9e0`.

The original launch handle returned 0 after 2 minutes 10.898 seconds. Reported
usage was 10.386 seconds CPU, 86.2 MiB peak memory and zero swap. The dedicated
TEST aggregate remained capped at 1 CPU, 3.25 GiB memory and 256 PIDs, with no
swap. Actual consumer limits were 0.2 CPU, 256 MiB and 32 PIDs.

## Observed result

1. The actual corrected CLI served its authenticated receipt. Discovery checked
   the committed native journal, exact owning invocation, retained kernel
   process identities and enrolled consumer image/profile.
2. The observer checked actual mounts, namespaces, UID mapping, cgroups,
   no-new-privileges and zero capabilities. Exactly two non-retried executor
   GETs returned unauthenticated 401 and authenticated unknown-command 404.
   A 404 alone is not global-idle proof; this was a fresh generation without an
   external route, coordinator or other application caller.
3. One signal through the retained owning-launcher pidfd requested graceful
   stop. No guessed PID, signal retry, forced kill or generation adoption was
   used. No application command was submitted.
4. An independent read-only audit established native `stopped` and issuance
   `sent`, actual consumer exit 0/PID 0/non-OOM, both original host PIDs absent,
   and empty API/issuer socket directories. All command/run/target/phase/
   operation rows and private operation-journal rows were zero. The prior 16
   TEST container ID/state/image rows were unchanged; the new stopped consumer
   remains retained as evidence.

## Retained audit correction

The first post-stop audit failed before its database/Engine checks because it
expected completed transient systemd metadata to remain loaded. The successful
unit had already been garbage-collected. That failed audit is preserved.

A separately reviewed correction ran once and passed. A missing unit alone
cannot pass: its GC branch requires exact boot/PID/invocation-bound entry,
`receipt_served`, `stopped` and PID1 unit/invocation deactivation records in
monotonic order, plus both host PIDs absent. It then verifies the closed new
databases, actual Engine outcome and unchanged prior inventory. Numeric launcher
exit 0 comes separately from the original retained handle, not from fabricated
status after unit GC. The audit performs Engine GETs only and opens no historical
campaign SQLite journal.

## Public evidence pins

| Artifact | SHA-256 |
| --- | --- |
| Discovery observation | `8972434bbc334f4c9d904afd56bccdecbc03b86b52d6d0574ec56d4583844efd` |
| GET-only observation | `b77909edfd61340da2306d1a3c1879b85e7a51fea5344a62352b4312e9e1ef38` |
| Independent final audit | `df73c45f19a501759182f1cc3d5f3f734e973f36c59af5868913d585002b30d7` |
| Closed native journal | `340605eacfdbcf00e5b7dbdf9551bdc7a145e2acbc05e36765d13a7026a1b7e1` |
| Closed adapter ledger | `ee52d3d15a830e4bc4fdfce092d32a96b37bc9bd9f02ef3f4c943438197adaa2` |
| Closed operation journal | `a2231481acea1667b3dd172fb279745723f29e344ac2a8390111259fda1a2a19` |

Public observations and the original failed audit remain in the operator's
`eh-cli-ready-generation-0afbc5d9-draft` evidence folder and the Engineering Hub
shared note `evidence/fresh-cli-readiness-0afbc5d9-20261021.md`. No private keys,
bearers or credential-file identities are published here.

## Still required

The real NAS-only TLS route, protected Hub client enrollment, authoritative Hub
database enrollment, independent-review deployment/rollback through that route,
ongoing owner/drain contract and actual restart/reboot acceptance remain separate
requirements. The ordinary Hub/MCP paths, ports, tokens, production, PA/Viewer,
TrueNAS configuration and Cloudflare settings were not changed by this test.
