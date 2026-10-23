"""Fixed composition + actual signed relay + owned SQLite audit, synthetic host.

UID0 is only for the fixture's private journal. Real Engine/kernel checks are
explicitly bypassed by the reused issuer fixture; no launch/NAS proof is claimed.
"""
import asyncio
from dataclasses import replace
import json
import os
import time

import pytest

pytest.importorskip('fcntl', reason='Linux fixture ownership')

from agent_message_bus.auth import AuthSettings
from engineering_hub.app import Settings, create_app
from engineering_hub.auth import AuthConfig
from engineering_hub.container_pilot import ContainerPilotTarget, ContainerPilotWorkflow
from engineering_hub.events import NativeHubEventFeed
from engineering_hub.pilot_ownership import PilotOwner
from engineering_hub.queued_pilot import QueuedContainerPilot
from engineering_hub.queued_pilot_runtime import QueuedPilotRuntime
from engineering_hub.repository import HubRepository
from engineering_hub.trusted_queued_session import parse_enrollment, TrustedQueuedSessionComposition
from engineering_hub.unified_pilot_startup import parse_profile
from engineering_hub.work_queue import WorkAction, WorkQueue
from hub_container_executor.asgi import create_app as executor_app
from hub_container_executor.launch_transport import UnixCurrentObservationTransport
from tests.test_hub_queued_pilot import DistinctContainerClient, ACTION, MACHINE
from tests.test_hub_unified_pilot_startup import profile_document, encode
from tests.test_hub_trusted_queued_session import document
from tests.test_hub_current_observation_end_to_end import owned_supervisor_wait, request, NoCommands
from tests.test_hub_current_executor_transport import material, server, TOKEN
from tests.test_hub_container_executor_launch_supervisor import (
    short_root, root_journal, synthetic_issuer, current_ready, current_pins,
)


def test_fixed_composition_actual_signed_path_stays_pending_until_manual_acceptance(synthetic_issuer, material, monkeypatch):
    import engineering_hub.pilot_ownership as ownership
    # Reused journal fixture's explicitly synthetic filesystem hook accepts only
    # fd; adapt its signature for the additional real owned Hub repository.
    observed_filesystem = ownership._local_filesystem
    monkeypatch.setattr(ownership, '_local_filesystem', lambda fd, **_: observed_filesystem(fd))
    authority, issuer_path, checks = synthetic_issuer
    profile_data = profile_document()
    manifest = parse_profile(encode(profile_data)).manifest
    # Configure synthetic issuer BEFORE retaining independent pins or sending
    # requests. No pin is learned from a signed network response.
    authority.release = replace(authority.release,
        implementation_commit=manifest.driver.implementation_commit,
        platform=manifest.runner_binding.platform)
    authority._daemon_pin = replace(authority._daemon_pin,
        daemon_id=manifest.runner_binding.daemon_identity)
    current_ready(authority)
    release, launch = current_pins(authority)
    relay = UnixCurrentObservationTransport(socket_path=str(issuer_path),
        expected_issuer_uid=os.geteuid(), expected_issuer_gid=os.getegid())
    no_commands = NoCommands()
    executor = executor_app(no_commands, bearer_token=TOKEN.decode(), current_observation_transport=relay)
    def respond(conn, _):
        header, body = seen[-1]
        with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
            events = runner.run(request(executor, header, body))
        start, end = events
        assert start['status'] == 200
        conn.sendall(b'HTTP/1.1 200 OK\r\n' + b'\r\n'.join(
            key + b': ' + value for key, value in start['headers']) + b'\r\n\r\n' + end['body'])
    composition = None
    try:
        with owned_supervisor_wait(authority), server(material, respond) as (origin, seen):
            profile_data['executor'] = {'https_origin': origin,
                **{name: str(material[name]) if name.endswith('_file') else material[name]
                   for name in ('ca_file', 'ca_sha256', 'credential_file', 'credential_sha256')}}
            profile = parse_profile(encode(profile_data))
            data = document()
            data['release'] = {name: getattr(release, name)
                for name in release.__dataclass_fields__ if name not in {'public_key', 'platform'}}
            data['release'].update(public_key_hex=release.public_key.hex(), platform=release.platform.model_dump(mode='json'))
            data['launch'] = {name: getattr(launch, name) for name in launch.__dataclass_fields__}
            data['report']['executor'] = dict(data['launch'], image_config_id=release.image_config_id)
            data['report']['expires_at_ms'] = launch.approval_expires_at_ms
            database = issuer_path.parent / 'hub.sqlite3'
            with PilotOwner(database, allow_ephemeral_test_storage=True) as owner:
                composition = TrustedQueuedSessionComposition(profile=profile, database=database,
                    enrollment=parse_enrollment(encode(data)),
                    ca_pem=material['ca_file'].read_bytes(), credential=TOKEN)
                target = ContainerPilotTarget.from_manifest(profile.manifest)
                gate = composition.reconciliation_factory(owner, target)
                repo = HubRepository(database, execution_principal='executor:container-pilot', pilot_owner=owner)
                client = DistinctContainerClient()
                workflow = ContainerPilotWorkflow(repo, client, [target], pilot_owner=owner)
                queue = WorkQueue(repo, {ACTION: WorkAction(MACHINE, project='engineering-hub')})
                bridge = QueuedContainerPilot(queue, workflow, action_key=ACTION, machine_id=MACHINE,
                    reconciliation=gate)
                feed = NativeHubEventFeed(database)
                runtime = QueuedPilotRuntime(bridge, feed)
                config = AuthConfig('https://hub.example', human_auth_mode='cloudflare_access',
                    cloudflare_issuer='https://fixture.cloudflareaccess.com', cloudflare_audience='fixture',
                    cloudflare_allowed_emails=('beau@example.test',))
                core = create_app(Settings(database, AuthSettings('h', 'c', claude_token='cl'), config,
                    staging_machine_id=MACHINE), repository=repo, event_feed=feed,
                    work_queue=queue, queued_pilot_runtime=runtime)
                broker = composition.session_acceptance_factory(core, owner, repo, target, gate)
                assert gate.admission_state == 'pending' and client.submissions == []
                actor = data['operator_actor']
                snapshot = broker.snapshot(actor=actor)
                broker.decide(actor=actor, request_id=snapshot['request_id'],
                    expected_revision=snapshot['revision'], action='accept')
                assert gate.admission_state == 'accepted' and client.submissions == []
                with repo._connection() as db:
                    assert db.execute('SELECT count(*) FROM hub_session_acceptance_audit').fetchone()[0] == 2
                assert checks and seen and not no_commands.touched
                composition.close()
    finally:
        if composition is not None:
            composition.close()
        relay.close()
