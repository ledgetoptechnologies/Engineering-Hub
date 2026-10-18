from pathlib import Path

from .core import HubExecutor


FIXTURE_TARGET = "3d-viewer:staging"
FIXTURE_CANDIDATE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
FIXTURE_ROLLBACK_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
FIXTURE_POLICY_REVISION = 1
FIXTURE_CANDIDATE_DIGEST = "sha256:4c031a5fbabf2e39b2aa313c390470863285ffea20396317c6c996a8e1fb8725"
FIXTURE_ROLLBACK_DIGEST = "sha256:3ab6e82eb9e517ba1b2a107b4d8b838e7e872a4f7a62c068a07b07d4227103ec"

FIXTURE_ARTIFACTS = {
    FIXTURE_CANDIDATE_DIGEST: b"engineering hub fixture v2\n",
    FIXTURE_ROLLBACK_DIGEST: b"engineering hub fixture v1\n",
}
FIXTURE_ARTIFACT_COMMITS = {
    FIXTURE_CANDIDATE_DIGEST: FIXTURE_CANDIDATE_COMMIT,
    FIXTURE_ROLLBACK_DIGEST: FIXTURE_ROLLBACK_COMMIT,
}
FIXTURE_ARTIFACT_ROLES = {
    FIXTURE_CANDIDATE_DIGEST: "candidate",
    FIXTURE_ROLLBACK_DIGEST: "rollback",
}


def make_fixture_executor(data_root):
    root = Path(data_root)
    return HubExecutor(
        root / "ledger.sqlite3",
        {FIXTURE_TARGET: root / "targets" / "3d-viewer-staging"},
        FIXTURE_ARTIFACTS,
        candidate_commit=FIXTURE_CANDIDATE_COMMIT,
        artifact_commits=FIXTURE_ARTIFACT_COMMITS,
        artifact_roles=FIXTURE_ARTIFACT_ROLES,
        policy_revision=FIXTURE_POLICY_REVISION,
    )
