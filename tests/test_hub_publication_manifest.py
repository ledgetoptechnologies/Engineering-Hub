"""Publication must promote the image that passed the local smoke checks."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'verify_hub_publication_manifest.py'
spec = importlib.util.spec_from_file_location('hub_publication_manifest', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def manifest(config):
    return json.dumps({'schemaVersion': 2,
        'mediaType': 'application/vnd.docker.distribution.manifest.v2+json',
        'config': {'digest': config}, 'layers': [{'digest': 'sha256:' + 'b' * 64}]},
        separators=(',', ':')).encode()


def descriptor(raw):
    return {'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}


def test_matching_registry_bytes_and_pulled_id_are_accepted():
    tested = 'sha256:' + 'a' * 64
    raw = manifest(tested)
    assert module.verify(raw, descriptor(raw), tested, tested) == descriptor(raw)['digest']


def test_substituted_tag_is_rejected_even_with_matching_descriptor():
    tested = 'sha256:' + 'a' * 64
    substituted = manifest('sha256:' + 'c' * 64)
    with pytest.raises(ValueError):
        module.verify(substituted, descriptor(substituted), tested)


def test_tag_change_between_manifest_and_descriptor_reads_is_rejected():
    tested = 'sha256:' + 'a' * 64
    raw = manifest(tested)
    with pytest.raises(ValueError):
        module.verify(raw, descriptor(manifest('sha256:' + 'c' * 64)), tested)


def test_different_pulled_image_is_rejected():
    tested = 'sha256:' + 'a' * 64
    raw = manifest(tested)
    with pytest.raises(ValueError):
        module.verify(raw, descriptor(raw), tested, 'sha256:' + 'c' * 64)


def test_index_is_rejected_until_platform_selection_is_verified():
    tested = 'sha256:' + 'a' * 64
    raw = json.dumps({'schemaVersion': 2,
        'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': []}).encode()
    with pytest.raises(ValueError):
        module.verify(raw, descriptor(raw), tested)


def test_workflow_binds_smokes_tag_and_pull_to_same_image_id():
    workflow = (SCRIPT.parents[1] / '.github/workflows/hub-control.yml').read_text()
    publication = workflow.split('  publish:', 1)[1]
    capture = publication.index('TESTED_CONFIG="$(docker image inspect "$BUILD_IMAGE"')
    first_smoke = publication.index('python scripts/hub_release_smoke.py --image "$TESTED_CONFIG"')
    push = publication.index('docker push "$BUILD_IMAGE"')
    check_tag = publication.index('test "$(docker image inspect "$BUILD_IMAGE"')
    assert capture < first_smoke < check_tag < push
    for script in ('release', 'split_container', 'unified_container', 'settings_container', 'protected_container'):
        assert f'python scripts/hub_{script}_smoke.py --image "$TESTED_CONFIG"' in publication
    assert publication.index('--pulled-config "$PULLED_CONFIG"') < publication.index('imagetools create')
