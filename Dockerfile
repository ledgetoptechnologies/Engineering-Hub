FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/agent-message-bus-entrypoint
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir fastapi uvicorn[standard] mcp pydantic python-multipart \
    && pip install --no-cache-dir . \
    && chmod 0555 /usr/local/bin/agent-message-bus-entrypoint \
    && useradd --system --uid 10001 --create-home agentbus
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["agent-message-bus-entrypoint"]
