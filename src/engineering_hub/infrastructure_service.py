"""Explicitly composed infrastructure adapter; no DB discovery or host probes."""
import inspect

from .infrastructure import InfrastructureRegistry, InfrastructurePrincipal
from .repository import AGENTS, HubError, HubRepository


class InfrastructureService:
    def __init__(self, registry, *, on_change=None):
        if type(registry) is not InfrastructureRegistry:
            raise TypeError('An explicitly configured InfrastructureRegistry is required')
        if on_change is not None and (not callable(on_change) or inspect.iscoroutinefunction(on_change)):
            raise TypeError('on_change must be a synchronous callback')
        self.registry, self.on_change = registry, on_change
        self.invalidation_failed = False

    @staticmethod
    def principal(actor):
        if type(actor) is not str:
            raise HubError('forbidden',403)
        HubRepository._actor(actor)
        return InfrastructurePrincipal('agent' if actor in AGENTS else 'human', actor)

    def require(self, actor, action='read'):
        principal = self.principal(actor)
        self.registry._authorize(principal, action)
        return principal

    def capabilities(self, actor):
        principal = self.require(actor)
        allowed = self.registry.capabilities.get(principal, frozenset())
        return {'device_write': principal.kind == 'human' and 'device_write' in allowed,
                'maintenance_write': 'maintenance_write' in allowed}

    def _changed(self, device_id):
        if self.on_change is not None:
            try:
                self.on_change({'kind': 'infrastructure.changed', 'device_id': device_id})
            except Exception:
                # The mutation is committed. An invalidation failure cannot
                # truthfully turn it into an unsaved write or roll it back.
                self.invalidation_failed = True

    def _device(self, principal, item):
        history = self.registry.maintenance_history(principal, item['id'])
        probes = self.registry.probe_states(principal, item['id'])['items']
        return {key: item[key] for key in ('id','name','hostname','addresses','network','vlan',
                                         'role','retired','revision','updated_at')} | {
            'observations': [{'probe_id': probe['probe_id'], 'source_label': probe['source_label'],
                              'state': {'healthy':'ok','unhealthy':'failed','unknown':'unknown','stale':'stale'}[probe['status']],
                              'observed_at': probe['observed_at'], 'valid_until': probe['valid_until']}
                             for probe in probes],
            'open_maintenance': next((entry for entry in history['items'] if entry['state']=='open'),None)}

    def list_devices(self, actor, *, include_retired=False):
        principal = self.require(actor)
        with self.registry.lock:
            items = self.registry.list_devices(principal, include_retired=include_retired)['items']
            return {'items': [self._device(principal,item) for item in items], 'capabilities': self.capabilities(actor)}

    def get_device(self, actor, device_id):
        principal = self.require(actor)
        with self.registry.lock:
            item = self.registry.get_device(principal,device_id)
            return {'device': self._device(principal,item),
                    'maintenance': self.registry.maintenance_history(principal,device_id),
                    'capabilities': self.capabilities(actor)}

    def upsert_device(self, actor, device, *, expected_revision):
        principal = self.require(actor,'device_write')
        item = self.registry.upsert_device(principal,device,expected_revision=expected_revision)
        self._changed(item['id'])
        return self._device(principal,item)

    def append_maintenance(self, actor, device_id, reason, client_request_id, *, expected_end=None):
        principal = self.require(actor,'maintenance_write')
        with self.registry.lock:
            before = self.registry.db.total_changes
            item = self.registry.append_maintenance(principal,device_id,reason,client_request_id,expected_end=expected_end)
            changed = self.registry.db.total_changes != before
        if changed:
            self._changed(item['device_id'])
        return item

    def resolve_maintenance(self, actor, maintenance_id, *, expected_revision, resolution):
        principal = self.require(actor,'maintenance_write')
        item = self.registry.resolve_maintenance(principal,maintenance_id,expected_revision=expected_revision,resolution=resolution)
        self._changed(item['device_id'])
        return item

    def record_probe(self, actor, device_id, status, *, observed_at, valid_until, probe_id, source_label=None):
        principal = self.require(actor,'probe_write')
        self.registry.record_probe(principal,device_id,status,observed_at=observed_at,valid_until=valid_until,
                                   probe_id=probe_id,source_label=source_label)
        self._changed(device_id)
        return self.get_device(actor,device_id)
