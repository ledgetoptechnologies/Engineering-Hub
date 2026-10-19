from datetime import datetime, timedelta, timezone

import pytest

from engineering_hub.delivery_guidance import topic_delivery_guidance
from engineering_hub.reachability import PublicReachability


NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def peer(status='reachable', **changes):
    values = dict(topic='topic-one', agent='hermes', status=status,
                  reason='session_ready' if status == 'reachable' else 'session_busy_queue_verified',
                  last_evidence_at=NOW - timedelta(seconds=1), valid_until=NOW + timedelta(seconds=1),
                  link_state='linked', transport_state='attached', availability_state='available'
                  if status == 'reachable' else 'busy', evidence_level='runtime_verified')
    return PublicReachability(**(values | changes))


@pytest.mark.parametrize('status,state,expectation', [
    ('reachable', 'online', 'may_respond'), ('busy_queued', 'busy', 'may_be_delayed')])
def test_fresh_runtime_evidence_can_suggest_notification_but_not_receipt(status, state, expectation):
    result = topic_delivery_guidance(peer(status), now=NOW)
    assert result.recipient_state == state
    assert result.send_mode == 'notify_if_still_reachable'
    assert result.response_expectation == expectation
    assert result.durable_delivery_required is True and result.receipt_required is True


@pytest.mark.parametrize('change', [
    {'valid_until': NOW}, {'valid_until': NOW - timedelta(microseconds=1)},
    {'valid_until': None}, {'last_evidence_at': None},
    {'last_evidence_at': NOW + timedelta(microseconds=1)},
    {'evidence_level': 'unverified'}, {'transport_state': 'unverified'},
    {'link_state': 'unknown'}, {'availability_state': 'unknown'}, {'reason': 'stream_only'},
])
@pytest.mark.parametrize('status', ['reachable', 'busy_queued'])
def test_expired_or_incoherent_green_never_suggests_live_notification(status, change):
    result = topic_delivery_guidance(peer(status, **change), now=NOW)
    assert result.recipient_state == 'unknown' and result.send_mode == 'queue_only'
    assert result.response_expectation == 'not_verified'


@pytest.mark.parametrize('status,reason,state,mode', [
    ('unlinked', 'no_exact_binding', 'unlinked', 'link_first'),
    ('linked_unverified', 'stream_only', 'unknown', 'queue_only'),
    ('offline', 'session_disconnected', 'offline', 'queue_only'),
    ('unknown_stale', 'observer_unavailable', 'unknown', 'queue_only'),
    ('unknown_stale', 'binding_generation_mismatch', 'unknown', 'queue_only'),
])
def test_unverified_or_disconnected_routes_preserve_durable_mail_and_receipt_requirement(status, reason, state, mode):
    value = PublicReachability(topic='other-topic', agent='claude', status=status, reason=reason)
    result = topic_delivery_guidance(value, now=NOW)
    assert (result.recipient_state, result.send_mode) == (state, mode)
    assert result.durable_delivery_required is True and result.receipt_required is True


def test_advice_expires_without_mutating_or_refreshing_the_evidence():
    value = peer()
    before = value.model_dump()
    assert topic_delivery_guidance(value, now=NOW).recipient_state == 'online'
    assert topic_delivery_guidance(value, now=NOW + timedelta(seconds=1)).recipient_state == 'unknown'
    assert value.model_dump() == before
    with pytest.raises(ValueError):
        topic_delivery_guidance(value, now=NOW.replace(tzinfo=None))
    with pytest.raises(TypeError):
        topic_delivery_guidance(before, now=NOW)
