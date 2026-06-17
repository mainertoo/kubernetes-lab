# syntax=docker/dockerfile:1.25
# Multi-stage non-root build; plan §9 security posture.

FROM python:3.14-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --user --no-warn-script-location -r requirements.txt

FROM python:3.14-slim
WORKDIR /app

# Non-root, read-only-friendly layout
RUN useradd -r -u 10001 -m -d /home/bridge bridge
ENV PATH=/home/bridge/.local/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=builder /root/.local /home/bridge/.local
COPY --chown=bridge:bridge *.py ./
COPY --chown=bridge:bridge lua/ ./lua/

USER bridge
EXPOSE 8080 8081 8082

# Default ports (override via env per config.py)
ENV WEBHOOK_PORT=8080 \
    ADMIN_PORT=8081 \
    METRICS_PORT=8082

# `python main.py` — kustomized via the bjw-s Deployment spec to add resource limits,
# automountServiceAccountToken: false, etc. (plan §9 + F2-009)
CMD ["python", "/app/main.py"]
