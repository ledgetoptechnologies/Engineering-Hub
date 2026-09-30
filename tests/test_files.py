import hashlib
import sqlite3

import pytest

from agent_message_bus.app import create_app
from agent_message_bus.auth import AuthSettings


@pytest.fixture
async def file_client(tmp_path):
    import httpx

    app = create_app(
        db_path=tmp_path / "messages.sqlite3",
        audit_path=tmp_path / "audit.jsonl",
        file_root=tmp_path / "files",
        auth=AuthSettings("h-token", "c-token", "a-token"),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, tmp_path / "files"


@pytest.mark.asyncio
async def test_authenticated_multipart_upload_list_metadata_and_download(file_client):
    client, root = file_client
    headers = {"Authorization": "Bearer h-token"}
    content = b"artifact payload\n"
    uploaded = await client.post(
        "/api/files",
        headers=headers,
        data={"path": "reports/result.txt"},
        files={"file": ("untrusted-name.txt", content, "text/plain")},
    )
    assert uploaded.status_code == 201
    metadata = uploaded.json()
    assert metadata["path"] == "reports/result.txt"
    assert metadata["size"] == len(content)
    assert metadata["sha256"] == hashlib.sha256(content).hexdigest()
    assert metadata["content_type"] == "text/plain"
    assert (root / "reports/result.txt").read_bytes() == content

    listed = await client.get("/api/files", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == {"files": [metadata]}

    details = await client.get("/api/files/reports/result.txt", headers=headers)
    assert details.status_code == 200
    assert details.json() == metadata

    downloaded = await client.get("/api/files/reports/result.txt/download", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert downloaded.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_file_authentication_and_admin_only_delete(file_client):
    client, _ = file_client
    upload = await client.post(
        "/api/files",
        headers={"Authorization": "Bearer h-token"},
        data={"path": "one.bin"},
        files={"file": ("one.bin", b"one")},
    )
    assert upload.status_code == 201

    assert (await client.get("/api/files")).status_code == 401
    assert (await client.get("/api/files", headers={"Authorization": "Bearer a-token"})).status_code == 401
    assert (await client.get("/api/files", headers={"Authorization": "Bearer c-token"})).status_code == 200
    assert (await client.delete("/api/files/one.bin", headers={"Authorization": "Bearer h-token"})).status_code == 401

    deleted = await client.delete("/api/files/one.bin", headers={"Authorization": "Bearer a-token"})
    assert deleted.status_code == 204
    assert (await client.get("/api/files/one.bin", headers={"Authorization": "Bearer c-token"})).status_code == 404


@pytest.mark.asyncio
async def test_file_paths_reject_traversal_and_symlink_escape(file_client, tmp_path):
    client, root = file_client
    headers = {"Authorization": "Bearer h-token"}
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    for unsafe in ("../escape.txt", "/absolute.txt", "linked/escape.txt", "a/../b.txt"):
        response = await client.post(
            "/api/files",
            headers=headers,
            data={"path": unsafe},
            files={"file": ("x", b"x")},
        )
        assert response.status_code == 400

    assert not (tmp_path / "escape.txt").exists()
    assert not list(root.rglob(".upload-*"))


@pytest.mark.asyncio
async def test_download_rejects_symlinked_file(file_client):
    client, root = file_client
    path = root / "safe.txt"
    path.write_text("secret")
    (root / "alias.txt").symlink_to(path)
    headers = {"Authorization": "Bearer h-token"}
    # Registering a symlink is not possible through upload, and a stale metadata row
    # must not turn into a symlink-following download.
    with sqlite3.connect(root.parent / "messages.sqlite3") as conn:
        conn.execute(
            "INSERT INTO files(path,size,sha256,content_type,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("alias.txt", 6, hashlib.sha256(b"secret").hexdigest(), "text/plain", "now", "now"),
        )
    response = await client.get("/api/files/alias.txt/download", headers=headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_files_survive_app_recreation(tmp_path):
    import httpx

    db_path = tmp_path / "messages.sqlite3"
    file_root = tmp_path / "files"
    auth = AuthSettings("h-token", "c-token", "a-token")
    first = create_app(db_path=db_path, audit_path=tmp_path / "first-audit.jsonl",
                       file_root=file_root, auth=auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://test") as client:
        uploaded = await client.post(
            "/api/files", headers={"Authorization": "Bearer h-token"},
            data={"path": "persisted.bin"}, files={"file": ("x", b"persisted")},
        )
        assert uploaded.status_code == 201

    second = create_app(db_path=db_path, audit_path=tmp_path / "second-audit.jsonl",
                        file_root=file_root, auth=auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://test") as client:
        downloaded = await client.get("/api/files/persisted.bin/download",
                                      headers={"Authorization": "Bearer c-token"})
        assert downloaded.status_code == 200
        assert downloaded.content == b"persisted"
