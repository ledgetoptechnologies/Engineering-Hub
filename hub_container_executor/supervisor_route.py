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
(capped per executor). The Hub signs, under its pinned dispatch key and
``HUB_PROOF_DOMAIN``, {protocol, audience, hub_key_id, job_id, run_id,
worker, fence, lease_epoch, reservation_receipt_sha256, command_id, counter,
executor, challenge_nonce, supervisor_boot_id} (no wall-clock field). In the
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
that also appear in the request body (command_id, run_id, fence) must equal
it; job_id, worker and lease_epoch are bound only by the Hub's signature.
Proof mode is durable: a route constructed with a proof verifier records it
in the journal, after which ``submit`` (and the journal's one-step admit)
refuse ``proof_required`` and a construction without a verifier is refused. Triggers make
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
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
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
              "CHECK((state = 'consumed') = (proof_sha256 IS NOT NULL)))")
# Inserted once per nonce and only as 'outstanding'; afterwards only ONE
# change, outstanding -> consumed (with the proof hash) or outstanding ->
# expired; every other column is frozen; never deleted. So a consumed nonce
# can never become expired and an expired one can never be consumed.
_CHALLENGE_TRIGGERS = {
    'supervisor_route_challenge_once': (
        'CREATE TRIGGER supervisor_route_challenge_once BEFORE INSERT ON supervisor_route_challenge '
        "WHEN NEW.state IS NOT 'outstanding' OR NEW.proof_sha256 IS NOT NULL OR EXISTS "
        '(SELECT 1 FROM supervisor_route_challenge WHERE nonce = NEW.nonce) '
        "BEGIN SELECT RAISE(ABORT, 'challenge exists'); END"),
    'supervisor_route_challenge_transition': (
        'CREATE TRIGGER supervisor_route_challenge_transition BEFORE UPDATE ON supervisor_route_challenge '
        "WHEN OLD.state IS NOT 'outstanding' OR NEW.state NOT IN ('consumed','expired') "
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
                     'backend_identity_mismatch', 'outcome_unrecorded', 'interrupted', 'signing_failed'})
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
    _require(type(value) is str and 0 < len(value) <= 4096 and all(33 <= ord(c) <= 126 for c in value),
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
    return (type(signer) not in (ReservationSigner, ChallengeStatementSigner)
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
                         'lease_epoch', 'reservation_receipt_sha256', 'command_id', 'counter', 'executor',
                         'challenge_nonce', 'supervisor_boot_id'})
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
    {protocol, audience, hub_key_id, job_id, run_id, worker, fence,
    lease_epoch, reservation_receipt_sha256, command_id, counter, executor,
    challenge_nonce, supervisor_boot_id} with strict types. There is NO
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
                             for k in ('job_id', 'run_id', 'worker'))
                     and _positive(payload['fence']) and _positive(payload['lease_epoch'])
                     and type(payload['reservation_receipt_sha256']) is str
                     and _HEX.fullmatch(payload['reservation_receipt_sha256'])
                     and _uuid(payload['command_id']) and _positive(payload['counter'], MAX_DISPATCH_COUNTER)
                     and type(payload['executor']) is str and _HEX.fullmatch(payload['executor'])
                     and _valid_nonce(payload['challenge_nonce']) and _uuid(payload['supervisor_boot_id']),
                     error)
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


