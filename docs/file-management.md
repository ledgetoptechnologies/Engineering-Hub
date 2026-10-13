# HTTP file management

The bus stores artifact bytes under `AGENT_BUS_FILE_ROOT` (default `data/files`) and stores metadata in the same SQLite database as messages. Docker maps this root to `/var/lib/agent-message-bus/files`, inside the persistent `./data` bind mount.

All file routes require an enabled agent bearer token (Hermes or Codex, and Claude Code only when Claude auth is enabled in the running image). The admin token can delete; it is separate from agent tokens and cannot use agent routes.

## Endpoints

### Upload

`POST /api/files` as multipart form data:

- `path`: required safe relative path, such as `runs/2026-09-30/output.json`
- `file`: required file part

The service reads the multipart part in bounded chunks into a temporary file, computes SHA-256 and size, then atomically renames it below the file root. Metadata is committed to SQLite with the rename. Uploading an existing path replaces it atomically.

### List and metadata

- `GET /api/files` returns `{ "files": [...] }`, ordered by relative path.
- `GET /api/files/{path}` returns one metadata object.
- `GET /api/files/{path}/metadata` is an explicit metadata alias.

Metadata includes `path`, `size`, `sha256`, `content_type`, `created_at`, and `updated_at`.

### Download and delete

- `GET /api/files/{path}/download` streams the bytes with the recorded content type.
- `DELETE /api/files/{path}` requires the admin token and returns `204`.

Paths must be relative POSIX paths. Empty components, `.`, `..`, backslashes, control characters, absolute paths, and symlinked components are rejected. Downloads open files with `O_NOFOLLOW` so a symlink cannot turn a metadata record into an outside-root read.
