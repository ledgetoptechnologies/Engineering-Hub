"""DISABLED-BY-DEFAULT supervisor-owned Hub command/observation route foundation.

Nothing runs at import and there is no default instance, factory or listener.
``SupervisorRoute`` refuses construction unless handed a
``SupervisorRouteConfig`` whose ``enabled`` is exactly ``True``. It is not wired
into the launch supervisor, the executor ASGI app, the Hub, any composition or
any service; constructing it is an explicit operator/test act.

Only the executor's submit operation (POST /v1/container-pilot/commands with a
strict ``ContainerPilotRequest`` body) is routed: it is the only executor
operation whose bytes carry both the command_id and the fence. The outer
command_id/fence must equal those inside the exact body, checked before any
journal write. GET/reconcile carry no fence and are rejected as unsupported.

What it does: each forwarded command is bound to its HTTP method, path,
SHA-256 of the exact body bytes, command_id, queue fence, the enrolled launch
epoch (launch_id + persisted monotonic launch counter + boot_id) and the pinned
backend identity (fields of the existing signed current-observation pins). A
durable pre-effect intent row is committed (SQLite, synchronous=FULL, rollback
journal, parent-directory fsync, ``PilotOwner`` single-owner lock, as in
``operation_journal``) in the same transaction that checks the epoch high-water
mark and command_id uniqueness BEFORE anything is forwarded. Identical replay
returns the recorded outcome, ``in_progress`` or ``held``; a different binding
under a known command_id is a conflict and is never forwarded.

Dispatch counter: that same admission transaction also consumes the next
durable per-executor dispatch counter (keyed by the pinned backend
container_id; previous + 1, strictly increasing, never reused) and stores it
on the intent row. The counter travels to the executor in the
``X-Hub-Route-Dispatch`` header: ``v1.`` + base64url of a canonical claim
binding executor, counter, HTTP method, path, body SHA-256, command_id, queue
fence and launch epoch (``dispatch_claim``), so it cannot be moved to another
request. The claim is UNSIGNED today (protected only by the bearer and the
peer check); its exact canonical bytes are what a later signature will cover.
It is not part of the binding, the digest or the signed record. Triggers make the counter table append-only per executor (no
lower/equal UPDATE, no DELETE, no second INSERT/REPLACE). A crash or failure
after the admission COMMIT leaves the attempt ``intent``/``unknown`` (held)
exactly as without the counter, and the consumed value is never sent again:
an identical replay is held, a new command_id gets a higher value. The
receiving side is ``DispatchCounterGate`` (also disabled by default, its own
store): it refuses a claim that does not match the actual request or any
counter not strictly greater than the last one it accepted for its executor
(replays, duplicates and reorderings are refused, not deduplicated), allows
gaps (consumed-but-unsent values), and FENCES the effect itself: ``run``
executes the caller's effect only after re-checking, under its effect lock
and durably, that the counter is still the latest accepted and newer than
any effect ever started, so an accepted-but-delayed counter cannot act after
a higher one (see the class docstring for dead-process behaviour).

Two-phase path (the one the Hub MUST use, because the Hub commits its own
command intent before a counter exists): ``reserve(command)`` sends NOTHING;
in one journal transaction it checks the epoch high-water, consumes the next
per-executor counter and records a ``reserved`` row holding a receipt signed
(domain ``RESERVE_DOMAIN``) over {protocol, audience, key_id, command_id,
body_sha256, method, path, pin, epoch, fence, counter, reserved_at_ms}. An
identical repeat returns the same receipt bytes; a different binding under
the command_id is refused. The Hub verifies it with its pinned
``ReservationReceiptVerifier`` and commits it in its own transaction, then
calls ``dispatch(receipt, command)`` once: the route re-verifies the receipt,
requires the exact ``reserved`` row, marks it ``dispatched`` and records the
intent in ONE transaction before any byte is sent, then sends with the
reserved counter. A dispatch whose counter is not above every counter
already recorded for sending to that executor, by either path, is
``reservation_superseded`` (sender-side fence; trigger-protected dispatched
high-water), so an older reservation can never be sent after a newer one.
Every failure, mismatch, repeat or uncertainty is ``held`` and never retried
under a new counter. A repeat ``reserve`` returns the receipt only while the
reservation is still ``reserved``; a receipt proves a past allocation, never
current state. ``abandon_reservation`` retires an
unsent reservation; reserved, abandoned and never-dispatched counters stay
consumed (gaps). Rows are trigger-protected: inserted once, only as
``reserved``, with a counter above every earlier reservation for that
executor; afterwards only the state changes, once, to dispatched/abandoned;
never deleted. Reservation signing uses a separate, default-REFUSING
``ReservationSigner``: with none configured, reserve/dispatch refuse.

Dispatch claim proof (supervisor half of docs/supervisor-dispatch-claim-proof.md):
``dispatch(receipt, command, proof)`` has NO proof-less form. The Hub first
asks ``issue_dispatch_challenge(command_id)``; the supervisor durably records
a one-use nonce for that reservation with its own CLOCK_BOOTTIME and boot id
(capped per executor), and only after that commit signs (default-refusing
``DispatchChallengeSigner``, ``CHALLENGE_DOMAIN``) {protocol, audience,
key_id, nonce, command_id, counter, reservation_receipt_sha256, executor,
pin, epoch, supervisor_boot_id, proof_window_ms}, binding the SHA-256 of
those exact bytes to the row before returning them. The Hub verifies the
challenge with its pinned ``DispatchChallengeVerifier`` against
``challenge_expectation(receipt, pin, epoch)`` and signs, under its pinned
dispatch key and ``HUB_PROOF_DOMAIN``, {protocol, audience, hub_key_id,
job_id, run_id, worker, machine_id, fence, owner_epoch,
reservation_receipt_sha256, command_id, counter, executor, challenge_nonce,
challenge_sha256, supervisor_boot_id} (no wall-clock field);
``challenge_sha256`` must equal the hash bound to the challenge row. In the
SAME transaction that marks the reservation dispatched, before any byte, the
supervisor requires the proof to bind exactly the receipt, reservation,
command fence and current boot, and the nonce to be outstanding, from this
boot and no older than ``proof_window_ms`` on its own clock, then consumes
it. Any failure is ``held`` with zero bytes and nothing consumed.
``expired_statement(nonce)`` is reservation-wide: in ONE transaction it
requires that no challenge for that reservation is consumed and that it is
not dispatched, expires ALL its outstanding challenges and retires the
reservation (abandoned, counter consumed), and signs ``expired_unconsumed`` /
``reservation_status: retired`` only after that commit;
``expire_challenges()`` retires dead nonces in bulk. Signed proof fields
that also appear in the request body or journal (command_id, run_id,
fence, counter, executor, challenge) must equal it. job_id, worker,
machine_id and owner_epoch are bound ONLY by the Hub's signature: the Hub
must enforce, inside its barrier transaction, a monotonic per-machine
generation (fence), the current job/worker/fence and owner epoch, with
old-writer and restore exclusions, and no proof may be released until that
is enforced and reviewed.
Proof mode is durable: a route constructed with a proof verifier records it
in the journal, after which ``submit`` (and the journal's one-step admit)
refuse ``proof_required`` and a construction without a verifier is refused.
What serializes route instances: the PilotOwner flock admits ONE open
journal per file (a second instance in the same or another process is
refused until the first closes), and ``close()`` refuses while a submit is in
flight. Independently of that, entering proof mode is refused while any
one-step intent is unresolved (``legacy_in_flight``), and each one-step send
holds a journal read transaction (SQLite SHARED lock) across exactly the
backend exchange after re-checking the mode and its own intent, so no
writer - even one ignoring the flock - can commit proof mode or resolve that
intent mid-send (this holds ONLY in rollback-journal mode, which the store
enforces: a WAL-mode file is refused on open, on every write connection and
inside the guard; direct file writes, restores and detached work remain
outside it); if the mode was entered after admit, the send is refused
(``proof_required``, intent held) with zero bytes. Cost: in legacy mode the
journal accepts no commits while a one-step exchange is in progress (other
writers fail after the busy timeout). Triggers make
a nonce outstanding -> consumed | expired exactly once, never deleted.
Defaults refuse: no ``HubDispatchProofVerifier`` -> no challenge, no
dispatch; no ``ChallengeStatementSigner`` -> no statement.

Authority model (defined behaviour): what the supervisor verifies is
barrier-held ORIGINAL ownership, as stated by the Hub's proof, plus its own
freshness window and one-use nonce. It does NOT and cannot check the Hub's
wall-clock lease: a proof delayed past the Hub-side lease expiry is still
accepted while the supervisor window holds, and refused once it passes. A
deployment needing "never after wall-clock lease expiry" needs a
supervisor-enforced clock or lease, which is not provided.

The one-step ``submit`` path is kept unchanged (the whole route is already
disabled by default, and the existing evidence covers it) for direct
operator/test use only. The two paths cannot be mixed for one command_id:
``submit`` refuses a reserved command (``reserved_command``) and ``reserve``
refuses an admitted one (``already_admitted``).

Original-command inspection (read-only; also disabled by default, each part
needs its own ``enabled=True`` config): the Hub asks the supervisor what
happened to ONE held launch request. ``SupervisorInspectionClient`` verifies
the supervisor peer (SO_PEERCRED pid/uid/gid + pidfd start ticks against an
enrolled ``SupervisorPeerPin``) before writing any byte, then sends
``POST /v1/supervisor-route/inspect`` with an inspection-scoped bearer
(``ehri1.`` prefix; a launch bearer can never carry it and the inspection
authenticator accepts nothing else) and a canonical request binding the full
route binding (method, path, body SHA-256, command_id, fence, epoch incl.
launch counter, pinned backend), the attempt's exact ``dispatch_claim``
(executor, dispatch counter, method, path, body SHA-256, command_id, fence,
launch epoch: the dispatch header's fields and canonical JSON, consistent with
the binding), the expected supervisor key id and a fresh nonce. The reply
is read with the same strict bounded framing and one total deadline.
``SupervisorInspectionResponder`` answers only from ONE read snapshot of the
route journal (intent, reservation and challenge rows): ``launched`` /
``refused`` (the recorded outcome of the one-step intent or of the
DISPATCHED reservation's intent), ``not_dispatched`` (a reserved or
abandoned/retired reservation, ONLY when the snapshot proves no challenge
was consumed and no intent exists; otherwise ``unknown``), ``not_found``
or ``unknown``, Ed25519-signed under its own domain over an exact echo of
the request plus a REQUIRED ``reservation`` field (so answers signed before
it existed are intentionally incompatible and never verify) binding the
reservation receipt's SHA-256, its state and, when dispatched, the ONE
consumed challenge's ``challenge_sha256`` and proof hash. The Hub verifies
through an ``InspectionVerifier`` (the base class refuses everything;
``PinnedKeyInspectionVerifier`` checks one pinned key). ``InspectionResolver``
records a verified signed terminal status matching the binding exactly as an
OBSERVATION in ``InspectionLedger`` (``observed_launched`` /
``observed_refused``); timeout, framing, HTTP denial, unsigned, bad
signature, wrong key, any binding mismatch, ``unknown`` and signed
``not_found`` / ``not_dispatched`` record nothing and stay held:
inspection never releases an undispatched reservation; the Hub barrier's
``expired_unconsumed`` statement is that release path. This module provides
VERIFICATION and OBSERVATION STORAGE only and never decides: the
authoritative step is ``HeldAttemptSource.record_observation_in_hub_transaction``,
a callback the Hub executes inside ITS OWN DB transaction together with its
owner/job checks (including the ``hold_version`` captured at hold),
independently re-verifying the raw signed request/answer bytes; the default
source refuses. Inspection never relaunches, resubmits or releases,
and no result of it permits a resubmit or release.

The backend peer (SO_PEERCRED pid/uid/gid + pidfd start ticks) is verified
against the pin immediately after connect and BEFORE any request byte is
written; a mismatch closes the socket with zero bytes sent and durably records
``refused`` (definitely not forwarded; a new command_id is required). The
caller-supplied executor bearer is sent as ``Authorization`` and is never
stored in journal rows, records, results, errors or reprs.

Response classification: 2xx with a strict ContainerPilotResponse echoing the
request -> ``completed`` (outcome accepted). 4xx with an exact executor error
body whose code matches the status (400/413 invalid_payload, 401 unauthorized,
403 forbidden, 404 command_not_found, 408 request_timeout) -> ``rejected``, a
definitive signed non-acceptance: PilotASGI answers these before invoking its
adapter. 401/403 therefore means "fix the credential and submit a NEW
command_id"; the old command_id stays terminally rejected, it is not held.
Everything else (5xx, other statuses or codes, malformed bodies, transport
loss, epoch change mid-command, a reply peer differing from the pin, failure
to record the outcome) -> durably ``unknown`` where possible and always
reported ``held``; never redispatched automatically. ``inspect`` is read-only
and never forwards. Definite outcomes carry an Ed25519 record signed by the
SAME enrolled issuer key the caller already holds (verified against the
enrolled public key), under a distinct signature domain, including an
observation nonce derived from the binding digest. No key material is
generated or discovered here.

Explicit limits (this foundation grants no authority by itself):

* Consumers must treat ``held`` after a journal failure as UNKNOWN, even if a
  later ``inspect`` finds no row for the command_id: a commit may have landed
  in a journal that is now unreadable or restored. Absence of a row is never
  permission to resubmit under a new command_id; resolve through the original
  command_id and operator reconciliation.

* The total deadline bounds only the socket exchange with the backend
  (connect, peer check, send, header and body receive). It does NOT bound the
  whole submit: acquiring the route mutex, the caller-supplied epoch source and
  the durable journal (transaction, fsync) may block outside it. A timeout never
  implies the command had no effect: once bytes may have been sent the outcome
  is held. There is NO hard end-to-end deadline: only ADMISSION is bounded
  (busy refusal below); once admitted, the epoch source and the journal
  (transaction, fsync) can still block outside the socket deadline.
* Busy admission: one operation at a time. A submit that cannot take the route
  mutex within ``busy_wait_ms`` (0..50, default 0 = non-blocking) returns
  ``busy`` before any effect: no epoch call, no journal intent, no bytes. It
  is retryable with the same command_id. Precedence while busy: an IDENTICAL
  replay of the in-flight command reports ``in_progress``; the same
  command_id with different content is rejected (``binding_conflict``); any
  other command is ``busy``. A busy-path ``in_progress`` is PROVISIONAL: it
  only says another submit of identical content owns the mutex. It is NOT
  proof of admission, durable intent or any effect (the owner may still fail
  before its intent commit, after which the same command can be submitted
  again normally). Use ``inspect`` for the durable state.
* Non-abandonable ownership: once admitted, the operation keeps the mutex until
  it reaches a recorded state (completed/rejected/refused/unknown). A
  BaseException (KeyboardInterrupt, CancelledError, SystemExit) raised in the
  submitting thread after durable intent first records ``unknown``
  (``interrupted``), then re-raises; if even that write fails, the committed
  ``intent`` row reads as held. asyncio cancellation of a ``to_thread`` caller
  does not stop the thread, which still records the outcome and only then
  releases the mutex. ``unknown`` is always reported ``held``.
* ``close()`` REFUSES while an operation is in flight: it raises
  ``SupervisorRouteInFlight`` naming the in-flight command_id and leaves the
  route open; it never returns as if idle. Retry close after the outcome.
* Behaviour against the real executor's Unix-socket server close semantics
  (Connection: close, EOF after the body) is exercised in tests with uvicorn but
  must be re-verified on the actual deployment transport before integration.

* SO_PEERCRED identifies the process that created/listened on the backend
  socket at connect time, not later holders of that socket or its FDs. The
  pin's container_id and Engine fields are NOT observable through the socket;
  only pid/uid/gid/start ticks are compared per exchange.
* FD delegation inside the container is unresolved: a pinned init process can
  pass its listening socket or connections to another process undetected.
* Independent Docker writers on the engine are not fenced; the queue fence and
  epoch bind only commands that pass through this route.
* The persisted monotonic launch counter is NOT produced by the launch
  supervisor today (its launch journal holds exactly one launch row and no
  counter). The epoch, including its counter, comes from a caller-supplied
  epoch source; this route only persists and enforces the highest counter it
  has SEEN (its own high-water mark), which is not an authoritative source.
* The executor does not sign its command responses (it holds no key). Backend
  reply authenticity rests only on the SO_PEERCRED/pidfd peer check and the
  bearer; the signed record attests what the supervisor forwarded and
  received, not independent proof of Docker effects or health.
* ``DispatchCounterGate`` is NOT wired into PilotASGI or any executor service:
  today nothing on the executor checks the dispatch header, so replay
  protection at the receiver exists only once an operator/test constructs the
  gate and runs the effect through ``run``. The header is unauthenticated
  beyond the bearer and peer check (no signer on either side); a party able
  to send with the bearer can choose any counter, and a large one would
  advance the gate. The gate's store must be private to the executor side.
* The gate's effect fence covers only a SYNCHRONOUS effect that finishes its
  side effect before returning. Detached work (threads, processes, engine
  jobs it starts and leaves running) is not fenced or detected; an
  enforceable drain contract for it is NOT implemented. An effect that
  raises or returns an awaitable keeps the gate fail-closed until
  ``release_unresolved_after_quiescence``, which relies entirely on the
  operator having established that the effect stopped: the gate cannot
  prove it.
* No reservation signer exists here: an operator must supply one; key
  storage, rotation and enrollment of its key_id/public key at the Hub are
  NOT implemented. The signer is called inside the reservation transaction,
  so a slow signer holds the journal lock. The Hub-side commit of the
  receipt against its job/run/fence/reservation, and wiring
  ``ReservationReceiptVerifier`` into a Hub client, are NOT implemented.
* Old/direct writers and restores need ENFORCED exclusion, not convention:
  the journal enforces a single PilotOwner, exact schema/trigger SQL and
  refuses older user_versions, but a restored or rolled-back journal could
  resurrect an outstanding (or even a consumed) challenge nonce or an
  undispatched reservation; that is not detected without an external anchor.
* ROLLBACK IS NOT DETECTED. All of this is local SQLite state: a store
  restored from a backup, a copied older file or a snapshot rollback is
  indistinguishable from the real one. A rolled-back gate store can accept
  (and act on) an old counter again, and a rolled-back route journal can
  reissue counters already sent. Proving non-rollback needs an external
  monotonic anchor (e.g. a signed high-water mark held elsewhere), which is
  NOT implemented.
* The dispatch counter is keyed by container_id: a relaunch with a new
  container starts a new sequence at 1 (and needs a fresh gate store).
* A custom backend object must honour the contract that ``PeerMismatch`` is
  raised only before any byte is written; ``UnixSocketBackend`` does.
* The actual supervisor/engine wiring (who constructs this, which socket the
  executor serves commands on, handing the supervisor's retained signer and
  the executor bearer to it) and the Hub-side client integration (calling
  ``verify_route_record``) are NOT implemented.
* Same-UID/root administrators remain trusted; the private-path guards do not
  sandbox hostile same-UID processes racing filesystem replacement.
* Inspection limits: every local SQLite store here (route journal, dispatch
  stores, ``InspectionLedger``) is UNANCHORED and cannot prove it was not
  rolled back or restored from a backup. A restored or replaced route journal
  is one more reason a missing row (signed ``not_found``) or ``unknown`` stays
  held: absence never establishes "no effect". A signed ``launched`` /
  ``refused`` attests only what the supervisor's journal recorded, under the
  same trust limits as the route record. The responder has no listener or
  HTTP server, the supervisor inspection key and the inspection credential are
  not provisioned anywhere, and the Hub must supply the held attempt's
  dispatch counter itself (``RouteResult`` does not carry it). Nothing
  constructs the client, responder or resolver.
* The inspection ledger is NOT the Hub job store and shares no transaction or
  lock with it: its held row is caller-supplied and its observed rows are
  only signed observations. This module provides verification + observation
  storage; the HUB OWNS THE TRANSACTIONAL DECISION. Recording an outcome must
  happen in the SAME Hub DB transaction as the authoritative owner/job checks
  (including the hold version captured at hold time), via
  ``HeldAttemptSource.record_observation_in_hub_transaction``; atomicity is
  only as strong as that Hub transaction, and nothing in this module provides
  it. The only source provided (the base class) refuses, so there is no
  production use until one bound to the Hub job store exists. A replayed
  observation is historical (signed ``answered_at_ms``, not fresh host/job
  state), re-verified (exact request + signed answer, current pinned key and
  binding), and again goes through the Hub transaction.
* The responder reads the route journal row written by today's ``admit``
  path; a later two-phase reserve/dispatch flow is not modelled here.
* The responder's CONCURRENCY is bounded (separate non-blocking pre-auth and
  post-auth slot pools) but its TIME is best-effort: contention is cut by
  try-acquire and SQLite busy/progress timeouts, while filesystem calls,
  connection open/close and a custom authenticator are not interruptible.
  Its own deadline is checked as a best-effort RETURN-TIME check: a signed
  answer is not returned after the deadline (an unsigned 503 is), although
  the signer may already have produced the bytes. Limits are PER INSTANCE:
  separate responder instances do not share slot pools. Work a departed
  client no longer waits for can still complete (detached work), and
  same-UID FD delegation and rollback anchoring remain open as above.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import socket
import sqlite3
import struct
import sys
import threading
import time
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from engineering_hub.container_pilot_client import ContainerPilotRequest, ContainerPilotResponse
from engineering_hub.current_executor_observation import CurrentExecutorReleasePins, CurrentLaunchPins


PROTOCOL = 'engineering-hub.supervisor-route-record.v1'
AUDIENCE = 'engineering-hub.hub-supervisor-route'
# Distinct from the launch-receipt and current-observation domains, so a route
# record signed by the same enrolled key can never verify as either of those.
SIGNATURE_DOMAIN = b'engineering-hub-supervisor-route-v1\x00'
_NONCE_DOMAIN = b'engineering-hub-supervisor-route-observation-nonce-v1\x00'
RESERVE_PROTOCOL = 'engineering-hub.supervisor-route-reservation.v1'
RESERVE_AUDIENCE = 'engineering-hub.hub-supervisor-route-reservation'
# Distinct from the route-record, launch-receipt and observation domains.
RESERVE_DOMAIN = b'engineering-hub-supervisor-route-reserve-v1\x00'
CHALLENGE_PROTOCOL = 'engineering-hub.supervisor-route-dispatch-challenge.v1'
CHALLENGE_AUDIENCE = 'engineering-hub.hub-dispatch-challenge'
# Signed by the supervisor's challenge key; distinct from every other domain.
CHALLENGE_DOMAIN = b'engineering-hub-supervisor-route-dispatch-challenge-v1\x00'
HUB_PROOF_PROTOCOL = 'engineering-hub.hub-dispatch-proof.v1'
HUB_PROOF_AUDIENCE = 'engineering-hub.supervisor-route-dispatch'
# Signed by the HUB's dispatch key; distinct from every supervisor domain.
HUB_PROOF_DOMAIN = b'engineering-hub-hub-dispatch-proof-v1\x00'
EXPIRED_PROTOCOL = 'engineering-hub.supervisor-route-challenge-expired.v1'
EXPIRED_AUDIENCE = 'engineering-hub.hub-dispatch-in-flight'
EXPIRED_DOMAIN = b'engineering-hub-supervisor-route-challenge-expired-v1\x00'
MAX_BODY_BYTES = 8192
MAX_RESPONSE_BYTES = 16 * 1024
MAX_RECORD_BYTES = 64 * 1024
SUBMIT_PATH = '/v1/container-pilot/commands'
_BUSY_MS = 250
_METHODS = frozenset({'GET', 'POST'})
_PATH = re.compile(r'/v1/container-pilot/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){0,3}\Z')
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_SIGNATURE = re.compile(r'[A-Za-z0-9_-]{86}\Z')
_INTENT = ('CREATE TABLE supervisor_route_intent (command_id TEXT PRIMARY KEY, '
           'binding_digest TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL, '
           'owner_epoch TEXT NOT NULL, error TEXT, record TEXT, response BLOB, attempt TEXT, '
           'dispatch_counter INTEGER NOT NULL)')
_EPOCH = ('CREATE TABLE supervisor_route_epoch (id INTEGER PRIMARY KEY CHECK(id=1), '
          'launch_counter INTEGER NOT NULL, launch_id TEXT NOT NULL, boot_id TEXT NOT NULL)')
_RESERVATION = ('CREATE TABLE supervisor_route_reservation (command_id TEXT PRIMARY KEY, '
                'executor_id TEXT NOT NULL, binding_digest TEXT NOT NULL, binding TEXT NOT NULL, '
                "counter INTEGER NOT NULL CHECK(typeof(counter)='integer' AND counter > 0), "
                "state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','abandoned')), "
                'receipt TEXT NOT NULL, UNIQUE (executor_id, counter))')
# Reserve once per command_id (also defeats INSERT OR REPLACE), only as
# 'reserved', and only with a counter above every earlier reservation for
# that executor; afterwards only state may change, once, reserved ->
# dispatched | abandoned; never deleted.
_RESERVATION_TRIGGERS = {
    'supervisor_route_reservation_once': (
        'CREATE TRIGGER supervisor_route_reservation_once BEFORE INSERT ON supervisor_route_reservation '
        "WHEN NEW.state IS NOT 'reserved' OR EXISTS (SELECT 1 FROM supervisor_route_reservation "
        'WHERE command_id = NEW.command_id OR (executor_id = NEW.executor_id AND counter >= NEW.counter)) '
        "BEGIN SELECT RAISE(ABORT, 'reservation exists'); END"),
    'supervisor_route_reservation_transition': (
        'CREATE TRIGGER supervisor_route_reservation_transition BEFORE UPDATE ON supervisor_route_reservation '
        "WHEN OLD.state IS NOT 'reserved' OR NEW.state NOT IN ('dispatched','abandoned') "
        'OR NEW.command_id IS NOT OLD.command_id OR NEW.executor_id IS NOT OLD.executor_id '
        'OR NEW.binding_digest IS NOT OLD.binding_digest OR NEW.binding IS NOT OLD.binding '
        'OR NEW.counter IS NOT OLD.counter OR NEW.receipt IS NOT OLD.receipt '
        "BEGIN SELECT RAISE(ABORT, 'reservation is final'); END"),
    'supervisor_route_reservation_kept': (
        'CREATE TRIGGER supervisor_route_reservation_kept BEFORE DELETE ON supervisor_route_reservation '
        "BEGIN SELECT RAISE(ABORT, 'reservation is permanent'); END"),
}
_CHALLENGE = ('CREATE TABLE supervisor_route_challenge (nonce TEXT PRIMARY KEY, command_id TEXT NOT NULL, '
              'executor_id TEXT NOT NULL, '
              "counter INTEGER NOT NULL CHECK(typeof(counter)='integer' AND counter > 0), "
              "issued_boottime_ns INTEGER NOT NULL CHECK(typeof(issued_boottime_ns)='integer' "
              'AND issued_boottime_ns >= 0), boot_id TEXT NOT NULL, '
              "state TEXT NOT NULL CHECK(state IN ('outstanding','consumed','expired')), proof_sha256 TEXT, "
              'challenge_sha256 TEXT, '
              "CHECK((state = 'consumed') = (proof_sha256 IS NOT NULL)), "
              "CHECK(state IS NOT 'consumed' OR challenge_sha256 IS NOT NULL))")
# Inserted once per nonce, only as 'outstanding' and unsigned. Then: the hash
# of its signed envelope is bound ONCE (NULL -> value, still outstanding),
# and at most ONE state change, outstanding -> consumed (with the proof hash;
# requires a bound challenge hash) or outstanding -> expired; every other
# column is frozen; never deleted. So a consumed nonce can never become
# expired, an expired one can never be consumed, and the hash never changes.
_CHALLENGE_TRIGGERS = {
    'supervisor_route_challenge_once': (
        'CREATE TRIGGER supervisor_route_challenge_once BEFORE INSERT ON supervisor_route_challenge '
        "WHEN NEW.state IS NOT 'outstanding' OR NEW.proof_sha256 IS NOT NULL "
        'OR NEW.challenge_sha256 IS NOT NULL OR EXISTS '
        '(SELECT 1 FROM supervisor_route_challenge WHERE nonce = NEW.nonce) '
        "BEGIN SELECT RAISE(ABORT, 'challenge exists'); END"),
    'supervisor_route_challenge_transition': (
        'CREATE TRIGGER supervisor_route_challenge_transition BEFORE UPDATE ON supervisor_route_challenge '
        "WHEN OLD.state IS NOT 'outstanding' "
        "OR NOT ((NEW.state = 'outstanding' AND NEW.proof_sha256 IS NULL AND OLD.challenge_sha256 IS NULL "
        "AND typeof(NEW.challenge_sha256) = 'text') "
        "OR (NEW.state IN ('consumed','expired') AND NEW.challenge_sha256 IS OLD.challenge_sha256)) "
        'OR NEW.nonce IS NOT OLD.nonce OR NEW.command_id IS NOT OLD.command_id '
        'OR NEW.executor_id IS NOT OLD.executor_id OR NEW.counter IS NOT OLD.counter '
        'OR NEW.issued_boottime_ns IS NOT OLD.issued_boottime_ns OR NEW.boot_id IS NOT OLD.boot_id '
        "BEGIN SELECT RAISE(ABORT, 'challenge is final'); END"),
    'supervisor_route_challenge_kept': (
        'CREATE TRIGGER supervisor_route_challenge_kept BEFORE DELETE ON supervisor_route_challenge '
        "BEGIN SELECT RAISE(ABORT, 'challenge is permanent'); END"),
}
# Proof mode, once recorded, is permanent for the journal: the single row can
# never be updated or deleted, so submit() can never be re-enabled.
_MODE = ('CREATE TABLE supervisor_route_mode (id INTEGER PRIMARY KEY CHECK(id=1), '
         "proof_required INTEGER NOT NULL CHECK(typeof(proof_required)='integer' AND proof_required=1))")
_MODE_TRIGGERS = {
    'supervisor_route_mode_once': (  # Also defeats INSERT OR REPLACE.
        'CREATE TRIGGER supervisor_route_mode_once BEFORE INSERT ON supervisor_route_mode '
        'WHEN EXISTS (SELECT 1 FROM supervisor_route_mode) '
        "BEGIN SELECT RAISE(ABORT, 'proof mode is permanent'); END"),
    'supervisor_route_mode_frozen': (
        'CREATE TRIGGER supervisor_route_mode_frozen BEFORE UPDATE ON supervisor_route_mode '
        "BEGIN SELECT RAISE(ABORT, 'proof mode is permanent'); END"),
    'supervisor_route_mode_kept': (
        'CREATE TRIGGER supervisor_route_mode_kept BEFORE DELETE ON supervisor_route_mode '
        "BEGIN SELECT RAISE(ABORT, 'proof mode is permanent'); END"),
}
_OWNER_TABLES = frozenset({'hub_pilot_owner', 'hub_pilot_owner_epochs'})
MAX_DISPATCH_COUNTER = 2**63 - 1
DISPATCH_HEADER = 'X-Hub-Route-Dispatch'
DISPATCH_PROTOCOL = 'engineering-hub.supervisor-route-dispatch.v1'
MAX_DISPATCH_HEADER = 2048
_DISPATCH = re.compile(r'v1\.([A-Za-z0-9_-]{1,2045})\Z')
_DISPATCH_KEYS = frozenset({'protocol', 'executor_id', 'counter', 'method', 'path', 'body_sha256',
                            'command_id', 'queue_fence', 'epoch'})


def _counter_schema(table):
    """Per-executor monotonic counter table plus triggers that make it
    append-only per executor: one INSERT per executor_id (which also defeats
    INSERT OR REPLACE), UPDATE only to a strictly greater value, no DELETE.
    The exact SQL is part of the schema check, so a dropped or altered
    trigger makes the store refuse to open or transact."""
    return {
        table: (f'CREATE TABLE {table} (executor_id TEXT PRIMARY KEY, counter INTEGER NOT NULL '
                f"CHECK(typeof(counter)='integer' AND counter > 0))"),
        table + '_insert_once': (
            f'CREATE TRIGGER {table}_insert_once BEFORE INSERT ON {table} '
            f'WHEN EXISTS (SELECT 1 FROM {table} WHERE executor_id = NEW.executor_id) '
            "BEGIN SELECT RAISE(ABORT, 'dispatch counter exists'); END"),
        table + '_increase_only': (
            f'CREATE TRIGGER {table}_increase_only BEFORE UPDATE ON {table} '
            'WHEN NEW.executor_id IS NOT OLD.executor_id OR NEW.counter <= OLD.counter '
            "BEGIN SELECT RAISE(ABORT, 'dispatch counter must increase'); END"),
        table + '_no_delete': (
            f'CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} '
            "BEGIN SELECT RAISE(ABORT, 'dispatch counter is permanent'); END"),
    }


_GATE_IN_FLIGHT = ('CREATE TABLE supervisor_route_dispatch_in_flight (executor_id TEXT PRIMARY KEY, '
                   "counter INTEGER NOT NULL CHECK(typeof(counter)='integer' AND counter > 0))")
_GATE_OUTCOME = ('CREATE TABLE supervisor_route_dispatch_outcome (executor_id TEXT NOT NULL, '
                 "counter INTEGER NOT NULL CHECK(typeof(counter)='integer' AND counter > 0), "
                 "outcome TEXT NOT NULL CHECK(outcome IN ('completed','released')), evidence TEXT, "
                 "CHECK((outcome='completed' AND evidence IS NULL) OR (outcome='released' "
                 "AND typeof(evidence)='text' AND length(evidence) BETWEEN 1 AND 1024)), "
                 'PRIMARY KEY (executor_id, counter))')
_EVIDENCE = re.compile(r'[\x20-\x7e]{1,1024}\Z')
_GATE_OUTCOME_FROZEN = ('CREATE TRIGGER supervisor_route_dispatch_outcome_frozen BEFORE UPDATE ON '
                        'supervisor_route_dispatch_outcome '
                        "BEGIN SELECT RAISE(ABORT, 'dispatch outcome is permanent'); END")
_GATE_OUTCOME_KEPT = ('CREATE TRIGGER supervisor_route_dispatch_outcome_kept BEFORE DELETE ON '
                      'supervisor_route_dispatch_outcome '
                      "BEGIN SELECT RAISE(ABORT, 'dispatch outcome is permanent'); END")
_STATES = frozenset({'intent', 'completed', 'rejected', 'refused', 'unknown'})
_RECORDED = frozenset({'completed', 'rejected'})
_ERRORS = frozenset({'transport_lost', 'malformed_response', 'backend_ambiguous', 'epoch_changed',
                     'backend_identity_mismatch', 'outcome_unrecorded', 'interrupted', 'signing_failed',
                     'proof_required', 'intent_resolved', 'send_guard_failed'})
# PilotASGI error codes emitted BEFORE its adapter is invoked, keyed by status.
# 403 is reserved for an explicit forbidden answer from a fronting executor.
_DEFINITIVE_REJECTIONS = {400: 'invalid_payload', 401: 'unauthorized', 403: 'forbidden',
                          404: 'command_not_found', 408: 'request_timeout', 413: 'invalid_payload'}
_PAYLOAD_KEYS = frozenset({'protocol', 'audience', 'issuer_id', 'binding', 'binding_digest',
                           'observation_nonce', 'outcome', 'backend_status', 'response_sha256',
                           'observed_peer', 'recorded_at_ms'})
_BINDING_KEYS = frozenset({'method', 'path', 'body_sha256', 'command_id', 'queue_fence', 'epoch', 'backend'})
_EPOCH_KEYS = frozenset({'launch_id', 'launch_counter', 'boot_id'})
_BACKEND_KEYS = frozenset({'container_id', 'host_pid', 'process_start_ticks', 'uid', 'gid',
                           'engine_id', 'engine_pid', 'engine_start_ticks'})
_PEER_KEYS = frozenset({'pid', 'uid', 'gid', 'start_ticks'})


class SupervisorRouteError(RuntimeError):
    """Sanitized denial; never reflects paths, bodies, keys, tokens or backend text."""


class SupervisorRouteDisabled(SupervisorRouteError):
    """No explicit enabled configuration: the route does not exist."""


class SupervisorRouteRejected(SupervisorRouteError):
    """Definitely NOT forwarded (invalid, stale epoch, mismatch, or conflicting replay)."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__('supervisor route rejected command: ' + reason)