@dataclass(frozen=True)
class DispatchChallenge:
    """A recorded one-use dispatch challenge. ``as_dict()`` is the canonical
    object the Hub binds into its proof (nonce, counter, executor, boot id).
    Freshness is enforced only by the supervisor; issue time is not exposed."""
    nonce: str
    command_id: str
    counter: int
    executor: str
    supervisor_boot_id: str
    proof_window_ms: int

    def as_dict(self):
        return {'protocol': CHALLENGE_PROTOCOL, 'nonce': self.nonce, 'command_id': self.command_id,
                'counter': self.counter, 'executor': self.executor,
                'supervisor_boot_id': self.supervisor_boot_id, 'proof_window_ms': self.proof_window_ms}

    def canonical(self):
        return _canonical(self.as_dict())


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
                with self._connection(readonly=True) as db:
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
                _require(db.execute('PRAGMA journal_mode=DELETE').fetchone() == ('delete',),
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
    reservation, challenge or mode table/triggers, user_version below 6) fails
    the exact schema
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
    _VERSION = 6

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
        journal already in proof mode is refused. Returns the durable mode."""
        with self._transaction() as db:
            recorded = self._proof_mode(db)
            if recorded and not proof_required:
                raise RouteJournalError('proof mode journal requires a dispatch proof verifier')
            if proof_required and not recorded:
                db.execute('INSERT INTO supervisor_route_mode VALUES (1, 1)')
            return recorded or proof_required

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
        # worker and lease_epoch are NOT in the body or any supervisor state:
        # they are covered only by the Hub's signature and its barrier.
        boot_id, now = self._clock(clock)
        if (proof['reservation_receipt_sha256'] != hashlib.sha256(receipt).hexdigest()
                or proof['command_id'] != row[0] or proof['counter'] != row[4] or proof['executor'] != row[1]
                or proof['fence'] != binding['queue_fence'] or proof['run_id'] != run_id
                or proof['supervisor_boot_id'] != boot_id):
            raise SupervisorRouteRejected('proof_mismatch')
        challenge = db.execute('SELECT command_id,executor_id,counter,issued_boottime_ns,boot_id,state '
                               'FROM supervisor_route_challenge WHERE nonce=?',
                               (proof['challenge_nonce'],)).fetchone()
        if challenge is None or challenge[:3] != (row[0], row[1], row[4]):
            raise SupervisorRouteRejected('challenge_unknown')
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
        Records an 'outstanding' row; returns (executor, counter, boot_id)."""
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
            db.execute("INSERT INTO supervisor_route_challenge VALUES (?,?,?,?,?,?,'outstanding',NULL)",
                       (nonce, command_id, row[1], row[4], now, boot_id))
            return row[1], row[4], boot_id

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
                 dispatch_proof_verifier=None, statement_signer=None, boot_clock=None):
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
        return self._send(command, request, binding, digest, epoch, pin, row)

    def _send(self, command, request, binding, digest, epoch, pin, counter):
        """Durable intent AND its consumed dispatch counter exist: ownership is
        non-abandonable and the counter is never reused."""
        self._inflight.add(command.command_id)
        try:
            return self._forward(command, request, binding, digest, epoch, pin, counter)
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

    def _forward(self, command, request, binding, digest, epoch, pin, counter):
        extra = {}
        if getattr(self._backend, 'carries_dispatch', False) is True:
            extra['dispatch'] = dispatch_header(dispatch_claim(binding, counter))
        try:
            reply = self._backend.forward(method=command.method, path=command.path, body=command.body,
                                          timeout_ms=self._timeout, pin=pin, **extra)
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
        return self._send(command, request, binding, digest, epoch, pin, payload['counter'])

    def _journal_op(self):
        _require(os.getpid() == self._pid, SupervisorRouteError('process'))
        with self._state:
            _require(not self._closed, SupervisorRouteError('supervisor route closed'))

    def issue_dispatch_challenge(self, command_id):
        """Records a fresh one-use challenge for a 'reserved', not superseded
        reservation and returns it (DispatchChallenge). Sends nothing. At most
        ``max_outstanding_challenges`` live challenges per executor
        (SupervisorRouteBusy above). Refuses without a configured proof
        verifier. Atomicity comes from the journal transaction, so this does
        not wait for an in-flight send."""
        self._journal_op()
        _require(_uuid(command_id))
        _require(self._proof_verifier.configured, SupervisorRouteError('hub dispatch proof verifier not configured'))
        nonce = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode()
        executor, counter, boot_id = self._journal.issue_challenge(command_id, nonce, self._clock, self._window_ns,
                                                                   self._challenge_cap)
        return DispatchChallenge(nonce, command_id, counter, executor, boot_id, self._window_ms)

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
