"""Per-device Prometheus host mapping and the owner's Grafana link."""
import json
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from agent_message_bus.agent_installation_scope import InstallationScopeRepository
from agent_message_bus.agent_sessions import AgentSessionRepository
from agent_message_bus.agent_sessions_config import AgentSessionsConfig
from agent_message_bus.auth import AuthSettings
from engineering_hub.infrastructure import CAPABILITIES, InfrastructurePrincipal, InfrastructureRegistry
from engineering_hub.infrastructure_api import attach_infrastructure_routes
from engineering_hub.infrastructure_discovery import normalize_targets
from engineering_hub.infrastructure_discovery_metrics import DiscoveryMetricsReader
from engineering_hub.infrastructure_metrics_access import MetricsAccess
from engineering_hub.infrastructure_module import grafana_host_url
from engineering_hub.infrastructure_service import InfrastructureService
from engineering_hub.private_gateway import browser_route_allowed
from engineering_hub.repository import HubError


OWNER = 'human:42'
OWNER_HEADERS = {'X-Fixture-Human': OWNER}
GRAFANA = 'http://192.0.2.80:3000/d/node/machine?var-host={host}'


@pytest.fixture
def setup(tmp_path):
    sessions = AgentSessionRepository(tmp_path / 'sessions.sqlite3', OWNER)
    scopes = InstallationScopeRepository(sessions)
    registry = InfrastructureRegistry(sqlite3.connect(':memory:', check_same_thread=False),
        capabilities={InfrastructurePrincipal('human', OWNER): CAPABILITIES})
    service = InfrastructureService(registry)
    auth = AuthSettings('synthetic-hermes', 'synthetic-codex', allowed_ips=frozenset({'192.0.2.50'}))
    config = AgentSessionsConfig(tmp_path / 'sessions.sqlite3', OWNER, 'https://hub.test')

    def human(request, *, mutation=False):
        if request.headers.get('x-fixture-human') != OWNER:
            raise HTTPException(403)
        return OWNER

    access = MetricsAccess(service, auth, human, owner=OWNER, scopes=scopes, connection_config=config)
    yield service, access, registry
    registry.close()


def owner_request():
    return Request({'type': 'http', 'method': 'GET', 'path': '/api/hub/v1/infrastructure/metrics',
                    'headers': [(b'host', b'hub.test'), (b'x-fixture-human', OWNER.encode())],
                    'client': ('192.0.2.50', 5000), 'query_string': b''})


def device(**over):
    return {'id': 'beast', 'name': 'Beast', 'addresses': [], 'role': 'Workstation', **over}


def test_metrics_host_is_stored_exactly_and_validated(setup):
    service, _, _ = setup
    saved = service.upsert_device(OWNER, device(hostname='beast.lan', metrics_host='BEAST'), expected_revision=0)
    assert saved['metrics_host'] == 'BEAST'  # case kept: Prometheus labels match exactly
    assert saved['hostname'] == 'beast.lan'
    cleared = service.upsert_device(OWNER, device(hostname='beast.lan'), expected_revision=1)
    assert cleared['metrics_host'] is None
    for bad in ('', ' BEAST', 'beast host', 'a' * 65, '-beast', 'beast/1', 7):
        with pytest.raises(HubError, match='invalid_device_metrics_host'):
            service.upsert_device(OWNER, device(metrics_host=bad), expected_revision=2)


def test_device_recorded_before_the_field_reads_as_none(setup):
    service, _, registry = setup
    service.upsert_device(OWNER, device(hostname='beast'), expected_revision=0)
    with registry.lock:
        data = json.loads(registry.db.execute("SELECT data FROM infra_devices WHERE id='beast'").fetchone()[0])
        data.pop('metrics_host')
        registry.db.execute("UPDATE infra_devices SET data=? WHERE id='beast'", (json.dumps(data),))
    listed = service.list_devices(OWNER)['items'][0]
    assert listed['metrics_host'] is None and listed['hostname'] == 'beast'


def test_inventory_uses_the_prometheus_host_instead_of_the_hostname(setup):
    service, access, _ = setup
    service.upsert_device(OWNER, device(hostname='beast', metrics_host='BEAST'), expected_revision=0)
    service.upsert_device(OWNER, device(id='nas', name='NAS', hostname='nas'), expected_revision=0)
    inventory = access.inventory(owner_request())
    assert {'device_id': 'beast', 'hostnames': ['BEAST']} in inventory
    assert {'device_id': 'nas', 'hostnames': ['nas']} in inventory
    targets = [{'labels': {'job': 'windows', 'instance': '192.0.2.5:9182', 'host': 'BEAST'}},
               {'labels': {'job': 'node', 'instance': '192.0.2.6:9100', 'host': 'beast'}}]
    rows = {row['host_label']: row for row in normalize_targets(targets, inventory)['rows']}
    # Only the explicit name matches; the lowercase hostname no longer claims a second machine.
    assert rows['BEAST']['association'] == 'matched' and rows['BEAST']['device_id'] == 'beast'
    assert rows['beast']['association'] == 'unregistered' and rows['beast']['device_id'] is None