class SupervisorRouteRefused(SupervisorRouteError):
    """Fail-closed: THIS submit attempt journaled and forwarded nothing.

    Raised only when that is proved (epoch source failed before any journal
    access, or the journal failed before BEGIN / inside a rolled-back
    transaction AND a read confirms no row exists for the command_id). It says
    nothing about other attempts: ``inspect(command_id)`` reports any earlier
    outcome. Uncertain admission is returned as ``held`` instead.
    """

    def __init__(self):
        super().__init__('supervisor route could not admit this submit attempt, which journaled and '
                         'forwarded nothing; inspect the same command_id for any earlier outcome')


class RouteJournalError(SupervisorRouteError):
    pass


class SupervisorRouteInFlight(SupervisorRouteError):
    """close() refused: an admitted operation has not reached a recorded state."""

    def __init__(self, command_id):
        self.command_id = command_id
        super().__init__('supervisor route operation in flight: ' + str(command_id))


class SupervisorRouteBusy(SupervisorRouteError):
    """Another operation owns the route: nothing journaled or sent; retryable."""

    def __init__(self):
        super().__init__('supervisor route busy; nothing was journaled or sent')


class ReservationUncertain(SupervisorRouteError):
    """reserve() could not prove whether its reservation committed. Nothing
    was sent. Repeating reserve with the IDENTICAL command is safe (it returns
    the stored receipt if one exists); ``inspect_reservation`` reads it."""

    def __init__(self):
        super().__init__('reservation outcome unknown; nothing was sent; repeat reserve with the identical '
                         'command or inspect the reservation')


class DispatchRefused(SupervisorRouteRejected):
    """Receiving side: the dispatch counter was missing, malformed, for another
    executor or body, or not strictly greater than the last accepted one.
    Nothing was recorded and the request must not be acted on."""


class PeerMismatch(Exception):
    """Backend contract: raised after connect and BEFORE any request byte is sent."""


def _require(condition, error=None):
    if not condition:
        raise error if error is not None else SupervisorRouteRejected('invalid')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _strict_json(raw, maximum):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ValueError

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject(_):
        raise ValueError
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject, parse_float=reject)


def _uuid(value, *, v4=False):
    try:
        return (type(value) is str and str(UUID(value)) == value
                and (not v4 or UUID(value).version == 4))
    except (ValueError, TypeError, AttributeError):
        return False


def _positive(value, upper=2**63 - 1):
    return type(value) is int and 0 < value <= upper


def _id32(value):
    return type(value) is int and 0 <= value < 2**32 - 1


def _now_ms():
    wall = time.time()
    _require(type(wall) in (int, float) and math.isfinite(wall) and wall > 0, SupervisorRouteError('clock'))
    return int(wall * 1000)


def _bearer(value):
    # No scoped credential (inspection or any transport scope: ehri1./ehrr1./
    # ehrc1./ehrd1./ehre1.) can ever be used as the launch bearer.
    _require(type(value) is str and 0 < len(value) <= 4096 and all(33 <= ord(c) <= 126 for c in value)
             and not value.startswith(_SCOPED_PREFIXES),
             SupervisorRouteError('executor bearer credential required'))
    return value


@dataclass(frozen=True)
class RouteEpoch:
    """Enrolled launch epoch: launch_id + monotonic launch counter + boot_id."""
    launch_id: str
    launch_counter: int
    boot_id: str

    def __post_init__(self):
        _require(_uuid(self.launch_id, v4=True) and _uuid(self.boot_id) and _positive(self.launch_counter))

    def as_dict(self):
        return {'launch_id': self.launch_id, 'launch_counter': self.launch_counter, 'boot_id': self.boot_id}


@dataclass(frozen=True)
class BackendPin:
    """Backend identity pinned for an epoch, from the signed observation pins."""
    container_id: str
    host_pid: int
    process_start_ticks: int
    uid: int
    gid: int
    engine_id: str
    engine_pid: int
    engine_start_ticks: int

    def __post_init__(self):
        _require(type(self.container_id) is str and _HEX.fullmatch(self.container_id)
                 and type(self.engine_id) is str and _ID.fullmatch(self.engine_id)
                 and all(_positive(v) for v in (self.host_pid, self.process_start_ticks,
                                                self.engine_pid, self.engine_start_ticks))
                 and _id32(self.uid) and _id32(self.gid))

    @classmethod
    def from_launch_pins(cls, pins, *, uid, gid):
        """Reuse the exact fields the current-observation protocol signs."""
        _require(type(pins) is CurrentLaunchPins)
        return cls(pins.container_id, pins.authenticated_host_pid, pins.process_start_ticks,
                   uid, gid, pins.engine_id, pins.engine_pid, pins.engine_start_ticks)

    def as_dict(self):
        return {'container_id': self.container_id, 'host_pid': self.host_pid,
                'process_start_ticks': self.process_start_ticks, 'uid': self.uid, 'gid': self.gid,
                'engine_id': self.engine_id, 'engine_pid': self.engine_pid,
                'engine_start_ticks': self.engine_start_ticks}

    def peer_dict(self):
        return {'pid': self.host_pid, 'uid': self.uid, 'gid': self.gid, 'start_ticks': self.process_start_ticks}


@dataclass(frozen=True)
class ObservedPeer:
    """Kernel-observed backend peer for ONE forwarded exchange."""
    pid: int
    uid: int
    gid: int
    start_ticks: int

    def matches(self, pin):
        return (type(pin) is BackendPin and (self.pid, self.uid, self.gid, self.start_ticks)
                == (pin.host_pid, pin.uid, pin.gid, pin.process_start_ticks))


@dataclass(frozen=True)
class BackendReply:
    status: int
    body: bytes
    peer: ObservedPeer


@dataclass(frozen=True)
class RouteCommand:
    """One Hub command plus the epoch the Hub believes is current."""
    method: str
    path: str
    body: bytes
    command_id: str
    queue_fence: int
    epoch: RouteEpoch

    def __post_init__(self):
        _require(type(self.method) is str and self.method in _METHODS
                 and type(self.path) is str and _PATH.fullmatch(self.path)
                 and type(self.body) is bytes and len(self.body) <= MAX_BODY_BYTES
                 and (self.method == 'POST' or self.body == b'')
                 and _uuid(self.command_id) and _positive(self.queue_fence)
                 and type(self.epoch) is RouteEpoch)


def _pilot_request(body):
    """Strict ContainerPilotRequest from the exact bytes; raises otherwise."""
    value = _strict_json(body, MAX_BODY_BYTES)
    _require(type(value) is dict and set(value) == set(ContainerPilotRequest.model_fields))
    return ContainerPilotRequest.model_validate(value)


def _submitted_request(command):
    """Parse the exact forwarded bytes per the executor submit contract.

    Raises Rejected unless the outer command_id/fence equal the inner ones.
    """
    if command.method != 'POST' or command.path != SUBMIT_PATH:
        raise SupervisorRouteRejected('unsupported_operation')
    try:
        request = _pilot_request(command.body)
    except Exception:
        raise SupervisorRouteRejected('invalid_body') from None
    if request.command_id != command.command_id:
        raise SupervisorRouteRejected('command_id_mismatch')
    if request.fencing_generation != command.queue_fence:
        raise SupervisorRouteRejected('fence_mismatch')
    return request


def binding_for(command, pin):
    """Canonical binding over every forwarded/attested field."""
    _require(type(command) is RouteCommand and type(pin) is BackendPin)
    return {'method': command.method, 'path': command.path,
            'body_sha256': hashlib.sha256(command.body).hexdigest(),
            'command_id': command.command_id, 'queue_fence': command.queue_fence,
            'epoch': command.epoch.as_dict(), 'backend': pin.as_dict()}


def binding_digest(binding):
    return hashlib.sha256(_canonical(binding)).hexdigest()


def observation_nonce(digest):
    _require(type(digest) is str and _HEX.fullmatch(digest))
    return hashlib.sha256(_NONCE_DOMAIN + bytes.fromhex(digest)).hexdigest()


def dispatch_claim(binding, counter):
    """The fields the dispatch header carries, all taken from the admitted
    binding: executor (pinned container_id), counter, HTTP method, path, body
    SHA-256, command_id, queue fence and launch epoch. A later signature can
    cover exactly ``_canonical(claim)``; today the claim is UNSIGNED."""
    _require(_valid_binding(binding) and _positive(counter, MAX_DISPATCH_COUNTER))
    return {'protocol': DISPATCH_PROTOCOL, 'executor_id': binding['backend']['container_id'],
            'counter': counter, 'method': binding['method'], 'path': binding['path'],
            'body_sha256': binding['body_sha256'], 'command_id': binding['command_id'],
            'queue_fence': binding['queue_fence'], 'epoch': dict(binding['epoch'])}


def _valid_claim(claim):
    return (_shape(claim, _DISPATCH_KEYS) and claim['protocol'] == DISPATCH_PROTOCOL
            and type(claim['executor_id']) is str and _HEX.fullmatch(claim['executor_id'])
            and _positive(claim['counter'], MAX_DISPATCH_COUNTER)
            and claim['method'] in _METHODS and type(claim['path']) is str and _PATH.fullmatch(claim['path'])
            and type(claim['body_sha256']) is str and _HEX.fullmatch(claim['body_sha256'])
            and _uuid(claim['command_id']) and _positive(claim['queue_fence'])
            and _shape(claim['epoch'], _EPOCH_KEYS) and _uuid(claim['epoch']['launch_id'], v4=True)
            and _uuid(claim['epoch']['boot_id']) and _positive(claim['epoch']['launch_counter']))


def dispatch_header(claim):
    """``X-Hub-Route-Dispatch`` value: ``v1.`` + unpadded base64url of the
    canonical claim JSON. Reserved for a later signed form: ``v2.<claim>.<sig>``
    with the signature over a distinct domain + these exact claim bytes."""
    _require(_valid_claim(claim))
    value = 'v1.' + base64.urlsafe_b64encode(_canonical(claim)).rstrip(b'=').decode()
    _require(len(value) <= MAX_DISPATCH_HEADER)
    return value


def parse_dispatch_header(value):
    """Strict, non-malleable parse -> claim dict. DispatchRefused for anything
    missing, non-canonical (base64 or JSON), wrongly typed or out of range."""
    try:
        match = _DISPATCH.fullmatch(value) if type(value) is str and len(value) <= MAX_DISPATCH_HEADER else None
        _require(match is not None)
        raw = base64.urlsafe_b64decode(match[1] + '=' * (-len(match[1]) % 4))
        _require(base64.urlsafe_b64encode(raw).rstrip(b'=').decode() == match[1])
        claim = _strict_json(raw, MAX_DISPATCH_HEADER)
        _require(_valid_claim(claim) and _canonical(claim) == raw)
    except Exception:
        raise DispatchRefused('dispatch_invalid') from None
    return claim


_RESERVATION_KEYS = frozenset({'protocol', 'audience', 'key_id', 'command_id', 'body_sha256', 'method', 'path',
                               'pin', 'epoch', 'fence', 'counter', 'reserved_at_ms'})
_EXPECTATION_KEYS = frozenset({'command_id', 'body_sha256', 'method', 'path', 'pin', 'epoch', 'fence'})


def _reservation_fields(binding):
    return {'command_id': binding['command_id'], 'body_sha256': binding['body_sha256'],
            'method': binding['method'], 'path': binding['path'], 'pin': dict(binding['backend']),
            'epoch': dict(binding['epoch']), 'fence': binding['queue_fence']}


def reservation_expectation(command, pin):
    """What a receipt must bind, from the Hub's own command bytes, epoch and
    independently enrolled backend pin: command_id, body_sha256, method,
    path, pin, epoch, fence."""
    return _reservation_fields(binding_for(command, pin))


def _verify_reservation(raw, *, public_key, key_id, expected):
    error = SupervisorRouteError('reservation receipt unverified')
    try:
        _require(type(public_key) is bytes and len(public_key) == 32 and type(key_id) is str
                 and _ID.fullmatch(key_id) and _shape(expected, _EXPECTATION_KEYS), error)
        envelope = _strict_json(raw, MAX_RECORD_BYTES)
        _require(_canonical(envelope) == raw and _shape(envelope, {'payload', 'signature'}), error)
        payload, signature = envelope['payload'], envelope['signature']
        _require(_shape(payload, _RESERVATION_KEYS) and type(signature) is str
                 and _SIGNATURE.fullmatch(signature), error)
        decoded = base64.urlsafe_b64decode(signature + '==')
        _require(len(decoded) == 64 and base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() == signature,
                 error)
        Ed25519PublicKey.from_public_bytes(public_key).verify(decoded, RESERVE_DOMAIN + _canonical(payload))
        binding = {'method': payload['method'], 'path': payload['path'], 'body_sha256': payload['body_sha256'],
                   'command_id': payload['command_id'], 'queue_fence': payload['fence'],
                   'epoch': payload['epoch'], 'backend': payload['pin']}
        _require(payload['protocol'] == RESERVE_PROTOCOL and payload['audience'] == RESERVE_AUDIENCE
                 and type(payload['key_id']) is str and payload['key_id'] == key_id and _valid_binding(binding)
                 and _positive(payload['counter'], MAX_DISPATCH_COUNTER) and _positive(payload['reserved_at_ms'])
                 and all(_canonical(payload[name]) == _canonical(value) for name, value in expected.items()),
                 error)
        return payload
    except SupervisorRouteError:
        raise
    except Exception:
        raise error from None


class ReservationSigner:
    """Reservation-receipt signer interface AND its default: this base class
    REFUSES to sign. A configured signer (supplied by the operator; none is
    provided, generated or discovered here) is any object exposing ``key_id``
    (str), ``public_key`` (32 raw Ed25519 bytes) and ``sign(message) -> 64
    bytes``, signing exactly ``RESERVE_DOMAIN + canonical(payload)``."""
    key_id = None
    public_key = None

    def sign(self, message):
        raise SupervisorRouteError('reservation signer not configured')


def _signer_ready(signer):
    return (type(signer) not in (ReservationSigner, ChallengeStatementSigner, DispatchChallengeSigner)
            and callable(getattr(signer, 'sign', None))
            and type(getattr(signer, 'key_id', None)) is str and bool(_ID.fullmatch(signer.key_id))
            and type(getattr(signer, 'public_key', None)) is bytes and len(signer.public_key) == 32)


class ReservationReceiptVerifier:
    """Hub-side reservation receipt verifier, DEFAULT REFUSING: constructed
    without a pinned ``public_key`` (32 raw bytes) and ``key_id`` it refuses
    every receipt. ``verify(raw, expected=reservation_expectation(...))``
    enforces the exact canonical bytes and shape, the signature under the
    pinned key and RESERVE_DOMAIN, protocol/audience/key_id, and that every
    bound field equals ``expected``; returns the payload (with ``counter``
    and ``reserved_at_ms``) or raises. Not yet wired into any Hub client.

    Hub contract (NOT implemented here): commit the verified receipt in the
    Hub's own DB transaction against the current job/run/fence and its own
    reservation record, then call ``SupervisorRoute.dispatch`` exactly once
    with those exact receipt bytes; never re-reserve under a new command_id
    to "retry" a held dispatch.
    """

    def __init__(self, *, public_key=None, key_id=None):
        self._public_key, self._key_id = public_key, key_id

    def __repr__(self):
        return 'ReservationReceiptVerifier(<pinned>)' if self._public_key is not None else \
            'ReservationReceiptVerifier(<refusing>)'

    def verify(self, raw, *, expected):
        if self._public_key is None or self._key_id is None:
            raise SupervisorRouteError('reservation receipt verifier not configured')
        return _verify_reservation(raw, public_key=self._public_key, key_id=self._key_id, expected=expected)


_NONCE_B64 = re.compile(r'[A-Za-z0-9_-]{43}\Z')
_PROOF_KEYS = frozenset({'protocol', 'audience', 'hub_key_id', 'job_id', 'run_id', 'worker', 'fence',
                         'machine_id', 'owner_epoch', 'reservation_receipt_sha256', 'command_id', 'counter',
                         'executor',
                         'challenge_nonce', 'challenge_sha256', 'supervisor_boot_id'})
_CHALLENGE_KEYS = frozenset({'protocol', 'audience', 'key_id', 'nonce', 'command_id', 'counter',
                             'reservation_receipt_sha256', 'executor', 'pin', 'epoch', 'supervisor_boot_id',
                             'proof_window_ms'})
_CHALLENGE_EXPECTATION_KEYS = frozenset({'command_id', 'counter', 'reservation_receipt_sha256', 'executor', 'pin',
                                         'epoch'})
_EXPIRED_KEYS = frozenset({'protocol', 'audience', 'key_id', 'nonce', 'command_id', 'counter', 'status',
                           'reservation_status', 'boot_id'})


def _valid_nonce(value):
    try:
        return (type(value) is str and bool(_NONCE_B64.fullmatch(value))
                and base64.urlsafe_b64encode(base64.urlsafe_b64decode(value + '=')).rstrip(b'=').decode() == value)
    except Exception:
        return False


def _signed_envelope(raw, keys, public_key, domain, error):
    """Strict canonical {payload, signature} under ``domain``; returns payload."""
    envelope = _strict_json(raw, MAX_RECORD_BYTES)
    _require(_canonical(envelope) == raw and _shape(envelope, {'payload', 'signature'}), error)
    payload, signature = envelope['payload'], envelope['signature']
    _require(_shape(payload, keys) and type(signature) is str and _SIGNATURE.fullmatch(signature), error)
    decoded = base64.urlsafe_b64decode(signature + '==')
    _require(len(decoded) == 64 and base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() == signature, error)
    Ed25519PublicKey.from_public_bytes(public_key).verify(decoded, domain + _canonical(payload))
    return payload


def _pinned(public_key, key_id):
    if public_key is None and key_id is None:
        return False
    _require(type(public_key) is bytes and len(public_key) == 32 and type(key_id) is str
             and bool(_ID.fullmatch(key_id)), SupervisorRouteError('invalid pinned key'))
    return True


