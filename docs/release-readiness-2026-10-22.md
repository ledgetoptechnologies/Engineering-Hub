# Release readiness — 2026-10-22 checkpoint

## Superseding PR52 recovery gate

The dated checkpoint below is retained as historical evidence, **not current
release readiness or an operator instruction**. In particular, its main478,
publication/billing and dormant-label statements are not current PR52 status.

PR52 head `76224e513f1d501875ed22e6f01972cecb77291d` passed 5,518 Linux tests
(68 skipped), 229 UI unit tests, 115 fixture browser tests and five exact-head
image checks. Those checks omitted two subsequently reproduced counterexamples:
protected decisions accepted off-menu free text through the API, and the old
`a333704dcf8c14ef071d4af434928e657e5baed0` reader returned500 for task,
decision and overview routes after candidate-format writes (clean control200).
These are revision-before-merge findings, not evidence that the live deployment
crashed for either reason. Later fixes require their own exact-source evidence;
the counts above must not be transferred to a changed head.

An image-only downgrade to a333 is **not an accepted post-write rollback**.
See [Hub record-format recovery](Hub-record-format-recovery.md) for the explicit
hold/forward-recovery gate, required preserved state, and acceptance criteria.
No compatible immutable recovery artifact has yet been accepted. Current-source
reopen tests prove neither old-image compatibility nor NAS restore/reboot safety.

The first protected, owned-inactive NAS upgrade is still a separate operator
gate. This document does not authorize automatic moves, legacy downgrade,
TrueNAS management, activation or changing the existing Compose. Beau applies
any eventual accepted NAS handoff. Subsequent historical text is retained below.

---

**Not a completed live staging rollout. No operator change is requested now.**
Current Compose, agent tokens, data, ports and Cloudflare policy stay unchanged.
This checkpoint separates source/container evidence from the outstanding live
acceptance. Older dated proposals are historical, not competing startup recipes.

## Observed authorities

| Surface | Evidence at this checkpoint | What it does not prove |
| --- | --- | --- |
| Live NAS Hub | Read-only `8770/health`: build `d143e5312f74f09e0ece93ea010b27561fb457a7`, Hub/bus databases and audit OK, native push healthy on local ZFS | New main is installed, correct-session wake, or executor readiness |
| Live authenticated overview | Autonomy `configured:false`, `state:disabled`, `running:false`; `runs:[]`; image observation source unconfigured | A reviewed task or connected agent is executing a job |
| Fresh direct-origin probes | Browser8771 returns401 both unsigned and with only a spoofed allowed-email header; unsigned agent overview8770 returns401. No cookie/token, proxy or redirect was used. | Every invalid-signature case, actual successful human sign-in or network isolation |
| Source main | `478d72db7e6e0d69b25007b2a505668bff9e0e6f`; fetched tree exactly matches tested `c7cdb513027d4ed925ea7eb189926f4b5cc37a1f` | Published registry artifact or NAS deployment |
| Local packaged QA image | `build-local-ui-c7cdb51`, resolved immutable identity `sha256:96d69d80097748f3c7c676e2818e9d6225a07e7d8cd897d78569abfe9873eb98` | Registry availability; its baked source identity is c7, not main478 |
| GitHub publication | Main478 runs `39820894382` / `39820894393` failed before verification jobs started; annotations identify account payments/spending restriction; publish skipped | A source failure, a usable new `latest`, or permission to bypass billing |

The existing review-only live task `5543d17a-d04b-4092-bdbe-a08867c2a91b`
is owned by Claude and reviewed by Hermes for **d143e53**, revision4. It is not
review or activation authority for main478. Preserve that task/history; do not
silently replace its candidate or relabel it a completed live pilot.

## Requirement-by-requirement acceptance

