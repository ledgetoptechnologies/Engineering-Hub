"""Advisory send guidance, never delivery authorization or a receipt."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .reachability import PublicReachability


class DeliveryGuidance(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    recipient_state: Literal['online', 'busy', 'offline', 'unknown', 'unlinked']
    send_mode: Literal['notify_if_still_reachable', 'queue_only', 'link_first']
    response_expectation: Literal['may_respond', 'may_be_delayed', 'not_verified']
    durable_delivery_required: Literal[True] = True
    receipt_required: Literal[True] = True


def topic_delivery_guidance(peer: PublicReachability, *, now=None) -> DeliveryGuidance:
    """Require fresh exact-topic runtime evidence before suggesting notification.

    A route may disconnect immediately afterward. Queue-only does not mean drop
    a message; offline/unknown messages still require durable addressed storage.
    No I/O, registry mutation, message send, listener or retry occurs here.
    """
    if type(peer) is not PublicReachability:
        raise TypeError('Validated public peer evidence required')
    now = datetime.now(timezone.utc) if now is None else now
    if type(now) is not datetime or now.utcoffset() is None:
        raise ValueError('Timezone-aware current time required')
    state = peer.status
    if state in {'reachable', 'busy_queued'}:
        expected_availability = 'available' if state == 'reachable' else 'busy'
        expected_reason = 'session_ready' if state == 'reachable' else 'session_busy_queue_verified'
        if (peer.valid_until is None or peer.valid_until <= now
                or peer.last_evidence_at is None or peer.last_evidence_at > now
                or peer.link_state != 'linked' or peer.transport_state != 'attached'
                or peer.evidence_level != 'runtime_verified'
                or peer.availability_state != expected_availability or peer.reason != expected_reason):
            state = 'unknown_stale'
    if state == 'reachable':
        return DeliveryGuidance(recipient_state='online', send_mode='notify_if_still_reachable',
                                response_expectation='may_respond')
    if state == 'busy_queued':
        return DeliveryGuidance(recipient_state='busy', send_mode='notify_if_still_reachable',
                                response_expectation='may_be_delayed')
    if state == 'unlinked':
        return DeliveryGuidance(recipient_state='unlinked', send_mode='link_first',
                                response_expectation='not_verified')
    return DeliveryGuidance(recipient_state='offline' if state == 'offline' else 'unknown',
                            send_mode='queue_only', response_expectation='not_verified')
