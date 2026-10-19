"""Explicit routing contract for the original bus inside the Hub application.

No application, database, credential, session or listener is created on import.
The bus remains the single owner of its stateful MCP and native notifications.
"""
from starlette.routing import Route


BUS_API_PREFIXES = (
    '/api/messages', '/api/message-receipts', '/api/presence',
    '/api/files', '/api/feedback',
)
BUS_ADMIN_PATH = '/api/admin/cleanup'


def is_bus_api(path):
    return any(path == prefix or path.startswith(prefix + '/')
               for prefix in BUS_API_PREFIXES)


def attach_bus_routes(app, bus_app):
    # ASGI-object Route endpoints preserve scope.path/root_path. A Mount here
    # would strip /mcp and defeat the bus's existing session-owner middleware.
    methods = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']
    async def canonical_mcp(scope, receive, send):
        # The original bus owns /mcp exactly. Canonicalize the alias without
        # a redirect (which could lose a POST) or stripping its session path.
        if scope['path'] == '/mcp/':
            scope = {**scope, 'path': '/mcp', 'raw_path': b'/mcp'}
        await bus_app(scope, receive, send)

    # Route wraps functions as request endpoints; an ASGI object preserves the
    # three-argument interface for both aliases.
    class MCPAlias:
        async def __call__(self, scope, receive, send):
            await canonical_mcp(scope, receive, send)

    alias = MCPAlias()
    for path in ('/mcp', '/mcp/'):
        app.router.routes.append(Route(path, endpoint=alias, methods=methods))
    for prefix in BUS_API_PREFIXES:
        app.router.routes.append(Route(prefix, endpoint=bus_app, methods=methods))
        app.router.routes.append(Route(prefix + '/{path:path}', endpoint=bus_app, methods=methods))
    app.router.routes.append(Route(BUS_ADMIN_PATH, endpoint=bus_app, methods=methods))
