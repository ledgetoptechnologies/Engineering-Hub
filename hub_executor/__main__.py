import os
from pathlib import Path

from .fixtures import make_fixture_executor
from .http import make_server
from .ownership import exclusive_owner


def main():
    token_file = os.environ.get("HUB_EXECUTOR_TOKEN_FILE")
    token_env = os.environ.get("HUB_EXECUTOR_TOKEN")
    if token_file and token_env:
        raise RuntimeError("configure the executor token by file or environment, not both")
    token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else token_env
    if not token:
        raise RuntimeError("fixture executor token is required")

    host = os.environ.get("HUB_EXECUTOR_HOST", "0.0.0.0")
    if host not in {"0.0.0.0", "127.0.0.1"}:
        raise RuntimeError("fixture executor bind host is not allowed")
    try:
        port = int(os.environ.get("HUB_EXECUTOR_PORT", "8080"))
    except ValueError as error:
        raise RuntimeError("fixture executor port is invalid") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("fixture executor port is invalid")

    root = os.environ.get("HUB_EXECUTOR_DATA_DIR", "/data")
    with exclusive_owner(Path(root) / '.service.lock'):
        executor = make_fixture_executor(root)
        server = make_server(executor, token, host=host, port=port)
        server.daemon_threads = False
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
