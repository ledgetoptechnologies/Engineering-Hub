"""Explicit Engineering Hub container-pilot host-adapter source."""

__all__ = ["HostAdapter", "KnownTerminalFailure", "OwnerBusy", "SQLiteLedger", "create_app"]


def __getattr__(name):
    """Load runtime-only host dependencies when a public export is used."""
    if name in ("HostAdapter", "KnownTerminalFailure"):
        from . import adapter
        return getattr(adapter, name)
    if name in ("OwnerBusy", "SQLiteLedger"):
        from . import ledger
        return getattr(ledger, name)
    if name == "create_app":
        from .asgi import create_app
        return create_app
    raise AttributeError(name)