| Requirement | Verified evidence | Remaining scope |
| --- | --- | --- |
| Preserve MCP identity/endpoints/state | Existing live MCP authenticated reads work at NAS agent8770; installed health is healthy. Local packaged three-agent/unified37-tool fixture preserves synthetic records across two recreations, with browser8771 and no alias. | Real operator upgrade parity, installed artifact identity, retained live delivery state and recovery acceptance. Do not revive8765. |
| Shared tasks, decisions, notes and plans | Real overview contains existing records. The dated actual browser observation in the pilot acceptance document proved an agent-authored note readable in the authenticated live UI. Current source/UI tests cover create/edit/conflicts/navigation and separate topic plans versus EH improvement backlog. | Deploy current UI; recheck actual authenticated session and real record flow without treating fixture data as live data. |
| Human sign-in | Beau selected a separate Hub Cloudflare Access application, superseding the earlier GitHub-only design. Actual 2026-10-21 browser observation showed Access identity and no extra login gate; direct unsigned/spoofed origin cases were denied. | Current deployed revision, both authorized accounts, session expiry/revocation and requested30-day behavior are not proved by that observation. Do not change Ops or policy as part of a source update. |
| Independent review, immutable deploy/health/rollback | The real isolated staging campaign5550 completed native queued failed-candidate deployment, independent unhealthy check, immutable healthy rollback and clean shutdown; source has exact task/review/artifact fencing. | A current published/enrolled artifact and actual live NAS-owned dispatch. Source-only reviews do not transfer authority to a different commit. |
| Machine coordination and development CI | Owned queue/source tests cover physical-machine reservation, non-preemptive production precedence, uncertainty holds and cancellation drain. PR47/48 add queue binding and a concrete **same-host** CI controller provider. | Enrolled real host integration and complete machine registry/writer exclusion. The same-host provider is not a remote NAS transport or a second scheduler. Live autonomy is still disabled. |
| NAS-to-staging private connection | The prior bounded NAS connection test returned unauthenticated401/authenticated404 for an unknown command, with zero command or reconciliation POSTs. | That establishes transport authentication only—not idle executor, installed provenance, current TLS window, safe enrollment or an executed workflow. Do not start a stopped proxy/engine or renew credentials from this document. |
| Restart/restore and repeatability | Actual isolated clean shutdown plus container recreation and source refusal/hold tests provide bounded evidence. | No real PC reboot, NAS restore, exact-session recovery or interrupted remote execution acceptance is established. Missing receipt/ownership is a hold, never automatic adoption or replay. |

External collaborator authorization, public demo hosting, labeling-set backend,
production execution and heavy data processing are **not** implied by these
checks. Labeling UI is dormant without its backend; mocked labeling tests do not
make that feature operational. Staging remains software-only.

## Exact source/container checks already completed

- Backend/storage combined source cc77:5248 Linux tests passed,68 skipped,3 known
  warnings. Main478 changes only the UI relative to that accepted backend tree.
- Exact combined UI c7:199 unit tests and TypeScript/Vite build passed;97 Edge
  browser tests passed. Browser APIs are deliberately mocked. Path/hash
  Back/Forward draft protection, auth-loss clearing, no-extra-click bootstrap,
  missing-route fallback and topic invalidation are covered.
- Actual Dockerfile from clean c7 archive: static assets/CSP, auth-disabled
  fixture/private-API denials and synthetic agent authentication passed.
- Protected installed-image fixture: actual disposable enrollment CLI inspect,
  wrong-digest abort, apply/reapply refusal; three owned-inactive stages/two
  recreations preserve tasks/decisions/notes/messages. Incorrect default/v1
  startup with writable root refuses before creating a phantom ledger. All
  fixture-owned containers/volumes were removed and absence verified.

These are passed checks, not instructions to rerun them against live paths.

## One future operator handoff, in order

1. After Beau resolves GitHub account availability, verify the exact main build
   and registry digest, its release checks and intended `latest` promotion.
   Do not repeatedly dispatch, make the repo public, install another publisher
   or change subscriptions to bypass the restriction.
2. Finish acceptance of the private child **inside** the existing EH host root:
   actual effective ACLs/UID mapping, rename protection and backup coverage.
   Rehearse the cold-ledger procedure; current SMB-visible metadata is not an
   accepted private boundary. No recursive permission repairs or automatic copy.
3. Beau applies one reviewed maintenance handoff for settings/bus/private
   selective mappings and the exact initial **owned-inactive** entrypoint.
   Keep agent8770/browser8771, tokens and Access policy unchanged. Preserve the
   original source/backup and record the new-authority mutation boundary.
4. Independently enroll the published candidate/rollback, real host/driver,
   baseline and private verified HTTPS executor route. Use dedicated executor
   credentials, not agent tokens. A validate result is shape checking, not
   permission to serve; owner acquisition is itself an effect.
5. Use a fresh exact reviewed task/manifest for a bounded live NAS-to-staging
   success and deliberate failure/rollback, then interrupted-work reconciliation
   and actual restart/restore acceptance. No generic production activation.
6. Only then enroll another project incrementally. A project card, flag, source
   library, stream count, CLI help or healthy health endpoint is not readiness.

Steps2–5 still need concrete accepted host/operator evidence. This list does not
grant NAS management, credentials, autostart or service changes. The original
ledger becomes stale after new authoritative writes; reverting the image or
mapping alone is not a lossless rollback.

## Current references

- [Protected layout and initial inactive process](protected-ledger-layout.md)
- [Offline cold-ledger cutover and recovery boundaries](offline-ledger-cutover-runbook.md)
- [Unified remote queued pilot selector](unified-queued-pilot.md)
- [Actual isolated staging and live-browser evidence](staging-pilot-acceptance-2026-10-21.md)
- [Same-host CI provider limits](development-ci-local-provider.md)

The existing one-root ordinary deployment remains the current installation
until that future handoff is applied. Do not combine its entrypoint with a
protected layout or treat the ordinary deployment guide as pilot activation.
