"""Redacted, short-lived transport observations, never runtime readiness."""
from datetime import datetime, timedelta, timezone
import math

from .repository import AGENTS


OBSERVATION_SECONDS = 30


def unavailable_connections():
    return {agent: dict(state='unavailable', source='embedded_bus', scope='this_server_process',
                        observed_at=None, valid_until=None, active_streams=None,
                        stale_streams=None,
                        inbox_subscriptions=None, subscribed=None,
                        last_stream_write_at=None, native_push_available=None)
            for agent in AGENTS}


def _timestamp(value):
    if type(value) is not str or len(value) > 64:
        raise ValueError('Invalid transport observation')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError('Invalid transport observation')
    return stamp


def project_bus_connections(snapshot, *, now=None):
    """Copy only closed public fields from the existing publisher snapshot.

    Any malformed source invalidates this whole observation. No cached copy,
    session identifiers, source exceptions or inferred offline state is exposed.
    """
    try:
        if (type(snapshot) is not dict or snapshot.get('scope') != 'this_server_process'
                or type(snapshot.get('native_push_available')) is not bool
                or type(snapshot.get('agents')) is not list or len(snapshot['agents']) != len(AGENTS)):
            raise ValueError()
        observed = _timestamp(snapshot['observed_at'])
        now = now or datetime.now(timezone.utc)
        if not -5 <= (now - observed).total_seconds() < OBSERVATION_SECONDS:
            raise ValueError()
        result = {}
        for item in snapshot['agents']:
            if type(item) is not dict or type(item.get('agent')) is not str:
                raise ValueError()
            agent = item['agent']
            streams, subscriptions, subscribed = (item['active_streams'], item['inbox_subscriptions'], item['subscribed'])
            stale_streams = item['stale_streams']
            valid_for = item['valid_for_seconds']
            if (agent not in AGENTS or agent in result
                    or type(streams) is not int or not 0 <= streams <= 2**31 - 1
                    or type(subscriptions) is not int or not 0 <= subscriptions <= 32
                    or subscriptions > streams
                    or type(stale_streams) is not int or not 0 <= stale_streams <= 2**31 - 1
                    or type(valid_for) not in (int, float) or not math.isfinite(valid_for)
                    or not 0 < valid_for <= OBSERVATION_SECONDS
                    or streams and (now - observed).total_seconds() >= valid_for
                    or type(subscribed) is not bool or subscribed != (subscriptions > 0)
                    or subscriptions and (not streams or not snapshot['native_push_available'])):
                raise ValueError()
            last_write = item['last_stream_write_at']
            if last_write is not None and (not streams or _timestamp(last_write) > observed):
                raise ValueError()
            result[agent] = dict(state='connected' if streams else ('stale' if stale_streams else 'not_observed'),
                                 source='embedded_bus', scope='this_server_process',
                                 observed_at=snapshot['observed_at'],
                                 valid_until=(observed + timedelta(seconds=valid_for)).isoformat().replace('+00:00', 'Z'),
                                 active_streams=streams, inbox_subscriptions=subscriptions,
                                 stale_streams=stale_streams,
                                 subscribed=subscribed, last_stream_write_at=last_write,
                                 native_push_available=snapshot['native_push_available'])
        return result
    except (ValueError, TypeError, KeyError, OverflowError):
        return unavailable_connections()