class HubDispatchProofVerifier:
    """Supervisor-side verifier of the Hub's one-use dispatch claim proof,
    DEFAULT REFUSING: without a pinned Hub Ed25519 ``public_key`` (32 raw
    bytes) and ``key_id`` it refuses every proof, and ``dispatch`` refuses.

    ``verify(raw)`` enforces the exact canonical envelope, the signature under
    the pinned key and HUB_PROOF_DOMAIN, and the exact payload
    {protocol, audience, hub_key_id, job_id, run_id, worker, machine_id,
    fence, owner_epoch, reservation_receipt_sha256, command_id, counter,
    executor, challenge_nonce, challenge_sha256, supervisor_boot_id} with
    strict types (job_id/run_id/worker/machine_id: identifier pattern;
    owner_epoch: UUID; fence: positive int)
    (``challenge_sha256`` = SHA-256 of the exact signed challenge bytes the
    Hub verified with DispatchChallengeVerifier). There is NO
    wall-clock field: freshness comes only from the supervisor's own
    challenge window. Binding to the receipt, reservation, command and boot,
    and nonce consumption, happen in the dispatch journal transaction.
    """

    def __init__(self, *, public_key=None, key_id=None):
        self.configured = _pinned(public_key, key_id)
        self._public_key, self._key_id = public_key, key_id

    def __repr__(self):
        return 'HubDispatchProofVerifier(<pinned>)' if self.configured else 'HubDispatchProofVerifier(<refusing>)'

    def verify(self, raw):
        if not self.configured:
            raise SupervisorRouteError('hub dispatch proof verifier not configured')
        error = SupervisorRouteError('hub dispatch proof unverified')
        try:
            payload = _signed_envelope(raw, _PROOF_KEYS, self._public_key, HUB_PROOF_DOMAIN, error)
            _require(payload['protocol'] == HUB_PROOF_PROTOCOL and payload['audience'] == HUB_PROOF_AUDIENCE
                     and type(payload['hub_key_id']) is str and payload['hub_key_id'] == self._key_id
                     and all(type(payload[k]) is str and _ID.fullmatch(payload[k])
                             for k in ('job_id', 'run_id', 'worker', 'machine_id'))
                     and _positive(payload['fence']) and _uuid(payload['owner_epoch'])
                     and type(payload['reservation_receipt_sha256']) is str
                     and _HEX.fullmatch(payload['reservation_receipt_sha256'])
                     and _uuid(payload['command_id']) and _positive(payload['counter'], MAX_DISPATCH_COUNTER)
                     and type(payload['executor']) is str and _HEX.fullmatch(payload['executor'])
                     and _valid_nonce(payload['challenge_nonce'])
                     and type(payload['challenge_sha256']) is str and _HEX.fullmatch(payload['challenge_sha256'])
                     and _uuid(payload['supervisor_boot_id']), error)
            return payload
        except SupervisorRouteError:
            raise
        except Exception:
            raise error from None


class ChallengeStatementSigner:
    """Signer interface for expired_unconsumed statements AND its default:
    this base class REFUSES. A configured signer (operator-supplied; none is
    provided here) exposes ``key_id``, ``public_key`` (32 raw bytes) and
    ``sign(message) -> 64 bytes`` over ``EXPIRED_DOMAIN + canonical(payload)``."""
    key_id = None
    public_key = None

    def sign(self, message):
        raise SupervisorRouteError('challenge statement signer not configured')


class ExpiredStatementVerifier:
    """Hub-side verifier of a supervisor expired_unconsumed statement,
    DEFAULT REFUSING without a pinned key. ``verify(raw, nonce=)`` returns the
    payload {protocol, audience, key_id, nonce, command_id, counter, status:
    'expired_unconsumed', reservation_status: 'retired', boot_id} or raises.
    It states that NO challenge for the reservation was or can be consumed
    and the reservation was retired unsent. Not wired into any Hub client."""

    def __init__(self, *, public_key=None, key_id=None):
        self.configured = _pinned(public_key, key_id)
        self._public_key, self._key_id = public_key, key_id

    def verify(self, raw, *, nonce):
        if not self.configured:
            raise SupervisorRouteError('expired statement verifier not configured')
        error = SupervisorRouteError('expired statement unverified')
        try:
            payload = _signed_envelope(raw, _EXPIRED_KEYS, self._public_key, EXPIRED_DOMAIN, error)
            _require(payload['protocol'] == EXPIRED_PROTOCOL and payload['audience'] == EXPIRED_AUDIENCE
                     and payload['key_id'] == self._key_id and payload['nonce'] == nonce and _valid_nonce(nonce)
                     and _uuid(payload['command_id']) and _positive(payload['counter'], MAX_DISPATCH_COUNTER)
                     and payload['status'] == 'expired_unconsumed' and payload['reservation_status'] == 'retired'
                     and _uuid(payload['boot_id']), error)
            return payload
        except SupervisorRouteError:
            raise
        except Exception:
            raise error from None


class DispatchChallengeSigner:
    """Signer interface for dispatch challenges AND its default: this base
    class REFUSES. A configured signer (operator-supplied; none is provided
    here) exposes ``key_id``, ``public_key`` (32 raw bytes) and
    ``sign(message) -> 64 bytes`` over ``CHALLENGE_DOMAIN + canonical(payload)``."""
    key_id = None
    public_key = None

    def sign(self, message):
        raise SupervisorRouteError('dispatch challenge signer not configured')


def _valid_pin(pin):
    return (_shape(pin, _BACKEND_KEYS) and type(pin['container_id']) is str and _HEX.fullmatch(pin['container_id'])
            and type(pin['engine_id']) is str and _ID.fullmatch(pin['engine_id'])
            and all(_positive(pin[k]) for k in ('host_pid', 'process_start_ticks', 'engine_pid', 'engine_start_ticks'))
            and _id32(pin['uid']) and _id32(pin['gid']))


def _valid_epoch(epoch):
    return (_shape(epoch, _EPOCH_KEYS) and _uuid(epoch['launch_id'], v4=True) and _uuid(epoch['boot_id'])
            and _positive(epoch['launch_counter']))


def challenge_expectation(receipt, pin, epoch):
    """``expected`` for DispatchChallengeVerifier, from the Hub's COMMITTED
    (already verified) reservation receipt, its enrolled BackendPin and its
    RouteEpoch: {command_id, counter, reservation_receipt_sha256, executor,
    pin, epoch}. Refuses if the receipt does not carry this pin and epoch."""
    error = SupervisorRouteError('challenge expectation unavailable')
    try:
        _require(type(pin) is BackendPin and type(epoch) is RouteEpoch, error)
        reserved = _strict_json(receipt, MAX_RECORD_BYTES)['payload']
        _require(_canonical(reserved['pin']) == _canonical(pin.as_dict())
                 and _canonical(reserved['epoch']) == _canonical(epoch.as_dict()), error)
        return {'command_id': reserved['command_id'], 'counter': reserved['counter'],
                'reservation_receipt_sha256': hashlib.sha256(receipt).hexdigest(),
                'executor': pin.container_id, 'pin': pin.as_dict(), 'epoch': epoch.as_dict()}
    except SupervisorRouteError:
        raise
    except Exception:
        raise error from None


class DispatchChallengeVerifier:
    """Hub-side verifier of a supervisor dispatch challenge, DEFAULT REFUSING
    without a pinned supervisor challenge key and key_id.

    ``verify(raw, expected=challenge_expectation(...))`` enforces the exact
    canonical envelope, the signature under the pinned key and
    CHALLENGE_DOMAIN, the exact payload {protocol, audience, key_id, nonce,
    command_id, counter, reservation_receipt_sha256, executor, pin, epoch,
    supervisor_boot_id, proof_window_ms} with strict types, and exact
    equality of command_id, counter, reservation_receipt_sha256, executor,
    pin and epoch with ``expected``. Returns the payload; the Hub then binds
    ``challenge_sha256 = sha256(raw)`` (and the nonce) into its proof. Not
    wired into any Hub client.
    """

    def __init__(self, *, public_key=None, key_id=None):
        self.configured = _pinned(public_key, key_id)
        self._public_key, self._key_id = public_key, key_id

    def __repr__(self):
        return 'DispatchChallengeVerifier(<pinned>)' if self.configured else 'DispatchChallengeVerifier(<refusing>)'

    def verify(self, raw, *, expected):
        if not self.configured:
            raise SupervisorRouteError('dispatch challenge verifier not configured')
        error = SupervisorRouteError('dispatch challenge unverified')
        try:
            _require(_shape(expected, _CHALLENGE_EXPECTATION_KEYS), error)
            payload = _signed_envelope(raw, _CHALLENGE_KEYS, self._public_key, CHALLENGE_DOMAIN, error)
            _require(payload['protocol'] == CHALLENGE_PROTOCOL and payload['audience'] == CHALLENGE_AUDIENCE
                     and type(payload['key_id']) is str and payload['key_id'] == self._key_id
                     and _valid_nonce(payload['nonce']) and _uuid(payload['command_id'])
                     and _positive(payload['counter'], MAX_DISPATCH_COUNTER)
                     and type(payload['reservation_receipt_sha256']) is str
                     and _HEX.fullmatch(payload['reservation_receipt_sha256'])
                     and type(payload['executor']) is str and _HEX.fullmatch(payload['executor'])
                     and _valid_pin(payload['pin']) and payload['pin']['container_id'] == payload['executor']
                     and _valid_epoch(payload['epoch']) and _uuid(payload['supervisor_boot_id'])
                     and type(payload['proof_window_ms']) is int and 1000 <= payload['proof_window_ms'] <= 30000
                     and all(_canonical(payload[name]) == _canonical(value) for name, value in expected.items()),
                     error)
            return payload
        except SupervisorRouteError:
            raise
        except Exception:
            raise error from None


def _system_boot_clock():
    """(boot_id, CLOCK_BOOTTIME ns): this host's own monotonic clock."""
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
    return boot_id, time.clock_gettime_ns(time.CLOCK_BOOTTIME)


@dataclass(frozen=True)
class RouteReservation:
    """A durable reservation: ``receipt`` is the exact signed bytes to commit
    and later pass to ``dispatch``; ``replayed`` for an identical repeat.
    ``state`` is the durable state read in the same transaction; reserve()
    only ever returns ``reserved`` (terminal states are refused). A receipt
    proves only that the counter WAS allocated for this binding, never its
    current state: use ``inspect_reservation`` for that."""
    command_id: str
    counter: int
    receipt: bytes
    replayed: bool
    state: str = 'reserved'


@dataclass(frozen=True)
class RouteResult:
    """state: completed | rejected | refused | in_progress | held | busy.

    Only completed (accepted) and rejected (definitive non-acceptance) carry a
    signed record. refused = verified never sent. held = uncertain. busy = not
    admitted, nothing journaled or sent (binding_digest is None); retryable.
    in_progress with binding_digest None is provisional mutex ownership by an
    identical submit only, not proof of admission, intent or effect.
    """
    state: str
    command_id: str
    binding_digest: str | None
    replayed: bool
    error: str | None = None
    record: bytes | None = None
    response: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SupervisorRouteConfig:
    """Explicit configuration. ``enabled`` defaults to False: no route exists."""
    enabled: bool = False
    journal_path: str | None = None
    create_journal: bool = False
    forward_timeout_ms: int = 5000
    allow_ephemeral_test_storage: bool = False
    busy_wait_ms: int = 0
    proof_window_ms: int = 5000
    max_outstanding_challenges: int = 4


def _shape(value, keys):
    return type(value) is dict and set(value) == keys


def _valid_binding(binding):
    return (_shape(binding, _BINDING_KEYS) and binding['method'] in _METHODS
            and type(binding['path']) is str and _PATH.fullmatch(binding['path'])
            and type(binding['body_sha256']) is str and _HEX.fullmatch(binding['body_sha256'])
            and _uuid(binding['command_id']) and _positive(binding['queue_fence'])
            and _shape(binding['epoch'], _EPOCH_KEYS)
            and _uuid(binding['epoch']['launch_id'], v4=True) and _uuid(binding['epoch']['boot_id'])
            and _positive(binding['epoch']['launch_counter'])
            and _shape(binding['backend'], _BACKEND_KEYS)
            and type(binding['backend']['container_id']) is str
            and _HEX.fullmatch(binding['backend']['container_id'])
            and type(binding['backend']['engine_id']) is str and _ID.fullmatch(binding['backend']['engine_id'])
            and all(_positive(binding['backend'][k]) for k in ('host_pid', 'process_start_ticks',
                                                              'engine_pid', 'engine_start_ticks'))
            and _id32(binding['backend']['uid']) and _id32(binding['backend']['gid']))


def verify_route_record(raw, *, public_key, issuer_id, expected_binding, received_status, received_body):
    """Hub-side verification interface (not yet wired into any Hub client).

    ``expected_binding`` is what the Hub intended (its own command bytes, epoch
    and the independently enrolled backend pin); ``received_status`` and
    ``received_body`` are what the Hub actually got back. Enforces the exact
    record shape and types, the signature under the enrolled key, the binding,
    nonce, response hash, status/outcome and that the observed peer equals the
    pinned backend. Returns the verified payload or raises.
    """
    error = SupervisorRouteError('supervisor route record unverified')
    try:
        _require(_valid_binding(expected_binding) and type(received_status) is int
                 and type(received_body) is bytes, error)
        envelope = _strict_json(raw, MAX_RECORD_BYTES)
        _require(_canonical(envelope) == raw and _shape(envelope, {'payload', 'signature'}), error)
        payload, signature = envelope['payload'], envelope['signature']
        _require(_shape(payload, _PAYLOAD_KEYS) and type(signature) is str
                 and _SIGNATURE.fullmatch(signature), error)
        decoded = base64.urlsafe_b64decode(signature + '==')
        _require(len(decoded) == 64 and base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() == signature,
                 error)
        Ed25519PublicKey.from_public_bytes(public_key).verify(decoded, SIGNATURE_DOMAIN + _canonical(payload))
        digest = binding_digest(expected_binding)
        peer = payload['observed_peer']
        status = payload['backend_status']
        _require(payload['protocol'] == PROTOCOL and payload['audience'] == AUDIENCE
                 and type(payload['issuer_id']) is str and payload['issuer_id'] == issuer_id
                 and _valid_binding(payload['binding'])
                 and _canonical(payload['binding']) == _canonical(expected_binding)
                 and payload['binding_digest'] == digest
                 and payload['observation_nonce'] == observation_nonce(digest)
                 and type(status) is int and status == received_status
                 and type(payload['response_sha256']) is str
                 and payload['response_sha256'] == hashlib.sha256(received_body).hexdigest()
                 and _shape(peer, _PEER_KEYS) and all(type(v) is int for v in peer.values())
                 and peer == {'pid': expected_binding['backend']['host_pid'],
                              'uid': expected_binding['backend']['uid'],
                              'gid': expected_binding['backend']['gid'],
                              'start_ticks': expected_binding['backend']['process_start_ticks']}
                 and _positive(payload['recorded_at_ms'])
                 and ((payload['outcome'] == 'accepted' and 200 <= status < 300)
                      or (payload['outcome'] == 'rejected' and status in _DEFINITIVE_REJECTIONS)), error)
        return payload
    except SupervisorRouteError:
        raise
    except Exception:
        raise error from None


class _DurableStore:
    """Single-owner durable SQLite store (Linux), shared by the route journal
    and the receiving-side dispatch gate.

    Mirrors ``operation_journal.SQLiteOperationJournal`` durability: PilotOwner
    flock/enrollment, BEGIN IMMEDIATE, synchronous=FULL, rollback journal,
    parent fsync after commit, 250ms busy bound per wait. Any failure raises
    RouteJournalError and the transaction is rolled back. The exact schema
    (tables AND triggers) and user_version are checked on open and inside
    every transaction.
    """
    _SCHEMA = {}
    _VERSION = 0

    def __init__(self, database_path, *, create, allow_ephemeral_test_storage=False):
        from engineering_hub.pilot_ownership import PilotOwner
        from .state import _private_dir, _private_file
        self._owner = None
        self._closed = True
        self._pid = os.getpid()
        self._mutex = threading.RLock()
        try:
            _require(sys.platform == 'linux' and type(create) is bool, RouteJournalError('linux'))
            self.path = Path(database_path)
            _require(self.path.is_absolute() and '..' not in self.path.parts, RouteJournalError('path'))
            _private_dir(self.path.parent)
            self._sidecars(_private_file)
            if create:
                _require(not self.path.exists() and not self.path.is_symlink(), RouteJournalError('fresh'))
            else:
                _private_file(self.path)
                # Rollback-journal (DELETE) mode only: refuse a WAL file before
                # SQLite opens it (header bytes 18/19 are 2 in WAL mode), and
                # again by pragma. Never converted silently.
                with open(self.path, 'rb') as header:
                    _require(header.read(20)[18:20] == b'\x01\x01', RouteJournalError('mode'))
                with self._connection(readonly=True) as db:
                    _require(db.execute('PRAGMA journal_mode').fetchone() == ('delete',), RouteJournalError('mode'))
                    self._schema(db)
            self._owner = PilotOwner(self.path, allow_ephemeral_test_storage=allow_ephemeral_test_storage)
            _require(not create or self._owner.previous_epoch is None, RouteJournalError('fresh'))
            self._closed = False
            if create:
                with self._transaction(initializing=True) as db:
                    for sql in self._SCHEMA.values():
                        db.execute(sql)
                    db.execute(f'PRAGMA user_version={self._VERSION}')
            with self._transaction() as db:
                pass
        except BaseException as error:
            if self._owner is not None:
                self._owner.close()
            self._closed = True
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise RouteJournalError('route journal unavailable') from None

    @property
    def owner_epoch(self):
        return self._owner.epoch

    def _schema(self, db):
        rows = db.execute("SELECT name, type, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'").fetchall()
        _require(len(rows) == len({name for name, _, _ in rows})
                 and {name for name, _, _ in rows} == set(self._SCHEMA) | _OWNER_TABLES
                 and all(kind == 'table' for name, kind, _ in rows if name in _OWNER_TABLES),
                 RouteJournalError('schema'))
        sql = {name: text for name, _, text in rows}
        _require(all(sql[name] == text for name, text in self._SCHEMA.items())
                 and db.execute('PRAGMA user_version').fetchone() == (self._VERSION,)
                 and db.execute('PRAGMA quick_check').fetchall() == [('ok',)], RouteJournalError('schema'))

    def _sidecars(self, private_file):
        for suffix in ('-journal', '-wal', '-shm'):
            path = self.path.with_name(self.path.name + suffix)
            if path.exists() or path.is_symlink():
                private_file(path)
                _require(suffix == '-journal', RouteJournalError('sidecar'))

    @contextmanager
    def _connection(self, *, readonly=False):
        db = sqlite3.connect(self.path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                             uri=True, timeout=_BUSY_MS / 1000, isolation_level=None)
        try:
            db.execute(f'PRAGMA busy_timeout={_BUSY_MS}')
            db.execute('PRAGMA trusted_schema=OFF')
            if not readonly:
                db.execute('PRAGMA synchronous=FULL')
                # The file must ALREADY be in rollback-journal mode: a WAL file
                # is refused, never converted back behind a reader's back.
                _require(db.execute('PRAGMA journal_mode').fetchone() == ('delete',)
                         and db.execute('PRAGMA journal_mode=DELETE').fetchone() == ('delete',),
                         RouteJournalError('mode'))
            yield db
        finally:
            db.close()

    def _active(self):
        from .state import _private_file
        _require(not self._closed and self._owner is not None and os.getpid() == self._pid,
                 RouteJournalError('inactive'))
        self._owner.assert_active(self.path)
        self._sidecars(_private_file)

    @contextmanager
    def _transaction(self, *, initializing=False):
        _require(os.getpid() == self._pid, RouteJournalError('process'))
        with self._mutex:
            # Phase of the most recent transaction, for the caller's proof of
            # "nothing committed": before_begin / in_transaction (rolled back
            # on error) / committing / committed.
            self.last_phase = 'before_begin'
            try:
                self._active()
                with self._connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    self.last_phase = 'in_transaction'
                    try:
                        _require(self._owner._read(db) == self._owner._row, RouteJournalError('owner'))
                        if not initializing:
                            self._schema(db)
                        yield db
                        _require(self._owner._read(db) == self._owner._row, RouteJournalError('owner'))
                        # From here the outcome of COMMIT is not provably rolled back.
                        self.last_phase = 'committing'
                        db.commit()
                        self.last_phase = 'committed'
                        os.fsync(self._owner._parent_fd)
                    except BaseException:
                        db.rollback()
                        raise
                self._active()
            except (RouteJournalError, SupervisorRouteRejected, SupervisorRouteBusy):
                raise
            except Exception:
                raise RouteJournalError('route journal unavailable') from None

    def close(self):
        """Releases ownership only while holding the store mutex: waits for any
        transaction or read in progress in another thread to finish, so the
        PilotOwner flock is never dropped mid-way. That wait is NOT bounded:
        only SQLite lock contention is limited (best effort, by the busy
        timeout); fsync and other filesystem calls can block indefinitely.
        Afterwards every transaction/read fails closed (``inactive``); a
        closed store never reopens itself. Lock order everywhere is
        [gate effect lock ->] store mutex; nothing that holds the store mutex
        ever takes the effect lock.

        In any process other than the constructing one (a forked child) it
        touches NEITHER lock, which may be held by a thread that does not
        exist there: it only marks itself inactive and closes its inherited
        descriptors (PilotOwner never unlocks, so the parent keeps the flock).
        """
        if os.getpid() != self._pid:
            self._closed = True
            if self._owner is not None:
                self._owner.close()
            return
        with self._mutex:
            self._closed = True
            if self._owner is not None:
                self._owner.close()


