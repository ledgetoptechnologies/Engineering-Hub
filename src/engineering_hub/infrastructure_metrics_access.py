"""Shared request admission for whole-machine metric reads.

Legacy family bearers are authenticated internal credentials. Per-PC bearers
need an explicit owner-managed internal classification; connection approval
alone is insufficient. Browser reads belong to the configured connection owner.
No project grants, request labels or Prometheus data establish this authority.
"""
import json

from fastapi import HTTPException, Request

from agent_message_bus.agent_installation_scope import InstallationScopeRepository
from agent_message_bus.agent_sessions import AgentSessionError
from agent_message_bus.agent_sessions_transport import (
    session_origin_allowed, source_allowed, transport_bearer,
)
from agent_message_bus.auth import AuthSettings
from .infrastructure_service import InfrastructureService
from .repository import AGENTS, HubError


class MetricsAccess:
    def __init__(self, service, auth, human, *, owner=None, scopes=None, connection_config=None):
        if type(service) is not InfrastructureService or type(auth) is not AuthSettings or not callable(human):
            raise TypeError('explicit_metrics_admission_required')
        if scopes is not None and type(scopes) is not InstallationScopeRepository:
            raise TypeError('explicit_installation_scopes_required')
        if scopes is not None and owner != scopes.sessions.owner:
            raise ValueError('metrics_owner_mismatch')
        self.service, self.auth, self.human = service, auth, human
        self.owner, self.scopes, self.connection_config = owner, scopes, connection_config

    def authorize(self, request):
        if not isinstance(request, Request):
            raise HubError('forbidden', 403)
        if request.headers.getlist('authorization'):
            secret = transport_bearer(request)
            if secret.lower().startswith(('ehs_', 'ehi_', 'ehc_')):
                if (self.scopes is None or self.connection_config is None
                        or not session_origin_allowed(request, self.connection_config)
                        or not source_allowed(request, self.auth)):
                    raise HubError('forbidden', 403)
                self.scopes.require_internal(secret)
            elif self.auth.agent(request) not in AGENTS:
                raise HubError('forbidden', 403)
        else:
            identity = self.human(request)
            if self.owner is None or identity != self.owner:
                raise HubError('forbidden', 403)
        self.service.registry._assert_admission()
        # Reader's synchronous callback contract requires None on success.

    def inventory(self, request):
        self.authorize(request)
        registry = self.service.registry
        # Only minimal exact association hints are read under the registry's
        # storage admission guard. This does not grant its mutation API.
        with registry.lock:
            registry._assert_admission()
            rows = registry.db.execute('SELECT data FROM infra_devices ORDER BY id LIMIT 4097').fetchall()
            if len(rows) > 4096:
                raise HubError('infrastructure_inventory_limit', 503)
            result = []
            for row in rows:
                try:
                    item = json.loads(row['data'])
                    if type(item) is not dict or type(item.get('id')) is not str:
                        raise ValueError()
                    if not item.get('retired', False):
                        # An explicit Prometheus host replaces the hostname for matching;
                        # one device never claims both names.
                        metrics_host = item.get('metrics_host')
                        hostname = metrics_host if type(metrics_host) is str else item.get('hostname')
                        result.append({'device_id': item['id'],
                                       'hostnames': [hostname] if type(hostname) is str else []})
                except (ValueError, TypeError, KeyError, RecursionError):
                    raise HubError('infrastructure_unavailable', 503) from None
            return result


def register_metrics_tool(mcp, reader):
    from mcp.server.fastmcp import Context
    from mcp.server.fastmcp.exceptions import ToolError

    @mcp.tool()
    async def hub_infrastructure_metrics(ctx: Context) -> dict:
        """Read discovered machine metrics; internal agents only, no commands or PromQL."""
        try:
            return await reader.read(ctx.request_context.request)
        except (HubError, AgentSessionError) as error:
            raise ToolError(error.code) from None
        except HTTPException:
            raise ToolError('forbidden') from None
        except Exception:
            raise ToolError('infrastructure_metrics_unavailable') from None
