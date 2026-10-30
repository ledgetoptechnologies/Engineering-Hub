"""Optional, operator-configured snapshot-only staging observation connection."""
from pathlib import Path
import json

from .staging_client import StagingClient, StagingConnectionConfig, _read_file, _unique
from .repository import HubError, HubRepository


class StagingObserver:
    def __init__(self, config, *, test_transport=None):
        self.admin_actors = config.admin_actors
        self._client = StagingClient(config, test_transport=test_transport)

    async def runtime(self, actor):
        HubRepository._actor(actor, human=True)
        if actor not in self.admin_actors:
            raise HubError('staging_admin_required', 403)
        try:
            value = await self._client.runtime()
            return {'configured': True, 'state': 'ready', 'snapshot': value}
        except Exception:
            return {'configured': True, 'state': 'unavailable', 'snapshot': None}

    async def close(self):
        await self._client.aclose()


def compose_observer(path):
    """Absence, disabled, invalid files and unavailable credentials fail closed."""
    try:
        path = Path(path)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        data = json.loads(_read_file(path, 65536), object_pairs_hook=_unique)
        if (type(data) is not dict or set(data) != {'schema', 'enabled', 'origin', 'credential_file', 'ca_file', 'admin_actors'}
                or type(data['enabled']) is not bool or type(data['admin_actors']) is not list
                or not data['admin_actors'] or len(set(data['admin_actors'])) != len(data['admin_actors'])):
            return False
        config = StagingConnectionConfig(schema=data['schema'], enabled=data['enabled'], origin=data['origin'],
            credential_file=data['credential_file'], ca_file=data['ca_file'], admin_actors=frozenset(data['admin_actors']))
        if not config.enabled or not config.origin.startswith('https://'):
            return False
        return StagingObserver(config)
    except Exception:
        return False
