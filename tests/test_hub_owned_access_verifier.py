"""Linux service composition only; no worker, Access or Docker effects."""
import pytest

pytest.importorskip('fcntl')

from engineering_hub.cloudflare_access import CloudflareAccessConfig, CloudflareAccessVerifier
from tests.test_hub_queued_pilot_service import setup, options, start


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['callback', 'subclass', 'config'])
async def test_invalid_owned_verifier_refuses_before_owner_or_listener(setup, monkeypatch, invalid):
    state = setup
    # The legacy fixture replaces this constructor with a lambda. This test
    # exercises the exact production type and its explicit composition seam.
    monkeypatch.setattr('engineering_hub.cloudflare_access.CloudflareAccessVerifier', CloudflareAccessVerifier)
    config = state.settings.auth_config.cloudflare_config
    if invalid == 'callback':
        access = lambda _: True
    elif invalid == 'subclass':
        class Substitute(CloudflareAccessVerifier):
            pass
        access = Substitute(config)
    else:
        access = CloudflareAccessVerifier(CloudflareAccessConfig(
            config.issuer, 'other-audience', config.allowed_emails))
    with pytest.raises(ValueError, match='explicit_access_verifier_binding_required'):
        await state.service.serve_owned_queued_pilot(state.settings, state.manifest,
            state.client_factory, access_verifier=access, **options(state))
    assert not state.owners and not state.clients
    assert not state.settings.database.exists() and not state.endpoint.exists()


@pytest.mark.asyncio
async def test_owned_factory_forwards_the_same_verifier_and_drains(setup, monkeypatch):
    state = setup
    monkeypatch.setattr('engineering_hub.cloudflare_access.CloudflareAccessVerifier', CloudflareAccessVerifier)
    access = CloudflareAccessVerifier(state.settings.auth_config.cloudflare_config)
    captured = []
    original = state.service.create_app

    def create(*args, **kwargs):
        captured.append(kwargs.get('access_verifier'))
        return original(*args, **kwargs)

    monkeypatch.setattr(state.service, 'create_app', create)
    stop, task, _ = await start(state, access_verifier=access)
    try:
        assert captured == [access]
    finally:
        stop.set()
        await task
    assert state.owners[0].close_count == 1
