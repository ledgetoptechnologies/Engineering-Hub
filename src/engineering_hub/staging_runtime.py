"""Bounded observation contract; inventory never grants execution authority."""
from datetime import datetime, timezone
import re


def validate_runtime_snapshot(value, *, now=None):
    now = now or datetime.now(timezone.utc)

    def require(ok):
        if not ok:
            raise ValueError('invalid_runtime_snapshot')

    def text(item, maximum):
        require(type(item) is str and 0 < len(item) <= maximum
                and all(ord(char) >= 32 and ord(char) != 127 for char in item))

    def timestamp(item, *, fresh=False):
        text(item, 64)
        try:
            instant = datetime.fromisoformat(item.replace('Z', '+00:00'))
            require(instant.tzinfo is not None)
            age = (now - instant).total_seconds()
            require(age >= -10 and (not fresh or age <= 90))
        except (TypeError, OverflowError, ValueError):
            raise ValueError('invalid_runtime_snapshot') from None

    require(type(value) is dict and set(value) == {
        'schema', 'observed_at', 'sources', 'containers', 'services'})
    require(value['schema'] == 'hub-staging-runtime.v1')
    timestamp(value['observed_at'], fresh=True)
    require(type(value['sources']) is list and 1 <= len(value['sources']) <= 2)
    states = {}
    for source in value['sources']:
        require(type(source) is dict and set(source) == {'id', 'state', 'observed_at'})
        require(type(source['id']) is str and source['id'] in {'worker', 'host'} and source['id'] not in states
                and type(source['state']) is str
                and source['state'] in {'observed', 'unavailable', 'stale', 'not_configured'})
        if source['state'] == 'observed':
            timestamp(source['observed_at'], fresh=True)
        elif source['observed_at'] is not None:
            timestamp(source['observed_at'])
        states[source['id']] = source['state']
    require('worker' in states)
    require(type(value['containers']) is list and len(value['containers']) <= 256)
    identities = set()
    for item in value['containers']:
        require(type(item) is dict and set(item) == {'id', 'name', 'image', 'state', 'scope', 'managed'})
        require(type(item['id']) is str and re.fullmatch('[a-f0-9]{64}', item['id']) is not None)
        require(type(item['scope']) is str and item['scope'] in states and states[item['scope']] == 'observed'
                and item['managed'] is False)
        identity = (item['scope'], item['id'])
        require(identity not in identities)
        identities.add(identity)
        text(item['name'], 128)
        text(item['image'], 512)
        require(type(item['state']) is str and item['state'] in {
            'running', 'paused', 'restarting', 'created', 'exited', 'dead', 'removing'})
    require(type(value['services']) is list and len(value['services']) <= 256)
    units = set()
    for item in value['services']:
        require(type(item) is dict and set(item) == {'name', 'state', 'scope'})
        require(item['scope'] == 'host' and states.get('host') == 'observed' and item['state'] == 'running')
        text(item['name'], 256)
        require(re.fullmatch(r'[A-Za-z0-9_.@:\\-]+\.service', item['name']) is not None
                and item['name'] not in units)
        units.add(item['name'])
    return value
