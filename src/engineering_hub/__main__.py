"""Private control-plane entry point, independent of the existing bus process."""
import argparse
import asyncio

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Engineering Hub private control service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--config", help="Absolute path to persisted, non-secret Hub settings; "
                        "selects unified split ingress on agent8770/browser8771")
    parser.add_argument("--split-ingress", action="store_true")
    parser.add_argument("--browser-port", type=int, default=8771)
    parser.add_argument("--browser-host", default=None,
                        help="Numeric browser IP for split ingress (default: 127.0.0.1); "
                             "non-loopback access still requires Cloudflare Access authentication")
    parser.add_argument("--agent-alias-port", type=int,
                        help="Optional compatibility port sharing the existing agent listener owner")
    parser.add_argument("--unified-mcp", action="store_true",
                        help="Serve existing bus capabilities and Hub tools as one private MCP")
    args = parser.parse_args()
    deployment = None
    if args.config is not None:
        from .deployment_config import load_deployment_config
        try:
            deployment = load_deployment_config(args.config)
        except ValueError:
            parser.error('Invalid or unavailable Hub settings file; configure it before startup')
        if deployment.protected_ledger:
            parser.error('Protected ledger requires an explicit owned entrypoint; no runtime started')
        if args.agent_alias_port is not None:
            parser.error('--config does not support an agent alias port')
        args.split_ingress = args.unified_mcp = True
        if args.browser_host is None:
            args.browser_host = '0.0.0.0'
    if args.unified_mcp and not args.split_ingress:
        parser.error('--unified-mcp requires --split-ingress')
    if args.agent_alias_port is not None and not args.split_ingress:
        parser.error('--agent-alias-port requires --split-ingress')
    if args.browser_host is not None and not args.split_ingress:
        parser.error('--browser-host requires --split-ingress')
    if args.split_ingress:
        if args.unified_mcp:
            from .unified_app import create_unified_app as create_app
        else:
            from .app import create_app
        from .frontends import HubFrontends
        from .server import serve_frontends
        def pair_factory():
            if deployment is not None:
                from .app import Settings
                return HubFrontends(create_app(Settings.from_env(deployment),
                                               bus_paths=deployment.bus_paths))
            return HubFrontends(create_app())
        asyncio.run(serve_frontends(pair_factory, agent_host=args.host, agent_port=args.port,
                                    browser_host="127.0.0.1" if args.browser_host is None else args.browser_host,
                                    browser_port=args.browser_port, agent_alias_port=args.agent_alias_port))
        return
    # OAuth callback URLs contain short-lived codes. Never log query strings.
    uvicorn.run("engineering_hub.app:create_app", factory=True, host=args.host,
                port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
