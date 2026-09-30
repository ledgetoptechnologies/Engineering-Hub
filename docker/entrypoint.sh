#!/bin/sh
set -eu
exec uvicorn agent_message_bus.app:app \
  --host "${AGENT_BUS_CONTAINER_HOST:-0.0.0.0}" \
  --port "${AGENT_BUS_PORT:-8765}"
