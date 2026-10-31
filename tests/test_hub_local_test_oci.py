"""Synthetic OCI graph to production build-record verification, no daemon."""
import json
import uuid
import pytest
from scripts.hub_local_test_oci import prepare_fixture_image, verify_fixture_image
from tests.test_prepare_local_oci_layout import fixture, archive
from tests.test_hub_container_pilot_manifest import make_manifest_data
from engineering_hub.container_pilot_manifest import PilotManifest
from hub_container_executor.build_record import BuildRecordError, VerifiedImage


def prepared(tmp_path):
    rows,digest=fixture()
    obj=prepare_fixture_image(archive(rows),tmp_path/'layout',digest,
        source_commit='a'*40,context_digest='sha256:'+'c'*64,recipe_digest='sha256:'+'d'*64,
        observations={'claim':'synthetic fixture'},preparation_id=str(uuid.uuid4()))
    return obj


def manifest_for(obj):
    data=make_manifest_data(); selection=data['candidate']
    selection['source_commit']='a'*40; selection['image_config_id']=obj.config_digest
    selection['provenance'].update(build_context_digest='sha256:'+'c'*64,
        build_recipe_digest='sha256:'+'d'*64,build_inputs_digest=obj.inputs_digest,
        attestation_digest=obj.record_digest)
    return PilotManifest.from_bytes(json.dumps(data).encode())


def test_exact_archive_record_and_production_verifier(tmp_path):
    obj=prepared(tmp_path); image,records,blobs,verifier=verify_fixture_image(manifest_for(obj),'candidate',obj)
    assert type(image) is VerifiedImage
    assert image.platform_manifest_digest==obj.manifest_digest and image.image_config_id==obj.config_digest
    assert verifier.verify_build_record(obj.record,{},manifest_for(obj),'candidate') is True
    assert records.get(obj.record_digest)==obj.record and blobs.get(obj.manifest_digest)==obj.blobs[obj.manifest_digest]


def test_manifest_mismatch_denied(tmp_path):
    obj=prepared(tmp_path); data=make_manifest_data()
    with pytest.raises(BuildRecordError): verify_fixture_image(PilotManifest.from_bytes(json.dumps(data).encode()),'candidate',obj)


def test_archive_digest_mismatch_denied(tmp_path):
    rows,_=fixture()
    with pytest.raises((ValueError,KeyError)):
        prepare_fixture_image(archive(rows),tmp_path/'layout','sha256:'+'0'*64,
            source_commit='a'*40,context_digest='sha256:'+'c'*64,recipe_digest='sha256:'+'d'*64,
            observations={},preparation_id=str(uuid.uuid4()))

def test_cached_discovery_uses_exact_digest_and_no_mutation(tmp_path):
    from scripts.hub_local_test_oci import discover_cached_image
    obj=prepared(tmp_path); image,*_=verify_fixture_image(manifest_for(obj),'candidate',obj); calls=[]
    def run(args):
        calls.append(args)
        if args[3]=='info': return 'daemon-1234567890'
        return image.image_config_id+'|linux|amd64|'
    assert discover_cached_image(image,daemon_id='daemon-1234567890',run=run) is image
    assert calls[1][-1]==image.platform_manifest_digest
    assert 'with index . "Variant"' in calls[1][-2]
    assert all(not any(x in a for x in ('pull','load','tag','rm')) for a in calls)


def test_cached_discovery_accepts_verified_manifest_id(tmp_path):
    from scripts.hub_local_test_oci import discover_cached_image
    obj=prepared(tmp_path); image,*_=verify_fixture_image(manifest_for(obj),'candidate',obj)
    def run(args): return 'daemon-1234567890' if args[3]=='info' else image.platform_manifest_digest+'|linux|amd64|'
    assert discover_cached_image(image,daemon_id='daemon-1234567890',run=run) is image

def test_cached_discovery_rejects_wrong_config(tmp_path):
    from scripts.hub_local_test_oci import discover_cached_image
    obj=prepared(tmp_path); image,*_=verify_fixture_image(manifest_for(obj),'candidate',obj)
    def run(args): return 'daemon-1234567890' if args[3]=='info' else 'sha256:'+'0'*64+'|linux|amd64|'
    with pytest.raises(ValueError): discover_cached_image(image,daemon_id='daemon-1234567890',run=run)
