# syntax=docker/dockerfile:1
# Multi-stage: build tools never reach the final image.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc python3-dev libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim AS runtime

# Non-root. A process that places orders should not also be able to rewrite its
# own code.
RUN groupadd --system --gid 1001 shortcircuit \
 && useradd  --system --uid 1001 --gid shortcircuit --create-home shortcircuit \
 && apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=shortcircuit:shortcircuit . /app

# Writable runtime dirs. .dockerignore keeps the host's logs/ and data/ out of
# the image, so these are created empty.
RUN mkdir -p /app/logs /app/data /app/reports \
 && chown -R shortcircuit:shortcircuit /app/logs /app/data /app/reports

USER shortcircuit

# Credentials are injected at run time, never baked in.
ENV FYERS_NO_INTERACTIVE=1

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import config, strategy.features" || exit 1

ENTRYPOINT ["python", "main.py"]
