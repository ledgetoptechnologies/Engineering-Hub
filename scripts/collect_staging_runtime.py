"""Compatibility launcher for the packaged staging runtime collector."""
from hub_staging_service.collector import main

if __name__ == '__main__':
    raise SystemExit(main())
