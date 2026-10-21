"""Pure acyclic commitment vectors; no file, Engine or receipt authority."""
from copy import deepcopy
import hashlib
import json

import pytest

pytest.importorskip('fcntl', reason='Installed executor parser uses Linux providers')

from hub_container_executor.configuration_commitment import (
    DOMAIN, SELF_REFERENCE_MARKER, configuration_commitment,
)
from hub_container_executor.installed_runner import InstalledRunnerError
from tests.test_hub_container_executor_installed_runner import configuration, canonical, changed
from tests.test_hub_container_executor_launch_supervisor import profile_data
from hub_container_executor.launch_supervisor import ExecutorLaunchProfile


def test_one_valid_runner_profile_is_constructed_as_finite_dag():
    runner = configuration()
    provisional_raw = canonical(runner)
    before = configuration_commitment(provisional_raw)
    profile = profile_data()
    next(item for item in profile['mounts'] if item['role'] == 'config')['content_digest'] = before.content_digest
    enrolled_profile = ExecutorLaunchProfile.model_validate(profile)
    runner['release_enrollment']['launch_profile_digest'] = enrolled_profile.digest()
    final_raw = canonical(runner)
    final = configuration_commitment(final_raw)
    root_policy = {'schema': 'executor_launch_supervisor_v2',
                   'configuration_raw_digest': final.raw_digest,
                   'profile_digest': enrolled_profile.digest()}
    assert before.content_digest == final.content_digest
    assert before.raw_digest != final.raw_digest
    assert final.release.launch_profile_digest == root_policy['profile_digest']
    assert final.raw_digest == 'sha256:' + hashlib.sha256(final_raw).hexdigest()
    normalized = deepcopy(runner)
    normalized['release_enrollment']['launch_profile_digest'] = SELF_REFERENCE_MARKER
    assert final.content_digest == 'sha256:' + hashlib.sha256(DOMAIN + canonical(normalized)).hexdigest()
    assert final.content_digest != 'sha256:' + hashlib.sha256(canonical(normalized)).hexdigest()
    assert runner['release_enrollment']['launch_profile_digest'] == enrolled_profile.digest()


@pytest.mark.parametrize('path,value', [
    ('release_enrollment.launch_profile_digest', 'sha256:' + 'b' * 64),
    ('exchange_timeout_ms', 1001), ('resources.journal_create', False),
    ('launch_transport.expected_issuer_uid', 65533),
    ('release_enrollment.public_key_hex', 'b' * 64),
])
def test_only_exact_self_digest_is_ignored_by_commitment_but_never_by_raw_pin(path, value):
    runner = configuration()
    before = configuration_commitment(canonical(runner))
    changed(runner, path, value)
    after = configuration_commitment(canonical(runner))
    assert before.raw_digest != after.raw_digest
    assert (before.content_digest == after.content_digest) is (path == 'release_enrollment.launch_profile_digest')


@pytest.mark.parametrize('mutate', [
    lambda raw: b' ' + raw,
    lambda raw: raw[:-1] + b',"schema":"duplicate"}',
    lambda raw: raw.replace(b'"exchange_timeout_ms":1000', b'"exchange_timeout_ms":1.0'),
    lambda raw: raw.replace(b'"launch_profile_digest":', b'"profile_digest":'),
    lambda raw: raw.replace(b'"sha256:' + b'a' * 64 + b'"', b'"marker"'),
    lambda raw: b'{}',
])
def test_full_closed_canonical_parser_precedes_normalization(mutate):
    with pytest.raises(InstalledRunnerError):
        configuration_commitment(mutate(canonical(configuration())))
