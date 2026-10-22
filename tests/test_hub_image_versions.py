"""Pure image comparison: no registry, Docker daemon, deployment or credentials."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from engineering_hub.image_versions import (
    ImageIdentity, ImageObservation, ImagePlatform, ImageVersionTarget,
    MAX_OBSERVATION_LIFETIME_SECONDS, compare_image_versions,
)

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
REPOSITORY = 'ghcr.io/ledgetoptechnologies/engineering-hub-control'
A, B, C = ('sha256:' + value * 64 for value in 'abc')
PLATFORM = ImagePlatform(os='linux', architecture='amd64')
TARGET = ImageVersionTarget(project='engineering-hub', host='nas', service='hub',
                            repository=REPOSITORY, reference='latest')


def observed(source, *, digest=A, kind='manifest', **changes):
    identity_changes = changes.pop('identity', {})
    data = {'repository': REPOSITORY, 'platform': PLATFORM, f'{kind}_digest': digest}
    data.update(identity_changes)
    return ImageObservation(identity=ImageIdentity(**data), source=source,
                            reference=changes.pop('reference', 'latest'),
                            observed_at=changes.pop('observed_at', NOW - timedelta(seconds=5)),
                            valid_until=changes.pop('valid_until', NOW + timedelta(minutes=5)),
                            **changes)


def compare(running=None, published=None, *, now=NOW):
    return compare_image_versions(TARGET, running, published, now=now)


@pytest.mark.parametrize('kind', ['config', 'manifest', 'index'])
@pytest.mark.parametrize('same', [True, False])
def test_same_kind_current_and_different_published_image(kind, same):
    status = compare(observed('runtime', kind=kind),
                     observed('registry', kind=kind, digest=A if same else B))
    if kind == 'index' and not same:
        assert status.state == 'unknown' and status.reason == 'identity_unverified'
        assert status.digest_kind is None and status.valid_until is None
        return
    assert status.state == ('up_to_date' if same else 'update_available')
    assert status.reason == ('same_image' if same else 'different_image')
    assert status.digest_kind == kind
    assert status.valid_until == NOW + timedelta(minutes=5)
    assert status.project == TARGET.project and status.environment == 'production'


def test_tag_or_revision_claim_alone_is_not_identity_proof():
    assert compare(observed('runtime', digest=None), observed('registry', digest=None)).reason == 'identity_unverified'
    with pytest.raises(ValidationError):
        ImageIdentity(repository=REPOSITORY, platform=PLATFORM, revision='latest')


def test_config_id_and_manifest_or_index_digest_are_not_comparable():
    for kind in ('manifest', 'index'):
        status = compare(observed('runtime', kind='config'), observed('registry', kind=kind))
        assert status.state == 'unknown' and status.reason == 'identity_unverified'
        assert status.current_digest is None and status.available_digest is None


def test_unchanged_platform_image_under_changed_index_is_up_to_date():
    status = compare(observed('runtime', identity={'index_digest': B}),
                     observed('registry', identity={'index_digest': C}))
    assert status.state == 'up_to_date' and status.digest_kind == 'manifest'


def test_index_only_change_for_another_architecture_cannot_claim_update():
    # Same declared linux/amd64 platform, but only index identities were
    # collected. A new arm64 child could explain the changed parent digest.
    status = compare(observed('runtime', kind='index'),
                     observed('registry', kind='index', digest=B))
    assert status.state == 'unknown' and status.reason == 'identity_unverified'
    assert status.current_digest is None and status.available_digest is None


def test_unchanged_config_under_changed_manifest_is_up_to_date():
    status = compare(observed('runtime', identity={'config_digest': C}),
                     observed('registry', digest=B, identity={'config_digest': C}))
    assert status.state == 'up_to_date' and status.digest_kind == 'config'


@pytest.mark.parametrize('parent', ['index', 'manifest'])
def test_equal_parent_with_conflicting_child_is_not_an_update(parent):
    status = compare(observed('runtime', kind='config', identity={f'{parent}_digest': C}),
                     observed('registry', kind='config', digest=B, identity={f'{parent}_digest': C}))
    assert status.state == 'unknown' and status.reason == 'identity_conflict'


@pytest.mark.parametrize('side', ['running', 'published'])
def test_expired_observation_fails_unknown(side):
    values = {'running': observed('runtime'), 'published': observed('registry')}
    values[side] = observed('runtime' if side == 'running' else 'registry',
                            observed_at=NOW - timedelta(minutes=2), valid_until=NOW)
    status = compare(**values)
    assert status.state == 'unknown' and status.reason == f'{side}_observation_expired'
    assert status.valid_until is None


@pytest.mark.parametrize('side', ['running', 'published'])
def test_future_observation_fails_unknown(side):
    values = {'running': observed('runtime'), 'published': observed('registry')}
    values[side] = observed('runtime' if side == 'running' else 'registry',
                            observed_at=NOW + timedelta(minutes=2))
    assert compare(**values).reason == f'{side}_observation_in_future'


def test_small_clock_skew_and_timezone_offsets_are_comparable():
    running = observed('runtime', observed_at=NOW + timedelta(seconds=10))
    status = compare(running, observed('registry'), now=NOW.astimezone(timezone(timedelta(hours=-5))))
    assert status.state == 'up_to_date' and status.checked_at.tzinfo == timezone.utc


def test_repository_and_reference_must_match_configured_release_track():
    assert compare(observed('runtime', identity={'repository': 'ghcr.io/other/hub'}),
                   observed('registry')).reason == 'repository_mismatch'
    assert compare(observed('runtime'), observed('registry', reference='dev')).reason == 'reference_mismatch'
    # A runtime's original mutable tag is not used to decide image equality.
    assert compare(observed('runtime', reference='old-tag'), observed('registry')).state == 'up_to_date'


@pytest.mark.parametrize('kind', ['index', 'manifest'])
def test_immutable_release_reference_must_name_published_manifest_chain(kind):
    target = ImageVersionTarget(**(TARGET.model_dump() | {'reference': A}))
    status = compare_image_versions(target, observed('runtime', kind=kind),
        observed('registry', kind=kind, reference=A), now=NOW)
    assert status.state == 'up_to_date'
    wrong = compare_image_versions(target, observed('runtime', kind=kind, digest=B),
        observed('registry', kind=kind, digest=B, reference=A), now=NOW)
    assert wrong.state == 'unknown' and wrong.reason == 'identity_conflict'


def test_config_digest_cannot_substitute_for_registry_manifest_reference():
    target = ImageVersionTarget(**(TARGET.model_dump() | {'reference': A}))
    status = compare_image_versions(target, observed('runtime', kind='config'),
        observed('registry', kind='config', reference=A), now=NOW)
    assert status.state == 'unknown' and status.reason == 'identity_conflict'


def test_platform_identity_and_variant_are_required_for_safe_comparison():
    assert compare(observed('runtime', identity={'platform': None}),
                   observed('registry')).reason == 'platform_unverified'
    for platform in (ImagePlatform(os='linux', architecture='arm64'),
                     ImagePlatform(os='linux', architecture='amd64', variant='v2')):
        assert compare(observed('runtime', identity={'platform': platform}),
                       observed('registry')).reason == 'platform_mismatch'


def test_missing_and_wrong_source_observations_are_not_updates():
    assert compare(None, observed('registry')).reason == 'running_not_observed'
    assert compare(observed('runtime'), None).reason == 'published_not_observed'
    assert compare(observed('registry'), observed('registry')).reason == 'observation_source_mismatch'
    assert compare(observed('runtime'), observed('runtime')).reason == 'observation_source_mismatch'


@pytest.mark.parametrize('field,value', [
    ('repository', 'https://ghcr.io/owner/image'), ('repository', 'ghcr.io/owner/image:latest'),
    ('repository', 'ghcr.io/owner/../image'), ('repository', 'ghcr.io/owner/image@sha256:' + 'a' * 64),
    ('host', 'user@host'), ('service', 'x\nsecret'), ('reference', 'latest/../../secret'),
    ('reference', 'a' * 129), ('project', ''),
])
def test_target_metadata_is_bounded_and_not_a_connection_url(field, value):
    with pytest.raises(ValidationError):
        ImageVersionTarget(**(TARGET.model_dump() | {field: value}))


@pytest.mark.parametrize('digest', ['latest', 'sha256:' + 'A' * 64, 'sha256:' + 'a' * 63, 'sha512:' + 'a' * 64])
def test_only_canonical_digests_are_evidence(digest):
    with pytest.raises(ValidationError):
        ImageIdentity(repository=REPOSITORY, manifest_digest=digest)


def test_invalid_time_intervals_and_naive_dates_are_rejected():
    for changes in ({'valid_until': NOW - timedelta(seconds=5)},
                    {'valid_until': NOW + timedelta(seconds=MAX_OBSERVATION_LIFETIME_SECONDS)},
                    {'observed_at': NOW.replace(tzinfo=None)}):
        with pytest.raises(ValidationError):
            observed('runtime', **changes)
    with pytest.raises(ValueError):
        compare(observed('runtime'), observed('registry'), now=NOW.replace(tzinfo=None))


def test_extra_fields_and_unvalidated_mappings_are_rejected():
    with pytest.raises(ValidationError):
        observed('runtime', token='must-not-be-accepted')
    with pytest.raises(TypeError):
        compare({}, observed('registry'))
    with pytest.raises(TypeError):
        compare_image_versions({}, None, None, now=NOW)


def test_models_are_immutable_and_output_contains_no_execution_authority():
    status = compare(observed('runtime'), observed('registry', digest=B))
    with pytest.raises(ValidationError):
        status.state = 'up_to_date'
    assert set(status.model_dump()) == {
        'project', 'host', 'service', 'environment', 'repository', 'reference', 'state', 'reason',
        'checked_at', 'running_observed_at', 'published_observed_at', 'valid_until',
        'digest_kind', 'current_digest', 'available_digest',
    }
