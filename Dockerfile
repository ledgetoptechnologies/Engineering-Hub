FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build

# Keep the runtime dependencies exactly aligned with the committed lockfile.
RUN python -m pip install --no-cache-dir uv==0.11.17
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-default-groups --no-editable

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"
ARG VCS_REF=unknown
ARG VERSION=unknown
LABEL org.opencontainers.image.title="Agent Message Bus" \
      org.opencontainers.image.description="Authenticated shared message and knowledge bus for Hermes, Codex, and Claude Code" \
      org.opencontainers.image.source="https://github.com/ledgetoptechnologies/internal-MCP" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.sh /usr/local/bin/agent-message-bus-entrypoint
RUN groupadd --system --gid 10001 agentbus \
    && useradd --system --uid 10001 --gid agentbus --create-home agentbus \
    && chmod 0555 /usr/local/bin/agent-message-bus-entrypoint

USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import json,os,urllib.request; host=os.environ.get('AGENT_BUS_CONTAINER_HOST','127.0.0.1'); host='127.0.0.1' if host=='0.0.0.0' else host; url='http://'+host+':'+os.environ.get('AGENT_BUS_PORT','8765')+'/health'; response=urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=3); raise SystemExit(0 if json.load(response).get('ok') is True else 1)"]
ENTRYPOINT ["/usr/local/bin/agent-message-bus-entrypoint"]
