FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/hub
WORKDIR /opt/hub
COPY --chown=568:568 hub_executor/ /opt/hub/hub_executor/
USER 568:568
EXPOSE 8080
CMD ["python3", "-m", "hub_executor"]
