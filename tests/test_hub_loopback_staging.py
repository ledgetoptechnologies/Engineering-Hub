"""Source-only loopback binding selection; no live authentication or router."""
from types import SimpleNamespace

import pytest
from engineering_hub import owned_inactive_hub as service


@pytest.mark.asyncio
async def test_configuration_loopback_ports_are_explicit_and_private(monkeypatch):
    deployment = SimpleNamespace(bus_paths={'test': 'value'}, validate_ledger_boundary=lambda: None)
    monkeypatch.setattr(service, 'load_deployment_config', lambda path: deployment)
    monkeypatch.setattr(service, 'Settings', SimpleNamespace(from_env=lambda value: 'settings'))
    calls = []
    async def capture(settings, **options):
        calls.append((settings, options))
    monkeypatch.setattr(service, 'serve_owned_inactive_hub', capture)
    await service.serve_configuration('/test/config', loopback_ports=(29070, 29071))
    assert calls == [('settings', {'bus_paths': deployment.bus_paths,
        'agent_host': '127.0.0.1', 'agent_port': 29070,
        'browser_host': '127.0.0.1', 'browser_port': 29071})]
    calls.clear()
    await service.serve_configuration('/test/config')
    assert calls == [('settings', {'bus_paths': deployment.bus_paths})]


@pytest.mark.asyncio
@pytest.mark.parametrize('ports', [(29070, 29070), (0, 29071), (65536, 29071),
                                   (True, 29071), [29070, 29071], (29070,)])
async def test_configuration_rejects_invalid_loopback_before_reading_config(monkeypatch, ports):
    monkeypatch.setattr(service, 'load_deployment_config', lambda path: pytest.fail('config opened'))
    with pytest.raises(service.OwnedInactiveHubError):
        await service.serve_configuration('/test/config', loopback_ports=ports)
