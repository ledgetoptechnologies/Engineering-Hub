"""Private control-plane entry point, independent of the existing bus process."""
import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Engineering Hub private control service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    # OAuth callback URLs contain short-lived codes. Never log query strings.
    uvicorn.run("engineering_hub.app:create_app", factory=True, host=args.host,
                port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