class RouteJournal(_DurableStore):
    """Dedicated durable route intent/outcome journal (Linux, single owner).

    No credential is ever passed to or stored by the journal.
    Each inserted intent carries a per-attempt nonce; rows with a NULL nonce
    are never mutated by the failed-admit handler. Each inserted intent also
    consumes the next per-executor dispatch counter in the SAME transaction
    (see ``admit``); two-phase reservations draw from the same counter (see
    ``reserve`` / ``dispatch_reserved``). A journal created with an earlier
    schema (no attempt or dispatch_counter column, no dispatch, dispatched,
    reservation, challenge (with challenge_sha256) or mode table/triggers,
    user_version below 7) fails the exact schema
    check and is refused today (fail closed); adopting one would require an
    offline migration/reconciliation step, which is NOT implemented.

    Two per-executor high-waters, both monotonic and trigger-protected:
    ``supervisor_route_dispatch`` = highest counter ALLOCATED (admit or
    reserve); ``supervisor_route_dispatched`` = highest counter whose intent
    was recorded for sending (one-step admit or reservation dispatch). A
    reservation may only be dispatched above the latter.
    """
    _SCHEMA = {'supervisor_route_intent': _INTENT, 'supervisor_route_epoch': _EPOCH,
               **_counter_schema('supervisor_route_dispatch'),
               **_counter_schema('supervisor_route_dispatched'),
               'supervisor_route_reservation': _RESERVATION, **_RESERVATION_TRIGGERS,
               'supervisor_route_challenge': _CHALLENGE, **_CHALLENGE_TRIGGERS,
               'supervisor_route_mode': _MODE, **_MODE_TRIGGERS}
    _VERSION = 7

    @staticmethod
    def _row(db, command_id):
        row = db.execute('SELECT command_id,binding_digest,binding,state,owner_epoch,error,record,response '
                         'FROM supervisor_route_intent WHERE command_id=?', (command_id,)).fetchone()
        if row is not None:
            _require(row[3] in _STATES and _HEX.fullmatch(row[1]) and (row[5] is None or row[5] in _ERRORS)
                     and (row[3] in _RECORDED) == (row[6] is not None), RouteJournalError('row'))
        return row

    def admit(self, command_id, digest, binding, epoch, attempt):
        """ONE transaction: replay/conflict check, epoch high-water, dispatch
        counter, intent row.

        Returns ('new', counter) when the caller may forward exactly once, or
        ('replay', row) for an identical binding. Raises Rejected otherwise.
        ``attempt`` is a fresh per-invocation nonce stored ONLY on a row this
        call inserts; it is journal-internal and never in results or records.
        ``counter`` is the pinned executor's (backend container_id) previous
        counter + 1, committed with the intent BEFORE any byte can be sent:
        once committed it is consumed whatever happens next (refused, held,
        crash), so a counter value is never sent twice. A rolled-back admit
        consumed nothing and sent nothing. Exhaustion is rejected unforwarded.
        """
        _require(type(attempt) is str and re.fullmatch(r'[0-9a-f]{32}', attempt), RouteJournalError('attempt'))
        with self._transaction() as db:
            row = self._row(db, command_id)
            if row is not None:
                if row[1] != digest or row[2] != _canonical(binding).decode():
                    raise SupervisorRouteRejected('binding_conflict')
                return 'replay', row
            if self._proof_mode(db):
                raise SupervisorRouteRejected('proof_required')  # Durable: no proof-less send, ever.
            if self._reservation(db, command_id) is not None:
                raise SupervisorRouteRejected('reserved_command')  # Only dispatch() may send it.
            self._advance_epoch(db, epoch)
            counter = self._next_counter(db, binding['backend']['container_id'])
            self._mark_dispatched(db, binding['backend']['container_id'], counter)
            db.execute("INSERT INTO supervisor_route_intent VALUES (?,?,?,'intent',?,NULL,NULL,NULL,?,?)",
                       (command_id, digest, _canonical(binding).decode(), self._owner.epoch, attempt, counter))
            return 'new', counter

    @staticmethod
    def _mark_dispatched(db, executor, counter):
        """Sender-side supersession fence (inside the caller's transaction):
        ``counter`` must exceed every counter already recorded for sending to
        ``executor`` by either path; advances that dispatched high-water."""
        last = db.execute('SELECT counter FROM supervisor_route_dispatched WHERE executor_id=?',
                          (executor,)).fetchone()
        _require(last is None or _positive(last[0], MAX_DISPATCH_COUNTER), RouteJournalError('counter'))
        if last is not None and not counter > last[0]:
            raise SupervisorRouteRejected('reservation_superseded')
        if last is None:
            db.execute('INSERT INTO supervisor_route_dispatched VALUES (?,?)', (executor, counter))
        else:
            db.execute('UPDATE supervisor_route_dispatched SET counter=? WHERE executor_id=?', (counter, executor))

    @staticmethod
    def _proof_mode(db):
        return db.execute('SELECT proof_required FROM supervisor_route_mode WHERE id=1').fetchone() == (1,)

    def enter_mode(self, *, proof_required):
        """ONE transaction at route construction: with ``proof_required`` the
        permanent proof-mode row is recorded (idempotent); without it, a
        journal already in proof mode is refused. Returns the durable mode.

        Entering proof mode is refused (``legacy_in_flight``) while ANY
        one-step intent row is still unresolved ('intent'): its send could
        still be in progress. Resolve it first (an outcome, or marked
        unknown, which reads held) - a crashed legacy owner's 'intent' row
        needs that operator reconciliation before proof mode can begin."""
        with self._transaction() as db:
            recorded = self._proof_mode(db)
            if recorded and not proof_required:
                raise RouteJournalError('proof mode journal requires a dispatch proof verifier')
            if proof_required and not recorded:
                if db.execute("SELECT 1 FROM supervisor_route_intent WHERE state='intent'").fetchone() is not None:
                    raise SupervisorRouteRejected('legacy_in_flight')
                db.execute('INSERT INTO supervisor_route_mode VALUES (1, 1)')
            return recorded or proof_required

    @contextmanager
    def legacy_send_guard(self, command_id, digest):
        """Held around EXACTLY the one-step backend exchange (no journal write
        happens inside it). Opens a read transaction that takes SQLite's
        SHARED lock, re-checks that the journal is NOT in proof mode and that
        this command's own intent row is still unresolved, and keeps the lock
        until the exchange returns. While it is held no writer can COMMIT
        (rollback-journal mode needs EXCLUSIVE), so proof mode cannot be
        recorded, nor this intent resolved, mid-send even by a writer that
        ignores the PilotOwner flock - in rollback-journal (DELETE) mode,
        which is asserted here (anything else raises RouteJournalError, so
        the send is held ``send_guard_failed`` with zero bytes). Raises
        Rejected('proof_required' / 'intent_resolved') before anything is
        sent. Direct file writes, restores and detached work stay outside."""
        _require(os.getpid() == self._pid, RouteJournalError('process'))
        with self._mutex:
            self._active()
        with self._connection(readonly=True) as db:
            db.execute('BEGIN')
            try:
                self._schema(db)  # First read: SHARED lock from here until rollback.
                # The SHARED lock blocks commits ONLY in rollback-journal mode
                # (in WAL, writers commit alongside readers). Changing the mode
                # needs an exclusive lock, so it is stable from here.
                _require(db.execute('PRAGMA journal_mode').fetchone() == ('delete',),
                         RouteJournalError('journal_mode'))
                if self._proof_mode(db):
                    raise SupervisorRouteRejected('proof_required')
                row = self._row(db, command_id)
                if row is None or row[1] != digest or row[3] != 'intent':
                    raise SupervisorRouteRejected('intent_resolved')
                yield
            finally:
                db.rollback()

    @staticmethod
    def _advance_epoch(db, epoch):
        """Durable epoch high-water check/advance (inside the caller's transaction)."""
        high = db.execute('SELECT launch_counter,launch_id,boot_id FROM supervisor_route_epoch').fetchone()
        if high is not None:
            if epoch.launch_counter < high[0]:
                raise SupervisorRouteRejected('stale_epoch')
            if epoch.launch_counter == high[0] and (epoch.launch_id, epoch.boot_id) != high[1:]:
                raise SupervisorRouteRejected('epoch_conflict')
        if high is None or epoch.launch_counter > high[0]:
            db.execute('INSERT OR REPLACE INTO supervisor_route_epoch VALUES (1,?,?,?)',
                       (epoch.launch_counter, epoch.launch_id, epoch.boot_id))

    @staticmethod
    def _next_counter(db, executor):
        """Consume previous + 1 for ``executor`` (inside the caller's transaction)."""
        last = db.execute('SELECT counter FROM supervisor_route_dispatch WHERE executor_id=?',
                          (executor,)).fetchone()
        _require(last is None or _positive(last[0], MAX_DISPATCH_COUNTER), RouteJournalError('counter'))
        counter = 1 if last is None else last[0] + 1
        if counter > MAX_DISPATCH_COUNTER:
            raise SupervisorRouteRejected('dispatch_counter_exhausted')
        if last is None:
            db.execute('INSERT INTO supervisor_route_dispatch VALUES (?,?)', (executor, counter))
        else:
            db.execute('UPDATE supervisor_route_dispatch SET counter=? WHERE executor_id=?', (counter, executor))
        return counter

    @staticmethod
    def _reservation(db, command_id):
        row = db.execute('SELECT command_id,executor_id,binding_digest,binding,counter,state,receipt '
                         'FROM supervisor_route_reservation WHERE command_id=?', (command_id,)).fetchone()
        if row is not None:
            _require(_HEX.fullmatch(row[2]) and _positive(row[4], MAX_DISPATCH_COUNTER)
                     and row[5] in ('reserved', 'dispatched', 'abandoned'), RouteJournalError('reservation'))
        return row

    def reserve(self, command_id, digest, binding, epoch, sign):
        """ONE transaction, SENDS NOTHING: binding conflict check, epoch
        high-water, next per-executor counter, and a 'reserved' row holding
        the signed receipt ``sign(counter)`` returns (signed inside the
        transaction, so a signing failure consumes nothing).

        Returns ('new', receipt) or ('replay', receipt) - the SAME stored bytes
        - for an identical binding whose row is still 'reserved'. Raises
        Rejected for a different binding (``binding_conflict``), a command_id
        already admitted one-step (``already_admitted``), or an identical
        binding whose reservation is terminal (``reservation_dispatched`` /
        ``reservation_abandoned``): a repeat never looks like a fresh one.
        """
        with self._transaction() as db:
            row = self._reservation(db, command_id)
            if row is not None:
                if row[2] != digest or row[3] != _canonical(binding).decode():
                    raise SupervisorRouteRejected('binding_conflict')
                if row[5] != 'reserved':
                    raise SupervisorRouteRejected('reservation_' + row[5])
                return 'replay', row[6].encode()
            if self._row(db, command_id) is not None:
                raise SupervisorRouteRejected('already_admitted')
            self._advance_epoch(db, epoch)
            executor = binding['backend']['container_id']
            counter = self._next_counter(db, executor)
            receipt = sign(counter)
            _require(type(receipt) is bytes, RouteJournalError('receipt'))
            db.execute("INSERT INTO supervisor_route_reservation VALUES (?,?,?,?,?,'reserved',?)",
                       (command_id, executor, digest, _canonical(binding).decode(), counter, receipt.decode()))
            return 'new', receipt

    def dispatch_reserved(self, command_id, digest, binding, receipt, counter, epoch, attempt, *, proof,
                          proof_sha256, clock, window_ns, run_id):
        """ONE transaction, consume-once: the 'reserved' row must exist with
        exactly this binding, these receipt bytes and this counter, and the
        epoch high-water must still be the receipt's epoch. Marks it
        'dispatched' and inserts the intent row (with the reserved counter)
        BEFORE the caller may send. Its counter must also exceed every
        counter already recorded for sending to that executor by EITHER path
        (dispatched high-water); a lower one is refused and nothing is
        marked. Lower reservations that were abandoned or never dispatched
        do not block a higher one (gaps). In the SAME transaction the
        (already signature-verified) Hub ``proof`` must bind exactly this
        receipt, reservation, command fence and the current boot, and its
        challenge nonce must be outstanding, issued in this boot for this
        reservation, and no older than ``window_ns`` on ``clock`` (the
        supervisor's own CLOCK_BOOTTIME); the nonce is then consumed. Raises
        Rejected(reason) otherwise, consuming nothing:
        reservation_missing / reservation_mismatch / already_dispatched /
        reservation_abandoned / epoch_changed / reservation_superseded /
        proof_mismatch / challenge_unknown / challenge_used /
        challenge_expired / challenge_consume_failed."""
        _require(type(attempt) is str and re.fullmatch(r'[0-9a-f]{32}', attempt), RouteJournalError('attempt'))
        with self._transaction() as db:
            row = self._reservation(db, command_id)
            if row is None:
                raise SupervisorRouteRejected('reservation_missing')
            if (row[2] != digest or row[3] != _canonical(binding).decode() or row[6] != receipt.decode()
                    or row[4] != counter):
                raise SupervisorRouteRejected('reservation_mismatch')
            if row[5] == 'dispatched':
                raise SupervisorRouteRejected('already_dispatched')
            if row[5] != 'reserved':
                raise SupervisorRouteRejected('reservation_abandoned')
            high = db.execute('SELECT launch_counter,launch_id,boot_id FROM supervisor_route_epoch').fetchone()
            if high != (epoch.launch_counter, epoch.launch_id, epoch.boot_id):
                raise SupervisorRouteRejected('epoch_changed')
            if self._row(db, command_id) is not None:
                raise SupervisorRouteRejected('already_dispatched')
            self._mark_dispatched(db, row[1], counter)
            self._consume_challenge(db, row, receipt, binding, proof, proof_sha256, clock, window_ns, run_id)
            db.execute("UPDATE supervisor_route_reservation SET state='dispatched' "
                       "WHERE command_id=? AND state='reserved'", (command_id,))
            db.execute("INSERT INTO supervisor_route_intent VALUES (?,?,?,'intent',?,NULL,NULL,NULL,?,?)",
                       (command_id, digest, _canonical(binding).decode(), self._owner.epoch, attempt, counter))

    @staticmethod
    def _clock(clock):
        boot_id, now = clock()
        _require(_uuid(boot_id) and type(now) is int and now >= 0, RouteJournalError('clock'))
        return boot_id, now

    def _consume_challenge(self, db, row, receipt, binding, proof, proof_sha256, clock, window_ns, run_id):
        # Signed fields also present in the exact request body (command_id,
        # run_id, fencing_generation == queue_fence) must equal it. job_id,
        # worker, machine_id and owner_epoch are NOT in the body or any
        # supervisor state: they are covered only by the Hub's signature and
        # its barrier (which must enforce them before releasing any proof).
        boot_id, now = self._clock(clock)
        if (proof['reservation_receipt_sha256'] != hashlib.sha256(receipt).hexdigest()
                or proof['command_id'] != row[0] or proof['counter'] != row[4] or proof['executor'] != row[1]
                or proof['fence'] != binding['queue_fence'] or proof['run_id'] != run_id
                or proof['supervisor_boot_id'] != boot_id):
            raise SupervisorRouteRejected('proof_mismatch')
        challenge = db.execute('SELECT command_id,executor_id,counter,issued_boottime_ns,boot_id,state,'
                               'challenge_sha256 FROM supervisor_route_challenge WHERE nonce=?',
                               (proof['challenge_nonce'],)).fetchone()
        if challenge is None or challenge[:3] != (row[0], row[1], row[4]):
            raise SupervisorRouteRejected('challenge_unknown')
        # The proof must bind the exact signed challenge bytes this supervisor
        # issued (an unsigned/unbound challenge can never match).
        if challenge[6] is None or proof['challenge_sha256'] != challenge[6]:
            raise SupervisorRouteRejected('proof_mismatch')
        if challenge[5] == 'consumed':
            raise SupervisorRouteRejected('challenge_used')
        if challenge[5] != 'outstanding' or challenge[4] != boot_id or not 0 <= now - challenge[3] <= window_ns:
            raise SupervisorRouteRejected('challenge_expired')
        consumed = db.execute("UPDATE supervisor_route_challenge SET state='consumed', proof_sha256=? "
                              "WHERE nonce=? AND state='outstanding'", (proof_sha256, proof['challenge_nonce']))
        # Read back before anything may be sent: exactly this nonce, consumed
        # with exactly this proof hash; anything else rolls the dispatch back.
        if consumed.rowcount != 1 or db.execute(
                'SELECT state, proof_sha256 FROM supervisor_route_challenge WHERE nonce=?',
                (proof['challenge_nonce'],)).fetchall() != [('consumed', proof_sha256)]:
            raise SupervisorRouteRejected('challenge_consume_failed')

    def issue_challenge(self, command_id, nonce, clock, window_ns, cap):
        """ONE transaction: the reservation must be 'reserved' and not
        superseded; at most ``cap`` LIVE (this boot, within the window)
        outstanding challenges per executor (else SupervisorRouteBusy).
        Records an 'outstanding', not yet signed row; returns the fields the
        signed challenge carries: executor, counter, boot_id, the receipt's
        SHA-256 and the reserved binding's pin and epoch."""
        with self._transaction() as db:
            row = self._reservation(db, command_id)
            if row is None:
                raise SupervisorRouteRejected('reservation_missing')
            if row[5] != 'reserved':
                raise SupervisorRouteRejected('reservation_' + row[5])
            high = db.execute('SELECT counter FROM supervisor_route_dispatched WHERE executor_id=?',
                              (row[1],)).fetchone()
            if high is not None and not row[4] > high[0]:
                raise SupervisorRouteRejected('reservation_superseded')
            boot_id, now = self._clock(clock)
            live = db.execute("SELECT COUNT(*) FROM supervisor_route_challenge WHERE executor_id=? "
                              "AND state='outstanding' AND boot_id=? AND ? - issued_boottime_ns <= ?",
                              (row[1], boot_id, now, window_ns)).fetchone()[0]
            if live >= cap:
                raise SupervisorRouteBusy()
            db.execute("INSERT INTO supervisor_route_challenge VALUES (?,?,?,?,?,?,'outstanding',NULL,NULL)",
                       (nonce, command_id, row[1], row[4], now, boot_id))
            binding = json.loads(row[3])
            return {'executor': row[1], 'counter': row[4], 'boot_id': boot_id,
                    'reservation_receipt_sha256': hashlib.sha256(row[6].encode()).hexdigest(),
                    'pin': binding['backend'], 'epoch': binding['epoch']}

    def bind_challenge(self, nonce, challenge_sha256):
        """ONE transaction: binds the SHA-256 of the exact signed challenge
        bytes to its still-outstanding, unsigned row (once)."""
        _require(type(challenge_sha256) is str and _HEX.fullmatch(challenge_sha256), RouteJournalError('hash'))
        with self._transaction() as db:
            bound = db.execute("UPDATE supervisor_route_challenge SET challenge_sha256=? WHERE nonce=? "
                               "AND state='outstanding' AND challenge_sha256 IS NULL", (challenge_sha256, nonce))
            _require(bound.rowcount == 1, RouteJournalError('challenge'))

    def expire_challenges(self, clock, window_ns):
        """Marks every outstanding challenge that can no longer be consumed
        (issued in another boot, or older than the window) expired."""
        with self._transaction() as db:
            boot_id, now = self._clock(clock)
            return db.execute("UPDATE supervisor_route_challenge SET state='expired' WHERE state='outstanding' "
                              'AND (boot_id IS NOT ? OR ? - issued_boottime_ns > ?)',
                              (boot_id, now, window_ns)).rowcount

    def revoke_unconsumed(self, nonce):
        """ONE transaction, RESERVATION-WIDE: the nonce must exist; NO challenge
        for its reservation may be consumed and the reservation must not be
        dispatched. Then EVERY outstanding challenge for that reservation is
        marked expired and the reservation is retired (reserved -> abandoned;
        its counter stays consumed), so no other nonce can still dispatch it.
        Idempotent once retired. Returns (command_id, counter, boot_id).
        Raises Rejected: challenge_unknown / challenge_used /
        reservation_dispatched."""
        with self._transaction() as db:
            challenge = db.execute('SELECT command_id,counter,boot_id,state FROM supervisor_route_challenge '
                                   'WHERE nonce=?', (nonce,)).fetchone()
            if challenge is None:
                raise SupervisorRouteRejected('challenge_unknown')
            if db.execute("SELECT 1 FROM supervisor_route_challenge WHERE command_id=? AND state='consumed'",
                          (challenge[0],)).fetchone() is not None:
                raise SupervisorRouteRejected('challenge_used')
            reservation = self._reservation(db, challenge[0])
            if reservation is None or reservation[5] == 'dispatched':
                raise SupervisorRouteRejected('reservation_dispatched')
            db.execute("UPDATE supervisor_route_challenge SET state='expired' "
                       "WHERE command_id=? AND state='outstanding'", (challenge[0],))
            db.execute("UPDATE supervisor_route_reservation SET state='abandoned' "
                       "WHERE command_id=? AND state='reserved'", (challenge[0],))
            return challenge[0], challenge[1], challenge[2]

    def read_challenge(self, nonce):
        """Read-only: (state, command_id, counter, boot_id) or None."""
        _require(os.getpid() == self._pid, RouteJournalError('process'))
        with self._mutex:
            try:
                self._active()
                with self._connection(readonly=True) as db:
                    self._schema(db)
                    row = db.execute('SELECT state,command_id,counter,boot_id FROM supervisor_route_challenge '
                                     'WHERE nonce=?', (nonce,)).fetchone()
            except RouteJournalError:
                raise
            except Exception:
                raise RouteJournalError('route journal unavailable') from None
        return row

    def abandon_reservation(self, command_id):
        """reserved -> abandoned (never sent; the counter stays consumed).
        Returns True if this call abandoned it, False otherwise."""
        with self._transaction() as db:
            return db.execute("UPDATE supervisor_route_reservation SET state='abandoned' "
                              "WHERE command_id=? AND state='reserved'", (command_id,)).rowcount == 1

    def read_reservation(self, command_id):
        """Read-only: (state, counter, receipt bytes) or None."""
        _require(os.getpid() == self._pid, RouteJournalError('process'))
        with self._mutex:
            try:
                self._active()
                with self._connection(readonly=True) as db:
                    self._schema(db)
                    row = self._reservation(db, command_id)
            except RouteJournalError:
                raise
            except Exception:
                raise RouteJournalError('route journal unavailable') from None
        return None if row is None else (row[5], row[4], row[6].encode())

    def abandon_attempt(self, command_id, digest, attempt):
        """Atomic compare-and-set: intent -> unknown ONLY for the row THIS
        attempt inserted. A pre-existing (replayed) intent, any recorded row,
        or a row without an attempt nonce is never matched or mutated."""
        _require(type(attempt) is str and re.fullmatch(r'[0-9a-f]{32}', attempt), RouteJournalError('attempt'))
        with self._transaction() as db:
            return db.execute("UPDATE supervisor_route_intent SET state='unknown', error='interrupted' "
                              "WHERE command_id=? AND binding_digest=? AND state='intent' "
                              "AND attempt IS NOT NULL AND attempt=?",
                              (command_id, digest, attempt)).rowcount

    def record_outcome(self, command_id, digest, state, record, response):
        _require(state in _RECORDED, RouteJournalError('state'))
        with self._transaction() as db:
            row = self._row(db, command_id)
            _require(row is not None and row[1] == digest and row[3] == 'intent', RouteJournalError('state'))
            db.execute("UPDATE supervisor_route_intent SET state=?, record=?, response=? "
                       "WHERE command_id=? AND state='intent'", (state, record.decode(), response, command_id))

    def _mark(self, command_id, digest, state, error):
        _require(error in _ERRORS, RouteJournalError('error'))
        with self._transaction() as db:
            row = self._row(db, command_id)
            _require(row is not None and row[1] == digest and row[3] in {'intent', state},
                     RouteJournalError('state'))
            if row[3] == 'intent':
                db.execute('UPDATE supervisor_route_intent SET state=?, error=? WHERE command_id=?',
                           (state, error, command_id))

    def mark_unknown(self, command_id, digest, error):
        self._mark(command_id, digest, 'unknown', error)

    def mark_refused(self, command_id, digest, error):
        """Verified NOT sent (zero bytes written); terminal for this command_id."""
        self._mark(command_id, digest, 'refused', error)

    def read(self, command_id):
        """Read-only inspection: a read transaction, no writes, no forwarding."""
        _require(os.getpid() == self._pid, RouteJournalError('process'))
        with self._mutex:
            try:
                self._active()
                with self._connection(readonly=True) as db:
                    self._schema(db)
                    return self._row(db, command_id)
            except RouteJournalError:
                raise
            except Exception:
                raise RouteJournalError('route journal unavailable') from None



@dataclass(frozen=True)
class DispatchGateConfig:
    """Explicit configuration. ``enabled`` defaults to False: no gate exists.

    ``executor_id`` is this executor's pinned container_id and ``launch_epoch``
    its enrolled RouteEpoch; claims for anything else are refused.
    ``effect_wait_ms`` (0..50) bounds the wait for another run's effect."""
    enabled: bool = False
    journal_path: str | None = None
    create_journal: bool = False
    executor_id: str | None = None
    launch_epoch: RouteEpoch | None = None
    allow_ephemeral_test_storage: bool = False
    effect_wait_ms: int = 0


class DispatchCounterGate(_DurableStore):
    """DISABLED-BY-DEFAULT receiving-side dispatch counter gate AND effect fence.

    There is no public "accept, then act" call: the only entry point is
    ``run(dispatch, method=, path=, body=, effect=)``, which runs the caller's
    side effect itself, so a caller cannot skip the fence or forget a release:

    1. Binding: the header claim must equal this executor, its launch epoch
       and the actual request (method, path, body SHA-256, and the command_id
       and fencing_generation parsed strictly from the body). Otherwise
       DispatchRefused; nothing recorded.
    2. Admission: one transaction refuses a counter not strictly greater than
       the last ACCEPTED one (``dispatch_replay``) and records it. Replays,
       duplicates and reorderings are refused, never deduplicated. Gaps are
       ALLOWED: the sender consumes a counter at admission even when the
       request is never sent, so requiring +1 would wedge on the first gap.
    3. Fence: under the gate's effect lock (bounded wait, else
       ``dispatch_busy``), one transaction re-checks that this counter is
       STILL the latest accepted one and is greater than every counter whose
       effect ever started (``dispatch_superseded``), and that no earlier
       effect is unresolved (``dispatch_in_flight_unresolved``); it then
       durably records "started" plus an in-flight marker. Only after that
       COMMIT is ``effect()`` called. So an accepted-then-delayed counter can
       never act after a higher one was accepted or acted.
    4. Completion: ONLY a normal, synchronous return of ``effect()`` replaces
       the in-flight marker with the permanent outcome ``completed``, before
       the lock is released. If effect raises (any BaseException), or
       returns an awaitable (``dispatch_effect_async``: a coroutine, task or
       future may be running or half-done), the marker is KEPT: the effect
       may have partially happened or left detached work, so its outcome is
       unknown and the gate stays fail-closed.

    Ownership is never released mid-effect: ``close()`` refuses
    (SupervisorRouteInFlight) while an effect runs, so this process keeps the
    PilotOwner flock, and no other process can open the store, until the
    effect has returned and its outcome is recorded. A process that dies
    mid-effect releases the flock only by dying, and leaves the marker.

    With a kept marker (dead process, raised or async effect, or a failed
    completion write) the gate FAILS CLOSED, loudly: every later ``run`` is
    refused with ``dispatch_in_flight_unresolved`` (``inspect`` names the
    counter) until an operator calls
    ``release_unresolved_after_quiescence(counter, evidence=...)``. That
    records ``released`` (effect outcome UNKNOWN) with the operator's
    evidence. The released counter, and every lower one, can never run
    again: "started" is monotonic.

    ``effect`` MUST be synchronous and finish its side effect before
    returning. OPEN GAP: work it hands to another thread/process/engine and
    leaves running is outside the fence and is not detectable here; an
    explicit, enforceable drain contract for detached work is NOT
    implemented. Any journal failure raises RouteJournalError and nothing
    further is done. Its own store, separate from the route journal. NOT
    wired into PilotASGI or any service.
    """
    _SCHEMA = {**_counter_schema('supervisor_route_dispatch_accepted'),
               **_counter_schema('supervisor_route_dispatch_started'),
               'supervisor_route_dispatch_in_flight': _GATE_IN_FLIGHT,
               'supervisor_route_dispatch_outcome': _GATE_OUTCOME,
               'supervisor_route_dispatch_outcome_frozen': _GATE_OUTCOME_FROZEN,
               'supervisor_route_dispatch_outcome_kept': _GATE_OUTCOME_KEPT}
    _VERSION = 1

    def __init__(self, *, config):
        if type(config) is not DispatchGateConfig or config.enabled is not True:
            raise SupervisorRouteDisabled('dispatch counter gate is disabled')
        _require(type(config.executor_id) is str and _HEX.fullmatch(config.executor_id)
                 and type(config.launch_epoch) is RouteEpoch and type(config.journal_path) is str
                 and type(config.effect_wait_ms) is int and 0 <= config.effect_wait_ms <= 50,
                 SupervisorRouteError('invalid dispatch counter gate'))
        self._executor, self._epoch = config.executor_id, config.launch_epoch.as_dict()
        self._effect_wait = config.effect_wait_ms / 1000
        self._effect_lock = threading.Lock()
        super().__init__(config.journal_path, create=config.create_journal,
                         allow_ephemeral_test_storage=config.allow_ephemeral_test_storage)

    def __repr__(self):
        return 'DispatchCounterGate(<enabled>)'

    def run(self, dispatch, *, method, path, body, effect):
        """Returns ``effect()``'s result after the fenced, recorded effect."""
        _require(callable(effect) and not inspect.iscoroutinefunction(effect),
                 SupervisorRouteError('synchronous effect required'))
        counter = self._bound(dispatch, method, path, body)
        self._accept(counter)
        return self._fenced(counter, effect)

    def _bound(self, dispatch, method, path, body):
        claim = parse_dispatch_header(dispatch)
        if claim['executor_id'] != self._executor:
            raise DispatchRefused('dispatch_wrong_executor')
        if claim['epoch'] != self._epoch:
            raise DispatchRefused('dispatch_wrong_epoch')
        try:
            _require(type(method) is str and type(path) is str and type(body) is bytes
                     and (claim['method'], claim['path']) == (method, path) == ('POST', SUBMIT_PATH)
                     and claim['body_sha256'] == hashlib.sha256(body).hexdigest())
            request = _pilot_request(body)
            _require(request.command_id == claim['command_id']
                     and request.fencing_generation == claim['queue_fence'])
        except Exception:
            raise DispatchRefused('dispatch_request_mismatch') from None
        return claim['counter']

    def _latest(self, db, table):
        row = db.execute(f'SELECT counter FROM {table} WHERE executor_id=?', (self._executor,)).fetchone()
        _require(row is None or _positive(row[0], MAX_DISPATCH_COUNTER), RouteJournalError('counter'))
        return None if row is None else row[0]

    def _advance(self, db, table, counter, previous):
        if previous is None:
            db.execute(f'INSERT INTO {table} VALUES (?,?)', (self._executor, counter))
        else:
            db.execute(f'UPDATE {table} SET counter=? WHERE executor_id=?', (counter, self._executor))

    def _accept(self, counter):
        with self._transaction() as db:
            last = self._latest(db, 'supervisor_route_dispatch_accepted')
            if last is not None and not counter > last:
                raise DispatchRefused('dispatch_replay')
            self._advance(db, 'supervisor_route_dispatch_accepted', counter, last)

    def _fence(self, counter):
        """Durable go/no-go for THIS counter's effect; called holding the lock."""
        with self._transaction() as db:
            if db.execute('SELECT 1 FROM supervisor_route_dispatch_in_flight').fetchone() is not None:
                raise DispatchRefused('dispatch_in_flight_unresolved')
            started = self._latest(db, 'supervisor_route_dispatch_started')
            if (self._latest(db, 'supervisor_route_dispatch_accepted') != counter
                    or (started is not None and not counter > started)):
                raise DispatchRefused('dispatch_superseded')
            self._advance(db, 'supervisor_route_dispatch_started', counter, started)
            db.execute('INSERT INTO supervisor_route_dispatch_in_flight VALUES (?,?)', (self._executor, counter))

    def _finish(self, counter, outcome, evidence=None):
        with self._transaction() as db:
            _require(db.execute('SELECT counter FROM supervisor_route_dispatch_in_flight WHERE executor_id=?',
                                (self._executor,)).fetchone() == (counter,), RouteJournalError('in_flight'))
            db.execute('DELETE FROM supervisor_route_dispatch_in_flight WHERE executor_id=?', (self._executor,))
            db.execute('INSERT INTO supervisor_route_dispatch_outcome VALUES (?,?,?,?)',
                       (self._executor, counter, outcome, evidence))

    def _fenced(self, counter, effect):
        acquired = (self._effect_lock.acquire(timeout=self._effect_wait) if self._effect_wait
                    else self._effect_lock.acquire(blocking=False))
        if not acquired:
            raise DispatchRefused('dispatch_busy')  # Counter stays consumed; it never acts later.
        try:
            self._fence(counter)
            # Durable in-flight from here. Any exception propagates with the
            # marker KEPT (outcome unknown); only a normal return completes.
            result = effect()
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()  # Never started by us; a task/future may be live.
                raise DispatchRefused('dispatch_effect_async')  # Marker kept.
            self._finish(counter, 'completed')
            return result
        finally:
            self._effect_lock.release()

    def release_unresolved_after_quiescence(self, counter, *, evidence):
        """OPERATOR ACT. Clears the kept in-flight marker of exactly ``counter``
        and durably records ``released`` (effect outcome UNKNOWN) with
        ``evidence`` (1..1024 printable ASCII: who established, and how, that
        the effect has STOPPED: process gone, detached work drained or killed).

        The gate CANNOT prove quiescence. It only refuses while an effect runs
        in THIS process, and other processes cannot open the store while this
        one owns it; it cannot see detached work. Releasing while the effect
        (or work it handed off) is still active lets a higher counter act
        concurrently with it. The released counter never runs again.
        """
        _require(_positive(counter, MAX_DISPATCH_COUNTER), SupervisorRouteError('counter'))
        _require(type(evidence) is str and _EVIDENCE.fullmatch(evidence),
                 SupervisorRouteError('operator quiescence evidence required'))
        if not self._effect_lock.acquire(blocking=False):
            raise DispatchRefused('dispatch_busy')  # A live run owns it: not quiescent.
        try:
            self._finish(counter, 'released', evidence)
        finally:
            self._effect_lock.release()

    def close(self):
        """Refuses (SupervisorRouteInFlight) while an effect is running, so the
        inter-process owner lock is never released mid-effect. Retry after
        the effect returns. Then, still holding the effect lock, waits for
        the store mutex (fixed order: effect lock -> store mutex) so no
        admission or inspection is mid-transaction when ownership is
        released. In a forked child it touches neither lock (see
        ``_DurableStore.close``)."""
        if os.getpid() != self._pid:
            return super().close()
        if not self._effect_lock.acquire(blocking=False):
            raise SupervisorRouteInFlight('dispatch effect')
        try:
            super().close()
        finally:
            self._effect_lock.release()

    def inspect(self):
        """Read-only {'accepted', 'started', 'in_flight'} counters (or None)."""
        _require(os.getpid() == self._pid, RouteJournalError('process'))
        with self._mutex:
            try:
                self._active()
                with self._connection(readonly=True) as db:
                    self._schema(db)
                    return {'accepted': self._latest(db, 'supervisor_route_dispatch_accepted'),
                            'started': self._latest(db, 'supervisor_route_dispatch_started'),
                            'in_flight': self._latest(db, 'supervisor_route_dispatch_in_flight')}
            except RouteJournalError:
                raise
            except Exception:
                raise RouteJournalError('dispatch gate unavailable') from None


