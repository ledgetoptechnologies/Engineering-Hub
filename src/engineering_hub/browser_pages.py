"""Closed, authenticated client-side page routes; never a filesystem fallback."""
from pathlib import Path
import re

from fastapi import HTTPException, Request
from starlette.responses import FileResponse


_SEGMENT = r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}'
_TABS = frozenset({'overview', 'work', 'decisions', 'notes', 'labeling', 'activity', 'plans'})
_PAGES = frozenset({'/', '/index.html', '/inbox', '/work', '/planning', '/projects',
                    '/notes', '/agents', '/staging'})


def client_page_allowed(method, path):
    """Only canonical ASCII page paths; API/assets and arbitrary files never match."""
    if method not in {'GET', 'HEAD'} or not isinstance(path, str) or len(path) > 1024:
        return False
    if not path.isascii() or any(value in path for value in ('%', '\\', '//', '?', '#')):
        return False
    parts = path.split('/')[1:]
    if any(part in {'.', '..'} for part in parts):
        return False
    if path in _PAGES:
        return True
    if len(parts) >= 2 and parts[0] == 'projects' and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', parts[1]):
        return (len(parts) == 2 or len(parts) == 3 and parts[2] in _TABS
                or len(parts) == 4 and parts[2] == 'labels'
                and re.fullmatch(_SEGMENT, parts[3]) is not None)
    if len(parts) == 3 and parts[:2] == ['agents', 'topics']:
        return re.fullmatch(_SEGMENT, parts[2]) is not None
    if 2 <= len(parts) <= 10 and parts[0] == 'notes':
        return all(re.fullmatch(_SEGMENT, part) is not None for part in parts[1:])
    return False


def attach_browser_page_routes(app, *, ui_directory, human, anonymous_shell=False,
                               verified_access_config=None):
    """Attach before StaticFiles. Existing middleware supplies CSP/no-store.

    Authenticate through the caller's existing human session boundary. A path
    can select only the one fixed packaged index, never a requested local file.
    Explicit patterns leave asset routing and API 404s to their existing routes.
    """
    index = Path(ui_directory) / 'index.html'

    async def page(request: Request):
        raw_path = request.scope.get('raw_path', request.url.path.encode('utf-8'))
        if (not client_page_allowed(request.method, request.url.path)
                or raw_path != request.url.path.encode('utf-8') or not index.is_file()):
            raise HTTPException(404, detail={'code': 'page_not_found'})
        bootstrap_shell = anonymous_shell and request.url.path in {'/', '/index.html'}
        if verified_access_config is not None and request.url.path in {'/', '/index.html'}:
            from .cloudflare_access import _verified_access
            # Only ingress's in-process verification proof can admit the shell
            # before its bootstrap request creates the Hub session. A header or
            # caller-supplied state value alone never grants this exception.
            bootstrap_shell = _verified_access(request.scope, verified_access_config) is not None
        if not bootstrap_shell:
            human(request)
        return FileResponse(index, media_type='text/html', headers={'Cache-Control': 'no-store'})

    for route in sorted(_PAGES) + [
            '/projects/{project}', '/projects/{project}/{tab}',
            '/projects/{project}/labels/{label_set}', '/notes/{note_path:path}',
            '/agents/topics/{topic}']:
        app.add_api_route(route, page, methods=['GET', 'HEAD'], include_in_schema=False)
