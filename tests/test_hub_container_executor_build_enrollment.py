"""Synthetic operator enrollment tests, not real build provenance acceptance."""
from dataclasses import replace
import json
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="Optional host package requires Linux ownership")

from hub_container_executor.build_enrollment import (
    BuildEnrollment, BuildEnrollmentError, OperatorEnrolledBuildVerifier,
)
from hub_container_executor.build_record import BuildRecordError, verify_image_record
from tests.test_hub_container_executor_build_record import (
    FakeStore, canonical, digest, make_record,
)


def fixture(role="candidate"):
    manifest, raw, blobs = make_record(role)
    record = json.loads(raw)
    enrollment = BuildEnrollment(manifest.digest(), role, digest(raw),
                                 record["builder_id"], record["invocation_id"])
    return manifest, raw, blobs, record, enrollment


def test_exact_enrollment_composes_with_full_metadata_verification():
    manifest, raw, blobs, record, enrollment = fixture()
    verifier = OperatorEnrolledBuildVerifier(enrollments=(enrollment,))
    verified = verify_image_record(manifest, "candidate", FakeStore({digest(raw): raw}),
                                   FakeStore(blobs), verifier)
    assert verified.record_digest == enrollment.record_digest
    assert verified.source_commit == manifest.candidate.source_commit
    assert verifier.verify_build_record(raw, record, manifest, "candidate") is True


@pytest.mark.parametrize("role", ["candidate", "rollback"])
def test_roles_only_use_their_own_explicit_enrollment(role):
    manifest, raw, _, record, enrollment = fixture(role)
    verifier = OperatorEnrolledBuildVerifier(enrollments=(enrollment,))
    assert verifier.verify_build_record(raw, record, manifest, role)
    other = "rollback" if role == "candidate" else "candidate"
    assert not verifier.verify_build_record(raw, record, manifest, other)


@pytest.mark.parametrize("change", ["builder", "invocation", "digest", "manifest", "role"])
def test_wrong_enrolled_identity_cannot_approve(change):
    manifest, raw, _, record, enrollment = fixture()
    values = {"builder": {"builder_id": "other-builder"},
              "invocation": {"invocation_id": str(uuid4())},
              "digest": {"record_digest": "sha256:" + "f" * 64},
              "manifest": {"manifest_digest": "sha256:" + "e" * 64},
              "role": {"role": "rollback"}}
    verifier = OperatorEnrolledBuildVerifier(enrollments=(replace(enrollment, **values[change]),))
    assert not verifier.verify_build_record(raw, record, manifest, "candidate")


@pytest.mark.parametrize("change", ["raw", "parsed", "manifest", "extra", "unknown-role"])
def test_untrusted_argument_changes_are_denied(change):
    manifest, raw, _, record, enrollment = fixture()
    verifier = OperatorEnrolledBuildVerifier(enrollments=(enrollment,))
    role = "candidate"
    if change == "raw":
        raw += b" "
    elif change == "parsed":
        record["builder_id"] = "forged"
    elif change == "manifest":
        # Still valid, but outside the exact enrolled campaign identity.
        manifest = manifest.model_copy(update={"campaign_id": str(uuid4())})
    elif change == "extra":
        record["approval"] = True
    else:
        role = "latest"
    assert verifier.verify_build_record(raw, record, manifest, role) is False


def test_changed_record_rehash_is_not_automatic_approval():
    manifest, raw, blobs, record, enrollment = fixture()
    verifier = OperatorEnrolledBuildVerifier(enrollments=(enrollment,))
    record["invocation_id"] = str(uuid4())
    changed_raw = canonical(record)
    provenance = manifest.candidate.provenance.model_copy(update={"attestation_digest": digest(changed_raw)})
    changed = manifest.model_copy(update={"candidate": manifest.candidate.model_copy(update={"provenance": provenance})})
    with pytest.raises(BuildRecordError, match="did not approve"):
        verify_image_record(changed, "candidate", FakeStore({digest(changed_raw): changed_raw}),
                            FakeStore(blobs), verifier)


def test_constructor_snapshots_even_bypassed_frozen_input():
    manifest, raw, _, record, enrollment = fixture()
    verifier = OperatorEnrolledBuildVerifier(enrollments=(enrollment,))
    object.__setattr__(enrollment, "builder_id", "changed-after-construction")
    assert verifier.verify_build_record(raw, record, manifest, "candidate")
    object.__setattr__(enrollment, "role", "bad")
    with pytest.raises(BuildEnrollmentError):
        OperatorEnrolledBuildVerifier(enrollments=(enrollment,))


@pytest.mark.parametrize("bad", [None, (), [], (None,), ("entry",)])
def test_missing_or_untyped_enrollment_is_not_accepted(bad):
    with pytest.raises(BuildEnrollmentError):
        OperatorEnrolledBuildVerifier(enrollments=bad)


def test_duplicate_or_excess_enrollment_is_not_accepted():
    *_, enrollment = fixture()
    for values in ((enrollment, enrollment), (enrollment,) * 129):
        with pytest.raises(BuildEnrollmentError):
            OperatorEnrolledBuildVerifier(enrollments=values)


@pytest.mark.parametrize("field,value", [
    ("manifest_digest", "sha256:" + "A" * 64), ("record_digest", "bad"),
    ("role", "latest"), ("builder_id", "../unsafe"), ("invocation_id", "not-uuid"),
])
def test_bad_enrollment_fields_rejected_without_value_disclosure(field, value):
    *_, enrollment = fixture()
    with pytest.raises(BuildEnrollmentError) as error:
        replace(enrollment, **{field: value})
    assert str(error.value) == "Invalid explicit build enrollment"