def _proc_start(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    suffix = raw[raw.rfind(')') + 2:].split()
    _require(len(suffix) >= 20 and suffix[0] not in {'Z', 'X'}, SupervisorRouteError('peer'))
    ticks = int(suffix[19])
    _require(ticks > 0, SupervisorRouteError('peer'))
    return ticks


MAX_HEADER_BYTES = 8192
MAX_HEADERS = 32
_STATUS_LINE = re.compile(rb'HTTP/1\.[01] ([1-5][0-9]{2})(?: [\x20-\x7e]*)?\Z')
_HEADER_LINE = re.compile(rb"([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*([\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?)?[ \t]*\Z")
_LENGTH = re.compile(rb'[0-9]{1,6}\Z')


def _framing():
    return SupervisorRouteError('backend framing')


def _read_response(peer, remaining):
    """Strict bounded HTTP/1.1 response reader under ONE total deadline.

    ``remaining()`` is recomputed before every recv and set as the socket
    timeout, so trickling or stalled peers cannot extend the exchange. Exactly
    one decimal Content-Length within MAX_RESPONSE_BYTES, no Transfer-Encoding,
    at most MAX_HEADERS headers / MAX_HEADER_BYTES header bytes, exactly
    Content-Length body bytes, then EOF (Connection: close). Any violation
    raises; the caller treats it as held, never as success or rejection.
    """
    def receive(size):
        peer.settimeout(remaining())
        part = peer.recv(size)
        if not part:
            raise _framing()
        return part
    buffer = bytearray()
    while True:
        end = buffer.find(b'\r\n\r\n')
        if end >= 0:
            break
        if len(buffer) > MAX_HEADER_BYTES:
            raise _framing()
        buffer += receive(MAX_HEADER_BYTES + 4 - len(buffer) if len(buffer) < MAX_HEADER_BYTES else 4)
    if end > MAX_HEADER_BYTES:
        raise _framing()
    head, body = bytes(buffer[:end]), bytearray(buffer[end + 4:])
    lines = head.split(b'\r\n')
    match = _STATUS_LINE.fullmatch(lines[0])
    if match is None or len(lines) - 1 > MAX_HEADERS:
        raise _framing()
    lengths = []
    for line in lines[1:]:
        header = _HEADER_LINE.fullmatch(line)
        if header is None:
            raise _framing()
        name = header[1].lower()
        if name == b'transfer-encoding':
            raise _framing()
        if name == b'content-length':
            lengths.append(header[2] or b'')
    if len(lengths) != 1 or not _LENGTH.fullmatch(lengths[0]):
        raise _framing()
    length = int(lengths[0])
    if length > MAX_RESPONSE_BYTES or len(body) > length:
        raise _framing()
    while len(body) < length:
        body += receive(length - len(body))
    # Connection: close was requested: exactly EOF must follow, within deadline.
    peer.settimeout(remaining())
    if peer.recv(1) != b'':
        raise _framing()
    remaining()
    return int(match[1]), bytes(body)


class UnixSocketBackend:
    """One-shot HTTP/1.1 over an explicit Unix socket to the executor ASGI.

    After connect and BEFORE writing any byte, the peer's SO_PEERCRED
    pid/uid/gid and pidfd-held start ticks must equal the pin, else
    PeerMismatch (zero bytes sent). The peer is rechecked after the reply.
    Sends the caller-supplied executor bearer; never stores it elsewhere,
    never includes it in repr or exceptions. No retries/reconnects/redirects.
    Carries the route's ``dispatch`` value (see ``dispatch_header``) as the
    ``X-Hub-Route-Dispatch`` header; a missing or malformed value is refused
    before connecting.
    """
    carries_dispatch = True

    def __init__(self, socket_path, *, bearer_token):
        _require(sys.platform == 'linux' and hasattr(socket, 'SO_PEERCRED') and hasattr(os, 'pidfd_open'),
                 SupervisorRouteError('linux'))
        _require(type(socket_path) is str and socket_path.startswith('/')
                 and os.path.normpath(socket_path) == socket_path, SupervisorRouteError('path'))
        self._path = socket_path
        self.__authorization = 'Bearer ' + _bearer(bearer_token)

    def __repr__(self):
        return 'UnixSocketBackend(<redacted>)'

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError('UnixSocketBackend is not serializable')

    def forward(self, *, method, path, body, timeout_ms, pin, dispatch=None):
        try:
            return self._forward(method, path, body, timeout_ms, pin, dispatch)
        except PeerMismatch:
            raise PeerMismatch() from None
        except Exception:
            # Sanitized: no header, token or backend text in the exception chain.
            raise SupervisorRouteError('backend exchange unavailable') from None

    def _forward(self, method, path, body, timeout_ms, pin, dispatch):
        claim = parse_dispatch_header(dispatch)  # Before connect: nothing sent if refused.
        _require((claim['method'], claim['path'], claim['body_sha256'])
                 == (method, path, hashlib.sha256(body).hexdigest()),
                 DispatchRefused('dispatch_request_mismatch'))
        deadline = time.monotonic() + timeout_ms / 1000

        def remaining():
            value = deadline - time.monotonic()
            if not value > 0:
                raise TimeoutError
            return value
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        pidfd = None
        try:
            peer.settimeout(remaining())
            peer.connect(self._path)
            pid, uid, gid = struct.unpack('=iII', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid <= 0:
                raise PeerMismatch()
            try:
                before = _proc_start(pid)
                pidfd = os.pidfd_open(pid, 0)
                start = _proc_start(pid)
            except Exception:
                raise PeerMismatch() from None
            observed = ObservedPeer(pid, uid, gid, start)
            if before != start or not observed.matches(pin):
                raise PeerMismatch()  # Socket closed in finally; nothing was written.
            lines = [f'{method} {path} HTTP/1.1', 'Host: supervisor-route', 'Accept-Encoding: identity',
                     'Connection: close', f'{DISPATCH_HEADER}: {dispatch}',
                     'Authorization: ' + self.__authorization]
            if method == 'POST':
                lines += ['Content-Type: application/json', f'Content-Length: {len(body)}']
            request = ('\r\n'.join(lines) + '\r\n\r\n').encode('ascii') + (body if method == 'POST' else b'')
            del lines
            # Python 3.5+: the socket timeout bounds the WHOLE sendall.
            peer.settimeout(remaining())
            peer.sendall(request)
            del request
            status, raw = _read_response(peer, remaining)
            _require(not select.select([pidfd], [], [], 0)[0] and _proc_start(pid) == start,
                     SupervisorRouteError('peer'))
            return BackendReply(status, raw, observed)
        finally:
            if pidfd is not None:
                os.close(pidfd)
            peer.close()


def _classify(request, reply):
    """('accepted'|'rejected', None) for definitive replies, else (None, error)."""
    status, body = reply.status, reply.body
    if type(status) is not int or type(body) is not bytes:
        return None, 'malformed_response'
    if 200 <= status < 300:
        try:
            value = _strict_json(body, MAX_RESPONSE_BYTES)
            _require(type(value) is dict and set(value) == set(ContainerPilotResponse.model_fields))
            response = ContainerPilotResponse.model_validate(value)
            _require(all(getattr(response, name) == getattr(request, name)
                         for name in ContainerPilotRequest.model_fields))
        except Exception:
            return None, 'malformed_response'
        return 'accepted', None
    if status in _DEFINITIVE_REJECTIONS:
        try:
            value = _strict_json(body, MAX_RESPONSE_BYTES)
            _require(_shape(value, {'error'}) and _shape(value['error'], {'code'})
                     and value['error']['code'] == _DEFINITIVE_REJECTIONS[status])
        except Exception:
            return None, 'malformed_response'
        return 'rejected', None
    return None, 'backend_ambiguous'


class SupervisorRoute:
    """Explicitly enabled, supervisor-owned Hub-facing command route.

    ``epoch_source()`` must return the supervisor's CURRENT (RouteEpoch,
    BackendPin); it is consulted before admission and again after forwarding.
    ``backend.forward(method=, path=, body=, timeout_ms=, pin=)`` returns a
    BackendReply, raises PeerMismatch before sending, or raises otherwise. A
    backend whose ``carries_dispatch`` is True also receives ``dispatch=``, the
    durably recorded dispatch counter header value; any other backend does not
    carry it, and a wired receiving-side gate would refuse its requests.
    ``signing_key`` is the already-enrolled issuer Ed25519 key and must match
    ``release.public_key``; none is created here. Any executor bearer belongs
    to the backend object, never to this route. Commands are serialized by one
    mutex; this is a foundation, not a server.
    """

    def __init__(self, *, config, release, signing_key, epoch_source, backend, reservation_signer=None,
                 dispatch_proof_verifier=None, statement_signer=None, boot_clock=None, challenge_signer=None):
        if type(config) is not SupervisorRouteConfig or config.enabled is not True:
            raise SupervisorRouteDisabled('supervisor route is disabled')
        _require(type(release) is CurrentExecutorReleasePins and isinstance(signing_key, Ed25519PrivateKey)
                 and signing_key.public_key().public_bytes(serialization.Encoding.Raw,
                     serialization.PublicFormat.Raw) == release.public_key
                 and callable(epoch_source) and callable(getattr(backend, 'forward', None))
                 and type(config.forward_timeout_ms) is int and 100 <= config.forward_timeout_ms <= 120000
                 and type(config.busy_wait_ms) is int and 0 <= config.busy_wait_ms <= 50
                 and type(config.journal_path) is str
                 and (reservation_signer is None or _signer_ready(reservation_signer))
                 and (dispatch_proof_verifier is None or type(dispatch_proof_verifier) is HubDispatchProofVerifier)
                 and (statement_signer is None or _signer_ready(statement_signer))
                 and (challenge_signer is None or _signer_ready(challenge_signer))
                 and (boot_clock is None or callable(boot_clock))
                 and type(config.proof_window_ms) is int and 1000 <= config.proof_window_ms <= 30000
                 and type(config.max_outstanding_challenges) is int
                 and 1 <= config.max_outstanding_challenges <= 64,
                 SupervisorRouteError('invalid supervisor route'))
        self._pid = os.getpid()
        # Default refuses: reserve()/dispatch() exist only with an explicit signer.
        self._reserver = ReservationSigner() if reservation_signer is None else reservation_signer
        # Defaults refuse: no proof verifier -> no challenge and no dispatch;
        # no statement signer -> no expired statement.
        self._proof_verifier = dispatch_proof_verifier or HubDispatchProofVerifier()
        self._statement_signer = ChallengeStatementSigner() if statement_signer is None else statement_signer
        self._challenge_signer = DispatchChallengeSigner() if challenge_signer is None else challenge_signer
        self._clock = _system_boot_clock if boot_clock is None else boot_clock
        self._window_ns = config.proof_window_ms * 1_000_000
        self._window_ms = config.proof_window_ms
        self._challenge_cap = config.max_outstanding_challenges
        self._mutex = threading.Lock()
        self._state = threading.Lock()  # Guards _active/_closed for busy reporting only.
        self._active = None  # The admitted RouteCommand currently owning the mutex.
        self._closed = False
        self._busy_wait = config.busy_wait_ms / 1000
        self._release, self._signer = release, signing_key
        self._epoch_source, self._backend = epoch_source, backend
        self._timeout = config.forward_timeout_ms
        self._inflight = set()
        self._journal = RouteJournal(config.journal_path, create=config.create_journal,
                                     allow_ephemeral_test_storage=config.allow_ephemeral_test_storage)
        try:
            # Proof mode is recorded durably and can never be switched off for
            # this journal: a later construction without a verifier refuses.
            self._proof_mode = self._journal.enter_mode(proof_required=self._proof_verifier.configured)
        except BaseException:
            self._journal.close()
            raise

    def __repr__(self):
        return 'SupervisorRoute(<enabled>)'

    def _current(self):
        epoch, pin = self._epoch_source()
        _require(type(epoch) is RouteEpoch and type(pin) is BackendPin, SupervisorRouteError('epoch'))
        return epoch, pin

    def _from_row(self, row, *, replayed):
        command_id, digest, _, state, owner_epoch, error, record, response = row
        if state in _RECORDED:
            return RouteResult(state, command_id, digest, replayed, None, record.encode(), response)
        if state == 'refused':
            return RouteResult('refused', command_id, digest, replayed, error)
        if state == 'intent' and owner_epoch == self._journal.owner_epoch and command_id in self._inflight:
            return RouteResult('in_progress', command_id, digest, replayed)
        # An intent left by an earlier owner (restart) or whose outcome this
        # process could not record is uncertain: held, never redispatched.
        return RouteResult('held', command_id, digest, replayed, error or 'interrupted')

    def submit(self, command):
        _require(os.getpid() == self._pid, SupervisorRouteError('process'))
        _require(type(command) is RouteCommand)
        # Contract check on the exact bytes BEFORE any journal access.
        request = _submitted_request(command)
        if self._proof_mode:
            # Proof mode: the only send path is dispatch(receipt, command, proof).
            # admit() re-checks the durable mode inside its transaction.
            raise SupervisorRouteRejected('proof_required')
        acquired = (self._mutex.acquire(timeout=self._busy_wait) if self._busy_wait
                    else self._mutex.acquire(blocking=False))
        if not acquired:
            with self._state:
                active = self._active
            if active is not None and active.command_id == command.command_id:
                if active == command:
                    return RouteResult('in_progress', command.command_id, None, True)
                raise SupervisorRouteRejected('binding_conflict')
            return RouteResult('busy', command.command_id, None, False)
        try:
            with self._state:
                _require(not self._closed, SupervisorRouteError('supervisor route closed'))
                self._active = command
            return self._admitted(command, request)
        finally:
            with self._state:
                self._active = None
            self._mutex.release()

    def _admitted(self, command, request):
        """Runs holding the mutex; returns only at a recorded (or held) state."""
        try:
            epoch, pin = self._current()
        except Exception:
            raise SupervisorRouteRefused() from None
        if command.epoch != epoch:
            # Never journaled, never forwarded. Older counters are also
            # rejected by the durable high-water check inside admit().
            raise SupervisorRouteRejected('stale_epoch' if command.epoch.launch_counter < epoch.launch_counter
                                          else 'epoch_mismatch')
        binding = binding_for(command, pin)
        digest = binding_digest(binding)
        attempt = secrets.token_hex(16)
        try:
            outcome, row = self._journal.admit(command.command_id, digest, binding, epoch, attempt)
        except SupervisorRouteRejected:
            raise
        except BaseException as error:
            # The intent may have committed even though admit raised (e.g. a
            # parent fsync failure or an interrupt right after COMMIT). Nothing
            # was forwarded; best effort turn ONLY the intent THIS attempt
            # inserted into unknown before the mutex is released (compare-and-
            # set on the attempt nonce). A replayed pre-existing intent is never
            # touched and stays held; if this fails, our 'intent' reads held.
            phase = getattr(self._journal, 'last_phase', None)
            try:
                self._journal.abandon_attempt(command.command_id, digest, attempt)
            except BaseException:
                pass
            if not isinstance(error, Exception):
                raise
            # 'refused' only when proved: no COMMIT was attempted by this call
            # AND a read confirms no row exists under this command_id at all.
            if phase in ('before_begin', 'in_transaction'):
                try:
                    if self._journal.read(command.command_id) is None:
                        raise SupervisorRouteRefused()
                except SupervisorRouteRefused:
                    raise SupervisorRouteRefused() from None
                except Exception:
                    pass
            # Possibly committed, or a prior row (maybe already forwarded)
            # exists: the original command_id's outcome must be inspected.
            return RouteResult('held', command.command_id, digest, False, 'admission_uncertain')
        if outcome == 'replay':
            return self._from_row(row, replayed=True)
        return self._send(command, request, binding, digest, epoch, pin, row, legacy=True)

    def _send(self, command, request, binding, digest, epoch, pin, counter, *, legacy):
        """Durable intent AND its consumed dispatch counter exist: ownership is
        non-abandonable and the counter is never reused."""
        self._inflight.add(command.command_id)
        try:
            return self._forward(command, request, binding, digest, epoch, pin, counter, legacy)
        except BaseException:
            # Cancellation/interrupt in this thread: record unknown before
            # the mutex is released, then re-raise. A no-op if an outcome
            # was already recorded; if this write fails, 'intent' = held.
            try:
                self._journal.mark_unknown(command.command_id, digest, 'interrupted')
            except BaseException:
                pass
            raise
        finally:
            self._inflight.discard(command.command_id)

    def _hold(self, command, digest, error):
        try:
            self._journal.mark_unknown(command.command_id, digest, error)
        except Exception:
            # The committed 'intent' row stays authoritative and reads as held.
            pass
        return RouteResult('held', command.command_id, digest, False, error)

    def _forward(self, command, request, binding, digest, epoch, pin, counter, legacy):
        extra = {}
        if getattr(self._backend, 'carries_dispatch', False) is True:
            extra['dispatch'] = dispatch_header(dispatch_claim(binding, counter))
        guard = None
        if legacy:
            # One-step path: re-check, in a journal read transaction held for
            # the whole exchange, that proof mode was not entered after admit.
            guard = self._journal.legacy_send_guard(command.command_id, digest)
            try:
                guard.__enter__()
            except SupervisorRouteRejected as refused:
                return self._hold(command, digest, refused.reason)  # Zero bytes; intent reads held.
            except Exception:
                return self._hold(command, digest, 'send_guard_failed')
        try:
            try:
                reply = self._backend.forward(method=command.method, path=command.path, body=command.body,
                                              timeout_ms=self._timeout, pin=pin, **extra)
            finally:
                if guard is not None:
                    guard.__exit__(None, None, None)  # Released before ANY journal write below.
        except PeerMismatch:
            try:
                self._journal.mark_refused(command.command_id, digest, 'backend_identity_mismatch')
            except Exception:
                return RouteResult('held', command.command_id, digest, False, 'backend_identity_mismatch')
            return RouteResult('refused', command.command_id, digest, False, 'backend_identity_mismatch')
        except Exception:
            return self._hold(command, digest, 'transport_lost')
        try:
            after = self._current()
        except Exception:
            return self._hold(command, digest, 'epoch_changed')
        if after != (epoch, pin):
            return self._hold(command, digest, 'epoch_changed')
        if type(reply) is not BackendReply or type(reply.peer) is not ObservedPeer:
            return self._hold(command, digest, 'malformed_response')
        if not reply.peer.matches(pin):
            return self._hold(command, digest, 'backend_identity_mismatch')
        outcome, error = _classify(request, reply)
        if outcome is None:
            return self._hold(command, digest, error)
        try:
            payload = {'protocol': PROTOCOL, 'audience': AUDIENCE, 'issuer_id': self._release.issuer_id,
                       'binding': binding, 'binding_digest': digest,
                       'observation_nonce': observation_nonce(digest), 'outcome': outcome,
                       'backend_status': reply.status,
                       'response_sha256': hashlib.sha256(reply.body).hexdigest(),
                       'observed_peer': {'pid': reply.peer.pid, 'uid': reply.peer.uid,
                                         'gid': reply.peer.gid, 'start_ticks': reply.peer.start_ticks},
                       'recorded_at_ms': _now_ms()}
            signature = base64.urlsafe_b64encode(self._signer.sign(SIGNATURE_DOMAIN + _canonical(payload))
                                                 ).rstrip(b'=').decode()
            record = _canonical({'payload': payload, 'signature': signature})
            _require(len(record) <= MAX_RECORD_BYTES)
        except Exception:
            return self._hold(command, digest, 'signing_failed')
        state = 'completed' if outcome == 'accepted' else 'rejected'
        try:
            self._journal.record_outcome(command.command_id, digest, state, record, reply.body)
        except Exception:
            # Forwarded, but not durably recorded: NEVER report success.
            return self._hold(command, digest, 'outcome_unrecorded')
        return RouteResult(state, command.command_id, digest, False, None, record, reply.body)

    def _exclusive(self, command):
        """Route mutex for reserve/dispatch/abandon, same busy bound as submit."""
        acquired = (self._mutex.acquire(timeout=self._busy_wait) if self._busy_wait
                    else self._mutex.acquire(blocking=False))
        if not acquired:
            return False
        try:
            with self._state:
                _require(not self._closed, SupervisorRouteError('supervisor route closed'))
                self._active = command
        except BaseException:
            self._mutex.release()
            raise
        return True

    def _release_exclusive(self):
        with self._state:
            self._active = None
        self._mutex.release()

    def _two_phase_ready(self, command):
        _require(os.getpid() == self._pid, SupervisorRouteError('process'))
        _require(type(command) is RouteCommand)
        request = _submitted_request(command)
        _require(_signer_ready(self._reserver), SupervisorRouteError('reservation signer not configured'))
        return request

    def reserve(self, command):
        """PHASE 1 of the two-phase path. SENDS NOTHING to the executor.

        Durably allocates the next per-executor dispatch counter for exactly
        this command (method, path, body SHA-256, command_id, fence, launch
        epoch, current backend pin) and records it 'reserved' with a receipt
        signed by the configured reservation signer. An identical repeat
        returns the SAME receipt bytes (``replayed``) only while the row is
        still ``reserved``; once dispatched or abandoned it is Rejected
        (``reservation_dispatched`` / ``reservation_abandoned``). A different
        binding under the command_id, or one already sent one-step, is
        Rejected. A receipt proves a past allocation, not current state.
        Raises SupervisorRouteBusy, SupervisorRouteRefused (epoch source
        failed, nothing journaled) or ReservationUncertain (repeat reserve).
        """
        self._two_phase_ready(command)
        if not self._exclusive(command):
            raise SupervisorRouteBusy()
        try:
            try:
                epoch, pin = self._current()
            except Exception:
                raise SupervisorRouteRefused() from None
            if command.epoch != epoch:
                raise SupervisorRouteRejected('stale_epoch' if command.epoch.launch_counter < epoch.launch_counter
                                              else 'epoch_mismatch')
            binding = binding_for(command, pin)
            signer, reserved_at = self._reserver, _now_ms()

            def sign(counter):
                payload = {'protocol': RESERVE_PROTOCOL, 'audience': RESERVE_AUDIENCE, 'key_id': signer.key_id,
                           **_reservation_fields(binding), 'counter': counter, 'reserved_at_ms': reserved_at}
                message = RESERVE_DOMAIN + _canonical(payload)
                signature = signer.sign(message)
                _require(type(signature) is bytes and len(signature) == 64)
                Ed25519PublicKey.from_public_bytes(signer.public_key).verify(signature, message)
                receipt = _canonical({'payload': payload, 'signature': base64.urlsafe_b64encode(signature)
                                      .rstrip(b'=').decode()})
                _require(len(receipt) <= MAX_RECORD_BYTES)
                return receipt
            try:
                outcome, receipt = self._journal.reserve(command.command_id, binding_digest(binding), binding,
                                                         epoch, sign)
            except SupervisorRouteRejected:
                raise
            except Exception:
                raise ReservationUncertain() from None
            return RouteReservation(command.command_id, json.loads(receipt)['payload']['counter'], receipt,
                                    outcome == 'replay', 'reserved')
        finally:
            self._release_exclusive()

    def dispatch(self, receipt, command, proof):
        """PHASE 2: explicit one-shot send of a reserved command.

        Sends only if the receipt verifies under this route's reservation key
        and binds exactly ``command`` with the current epoch and pin, AND the
        matching 'reserved' journal row exists with these exact receipt bytes,
        AND the Hub's one-use claim ``proof`` verifies under the pinned Hub key
        and, in the same transaction, binds this receipt/reservation/command
        fence/current boot and an outstanding challenge nonce issued for this
        reservation within ``proof_window_ms`` on the supervisor's own
        CLOCK_BOOTTIME; that nonce is consumed there. Without a configured
        HubDispatchProofVerifier this refuses: there is no proof-less dispatch.
        The row is marked 'dispatched' (consume-once) and the intent recorded
        in ONE transaction BEFORE any byte is sent; the send then uses the
        reserved counter and is classified exactly like ``submit``. A
        reservation whose counter is not above every counter already sent to
        that executor (either path) is ``held``/``reservation_superseded``
        with zero bytes and nothing marked. Every
        failure, mismatch, missing row, repeat or uncertain journal outcome
        is reported ``held`` (or ``busy``, nothing touched): never retried and
        never re-sent under a new counter. Use ``inspect`` for the outcome.
        """
        request = self._two_phase_ready(command)
        _require(type(receipt) is bytes and type(proof) is bytes)
        _require(self._proof_verifier.configured, SupervisorRouteError('hub dispatch proof verifier not configured'))
        if not self._exclusive(command):
            return RouteResult('busy', command.command_id, None, False)
        try:
            return self._dispatch(receipt, command, request, proof)
        finally:
            self._release_exclusive()

    def _dispatch(self, receipt, command, request, proof):
        try:
            epoch, pin = self._current()
        except Exception:
            return RouteResult('held', command.command_id, None, False, 'epoch_unavailable')
        binding = binding_for(command, pin)
        digest = binding_digest(binding)
        if command.epoch != epoch:
            return RouteResult('held', command.command_id, digest, False, 'epoch_changed')
        try:
            payload = _verify_reservation(receipt, public_key=self._reserver.public_key,
                                          key_id=self._reserver.key_id, expected=_reservation_fields(binding))
        except Exception:
            return RouteResult('held', command.command_id, digest, False, 'receipt_invalid')
        try:
            claim = self._proof_verifier.verify(proof)
        except Exception:
            return RouteResult('held', command.command_id, digest, False, 'proof_invalid')
        attempt = secrets.token_hex(16)
        try:
            self._journal.dispatch_reserved(command.command_id, digest, binding, receipt, payload['counter'],
                                            epoch, attempt, proof=claim,
                                            proof_sha256=hashlib.sha256(proof).hexdigest(), clock=self._clock,
                                            window_ns=self._window_ns, run_id=request.run_id)
        except SupervisorRouteRejected as rejected:
            return RouteResult('held', command.command_id, digest, False, rejected.reason)
        except BaseException as error:
            # The dispatched mark + intent may have committed: only the intent
            # THIS attempt inserted is turned unknown; it is never re-sent.
            try:
                self._journal.abandon_attempt(command.command_id, digest, attempt)
            except BaseException:
                pass
            if not isinstance(error, Exception):
                raise
            return RouteResult('held', command.command_id, digest, False, 'admission_uncertain')
        return self._send(command, request, binding, digest, epoch, pin, payload['counter'], legacy=False)

    def _journal_op(self):
        _require(os.getpid() == self._pid, SupervisorRouteError('process'))
        with self._state:
            _require(not self._closed, SupervisorRouteError('supervisor route closed'))

    def issue_dispatch_challenge(self, command_id):
        """Records a fresh one-use challenge for a 'reserved', not superseded
        reservation and returns the SIGNED challenge envelope (canonical
        {payload, signature} bytes under CHALLENGE_DOMAIN; see
        DispatchChallengeVerifier). Sends nothing to the executor.

        Order: (1) the outstanding row commits durably; (2) only then is the
        payload signed; (3) the SHA-256 of the exact signed bytes is bound to
        the row in a second durable transaction; (4) only then are the bytes
        returned. A failure at (1) signs nothing; a failure at (2)/(3)
        returns nothing, and the unbound row can never be consumed (it
        simply expires). At most ``max_outstanding_challenges`` live
        challenges per executor (SupervisorRouteBusy above). Refuses without
        a configured proof verifier or challenge signer. Atomicity comes from
        the journal transactions, so this does not wait for an in-flight send.
        """
        self._journal_op()
        _require(_uuid(command_id))
        _require(self._proof_verifier.configured, SupervisorRouteError('hub dispatch proof verifier not configured'))
        signer = self._challenge_signer
        _require(_signer_ready(signer), SupervisorRouteError('dispatch challenge signer not configured'))
        nonce = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode()
        fields = self._journal.issue_challenge(command_id, nonce, self._clock, self._window_ns, self._challenge_cap)
        payload = {'protocol': CHALLENGE_PROTOCOL, 'audience': CHALLENGE_AUDIENCE, 'key_id': signer.key_id,
                   'nonce': nonce, 'command_id': command_id, 'counter': fields['counter'],
                   'reservation_receipt_sha256': fields['reservation_receipt_sha256'], 'executor': fields['executor'],
                   'pin': fields['pin'], 'epoch': fields['epoch'], 'supervisor_boot_id': fields['boot_id'],
                   'proof_window_ms': self._window_ms}
        message = CHALLENGE_DOMAIN + _canonical(payload)
        signature = signer.sign(message)
        _require(type(signature) is bytes and len(signature) == 64, SupervisorRouteError('challenge signing'))
        Ed25519PublicKey.from_public_bytes(signer.public_key).verify(signature, message)
        raw = _canonical({'payload': payload, 'signature': base64.urlsafe_b64encode(signature).rstrip(b'=').decode()})
        self._journal.bind_challenge(nonce, hashlib.sha256(raw).hexdigest())
        return raw

    def expire_challenges(self):
        """Marks every outstanding challenge from another boot or older than the
        window expired; returns how many. A consumed one is never touched."""
        self._journal_op()
        return self._journal.expire_challenges(self._clock, self._window_ns)

    def expired_statement(self, nonce):
        """Signed {protocol, audience, key_id, nonce, command_id, counter,
        status: 'expired_unconsumed', reservation_status: 'retired', boot_id}.

        RESERVATION-WIDE: ONE journal transaction checks that no challenge for
        the nonce's reservation is consumed and the reservation is not
        dispatched, marks EVERY outstanding challenge for it expired and
        retires the reservation (abandoned; counter stays consumed), so any
        racing dispatch with any of its nonces is held. Only after that commit
        is durable is anything signed; if the transaction fails or its outcome
        is uncertain nothing is signed (repeat later: an expired nonce stays
        expired). Refuses without a configured statement signer."""
        self._journal_op()
        _require(_valid_nonce(nonce))
        signer = self._statement_signer
        _require(_signer_ready(signer), SupervisorRouteError('challenge statement signer not configured'))
        command_id, counter, boot_id = self._journal.revoke_unconsumed(nonce)
        payload = {'protocol': EXPIRED_PROTOCOL, 'audience': EXPIRED_AUDIENCE, 'key_id': signer.key_id,
                   'nonce': nonce, 'command_id': command_id, 'counter': counter, 'status': 'expired_unconsumed',
                   'reservation_status': 'retired', 'boot_id': boot_id}
        message = EXPIRED_DOMAIN + _canonical(payload)
        signature = signer.sign(message)
        _require(type(signature) is bytes and len(signature) == 64, SupervisorRouteError('statement signing'))
        Ed25519PublicKey.from_public_bytes(signer.public_key).verify(signature, message)
        return _canonical({'payload': payload,
                           'signature': base64.urlsafe_b64encode(signature).rstrip(b'=').decode()})

    def inspect_challenge(self, nonce):
        """Read-only: (state, command_id, counter, boot_id) or None."""
        self._journal_op()
        _require(_valid_nonce(nonce))
        return self._journal.read_challenge(nonce)

    def abandon_reservation(self, command_id):
        """reserved -> abandoned, never sent; the counter stays consumed. True
        if this call abandoned it; False if missing, dispatched or abandoned."""
        _require(os.getpid() == self._pid, SupervisorRouteError('process'))
        _require(_uuid(command_id))
        if not self._mutex.acquire(blocking=False):
            raise SupervisorRouteBusy()
        try:
            with self._state:
                _require(not self._closed, SupervisorRouteError('supervisor route closed'))
            return self._journal.abandon_reservation(command_id)
        finally:
            self._mutex.release()

    def inspect_reservation(self, command_id):
        """Read-only: (state, counter, receipt) or None; never sends or writes."""
        _require(os.getpid() == self._pid, SupervisorRouteError('process'))
        _require(_uuid(command_id))
        return self._journal.read_reservation(command_id)

    def inspect(self, command_id):
        """Read-only recorded state; never forwards, never writes."""
        _require(os.getpid() == self._pid, SupervisorRouteError('process'))
        _require(_uuid(command_id))
        try:
            row = self._journal.read(command_id)
        except Exception:
            raise SupervisorRouteRefused() from None
        return None if row is None else self._from_row(row, replayed=True)

    def close(self):
        """Refuses (SupervisorRouteInFlight) while an operation is in flight.

        A forked child (or any process other than the constructor's) is
        refused BEFORE touching the inherited mutex or journal.
        """
        if os.getpid() != self._pid:
            raise SupervisorRouteError('supervisor route is bound to its constructing process')
        if not self._mutex.acquire(blocking=False):
            with self._state:
                active = self._active
            raise SupervisorRouteInFlight(None if active is None else active.command_id)
        try:
            with self._state:
                self._closed = True
            self._signer = None
            self._journal.close()
        finally:
            self._mutex.release()


# --- Original-command inspection (Hub -> supervisor, read-only) -------------

INSPECTION_REQUEST_PROTOCOL = 'engineering-hub.supervisor-route-inspection-request.v1'
INSPECTION_ANSWER_PROTOCOL = 'engineering-hub.supervisor-route-inspection-answer.v1'
INSPECTION_AUDIENCE = 'engineering-hub.hub-supervisor-route-inspection'
INSPECTION_SCOPE = 'supervisor-route.inspect'
# Distinct from the route-record, launch-receipt and observation domains.
INSPECTION_SIGNATURE_DOMAIN = b'engineering-hub-supervisor-route-inspection-v1\x00'
INSPECTION_PATH = '/v1/supervisor-route/inspect'
# Scope marker of the inspection credential. ``_bearer`` refuses it as a
# launch bearer and ``_inspection_bearer`` requires it, so neither credential
# can stand in for the other.
INSPECTION_TOKEN_PREFIX = 'ehri1.'
MAX_INSPECTION_REQUEST_BYTES = 8192
INSPECTION_STATUSES = frozenset({'launched', 'refused', 'not_dispatched', 'not_found', 'unknown'})
_RESERVATION_INFO_KEYS = frozenset({'reservation_receipt_sha256', 'reservation_state', 'challenge_sha256',
                                    'proof_sha256'})
# Terminal inspection status -> route journal states it may stand for.
_TERMINAL = {'launched': frozenset({'completed'}), 'refused': frozenset({'rejected', 'refused'})}
_INSPECTION_REQUEST_KEYS = frozenset({'protocol', 'audience', 'scope', 'binding', 'binding_digest', 'dispatch',
                                      'key_id', 'request_nonce'})
# The answer shape is versioned by its exact key set: the required
# ``reservation`` key (added with two-phase reserve/dispatch) makes earlier
# signed answers INTENTIONALLY unverifiable; there is no compatibility mode.
_INSPECTION_ANSWER_KEYS = frozenset({'protocol', 'audience', 'key_id', 'request', 'request_sha256', 'status',
                                     'route_state', 'reservation', 'answered_at_ms'})
_REQUEST_NONCE = re.compile(r'[0-9a-f]{32}\Z')
# PUBLIC CONTRACT: every ``InspectionResolution.error`` of a ``held`` result is
# exactly one of these. None of them permits resubmitting, redispatching or
# releasing anything.
#   no_held_attempt        no held row with this exact binding (nothing sent)
#   peer_mismatch          supervisor peer differs from its pin (zero bytes sent)
#   transport_lost         connect/send/timeout/strict-framing failure
#   http_status            supervisor answered other than 200 (unsigned denial)
#   unverified             answer failed signature/key/shape/binding verification
#   not_found              signed: neither reservation, intent nor challenge row
#   not_dispatched         signed: reservation reserved/retired and provably unconsumed
#   unknown                signed: anything else (incl. inconsistent rows)
#   observation_unrecorded the verified observation could not be persisted
#   replay_unverified      a persisted observation failed re-verification
#   hub_unavailable        the held row has no usable hold version
#   hub_not_recorded       the Hub's own transaction did not commit the outcome
INSPECTION_HELD_REASONS = frozenset({
    'no_held_attempt', 'peer_mismatch', 'transport_lost', 'http_status', 'unverified', 'not_found',
    'not_dispatched', 'unknown', 'observation_unrecorded', 'replay_unverified', 'hub_unavailable',
    'hub_not_recorded'})
_INSPECTION_ERRORS = INSPECTION_HELD_REASONS


def inspection_key_id(public_key):
    """Key id of a raw Ed25519 public key: SHA-256 hex of its 32 raw bytes."""
    _require(type(public_key) is bytes and len(public_key) == 32, SupervisorRouteError('inspection key'))
    return hashlib.sha256(public_key).hexdigest()


def _inspection_bearer(value):
    _require(type(value) is str and value.startswith(INSPECTION_TOKEN_PREFIX)
             and 32 <= len(value) - len(INSPECTION_TOKEN_PREFIX) and len(value) <= 4096
             and all(33 <= ord(c) <= 126 for c in value),
             SupervisorRouteError('inspection credential required'))
    return value


def inspection_request(command, pin, dispatch_counter, *, key_id, request_nonce):
    """Canonical inspection request: the full route binding (incl. the pinned
    backend), its digest, the exact ``dispatch_claim`` of the attempt
    (executor, counter, method, path, body SHA-256, command_id, fence, launch
    epoch; same fields and canonical JSON as the dispatch header), the
    expected supervisor key id and a fresh per-inspection nonce."""
    binding = binding_for(command, pin)
    _require(type(key_id) is str and _HEX.fullmatch(key_id)
             and type(request_nonce) is str and _REQUEST_NONCE.fullmatch(request_nonce))
    return {'protocol': INSPECTION_REQUEST_PROTOCOL, 'audience': INSPECTION_AUDIENCE, 'scope': INSPECTION_SCOPE,
            'binding': binding, 'binding_digest': binding_digest(binding),
            'dispatch': dispatch_claim(binding, dispatch_counter), 'key_id': key_id, 'request_nonce': request_nonce}


def _valid_inspection_request(request):
    return (_shape(request, _INSPECTION_REQUEST_KEYS) and request['protocol'] == INSPECTION_REQUEST_PROTOCOL
            and request['audience'] == INSPECTION_AUDIENCE and request['scope'] == INSPECTION_SCOPE
            and _valid_binding(request['binding'])
            and request['binding_digest'] == binding_digest(request['binding'])
            and _valid_claim(request['dispatch'])
            and _canonical(request['dispatch']) == _canonical(dispatch_claim(request['binding'],
                                                                             request['dispatch']['counter']))
            and type(request['key_id']) is str and _HEX.fullmatch(request['key_id']) is not None
            and type(request['request_nonce']) is str and _REQUEST_NONCE.fullmatch(request['request_nonce'])
            is not None)


def _valid_reservation_info(status, info):
    """Signed ``reservation`` field: None (one-step path / no reservation) or
    exactly {receipt hash, state, challenge hash, proof hash}, consistent
    with the status: not_found -> None; not_dispatched -> reserved/abandoned
    without challenge or proof; launched/refused -> None or dispatched WITH
    both hashes; unknown -> None or any well-formed value."""
    if info is None:
        return status != 'not_dispatched'

    def digest(value, optional):
        return (optional and value is None) or (type(value) is str and _HEX.fullmatch(value) is not None)
    if not (_shape(info, _RESERVATION_INFO_KEYS) and digest(info['reservation_receipt_sha256'], False)
            and info['reservation_state'] in ('reserved', 'dispatched', 'abandoned')
            and digest(info['challenge_sha256'], True) and digest(info['proof_sha256'], True)
            and (info['challenge_sha256'] is None) == (info['proof_sha256'] is None)):
        return False
    if status == 'not_found':
        return False
    if status == 'not_dispatched':
        return info['reservation_state'] in ('reserved', 'abandoned') and info['challenge_sha256'] is None
    if status in _TERMINAL:
        return info['reservation_state'] == 'dispatched' and info['challenge_sha256'] is not None
    return True


def _read_inspection_row(journal, command_id, budget_ms):
    """Read-only snapshot ``(intent, reservation, challenges)`` for ONE
    command_id from the route journal under ONE time budget, in ONE read
    transaction (so the three agree); never writes or forwards.

    ``intent`` = (binding_digest, binding, state, dispatch_counter) or None;
    ``reservation`` = (executor_id, binding_digest, binding, counter, state,
    receipt) or None; ``challenges`` = [(state, challenge_sha256,
    proof_sha256)] for that command_id. The journal mutex is only TRIED for
    the budget, the private read-only connection's busy timeout is what
    remains of it, and a progress handler aborts any statement (schema/
    quick_check included) once it is spent, so a slow or locked journal makes
    this raise instead of pinning a worker. A journal not in rollback-journal
    mode is refused, as everywhere else in this module: checked before the
    transaction AND re-checked after the snapshot reads while its SHARED lock
    is still held, so a switch to WAL by a flock-ignoring writer before the
    snapshot is caught (-> unsigned 503, nothing signed).
    """
    _require(type(journal) is RouteJournal and os.getpid() == journal._pid
             and type(budget_ms) is int and budget_ms > 0, RouteJournalError('process'))
    deadline = time.monotonic() + budget_ms / 1000
    if not journal._mutex.acquire(timeout=budget_ms / 1000):
        raise RouteJournalError('route journal busy')
    try:
        journal._active()
        remaining = deadline - time.monotonic()
        _require(remaining > 0, RouteJournalError('route journal busy'))
        db = sqlite3.connect(journal.path.as_uri() + '?mode=ro', uri=True, timeout=remaining,
                             isolation_level=None)
        try:
            db.execute(f'PRAGMA busy_timeout={max(1, int(remaining * 1000))}')
            db.execute('PRAGMA trusted_schema=OFF')
            db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
            _require(db.execute('PRAGMA journal_mode').fetchone() == ('delete',), RouteJournalError('mode'))
            db.execute('BEGIN')
            try:
                journal._schema(db)
                intent = None
                if journal._row(db, command_id) is not None:
                    intent = db.execute('SELECT binding_digest,binding,state,dispatch_counter '
                                        'FROM supervisor_route_intent WHERE command_id=?', (command_id,)).fetchone()
                    _require(intent is not None and _positive(intent[3], MAX_DISPATCH_COUNTER),
                             RouteJournalError('row'))
                reservation = journal._reservation(db, command_id)
                if reservation is not None:
                    reservation = (reservation[1], reservation[2], reservation[3], reservation[4], reservation[5],
                                   reservation[6])
                challenges = db.execute('SELECT state,challenge_sha256,proof_sha256 FROM supervisor_route_challenge '
                                        'WHERE command_id=? ORDER BY nonce', (command_id,)).fetchall()
                _require(all(row[0] in ('outstanding', 'consumed', 'expired') for row in challenges),
                         RouteJournalError('challenge'))
                # Re-checked AFTER the reads, while this transaction still holds
                # the SHARED lock (as legacy_send_guard does): a writer that
                # ignores the flock may have switched the file to WAL between
                # the first check and BEGIN's first read; in rollback-journal
                # mode no writer can switch it while the lock is held.
                _snapshot_mode(db)
            finally:
                db.execute('ROLLBACK')
            _require(time.monotonic() <= deadline, RouteJournalError('route journal busy'))
            return intent, reservation, challenges
        finally:
            db.close()
    except RouteJournalError:
        raise
    except Exception:
        raise RouteJournalError('route journal unavailable') from None
    finally:
        journal._mutex.release()


def _snapshot_mode(db):
    _require(db.execute('PRAGMA journal_mode').fetchone() == ('delete',), RouteJournalError('mode'))


def _inspection_status(request, snapshot):
    """(status, route_state, reservation) from ONE journal snapshot.

    One-step path (no reservation row): the intent's recorded outcome, else
    ``unknown``; no row at all -> ``not_found``; challenge rows without a
    reservation (orphans) -> ``unknown``. Two-phase path: the
    reservation must carry exactly the request's binding, executor and
    counter, else ``unknown``. ``dispatched`` -> the dispatched intent's
    outcome (launched/refused) with the ONE consumed challenge's hash and
    proof hash, else ``unknown``. ``reserved``/``abandoned`` (retired) ->
    ``not_dispatched`` ONLY when the snapshot proves no challenge was
    consumed and no intent exists; otherwise ``unknown``. Everything else is
    ``unknown``.
    """
    intent, reservation, challenges = snapshot
    binding, counter = _canonical(request['binding']).decode(), request['dispatch']['counter']

    def matches(row):
        return row[0] == request['binding_digest'] and row[1] == binding and row[3] == counter

    def outcome(row):
        if row[2] == 'completed':
            return 'launched', 'completed'
        if row[2] in ('rejected', 'refused'):
            return 'refused', row[2]
        return 'unknown', None
    if reservation is None:
        if challenges:
            # Challenge rows with no reservation are an inconsistent snapshot.
            return 'unknown', None, None
        if intent is None:
            # Absence is NOT proof of no effect (restored or replaced journal).
            return 'not_found', None, None
        return (*outcome(intent), None) if matches(intent) else ('unknown', None, None)
    executor, digest, reserved_binding, reserved_counter, state, receipt = reservation
    if executor != request['dispatch']['executor_id'] or not matches((digest, reserved_binding, None,
                                                                       reserved_counter)):
        return 'unknown', None, None
    info = {'reservation_receipt_sha256': hashlib.sha256(receipt.encode()).hexdigest(),
            'reservation_state': state, 'challenge_sha256': None, 'proof_sha256': None}
    consumed = [row for row in challenges if row[0] == 'consumed']
    if state in ('reserved', 'abandoned'):
        if consumed or intent is not None:
            return 'unknown', None, info
        return 'not_dispatched', None, info
    if state != 'dispatched' or len(consumed) != 1 or intent is None or not matches(intent):
        return 'unknown', None, info
    info['challenge_sha256'], info['proof_sha256'] = consumed[0][1], consumed[0][2]
    status, route_state = outcome(intent)
    return status, route_state, info


class InspectionAuthenticator:
    """DEFAULT-REFUSING caller authenticator for the inspection responder.

    The base class authenticates nobody. A subclass returns exactly
    ``INSPECTION_SCOPE`` for an authorized inspection caller, else raises.
    """

    def authenticate(self, authorization):
        raise SupervisorRouteError('inspection caller not authenticated')


class InspectionTokenAuthenticator(InspectionAuthenticator):
    """Accepts only the one enrolled inspection-scoped bearer (constant-time).

    Only a SHA-256 of the token is kept. A launch/executor bearer cannot be
    enrolled (no scope prefix) and cannot authenticate here.
    """

    def __init__(self, token):
        self.__digest = hashlib.sha256(_inspection_bearer(token).encode('ascii')).digest()

    def __repr__(self):
        return 'InspectionTokenAuthenticator(<redacted>)'

    def __reduce__(self):
        raise TypeError('InspectionTokenAuthenticator is not serializable')

    def authenticate(self, authorization):
        denied = SupervisorRouteError('inspection caller not authenticated')
        scheme, _, token = authorization.partition(' ') if type(authorization) is str else ('', '', '')
        try:
            _inspection_bearer(token)
        except SupervisorRouteError:
            raise denied from None
        if scheme != 'Bearer' or not hmac.compare_digest(hashlib.sha256(token.encode('ascii')).digest(),
                                                         self.__digest):
            raise denied
        return INSPECTION_SCOPE


class InspectionVerifier:
    """DEFAULT-REFUSING Hub-side verifier of signed inspection answers.

    The base class verifies nothing: every answer is refused, so every
    inspection through it stays held. ``verify(raw, request=)`` returns the
    verified answer payload or raises.
    """
    key_id = None

    def verify(self, raw, *, request):
        raise SupervisorRouteError('inspection verifier not configured')


class PinnedKeyInspectionVerifier(InspectionVerifier):
    """Verifies against ONE independently pinned supervisor Ed25519 public key.

    Enforces the exact envelope shape and canonical bytes, the signature under
    the inspection domain, the pinned key id (equal to the request's), an
    exact echo of the Hub's own request (every binding field, the dispatch
    claim, key id, nonce), and the status/route_state pairing.
    """

    def __init__(self, public_key):
        self.key_id = inspection_key_id(public_key)
        self._key = Ed25519PublicKey.from_public_bytes(public_key)

    def verify(self, raw, *, request):
        error = SupervisorRouteError('supervisor inspection answer unverified')
        try:
            _require(_valid_inspection_request(request) and request['key_id'] == self.key_id, error)
            envelope = _strict_json(raw, MAX_RESPONSE_BYTES)
            _require(_canonical(envelope) == raw and _shape(envelope, {'payload', 'signature'}), error)
            payload, signature = envelope['payload'], envelope['signature']
            _require(_shape(payload, _INSPECTION_ANSWER_KEYS) and type(signature) is str
                     and _SIGNATURE.fullmatch(signature), error)
            decoded = base64.urlsafe_b64decode(signature + '==')
            _require(len(decoded) == 64 and base64.urlsafe_b64encode(decoded).rstrip(b'=').decode() == signature,
                     error)
            self._key.verify(decoded, INSPECTION_SIGNATURE_DOMAIN + _canonical(payload))
            status, route_state = payload['status'], payload['route_state']
            _require(payload['protocol'] == INSPECTION_ANSWER_PROTOCOL
                     and payload['audience'] == INSPECTION_AUDIENCE
                     and type(payload['key_id']) is str and payload['key_id'] == self.key_id
                     and _canonical(payload['request']) == _canonical(request)
                     and payload['request_sha256'] == hashlib.sha256(_canonical(request)).hexdigest()
                     and type(status) is str and status in INSPECTION_STATUSES
                     and (route_state in _TERMINAL[status] if status in _TERMINAL else route_state is None)
                     and _valid_reservation_info(status, payload['reservation'])
                     and _positive(payload['answered_at_ms']), error)
            return payload
        except SupervisorRouteError:
            raise
        except Exception:
            raise error from None


@dataclass(frozen=True)
class SupervisorPeerPin:
    """Independently enrolled identity of the supervisor's inspection listener."""
    pid: int
    uid: int
    gid: int
    start_ticks: int

    def __post_init__(self):
        _require(_positive(self.pid) and _positive(self.start_ticks) and _id32(self.uid) and _id32(self.gid))


@dataclass(frozen=True)
class InspectionResponderConfig:
    """Explicit configuration. ``enabled`` defaults to False: no responder exists.

    ``max_authenticating`` (1..8) callers may be in the authenticator at once
    and ``max_concurrent`` (1..8) authenticated answers may run at once; any
    other caller gets an unsigned 503 ``busy`` immediately (never queued).
    ``answer_budget_ms`` (20..2000, at least ``read_budget_ms``) is the
    responder's own deadline per call, a return-time check on signed answers;
    ``read_budget_ms`` (10..1000) caps each durable journal read.
    Keep both well under the client's total deadline.
    """
    enabled: bool = False
    max_concurrent: int = 2
    read_budget_ms: int = 100
    max_authenticating: int = 2
    answer_budget_ms: int = 250


class SupervisorInspectionResponder:
    """DISABLED-BY-DEFAULT supervisor side: answers from the durable route journal.

    ``answer(method=, path=, authorization=, body=)`` -> (http_status, bytes).
    Authenticates the caller FIRST (inspection scope only), then parses the
    exact canonical request, then reads ONLY the route journal row for the
    command_id (read-only; never forwards, writes, relaunches or releases).
    Status (see ``_inspection_status``): ``launched`` / ``refused`` from the
    one-step intent's or the dispatched reservation's recorded outcome,
    ``not_dispatched`` for a reserved/abandoned reservation only when no
    challenge was consumed, ``not_found`` when there is neither a
    reservation nor an intent, ``unknown`` for everything else (intent or
    unknown rows, ANY difference from the request's binding, executor or
    claim counter, inconsistent challenge/reservation rows). Signed with the explicitly supplied supervisor key over the
    full request echo; denials (401/400/404/503) are unsigned.

    Bounds (independent of the client's deadline). CONCURRENCY is bounded:
    two separate non-blocking slot pools, ``max_authenticating`` around the
    authenticator callback and ``max_concurrent`` around the authenticated
    work, so a slow or flooded authenticator can never occupy the slots of
    already-authenticated answers (unauthenticated callers can still contend
    for the pre-auth pool and make new callers ``busy``; that is the chosen
    trade-off). TIME is best-effort, not a hard wall-clock bound: the journal
    read tries its mutex for at most the remaining budget and SQLite busy /
    progress timeouts stop contended statements, but filesystem calls,
    connection open/close and a custom authenticator are not interruptible.
    The responder's own deadline (``answer_budget_ms`` from entry) is a
    best-effort RETURN-TIME check: whatever the status, a signed answer is
    not returned after it; an unsigned 503 ``unavailable`` is returned
    instead (the signer may already have produced the bytes, which are
    dropped). Slot pools are PER INSTANCE: separate responder instances do
    not share limits. Detached work is still possible (a client that gave
    up is not noticed). No listener or HTTP server is provided and nothing
    constructs this.
    """

    def __init__(self, *, config, route, authenticator, signing_key):
        if type(config) is not InspectionResponderConfig or config.enabled is not True:
            raise SupervisorRouteDisabled('supervisor inspection responder is disabled')
        _require(type(route) is SupervisorRoute and isinstance(authenticator, InspectionAuthenticator)
                 and isinstance(signing_key, Ed25519PrivateKey)
                 and type(config.max_concurrent) is int and 1 <= config.max_concurrent <= 8
                 and type(config.read_budget_ms) is int and 10 <= config.read_budget_ms <= 1000
                 and type(config.max_authenticating) is int and 1 <= config.max_authenticating <= 8
                 and type(config.answer_budget_ms) is int
                 and max(20, config.read_budget_ms) <= config.answer_budget_ms <= 2000,
                 SupervisorRouteError('invalid supervisor inspection responder'))
        self._route, self._authenticator, self._signer = route, authenticator, signing_key
        self._auth_slots = threading.BoundedSemaphore(config.max_authenticating)
        self._slots = threading.BoundedSemaphore(config.max_concurrent)
        self._read_budget_ms = config.read_budget_ms
        self._answer_budget = config.answer_budget_ms / 1000
        self.key_id = inspection_key_id(signing_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def __repr__(self):
        return 'SupervisorInspectionResponder(<enabled>)'

    @staticmethod
    def _denied(status, code):
        return status, _canonical({'error': {'code': code}})

    def answer(self, *, method, path, authorization, body):
        deadline = time.monotonic() + self._answer_budget
        # Bounded concurrency: never queue; a saturated pool answers busy.
        if not self._auth_slots.acquire(blocking=False):
            return self._denied(503, 'busy')
        try:
            _require(self._authenticator.authenticate(authorization) == INSPECTION_SCOPE)
        except Exception:
            return self._denied(401, 'unauthorized')
        finally:
            self._auth_slots.release()
        if not self._slots.acquire(blocking=False):
            return self._denied(503, 'busy')
        try:
            status, raw = self._answer(method, path, body, deadline)
        finally:
            self._slots.release()
        if status == 200 and not time.monotonic() <= deadline:
            # Return-time check: a signed answer is not returned after the deadline.
            return self._denied(503, 'unavailable')
        return status, raw

    def _answer(self, method, path, body, deadline):
        if method != 'POST' or path != INSPECTION_PATH:
            return self._denied(404, 'not_found')
        try:
            request = _strict_json(body, MAX_INSPECTION_REQUEST_BYTES)
            _require(_canonical(request) == body and _valid_inspection_request(request))
        except Exception:
            return self._denied(400, 'invalid_payload')
        if request['key_id'] != self.key_id:
            return self._denied(400, 'wrong_key')
        budget_ms = min(self._read_budget_ms, int((deadline - time.monotonic()) * 1000))
        if budget_ms <= 0:
            return self._denied(503, 'unavailable')
        try:
            row = _read_inspection_row(self._route._journal, request['binding']['command_id'], budget_ms)
        except Exception:
            return self._denied(503, 'unavailable')
        try:
            status, route_state, reservation = _inspection_status(request, row)
        except Exception:
            return self._denied(503, 'unavailable')
        try:
            payload = {'protocol': INSPECTION_ANSWER_PROTOCOL, 'audience': INSPECTION_AUDIENCE,
                       'key_id': self.key_id, 'request': request,
                       'request_sha256': hashlib.sha256(_canonical(request)).hexdigest(),
                       'status': status, 'route_state': route_state, 'reservation': reservation,
                       'answered_at_ms': _now_ms()}
            signature = base64.urlsafe_b64encode(self._signer.sign(
                INSPECTION_SIGNATURE_DOMAIN + _canonical(payload))).rstrip(b'=').decode()
            raw = _canonical({'payload': payload, 'signature': signature})
            _require(len(raw) <= MAX_RESPONSE_BYTES)
        except Exception:
            return self._denied(503, 'unavailable')
        return 200, raw


@dataclass(frozen=True)
class InspectionClientConfig:
    """Explicit configuration. ``enabled`` defaults to False: no client exists."""
    enabled: bool = False
    socket_path: str | None = None
    timeout_ms: int = 2000


@dataclass(frozen=True)
class InspectionAnswer:
    """``verified`` is True only with a verifier-accepted payload; else ``error``."""
    verified: bool
    status: str | None = None
    route_state: str | None = None
    payload: dict | None = field(default=None, repr=False)
    raw: bytes | None = field(default=None, repr=False)
    error: str | None = None


def _check_supervisor_peer(sock, expected):
    """SO_PEERCRED pid/uid/gid + pidfd start ticks of the connected peer vs
    the pin. Returns (pid, pidfd, start); PeerMismatch before any byte is sent."""
    pid, uid, gid = struct.unpack('=iII', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if pid <= 0:
        raise PeerMismatch()
    pidfd = None
    try:
        before = _proc_start(pid)
        pidfd = os.pidfd_open(pid, 0)
        start = _proc_start(pid)
    except Exception:
        if pidfd is not None:
            os.close(pidfd)
        raise PeerMismatch() from None
    if before != start or (pid, uid, gid, start) != (expected.pid, expected.uid, expected.gid, expected.start_ticks):
        os.close(pidfd)
        raise PeerMismatch()
    return pid, pidfd, start


class SupervisorInspectionClient:
    """DISABLED-BY-DEFAULT Hub-side one-shot inspection over a Unix socket.

    Connects to ``socket_path``, verifies the supervisor peer against the
    enrolled ``SupervisorPeerPin`` BEFORE writing any byte (mismatch: zero
    bytes sent), sends one ``POST INSPECTION_PATH`` with the inspection-scoped
    bearer and the canonical request, reads the reply with the module's strict
    bounded framing (``_read_response``) under ONE total deadline, rechecks
    the peer, and hands the bytes to the ``InspectionVerifier``. Transport and
    verification failures are returned as an unverified ``InspectionAnswer``,
    never raised. No retries, reconnects or redirects.
    """

    def __init__(self, *, config, credential, supervisor_peer, verifier):
        if type(config) is not InspectionClientConfig or config.enabled is not True:
            raise SupervisorRouteDisabled('supervisor inspection client is disabled')
        _require(sys.platform == 'linux' and hasattr(socket, 'SO_PEERCRED') and hasattr(os, 'pidfd_open'),
                 SupervisorRouteError('linux'))
        _require(type(config.socket_path) is str and config.socket_path.startswith('/')
                 and os.path.normpath(config.socket_path) == config.socket_path
                 and type(config.timeout_ms) is int and 100 <= config.timeout_ms <= 120000
                 and type(supervisor_peer) is SupervisorPeerPin and isinstance(verifier, InspectionVerifier)
                 and type(verifier.key_id) is str and _HEX.fullmatch(verifier.key_id),
                 SupervisorRouteError('invalid supervisor inspection client'))
        self._path, self._timeout = config.socket_path, config.timeout_ms
        self._peer, self._verifier = supervisor_peer, verifier
        self.__authorization = 'Bearer ' + _inspection_bearer(credential)

    def __repr__(self):
        return 'SupervisorInspectionClient(<redacted>)'

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError('SupervisorInspectionClient is not serializable')

    def request_for(self, command, pin, dispatch_counter):
        return inspection_request(command, pin, dispatch_counter, key_id=self._verifier.key_id,
                                  request_nonce=secrets.token_hex(16))

    def inspect(self, request):
        _require(_valid_inspection_request(request))  # Before connecting.
        try:
            status, raw = self._exchange(_canonical(request))
        except PeerMismatch:
            return InspectionAnswer(False, error='peer_mismatch')
        except Exception:
            return InspectionAnswer(False, error='transport_lost')
        if status != 200:
            return InspectionAnswer(False, error='http_status')
        try:
            payload = self._verifier.verify(raw, request=request)
        except Exception:
            return InspectionAnswer(False, error='unverified')
        return InspectionAnswer(True, payload['status'], payload['route_state'], payload, raw)

    def _exchange(self, body):
        deadline = time.monotonic() + self._timeout / 1000

        def remaining():
            value = deadline - time.monotonic()
            if not value > 0:
                raise TimeoutError
            return value
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        pidfd = None
        try:
            peer.settimeout(remaining())
            peer.connect(self._path)
            pid, pidfd, start = _check_supervisor_peer(peer, self._peer)  # Nothing written yet.
            request = ('\r\n'.join([f'POST {INSPECTION_PATH} HTTP/1.1', 'Host: supervisor-inspection',
                                    'Accept-Encoding: identity', 'Connection: close',
                                    'Content-Type: application/json', f'Content-Length: {len(body)}',
                                    'Authorization: ' + self.__authorization]) + '\r\n\r\n').encode('ascii') + body
            peer.settimeout(remaining())
            peer.sendall(request)
            del request
            status, raw = _read_response(peer, remaining)
            _require(not select.select([pidfd], [], [], 0)[0] and _proc_start(pid) == start,
                     SupervisorRouteError('peer'))
            return status, raw
        except PeerMismatch:
            raise PeerMismatch() from None
        except Exception:
            # Sanitized: no header, token or supervisor text in the chain.
            raise SupervisorRouteError('inspection exchange unavailable') from None
        finally:
            if pidfd is not None:
                os.close(pidfd)
            peer.close()


_OBSERVED = {'launched': 'observed_launched', 'refused': 'observed_refused'}
_MAX_HOLD_VERSION = 256
_OBSERVATION = ('CREATE TABLE supervisor_route_inspection (command_id TEXT PRIMARY KEY, '
                'binding_digest TEXT NOT NULL, binding TEXT NOT NULL, executor_id TEXT NOT NULL, '
                "dispatch_counter INTEGER NOT NULL CHECK(typeof(dispatch_counter)='integer' "
                'AND dispatch_counter > 0), hold_version TEXT NOT NULL, '
                "state TEXT NOT NULL CHECK(state IN ('held','observed_launched','observed_refused')), "
                'route_state TEXT, key_id TEXT, answer TEXT, request TEXT, '
                "CHECK((state='held' AND route_state IS NULL AND key_id IS NULL AND answer IS NULL "
                'AND request IS NULL) '
                "OR (state='observed_launched' AND route_state='completed' AND key_id IS NOT NULL "
                'AND answer IS NOT NULL AND request IS NOT NULL) '
                "OR (state='observed_refused' AND route_state IN ('rejected','refused') AND key_id IS NOT NULL "
                'AND answer IS NOT NULL AND request IS NOT NULL)))')
_OBSERVATION_SCHEMA = {
    'supervisor_route_inspection': _OBSERVATION,
    'supervisor_route_inspection_insert_held': (
        'CREATE TRIGGER supervisor_route_inspection_insert_held BEFORE INSERT ON supervisor_route_inspection '
        "WHEN NEW.state IS NOT 'held' OR EXISTS (SELECT 1 FROM supervisor_route_inspection "
        'WHERE command_id = NEW.command_id) '
        "BEGIN SELECT RAISE(ABORT, 'inspection rows are inserted once, held'); END"),
    'supervisor_route_inspection_observe_once': (
        'CREATE TRIGGER supervisor_route_inspection_observe_once BEFORE UPDATE ON supervisor_route_inspection '
        "WHEN OLD.state IS NOT 'held' OR NEW.state NOT IN ('observed_launched','observed_refused') "
        'OR NEW.command_id IS NOT OLD.command_id OR NEW.binding_digest IS NOT OLD.binding_digest '
        'OR NEW.binding IS NOT OLD.binding OR NEW.executor_id IS NOT OLD.executor_id '
        'OR NEW.dispatch_counter IS NOT OLD.dispatch_counter OR NEW.hold_version IS NOT OLD.hold_version '
        "BEGIN SELECT RAISE(ABORT, 'only a held inspection row records one observation'); END"),
    'supervisor_route_inspection_no_delete': (
        'CREATE TRIGGER supervisor_route_inspection_no_delete BEFORE DELETE ON supervisor_route_inspection '
        "BEGIN SELECT RAISE(ABORT, 'inspection rows are permanent'); END"),
}


def _valid_hold_version(value):
    return (type(value) is str and 0 < len(value) <= _MAX_HOLD_VERSION
            and all(33 <= ord(c) <= 126 for c in value))


class InspectionLedger(_DurableStore):
    """Hub-side durable record of held attempts and the supervisor's signed
    OBSERVATIONS of them. It is NOT the Hub job store and never resolves,
    releases or authorizes anything.

    Same durability as the route journal (single owner, BEGIN IMMEDIATE,
    synchronous=FULL, parent fsync, exact schema incl. triggers). Triggers
    make rows insert-once as ``held`` (with the source's opaque hold version,
    if it gave one), movable once only from ``held`` to ``observed_launched``
    / ``observed_refused`` with every binding column and the hold version
    unchanged, and never deleted. An observed row means only "the supervisor
    signed this status for this exact binding at its signed answered_at_ms";
    it keeps the EXACT canonical request bytes and the EXACT signed answer
    bytes (payload + signature) with its key id, as evidence re-verified on
    every read and never trusted as stored. Like every local SQLite file it
    is UNANCHORED: it cannot prove it was not rolled back, restored or edited
    by a same-UID process.
    """
    _SCHEMA = _OBSERVATION_SCHEMA
    _VERSION = 1
    _COLUMNS = ('command_id,binding_digest,binding,executor_id,dispatch_counter,hold_version,state,route_state,'
                'key_id,answer,request')

    @staticmethod
    def _key(request):
        return (request['binding_digest'], _canonical(request['binding']).decode(),
                request['dispatch']['executor_id'], request['dispatch']['counter'])

    def _select(self, db, command_id):
        return db.execute(f'SELECT {self._COLUMNS} FROM supervisor_route_inspection WHERE command_id=?',
                          (command_id,)).fetchone()

    def hold(self, request, hold_version):
        """Record a held attempt with a VALID source hold version. An identical
        repeat (same binding, same version) is a no-op; the same binding with
        another version is refused (``hold_version_changed``) and a different
        binding is refused (``binding_conflict``), changing nothing."""
        _require(_valid_hold_version(hold_version), RouteJournalError('version'))
        command_id = request['binding']['command_id']
        with self._transaction() as db:
            row = self._select(db, command_id)
            if row is not None:
                if row[1:5] != self._key(request):
                    raise SupervisorRouteRejected('binding_conflict')
                if row[5] != hold_version:
                    raise SupervisorRouteRejected('hold_version_changed')
                return
            db.execute('INSERT INTO supervisor_route_inspection VALUES '
                       "(?,?,?,?,?,?,'held',NULL,NULL,NULL,NULL)", (command_id, *self._key(request), hold_version))

    def read(self, command_id):
        _require(os.getpid() == self._pid, RouteJournalError('process'))
        with self._mutex:
            try:
                self._active()
                with self._connection(readonly=True) as db:
                    self._schema(db)
                    return self._select(db, command_id)
            except RouteJournalError:
                raise
            except Exception:
                raise RouteJournalError('inspection ledger unavailable') from None

    def record_observation(self, request, status, route_state, key_id, answer):
        """held -> observed_<status> for the EXACT binding.

        Returns True only if THESE exact request and answer bytes are what is
        persisted: either this call's UPDATE (rowcount 1, then read back
        inside the same transaction) or an earlier write of byte-identical
        request and answer. Returns False if another observation of the same
        status, route_state and key (e.g. a concurrent inspection with another
        nonce) is persisted instead; the caller must then use THAT persisted
        observation, never these unpersisted bytes. A different status,
        route_state or key raises (``observation_conflict``); nothing changes.
        """
        _require(status in _TERMINAL and route_state in _TERMINAL[status] and type(answer) is bytes,
                 RouteJournalError('state'))
        state, command_id = _OBSERVED[status], request['binding']['command_id']
        ours = (state, route_state, key_id, answer.decode('ascii'), _canonical(request).decode('ascii'))
        with self._transaction() as db:
            row = self._select(db, command_id)
            _require(row is not None and row[1:5] == self._key(request), RouteJournalError('binding'))
            if row[6] != 'held':
                _require(row[6:9] == ours[:3], RouteJournalError('observation_conflict'))
                return row[6:11] == ours  # Duplicate ONLY on exact byte match.
            changed = db.execute('UPDATE supervisor_route_inspection SET state=?, route_state=?, key_id=?, '
                                 "answer=?, request=? WHERE command_id=? AND state='held'",
                                 (*ours, command_id)).rowcount
            _require(changed == 1 and self._select(db, command_id)[6:11] == ours, RouteJournalError('readback'))
            return True


@dataclass(frozen=True)
class InspectionObservation:
    """A verified signed supervisor statement about ONE exact binding.

    It is evidence of what the supervisor's journal said at ``answered_at_ms``
    (signed), not fresh host or Hub job state. ``historical`` is True when it
    was replayed from the ledger rather than answered in this call.

    ``request`` and ``answer`` are the RAW signed bytes (the exact canonical
    request that the answer echoes, and the signed answer). Every other field
    is a convenience HINT computed by this module and MUST NOT be trusted by
    the Hub: inside its own transaction the Hub re-verifies the raw bytes
    against its own pinned key (e.g. ``PinnedKeyInspectionVerifier.verify(
    answer, request=json.loads(request))`` after a strict canonical parse),
    derives the status, route_state, binding, dispatch claim and time from
    the SIGNED payload, compares them with its own job record, and refuses
    if any hint disagrees.
    """
    command_id: str
    status: str
    route_state: str
    answered_at_ms: int
    key_id: str
    binding_digest: str
    dispatch: dict
    historical: bool
    request: bytes = field(repr=False)
    answer: bytes = field(repr=False)
    reservation: dict | None = None


class HeldAttemptSource:
    """DEFAULT-REFUSING interface to the AUTHORITATIVE Hub job store.

    This module verifies and stores observations; it NEVER decides. Nothing
    here can move, resolve or release a Hub job. The Hub owns that decision:

    * ``hold_version(command, pin, dispatch_counter)`` -> an opaque version
      string (printable ASCII, at most 256 chars) naming the exact held
      attempt as the Hub job store has it now. ``InspectionResolver.hold``
      records nothing unless it gets a valid one (a temporary failure can
      simply be retried); a later hold of the same binding must return the
      SAME version, else it is refused (``hold_version_changed``).
    * ``record_observation_in_hub_transaction(version, observation)`` -> the
      callback the Hub executes inside ITS OWN DB transaction. In that one
      transaction it must INDEPENDENTLY: (1) strictly parse
      ``observation.request`` (canonical JSON) and verify
      ``observation.answer`` against it with its own pinned supervisor key;
      (2) take the outcome (status, route_state), binding, dispatch claim and
      time ONLY from those signed bytes, never from the module-computed
      fields, refusing if any of them disagrees; (3) check that the signed
      binding/claim is exactly its own held job and that ``version`` is that
      job's current hold version, plus its owner checks; then (4) record the
      outcome. It returns exactly True only if that transaction COMMITTED the
      outcome. Anything else (False, any other value, an exception, a
      rollback) means nothing was recorded.

    Atomicity is ONLY as strong as that Hub transaction; this module provides
    none (the inspection ledger shares no transaction or lock with the Hub
    DB). The base class refuses both, so every attempt stays held until a
    source bound to the Hub job store exists (none does today).
    """

    def hold_version(self, command, pin, dispatch_counter):
        raise SupervisorRouteError('held attempt source not configured')

    def record_observation_in_hub_transaction(self, version, observation):
        raise SupervisorRouteError('held attempt source not configured')


@dataclass(frozen=True)
class InspectionResolverConfig:
    """Explicit configuration. ``enabled`` defaults to False: no resolver exists."""
    enabled: bool = False
    ledger_path: str | None = None
    create_ledger: bool = False
    allow_ephemeral_test_storage: bool = False


@dataclass(frozen=True)
class InspectionResolution:
    """state: held | applied.

    ``applied`` means only that the Hub's own DB transaction (the source
    callback) committed the observation with its owner/job checks; ``held``
    is everything else (including a verified observation the Hub refused or
    that could not be offered). ``observation`` is the verified signed observation, if
    any (``historical`` when replayed). Nothing here permits resubmitting,
    redispatching or releasing.
    """
    state: str
    command_id: str
    observation: InspectionObservation | None = None
    error: str | None = None
    status: str | None = None

    @property
    def applied(self):
        return self.state == 'applied'


class InspectionResolver:
    """DISABLED-BY-DEFAULT Hub-side inspection of HELD attempts.

    ``hold(command, pin, dispatch_counter)`` durably records a held attempt
    with the source's opaque ``hold_version``; without a valid version it
    records nothing and raises, so it can be retried.
    ``inspect(...)`` contacts the supervisor only for a row held with the
    identical binding. A verified, signed ``launched``/``refused`` whose
    echoed request equals the held binding and dispatch claim is recorded as
    an OBSERVATION; everything else (no row, transport, framing, HTTP denial,
    unsigned, bad signature, wrong key, any binding mismatch, ``unknown``,
    signed ``not_found``) records nothing and returns ``held``.

    The observation never moves the Hub job by itself and this class never
    decides. The only authoritative step is
    ``source.record_observation_in_hub_transaction(hold_version, obs)``, which
    the Hub executes in its own DB transaction with its owner/job checks;
    only its exact True yields ``applied``. A refusal or an exception ->
    ``held`` (with the observation attached); the default source cannot
    even hold (no hold version), so nothing is recorded. The Hub must re-verify
    the raw signed bytes itself and not trust this module's hint fields.
    Atomicity is only as strong as that Hub transaction.

    Only PERSISTED bytes are ever offered: if a concurrent inspection of the
    same attempt persisted its (byte-different, e.g. other nonce) observation
    first, this call re-reads and re-verifies THAT row and offers it as
    historical; its own unpersisted answer is dropped. Any write or readback
    mismatch -> ``held``.

    A recorded observation is replayed as HISTORICAL (signed answered_at_ms),
    re-verified against the CURRENT pinned key and binding (key rotation,
    another pin or binding, tampered or missing bytes -> ``held``), and again
    goes through the Hub transaction with the hold version; replay never
    moves anything on its own. Not wired into any Hub service.
    """

    def __init__(self, *, config, client, source):
        if type(config) is not InspectionResolverConfig or config.enabled is not True:
            raise SupervisorRouteDisabled('supervisor inspection resolver is disabled')
        _require(type(client) is SupervisorInspectionClient and type(config.ledger_path) is str
                 and isinstance(source, HeldAttemptSource),
                 SupervisorRouteError('invalid supervisor inspection resolver'))
        self._client, self._source = client, source
        self._ledger = InspectionLedger(config.ledger_path, create=config.create_ledger,
                                        allow_ephemeral_test_storage=config.allow_ephemeral_test_storage)

    def __repr__(self):
        return 'InspectionResolver(<enabled>)'

    def hold(self, command, pin, dispatch_counter):
        """Durably record a held attempt with the source's hold version.

        Records NOTHING (``SupervisorRouteRejected('hold_version_unavailable')``)
        unless the source returns a valid version, so a temporary source
        failure never persists an unusable row; the caller may retry.
        """
        request = self._client.request_for(command, pin, dispatch_counter)
        try:
            version = self._source.hold_version(command, pin, dispatch_counter)
        except Exception:
            version = None
        if not _valid_hold_version(version):
            raise SupervisorRouteRejected('hold_version_unavailable')
        self._ledger.hold(request, version)

    def _replay(self, row, request):
        """Re-verify a recorded observation; returns (payload, answer, request bytes) or None."""
        try:
            command_id, state, route_state, key_id, answer, sent = row[0], *row[6:11]
            _require(type(sent) is str and type(answer) is str and key_id == request['key_id'])
            raw = sent.encode('ascii')
            stored = _strict_json(raw, MAX_INSPECTION_REQUEST_BYTES)
            _require(_canonical(stored) == raw and _valid_inspection_request(stored)
                     and _canonical({**stored, 'request_nonce': None})
                     == _canonical({**request, 'request_nonce': None})
                     and stored['binding']['command_id'] == command_id)
            payload = self._client._verifier.verify(answer.encode('ascii'), request=stored)
            status = payload['status']
            _require(status in _TERMINAL and (_OBSERVED[status], payload['route_state'], payload['key_id'])
                     == (state, route_state, key_id) and route_state in _TERMINAL[status])
            return payload, answer.encode('ascii'), raw
        except Exception:
            return None

    def _apply(self, row, observation):
        """The ONLY path to ``applied``: the Hub's own DB transaction (source callback)."""
        version = row[5]
        if not _valid_hold_version(version):
            return InspectionResolution('held', observation.command_id, observation, 'hub_unavailable',
                                        observation.status)
        try:
            applied = self._source.record_observation_in_hub_transaction(version, observation) is True
        except Exception:
            applied = False
        if not applied:
            return InspectionResolution('held', observation.command_id, observation, 'hub_not_recorded',
                                        observation.status)
        return InspectionResolution('applied', observation.command_id, observation, None, observation.status)

    @staticmethod
    def _observation(request, payload, answer, historical, sent):
        reservation = payload['reservation']
        return InspectionObservation(request['binding']['command_id'], payload['status'], payload['route_state'],
                                     payload['answered_at_ms'], payload['key_id'], request['binding_digest'],
                                     json.loads(_canonical(request['dispatch'])), historical, sent, answer,
                                     None if reservation is None else dict(reservation))

    def inspect(self, command, pin, dispatch_counter):
        request = self._client.request_for(command, pin, dispatch_counter)
        command_id = command.command_id

        def held(error, status=None, observation=None):
            return InspectionResolution('held', command_id, observation,
                                        error if error in INSPECTION_HELD_REASONS else 'unverified', status)
        try:
            row = self._ledger.read(command_id)
        except Exception:
            return held('no_held_attempt')
        if row is None or row[1:5] != InspectionLedger._key(request):
            return held('no_held_attempt')  # Nothing sent.
        if row[6] != 'held':
            replayed = self._replay(row, request)
            if replayed is None:
                return held('replay_unverified')  # Never the stored observation unverified.
            # Historical evidence only: an authoritative effect still needs the Hub transaction.
            return self._apply(row, self._observation(request, replayed[0], replayed[1], True, replayed[2]))
        answer = self._client.inspect(request)
        if not answer.verified:
            return held(answer.error if answer.error in _INSPECTION_ERRORS else 'unverified')
        if answer.status in ('not_found', 'unknown', 'not_dispatched'):
            # Signed, but none of these is proof of no effect and none releases
            # anything: the Hub barrier's expired_unconsumed statement is the
            # release path for an undispatched reservation, not this module.
            return held(answer.status, answer.status)
        payload = answer.payload
        if (answer.status not in _TERMINAL or answer.route_state not in _TERMINAL[answer.status]
                or _canonical(payload['request']) != _canonical(request)
                or payload['key_id'] != request['key_id']):
            return held('unverified')
        observation = self._observation(request, payload, answer.raw, False, _canonical(request))
        try:
            persisted = self._ledger.record_observation(request, answer.status, answer.route_state,
                                                        payload['key_id'], answer.raw)
        except Exception:
            return held('observation_unrecorded', answer.status, observation)
        if persisted is True:
            return self._apply(row, observation)
        # Another inspection's observation was persisted first: only THAT
        # persisted, re-read and re-verified observation may be offered.
        try:
            stored = self._ledger.read(command_id)
        except Exception:
            return held('observation_unrecorded', answer.status)
        if stored is None or stored[:6] != row[:6] or stored[6] == 'held':
            return held('observation_unrecorded', answer.status)
        replayed = self._replay(stored, request)
        if replayed is None:
            return held('replay_unverified', answer.status)
        return self._apply(stored, self._observation(request, replayed[0], replayed[1], True, replayed[2]))

    def close(self):
        self._ledger.close()


# --- Hub -> supervisor transport for reserve / challenge / dispatch / expiry --
#
# docs/supervisor-dispatch-transport.md. DISABLED BY DEFAULT and default-
# refusing throughout; nothing constructs, wires or listens. Authority comes
# from the signed envelopes (verified by the Hub with its own pinned
# verifiers), never from this channel: the transport only adds WHO MAY ASK
# (per-scope bearers), WHO ANSWERS (pinned peer / machine identity) and
# BOUNDED COST (strict framing, one total deadline, non-blocking pools).

TRANSPORT_RESERVE_PATH = '/v1/supervisor-route/reserve'
TRANSPORT_CHALLENGE_PATH = '/v1/supervisor-route/challenge'
TRANSPORT_DISPATCH_PATH = '/v1/supervisor-route/dispatch'
TRANSPORT_EXPIRE_PATH = '/v1/supervisor-route/expired-statement'
RESERVE_SCOPE = 'supervisor-route.reserve'
CHALLENGE_SCOPE = 'supervisor-route.challenge'
DISPATCH_SCOPE = 'supervisor-route.dispatch'
EXPIRE_SCOPE = 'supervisor-route.expire'
# Scope -> credential prefix. Each prefix is refused by ``_bearer`` (the
# launch/executor credential) and by every other scope.
TRANSPORT_TOKEN_PREFIXES = {RESERVE_SCOPE: 'ehrr1.', CHALLENGE_SCOPE: 'ehrc1.', DISPATCH_SCOPE: 'ehrd1.',
                            EXPIRE_SCOPE: 'ehre1.', INSPECTION_SCOPE: INSPECTION_TOKEN_PREFIX}
_SCOPED_PREFIXES = tuple(TRANSPORT_TOKEN_PREFIXES.values())
# path -> (scope, route group, request body cap). Groups have separate pools:
# the Hub's dispatch path and its resolver path cannot starve each other.
_TRANSPORT_ROUTES = {
    TRANSPORT_RESERVE_PATH: (RESERVE_SCOPE, 'dispatch_path', 16 * 1024),
    TRANSPORT_CHALLENGE_PATH: (CHALLENGE_SCOPE, 'dispatch_path', 512),
    TRANSPORT_DISPATCH_PATH: (DISPATCH_SCOPE, 'dispatch_path', 32 * 1024),
    TRANSPORT_EXPIRE_PATH: (EXPIRE_SCOPE, 'resolver', 512),
    INSPECTION_PATH: (INSPECTION_SCOPE, 'resolver', MAX_INSPECTION_REQUEST_BYTES),
}
_UNKNOWN_PATH_CAP = 512
_COMMAND_KEYS = frozenset({'method', 'path', 'body', 'command_id', 'queue_fence', 'epoch'})
_B64 = re.compile(r'[A-Za-z0-9_-]*\Z')
_HINT_KEYS = frozenset({'state', 'command_id', 'binding_digest', 'replayed', 'error'})
_HINT_STATES = frozenset({'completed', 'rejected', 'refused', 'in_progress', 'held', 'busy'})
_REASON = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')


def _scoped_bearer(value, scope):
    prefix = TRANSPORT_TOKEN_PREFIXES[scope]
    _require(type(value) is str and value.startswith(prefix) and 32 <= len(value) - len(prefix)
             and len(value) <= 4096 and all(33 <= ord(c) <= 126 for c in value),
             SupervisorRouteError('scoped transport credential required'))
    return value


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _unb64(value, maximum):
    """Strict unpadded base64url -> bytes (canonical round trip, size-capped)."""
    _require(type(value) is str and _B64.fullmatch(value) and len(value) <= maximum * 4 // 3 + 4
             and len(value) % 4 != 1)
    raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    _require(_b64(raw) == value and len(raw) <= maximum)
    return raw


def transport_command(command):
    """The exact wire form of a RouteCommand (body as unpadded base64url)."""
    _require(type(command) is RouteCommand)
    return {'method': command.method, 'path': command.path, 'body': _b64(command.body),
            'command_id': command.command_id, 'queue_fence': command.queue_fence, 'epoch': command.epoch.as_dict()}


def _command_from(value):
    _require(_shape(value, _COMMAND_KEYS) and _shape(value['epoch'], _EPOCH_KEYS))
    epoch = value['epoch']
    return RouteCommand(value['method'], value['path'], _unb64(value['body'], MAX_BODY_BYTES), value['command_id'],
                        value['queue_fence'], RouteEpoch(epoch['launch_id'], epoch['launch_counter'], epoch['boot_id']))


def transport_request(path, **fields):
    """Canonical request body for ``path``; bytes fields go as base64url."""
    if path == TRANSPORT_RESERVE_PATH:
        value = {'command': transport_command(fields['command'])}
    elif path == TRANSPORT_CHALLENGE_PATH:
        value = {'command_id': fields['command_id']}
    elif path == TRANSPORT_DISPATCH_PATH:
        _require(type(fields['receipt']) is bytes and type(fields['proof']) is bytes)
        value = {'receipt': _b64(fields['receipt']), 'command': transport_command(fields['command']),
                 'proof': _b64(fields['proof'])}
    elif path == TRANSPORT_EXPIRE_PATH:
        value = {'nonce': fields['nonce']}
    else:
        raise SupervisorRouteRejected('unknown_route')
    return _canonical(value)


class RouteTokenAuthenticator:
    """Accepts only the ONE enrolled bearer of ONE transport scope (constant-time).

    Only a SHA-256 of the token is kept. The token must carry its scope's
    prefix, so a token of another scope (or the launch/executor bearer, which
    can never carry a scoped prefix) is refused before any comparison. For
    ``supervisor-route.inspect`` use InspectionTokenAuthenticator.
    """

    def __init__(self, scope, token):
        _require(scope in TRANSPORT_TOKEN_PREFIXES and scope != INSPECTION_SCOPE,
                 SupervisorRouteError('transport scope'))
        self.scope = scope
        self.__digest = hashlib.sha256(_scoped_bearer(token, scope).encode('ascii')).digest()

    def __repr__(self):
        return f'RouteTokenAuthenticator({self.scope}, <redacted>)'

    def __reduce__(self):
        raise TypeError('RouteTokenAuthenticator is not serializable')

    def authenticate(self, authorization):
        denied = SupervisorRouteError('transport caller not authenticated')
        scheme, _, token = authorization.partition(' ') if type(authorization) is str else ('', '', '')
        try:
            _scoped_bearer(token, self.scope)
        except SupervisorRouteError:
            raise denied from None
        if scheme != 'Bearer' or not hmac.compare_digest(hashlib.sha256(token.encode('ascii')).digest(),
                                                         self.__digest):
            raise denied
        return self.scope


@dataclass(frozen=True)
class TransportResponderConfig:
    """Explicit configuration. ``enabled`` defaults to False: no responder exists.

    Per route group (``dispatch_path``: reserve/challenge/dispatch;
    ``resolver``: expired-statement/inspect), ``max_authenticating`` (1..8)
    callers may be in the authenticator and ``max_concurrent`` (1..8)
    authenticated calls may run; any other caller gets an unsigned 503
    ``busy`` at once (never queued). ``answer_budget_ms`` (20..5000) is a
    return-time check: no signed reply is returned after it."""
    enabled: bool = False
    max_authenticating: int = 2
    max_concurrent: int = 2
    answer_budget_ms: int = 1000


class SupervisorTransportResponder:
    """DISABLED-BY-DEFAULT supervisor side of the transport routes.

    ``answer(method=, path=, authorization=, body=)`` -> (http_status, bytes).
    Route by path (unknown -> unsigned 404), take a non-blocking PRE-AUTH slot
    of the route's group, authenticate with exactly that route's scope (a
    missing authenticator authenticates nobody), take a POST-AUTH slot, parse
    the exact canonical request, call the route operation and reply:

    * reserve -> 200 the exact signed reservation receipt bytes;
    * challenge -> 200 the exact signed dispatch challenge bytes;
    * expired-statement -> 200 the exact signed expired_unconsumed statement;
    * dispatch -> 200 an UNSIGNED hint {"hint": {state, command_id,
      binding_digest, replayed, error}}; never authority for anything;
    * inspect -> delegated unchanged to the SupervisorInspectionResponder.

    Denials are unsigned: 401 unauthorized, 400 invalid_payload, 404,
    409 {"error": {"code": "rejected", "reason": <route reason>}}, 503 busy /
    unavailable. A signed 200 is not returned after ``answer_budget_ms``
    (best-effort, as for inspection: the route operation itself cannot be
    interrupted and has already taken effect, e.g. an issued challenge).
    Pools are PER INSTANCE.
    """

    def __init__(self, *, config, route, authenticators=(), inspection=None):
        if type(config) is not TransportResponderConfig or config.enabled is not True:
            raise SupervisorRouteDisabled('supervisor transport responder is disabled')
        authenticators = tuple(authenticators)
        _require(type(route) is SupervisorRoute
                 and all(type(a) is RouteTokenAuthenticator for a in authenticators)
                 and len({a.scope for a in authenticators}) == len(authenticators)
                 and (inspection is None or type(inspection) is SupervisorInspectionResponder)
                 and type(config.max_authenticating) is int and 1 <= config.max_authenticating <= 8
                 and type(config.max_concurrent) is int and 1 <= config.max_concurrent <= 8
                 and type(config.answer_budget_ms) is int and 20 <= config.answer_budget_ms <= 5000,
                 SupervisorRouteError('invalid supervisor transport responder'))
        self._route, self._inspection = route, inspection
        self._authenticators = {a.scope: a for a in authenticators}
        self._auth_slots = {group: threading.BoundedSemaphore(config.max_authenticating)
                            for group in ('dispatch_path', 'resolver')}
        self._slots = {group: threading.BoundedSemaphore(config.max_concurrent)
                       for group in ('dispatch_path', 'resolver')}
        self._budget = config.answer_budget_ms / 1000

    def __repr__(self):
        return 'SupervisorTransportResponder(<enabled>)'

    @staticmethod
    def body_cap(path):
        return _TRANSPORT_ROUTES[path][2] if path in _TRANSPORT_ROUTES else _UNKNOWN_PATH_CAP

    @staticmethod
    def _denied(status, code, reason=None):
        error = {'code': code} if reason is None else {'code': code, 'reason': reason}
        return status, _canonical({'error': error})

    def answer(self, *, method, path, authorization, body):
        deadline = time.monotonic() + self._budget
        if method != 'POST' or path not in _TRANSPORT_ROUTES:
            return self._denied(404, 'not_found')
        scope, group, cap = _TRANSPORT_ROUTES[path]
        if type(body) is not bytes or len(body) > cap:
            return self._denied(413, 'too_large')
        if not self._auth_slots[group].acquire(blocking=False):
            return self._denied(503, 'busy')
        try:
            if scope == INSPECTION_SCOPE:
                if self._inspection is None:
                    return self._denied(404, 'not_found')
                # Unchanged inspection semantics (own authenticator and pools).
                return self._inspection.answer(method=method, path=path, authorization=authorization, body=body)
            try:
                authenticator = self._authenticators.get(scope)
                _require(authenticator is not None and authenticator.authenticate(authorization) == scope)
            except Exception:
                return self._denied(401, 'unauthorized')
        finally:
            self._auth_slots[group].release()
        if not self._slots[group].acquire(blocking=False):
            return self._denied(503, 'busy')
        try:
            status, raw, signed = self._call(path, body)
        finally:
            self._slots[group].release()
        if status == 200 and signed and not time.monotonic() <= deadline:
            return self._denied(503, 'unavailable')  # Return-time check on signed replies.
        return status, raw

    def _call(self, path, body):
        try:
            value = _strict_json(body, self.body_cap(path))
            _require(_canonical(value) == body)
            if path == TRANSPORT_RESERVE_PATH:
                _require(_shape(value, {'command'}))
                command = _command_from(value['command'])  # Parsed here: any invalid field is a 400.
                call, signed = (lambda: self._route.reserve(command).receipt), True
            elif path == TRANSPORT_CHALLENGE_PATH:
                _require(_shape(value, {'command_id'}) and _uuid(value['command_id']))
                call, signed = (lambda: self._route.issue_dispatch_challenge(value['command_id'])), True
            elif path == TRANSPORT_DISPATCH_PATH:
                _require(_shape(value, {'receipt', 'command', 'proof'}))
                receipt, proof = _unb64(value['receipt'], MAX_RECORD_BYTES), _unb64(value['proof'], MAX_RECORD_BYTES)
                command = _command_from(value['command'])
                call, signed = (lambda: self._route.dispatch(receipt, command, proof)), False
            else:
                _require(_shape(value, {'nonce'}) and _valid_nonce(value['nonce']))
                call, signed = (lambda: self._route.expired_statement(value['nonce'])), True
        except Exception:
            return (*self._denied(400, 'invalid_payload'), False)
        try:
            result = call()
        except SupervisorRouteRejected as rejected:
            reason = rejected.reason if type(rejected.reason) is str and _REASON.fullmatch(rejected.reason) \
                else 'invalid'
            return (*self._denied(409, 'rejected', reason), False)
        except SupervisorRouteBusy:
            return (*self._denied(503, 'busy'), False)
        except Exception:
            return (*self._denied(503, 'unavailable'), False)
        if signed:
            _require(type(result) is bytes)
            return 200, result, True
        hint = {'state': result.state, 'command_id': result.command_id, 'binding_digest': result.binding_digest,
                'replayed': result.replayed, 'error': result.error}
        return 200, _canonical({'hint': hint, 'unsigned': True}), False


MAX_TRANSPORT_REQUEST_BYTES = 32 * 1024
_REQUEST_LINE = re.compile(rb'(POST|GET) (/[A-Za-z0-9/_.-]{0,255}) HTTP/1\.1\Z')


class _Oversize(Exception):
    pass


def _read_request(peer, remaining, cap_for):
    """Strict bounded HTTP/1.1 request reader under ONE total deadline (the
    server-side twin of ``_read_response``): request line, at most
    MAX_HEADERS headers / MAX_HEADER_BYTES, exactly one decimal
    Content-Length within the route's cap (else _Oversize BEFORE reading the
    body), at most one Authorization, no Transfer-Encoding, exactly that many
    body bytes. Returns (method, path, authorization, body)."""
    def receive(size):
        peer.settimeout(remaining())
        part = peer.recv(size)
        if not part:
            raise _framing()
        return part
    buffer = bytearray()
    while True:
        end = buffer.find(b'\r\n\r\n')
        if end >= 0:
            break
        if len(buffer) > MAX_HEADER_BYTES:
            raise _framing()
        buffer += receive(MAX_HEADER_BYTES + 4 - len(buffer) if len(buffer) < MAX_HEADER_BYTES else 4)
    if end > MAX_HEADER_BYTES:
        raise _framing()
    head, body = bytes(buffer[:end]), bytearray(buffer[end + 4:])
    lines = head.split(b'\r\n')
    match = _REQUEST_LINE.fullmatch(lines[0])
    if match is None or len(lines) - 1 > MAX_HEADERS:
        raise _framing()
    method, path = match[1].decode('ascii'), match[2].decode('ascii')
    lengths, authorizations = [], []
    for line in lines[1:]:
        header = _HEADER_LINE.fullmatch(line)
        if header is None:
            raise _framing()
        name = header[1].lower()
        if name == b'transfer-encoding':
            raise _framing()
        if name == b'content-length':
            lengths.append(header[2] or b'')
        if name == b'authorization':
            authorizations.append((header[2] or b'').decode('ascii'))
    if len(lengths) != 1 or not _LENGTH.fullmatch(lengths[0]) or len(authorizations) > 1:
        raise _framing()
    length = int(lengths[0])
    if length > min(cap_for(path), MAX_TRANSPORT_REQUEST_BYTES):
        raise _Oversize()
    if len(body) > length:
        raise _framing()
    while len(body) < length:
        body += receive(length - len(body))
    return method, path, (authorizations[0] if authorizations else None), bytes(body)


def spki_sha256(certificate):
    """SHA-256 hex of a certificate's SubjectPublicKeyInfo (DER or PEM bytes)."""
    from cryptography import x509
    _require(type(certificate) is bytes and certificate, SupervisorRouteError('certificate'))
    cert = (x509.load_pem_x509_certificate(certificate) if certificate.startswith(b'-----BEGIN')
            else x509.load_der_x509_certificate(certificate))
    return hashlib.sha256(cert.public_key().public_bytes(serialization.Encoding.DER,
                                                         serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def _tls_context(*, server, certificate_path, private_key_path, anchor_path):
    """TLS 1.3 only, mutual. OpenSSL needs a verification anchor to request and
    accept the peer's certificate: ``anchor_path`` should hold exactly the
    enrolled peer's self-signed certificate (no CA, no system store, no
    hostname check). The peer's SPKI is pinned on top, after the handshake,
    so an over-broad anchor file still cannot admit another key."""
    import ssl
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    if not server:
        context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=anchor_path)
    context.load_cert_chain(certificate_path, private_key_path)
    return context


def _check_spki(tls, expected):
    der = tls.getpeercert(binary_form=True)
    if not der or not hmac.compare_digest(spki_sha256(der), expected):
        raise PeerMismatch()


def _valid_path(value):
    return type(value) is str and value.startswith('/') and os.path.normpath(value) == value


@dataclass(frozen=True)
class HubUnixIdentity:
    """Enrolled Hub service identity for the Unix-socket channel (SO_PEERCRED)."""
    uid: int
    gid: int

    def __post_init__(self):
        _require(_id32(self.uid) and _id32(self.gid))


@dataclass(frozen=True)
class TlsServerIdentity:
    """Supervisor TLS listener identity: its own TLS key (separate from every
    signing key) and the Hub host's pinned client SPKI."""
    certificate_path: str
    private_key_path: str
    client_anchor_path: str
    client_spki_sha256: str

    def __post_init__(self):
        _require(all(_valid_path(p) for p in (self.certificate_path, self.private_key_path,
                                               self.client_anchor_path))
                 and type(self.client_spki_sha256) is str and _HEX.fullmatch(self.client_spki_sha256))


@dataclass(frozen=True)
class TransportServerConfig:
    """Explicit configuration. ``enabled`` defaults to False: no server exists.

    ``request_timeout_ms`` bounds the handshake plus the request read and
    ``reply_timeout_ms`` the reply write, each ONE total deadline."""
    enabled: bool = False
    request_timeout_ms: int = 2000
    reply_timeout_ms: int = 2000


class SupervisorTransportServer:
    """DISABLED-BY-DEFAULT per-connection handler for the supervisor transport.

    ``serve_connection(conn)`` takes ONE accepted connection (AF_UNIX for a
    HubUnixIdentity, TCP for a TlsServerIdentity), checks the caller's
    machine identity BEFORE reading any application byte (Unix: SO_PEERCRED
    uid/gid equal to the enrolled Hub; TLS: TLS 1.3 mutual handshake, then the
    client certificate's SPKI SHA-256 equal to the pin), reads ONE request
    with strict framing (per-route body cap), hands it to the responder,
    writes the reply and closes. Returns a short outcome string. No accept
    loop or listener is provided: the operator owns binding (one dedicated
    socket/interface) and threads. Supervisor TLS key material is used ONLY
    for the channel; it must be separate from every signing key (checked for
    Ed25519 keys against ``signing_public_keys``).
    """

    def __init__(self, *, config, responder, identity, signing_public_keys=()):
        if type(config) is not TransportServerConfig or config.enabled is not True:
            raise SupervisorRouteDisabled('supervisor transport server is disabled')
        _require(type(responder) is SupervisorTransportResponder
                 and type(identity) in (HubUnixIdentity, TlsServerIdentity)
                 and type(config.request_timeout_ms) is int and 100 <= config.request_timeout_ms <= 60000
                 and type(config.reply_timeout_ms) is int and 100 <= config.reply_timeout_ms <= 60000,
                 SupervisorRouteError('invalid supervisor transport server'))
        self._responder, self._identity = responder, identity
        self._request_timeout, self._reply_timeout = config.request_timeout_ms / 1000, config.reply_timeout_ms / 1000
        self._tls = None
        if type(identity) is TlsServerIdentity:
            certificate = Path(identity.certificate_path).read_bytes()
            from cryptography import x509
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey as _Ed
            key = x509.load_pem_x509_certificate(certificate).public_key()
            if isinstance(key, _Ed):
                raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
                _require(raw not in set(signing_public_keys), SupervisorRouteError('tls key reuses a signing key'))
            self._tls = _tls_context(server=True, certificate_path=identity.certificate_path,
                                     private_key_path=identity.private_key_path,
                                     anchor_path=identity.client_anchor_path)

    def __repr__(self):
        return 'SupervisorTransportServer(<enabled>)'

    def serve_connection(self, conn):
        deadline = time.monotonic() + self._request_timeout

        def remaining():
            value = deadline - time.monotonic()
            if not value > 0:
                raise TimeoutError
            return value
        peer = conn
        try:
            try:
                peer = self._accept_identity(conn, remaining)  # Nothing read yet.
            except Exception:
                return 'peer_refused'
            try:
                method, path, authorization, body = _read_request(peer, remaining, self._responder.body_cap)
            except _Oversize:
                self._reply(peer, *SupervisorTransportResponder._denied(413, 'too_large'))
                return 'too_large'
            except TimeoutError:
                return 'timeout'
            except Exception:
                return 'framing'
            status, raw = self._responder.answer(method=method, path=path, authorization=authorization, body=body)
            self._reply(peer, status, raw)
            return 'answered'
        finally:
            try:
                peer.close()
            finally:
                if peer is not conn:
                    conn.close()

    def _accept_identity(self, conn, remaining):
        if self._tls is None:
            pid, uid, gid = struct.unpack('=iII', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid <= 0 or (uid, gid) != (self._identity.uid, self._identity.gid):
                raise PeerMismatch()
            return conn
        conn.settimeout(remaining())
        tls = self._tls.wrap_socket(conn, server_side=True, do_handshake_on_connect=False)
        tls.settimeout(remaining())
        tls.do_handshake()
        _check_spki(tls, self._identity.client_spki_sha256)
        return tls

    def _reply(self, peer, status, raw):
        """Writes one response under the reply deadline (test seam: overridable)."""
        try:
            peer.settimeout(self._reply_timeout)
            peer.sendall(f'HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n'
                         'Connection: close\r\n\r\n'.encode('ascii') + raw)
        except Exception:
            pass


@dataclass(frozen=True)
class UnixTransportChannel:
    """Same host: the supervisor's Unix socket and its enrolled peer pin."""
    socket_path: str
    supervisor_peer: SupervisorPeerPin

    def __post_init__(self):
        _require(_valid_path(self.socket_path) and type(self.supervisor_peer) is SupervisorPeerPin)


@dataclass(frozen=True)
class TlsTransportChannel:
    """Different hosts: TLS 1.3 mutual auth; the Hub's pinned client key is its
    machine identity and the supervisor's SPKI is pinned (no CA, no hostname)."""
    host: str
    port: int
    client_certificate_path: str
    client_private_key_path: str
    server_anchor_path: str
    server_spki_sha256: str

    def __post_init__(self):
        _require(type(self.host) is str and 0 < len(self.host) <= 253 and type(self.port) is int
                 and 0 < self.port < 65536
                 and all(_valid_path(p) for p in (self.client_certificate_path, self.client_private_key_path,
                                                   self.server_anchor_path))
                 and type(self.server_spki_sha256) is str and _HEX.fullmatch(self.server_spki_sha256))


@dataclass(frozen=True)
class TransportClientConfig:
    """Explicit configuration. ``enabled`` defaults to False: no client exists."""
    enabled: bool = False
    timeout_ms: int = 2000


@dataclass(frozen=True)
class TransportReply:
    """One exchange. ``raw`` = the exact 200 body or None; ``hint`` = the
    parsed UNSIGNED dispatch hint (dispatch only).

    ``held`` is ALWAYS True: no transport reply moves Hub state by itself, not
    even a 200. ``unverified`` is the candidate signed body of a signed-route
    200 (else None); the attempt stays held until the Hub's own pinned verifier
    accepts those exact bytes inside its own transaction. A dispatch reply has
    no candidate: its hint never moves Hub state; only a verified inspection
    answer recorded in the Hub's own transaction settles the attempt."""
    raw: bytes | None = field(default=None, repr=False)
    status: int | None = None
    error: str | None = None
    hint: dict | None = None

    @property
    def held(self):
        return True

    @property
    def unverified(self):
        return self.raw if self.hint is None else None


class SupervisorTransportClient:
    """DISABLED-BY-DEFAULT Hub side: one method per route, one exchange each.

    ``credentials`` maps scope -> bearer; a component gets only the scopes it
    needs (dispatch path: reserve/challenge/dispatch; resolver:
    expire/inspect). The supervisor's identity is verified BEFORE any
    application byte is written (Unix: SupervisorPeerPin via SO_PEERCRED +
    pidfd start ticks, re-checked after the reply; TLS: pinned SPKI after the
    handshake) - a mismatch sends zero bytes. One total deadline per
    exchange with the strict ``_read_response`` framing. Every failure is a
    held TransportReply with ``error`` (peer_mismatch / transport_lost /
    http_status / malformed), never raised and never a state move. The Hub
    MUST still verify every 200 body with its own pinned verifiers.
    """

    def __init__(self, *, config, channel, credentials):
        if type(config) is not TransportClientConfig or config.enabled is not True:
            raise SupervisorRouteDisabled('supervisor transport client is disabled')
        _require(type(channel) in (UnixTransportChannel, TlsTransportChannel)
                 and type(config.timeout_ms) is int and 100 <= config.timeout_ms <= 120000
                 and isinstance(credentials, dict) and credentials
                 and all(scope in TRANSPORT_TOKEN_PREFIXES for scope in credentials),
                 SupervisorRouteError('invalid supervisor transport client'))
        self.__authorizations = {scope: 'Bearer ' + _scoped_bearer(token, scope)
                                 for scope, token in credentials.items()}
        self._channel, self._timeout = channel, config.timeout_ms
        self._tls = None
        if type(channel) is TlsTransportChannel:
            self._tls = _tls_context(server=False, certificate_path=channel.client_certificate_path,
                                     private_key_path=channel.client_private_key_path,
                                     anchor_path=channel.server_anchor_path)

    def __repr__(self):
        return 'SupervisorTransportClient(<redacted>)'

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError('SupervisorTransportClient is not serializable')

    def reserve(self, command):
        return self._call(TRANSPORT_RESERVE_PATH, transport_request(TRANSPORT_RESERVE_PATH, command=command))

    def challenge(self, command_id):
        return self._call(TRANSPORT_CHALLENGE_PATH, transport_request(TRANSPORT_CHALLENGE_PATH,
                                                                      command_id=command_id))

    def dispatch(self, receipt, command, proof):
        reply = self._call(TRANSPORT_DISPATCH_PATH, transport_request(TRANSPORT_DISPATCH_PATH, receipt=receipt,
                                                                      command=command, proof=proof))
        if reply.raw is None:
            return reply
        try:
            value = _strict_json(reply.raw, MAX_RESPONSE_BYTES)
            hint = value['hint']
            _require(_shape(value, {'hint', 'unsigned'}) and value['unsigned'] is True
                     and _shape(hint, _HINT_KEYS) and hint['state'] in _HINT_STATES)
        except Exception:
            return TransportReply(None, reply.status, 'malformed')
        return TransportReply(reply.raw, reply.status, None, hint)

    def expired_statement(self, nonce):
        return self._call(TRANSPORT_EXPIRE_PATH, transport_request(TRANSPORT_EXPIRE_PATH, nonce=nonce))

    def inspect(self, request):
        _require(_valid_inspection_request(request))
        return self._call(INSPECTION_PATH, _canonical(request))

    def _call(self, path, body):
        scope = _TRANSPORT_ROUTES[path][0]
        authorization = self.__authorizations.get(scope)
        _require(authorization is not None, SupervisorRouteError('no credential for ' + scope))
        try:
            status, raw = self._exchange(path, authorization, body)
        except PeerMismatch:
            return TransportReply(None, None, 'peer_mismatch')
        except Exception:
            return TransportReply(None, None, 'transport_lost')
        if status != 200:
            return TransportReply(None, status, 'http_status')
        return TransportReply(raw, status)

    def _exchange(self, path, authorization, body):
        deadline = time.monotonic() + self._timeout / 1000

        def remaining():
            value = deadline - time.monotonic()
            if not value > 0:
                raise TimeoutError
            return value
        request = ('\r\n'.join([f'POST {path} HTTP/1.1', 'Host: supervisor-route', 'Accept-Encoding: identity',
                                'Connection: close', 'Content-Type: application/json',
                                f'Content-Length: {len(body)}', 'Authorization: ' + authorization])
                   + '\r\n\r\n').encode('ascii') + body
        pidfd = peer = None
        try:
            if self._tls is None:
                peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                peer.settimeout(remaining())
                peer.connect(self._channel.socket_path)
                pid, pidfd, start = _check_supervisor_peer(peer, self._channel.supervisor_peer)
            else:
                import ssl
                raw_socket = socket.create_connection((self._channel.host, self._channel.port), timeout=remaining())
                try:
                    peer = self._tls.wrap_socket(raw_socket, server_hostname=None, do_handshake_on_connect=False)
                except BaseException:
                    raw_socket.close()
                    raise
                peer.settimeout(remaining())
                try:
                    peer.do_handshake()
                except ssl.SSLCertVerificationError:
                    raise PeerMismatch() from None
                _check_spki(peer, self._channel.server_spki_sha256)
            # Identity verified: only now is any application byte written.
            peer.settimeout(remaining())
            peer.sendall(request)
            status, raw = _read_response(peer, remaining)
            if pidfd is not None:
                _require(not select.select([pidfd], [], [], 0)[0] and _proc_start(pid) == start,
                         SupervisorRouteError('peer'))
            return status, raw
        except PeerMismatch:
            raise PeerMismatch() from None
        except Exception:
            # Sanitized: no header, token or supervisor text in the chain.
            raise SupervisorRouteError('transport exchange unavailable') from None
        finally:
            del request
            if pidfd is not None:
                os.close(pidfd)
            if peer is not None:
                peer.close()