def test_two_devices_claiming_one_host_stay_ambiguous(setup):
    service, access, _ = setup
    service.upsert_device(OWNER, device(metrics_host='BEAST'), expected_revision=0)
    service.upsert_device(OWNER, device(id='other', name='Other', hostname='beast2', metrics_host='BEAST'), expected_revision=0)
    targets = [{'labels': {'job': 'windows', 'instance': '192.0.2.5:9182', 'host': 'BEAST'}}]
    row = normalize_targets(targets, access.inventory(owner_request()))['rows'][0]
    assert row['association'] == 'ambiguous' and row['device_id'] is None


def test_device_api_accepts_and_returns_metrics_host(setup):
    service, _, _ = setup
    app = FastAPI()
    attach_infrastructure_routes(app, service, lambda request, mutation=False: OWNER)
    with TestClient(app) as client:
        body = {'name': 'Beast', 'hostname': 'beast', 'metrics_host': 'BEAST', 'addresses': [], 'network': None,
                'vlan': None, 'role': 'Workstation', 'retired': False, 'expected_revision': 0}
        saved = client.put('/api/hub/v1/infrastructure/devices/beast', json=body,
                           headers={'Authorization': 'Bearer x'})
        assert saved.status_code == 200, saved.text
        assert saved.json()['metrics_host'] == 'BEAST'
        assert client.get('/api/hub/v1/infrastructure/devices/beast').json()['device']['metrics_host'] == 'BEAST'


def links_app(service, access, *, metrics=True, grafana=None):
    reader = DiscoveryMetricsReader(service, None, authorize=access.authorize, inventory=access.inventory) if metrics else None
    app = FastAPI()

    def metrics_identity(request):
        access.authorize(request)
        return request
    attach_infrastructure_routes(app, service, lambda request, mutation=False: OWNER, metrics=reader,
                                 metrics_identity=metrics_identity, grafana_host_url=grafana)
    return app


def test_links_route_returns_the_configured_template_only_to_admitted_readers(setup):
    service, access, _ = setup
    links = '/api/hub/v1/infrastructure/metrics/links'
    with TestClient(links_app(service, access, grafana=GRAFANA)) as client:
        assert client.get(links, headers=OWNER_HEADERS).json() == {'schema_version': 1, 'grafana_host_url': GRAFANA}
        assert client.get(links, headers={'X-Fixture-Human': 'human:other'}).status_code == 403
        with pytest.raises(HubError, match='invalid_infrastructure_filter'):
            client.get(links, params={'host': 'x'}, headers=OWNER_HEADERS)
    with TestClient(links_app(service, access)) as client:
        assert client.get(links, headers=OWNER_HEADERS).json() == {'schema_version': 1, 'grafana_host_url': None}
    with TestClient(links_app(service, access, metrics=False)) as client:
        with pytest.raises(HubError, match='infrastructure_metrics_not_configured'):
            client.get(links, headers=OWNER_HEADERS)


def test_grafana_template_validation():
    for good in (GRAFANA, 'https://grafana.example.test/d/abc?var-host={host}&from=now-24h',
                 'https://grafana.example.test/d/abc/{host}'):
        assert grafana_host_url(good) == good
    for bad in ('https://grafana.example.test/d/abc',                    # no {host}
                'https://grafana.example.test/{host}/{host}',            # twice
                'https://{host}.example.test/d/abc',                     # would pick the server
                'https://user:pass@grafana.example.test/d?h={host}',     # credentials
                'javascript:alert(1)//{host}', 'ftp://grafana.example.test/{host}',
                'https://grafana.example.test/d?h={host}#{x}', ' https://grafana.example.test/{host}',
                'https://grafana.example.test/d#{host}', 'https://grafana.example.test/' + 'a' * 600 + '{host}', None, 3):
        with pytest.raises(ValueError):
            grafana_host_url(bad)


def test_gateway_allows_only_reads_of_the_links_route():
    assert browser_route_allowed('GET', '/api/hub/v1/infrastructure/metrics/links')
    assert not browser_route_allowed('POST', '/api/hub/v1/infrastructure/metrics/links')
    assert not browser_route_allowed('GET', '/api/hub/v1/infrastructure/metrics/links/extra')
