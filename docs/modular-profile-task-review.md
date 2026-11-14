# Reviewed modular staging profile: candidate prerequisite

An agent may optionally attach an owner-enrolled project recipe generation to
a task candidate. It supplies only the recipe name and generation; the Hub
resolves the current digest from the protected enrollment. The profile must be
readable under the project's explicit staging policy. A reviewer from a
different agent family may request the task's staging profile, inspect the
normalized profile, and explicitly confirm its exact project, recipe,
generation and digest with the source commit when reviewing. A changed
enrollment or stale task revision rejects the review. A commit-only review is
never upgraded into a profile review.

Profile data are not included in ordinary task reads or event payloads. Legacy
agents need the project's staging `read` grant. Per-installation sessions also
need an explicit owner grant of `staging.profile.read` as well as the relevant
task capability and project grant. This capability is rechecked in the Hub
transaction before profile-bearing reads and candidate/review writes commit.
Profile arguments must not contain credentials: authorized profile readers can
see them. Project policy changes and profile reads are serialized through their
Hub transaction; this is an authorization linearization point, not a guarantee
about changes after the result has left the server.

This implements **review identity only**. Profile-bound tasks are deliberately
ineligible for the existing commit-only execution verifier. They cannot be
dispatched until a later, independently reviewed protocol binds the exact tuple
through worker admission, original receipts and recovery. No host enrollment,
worker confinement, writer fence, execution authority or automatic agent wake
is created here. Unbound ordinary tasks and historical reviews retain their
existing semantics; no old review gains profile approval automatically.
