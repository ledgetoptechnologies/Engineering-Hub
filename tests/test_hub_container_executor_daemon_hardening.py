"""Review regressions using fake Engine responses, never a Docker connection."""
from tests import test_hub_container_executor_daemon_backend as fixtures
import os

import pytest
from hub_container_executor.daemon_backend import DaemonBackendError, DockerEngineBackend


def test_create_accepts_documented_empty_tmpfs_mount_mode(tmp_path, monkeypatch):
    original = fixtures.container_inspect

    def documented_inspect(*args, **kwargs):
        result = original(*args, **kwargs)
        for mount in result['Mounts']:
            if mount['Type'] == 'tmpfs':
                mount['Mode'] = ''
        return result

    monkeypatch.setattr(fixtures, 'container_inspect', documented_inspect)
    fixtures.test_create_uses_only_immutable_digest_and_exact_closed_profile(tmp_path)


def test_outside_hard_link_is_denied_before_create_post(tmp_path, monkeypatch):
    original_generation = fixtures.safe_generation
    original_json = DockerEngineBackend._json
    posts = []

    def linked_generation(root, generation_id='fixture-g1'):
        generation = original_generation(root, generation_id)
        outside = root / 'outside-state.sqlite3'
        outside.write_bytes(b'private fixture inode outside enrolled generation')
        os.link(outside, generation / 'linked-state.sqlite3')
        assert outside.stat().st_nlink == 2
        return generation

    def record_posts(self, method, route, *args, **kwargs):
        if method == 'POST':
            posts.append(route)
        return original_json(self, method, route, *args, **kwargs)

    monkeypatch.setattr(fixtures, 'safe_generation', linked_generation)
    monkeypatch.setattr(DockerEngineBackend, '_json', record_posts)
    with pytest.raises(DaemonBackendError, match='safely enrolled'):
        fixtures.test_create_uses_only_immutable_digest_and_exact_closed_profile(tmp_path)
    assert posts == []


@pytest.mark.parametrize('missing_option', ['noexec', 'nosuid'])
def test_empty_tmpfs_mode_does_not_bypass_host_security_options(tmp_path, monkeypatch, missing_option):
    original = fixtures.container_inspect

    def unsafe_options(*args, **kwargs):
        result = original(*args, **kwargs)
        for mount in result['Mounts']:
            if mount['Type'] == 'tmpfs':
                mount['Mode'] = ''
        options = result['HostConfig']['Tmpfs']['/tmp'].split(',')
        result['HostConfig']['Tmpfs']['/tmp'] = ','.join(o for o in options if o != missing_option)
        return result

    monkeypatch.setattr(fixtures, 'container_inspect', unsafe_options)
    with pytest.raises(DaemonBackendError, match='tmpfs options differ'):
        fixtures.test_create_uses_only_immutable_digest_and_exact_closed_profile(tmp_path)


@pytest.mark.parametrize('change', ['missing-nosuid', 'extra-exec', 'duplicate-noexec'])
def test_malformed_nonempty_tmpfs_mode_is_rejected(tmp_path, monkeypatch, change):
    original = fixtures.container_inspect

    def malformed_mode(*args, **kwargs):
        result = original(*args, **kwargs)
        mode = result['HostConfig']['Tmpfs']['/tmp']
        if change == 'missing-nosuid':
            mode = ','.join(token for token in mode.split(',') if token != 'nosuid')
        elif change == 'extra-exec':
            mode += ',exec'
        else:
            mode += ',noexec'
        for mount in result['Mounts']:
            if mount['Type'] == 'tmpfs':
                mount['Mode'] = mode
        return result

    monkeypatch.setattr(fixtures, 'container_inspect', malformed_mode)
    with pytest.raises(DaemonBackendError, match='scratch mount differs'):
        fixtures.test_create_uses_only_immutable_digest_and_exact_closed_profile(tmp_path)
