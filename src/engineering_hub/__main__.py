"""Private control-plane entry point, independent of the existing bus process."""
import argparse
import asyncio

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Engineering Hub private control service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--split-ingress", action="store_true")
    parser.add_argument("--browser-port", type=int, default=8771)
    args = parser.parse_args()
    if args.split_ingress:
        from .app import create_app
        from .frontends import HubFrontends
        from .server import serve_frontends
        def pair_factory():
            return HubFrontends(create_app())
        asyncio.run(serve_frontends(pair_factory, agent_host=args.host, agent_port=args.port,
                                    browser_port=args.browser_port))
        return
    # OAuth callback URLs contain short-lived codes. Never log query strings.
    uvicorn.run("engineering_hub.app:create_app", factory=True, host=args.host,
                port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
