---
name: hub-role
description: Take, hold, hand off or release an Engineering Hub role, such as ui, backend-review, integration, ops, business-docs or orchestrator, so sessions can be rolled without losing work. Use when Beau says "take the X role", "you are the X agent", "roll this session", or context is getting full.
status: DRAFT - not installed. It depends on Hub role and lease APIs that do not exist yet (see designs/orchestrator-roles-progress.md).
---

# Hub roles

A role is durable and a session is disposable. Work, claims and dispatches
belong to the role, so a fresh session can carry on where the old one stopped.

## Take a role
1. Acquire the role lease. If a live session holds it, do not take over. Tell Beau which session holds it.
2. Read the role record (owned projects and paths, standing rules, tool scope) and its handoff note.
3. Read your open dispatches and claimed tasks for this role. Continue the one the handoff marks as current.
4. Start the bus listener, then post one line to the orchestrator: "<role> staffed by <session>".

## While holding it
- **Renew the lease:** renew it on its schedule. If renewal fails, stop starting new effects and re-acquire.
- **Stay in scope:** work only inside the role's owned scope. Hand anything else to the orchestrator as `ask` or `blocked`.
- **Keep progress current:**
  - at the start, write a short checklist;
  - tick items as they finish;
  - set a one-line status;
  - set blocked_on when stuck.
  The Hub computes percent and ETA from this, so don't invent them. An optional ETA guess is allowed and is labelled as a guess.
- **Report every dispatch:** report `done` (with evidence: commit, tests, link), `blocked` or `ask`, keyed by dispatch id.

## Hand off (roll the session)
Do this when context is high, before compaction would lose detail.
1. Finish or checkpoint the current step. Do not leave a half-applied change uncommitted without saying so.
2. Write the handoff note (compare-and-swap):
   - current dispatch and its state;
   - branches and worktrees in use;
   - what you were about to do next;
   - open questions;
   - anything that must not be done.
3. Release the lease and stop your listener.
4. Tell Beau in one line: "Ready to roll: open a new session and say 'take the <role> role'."
