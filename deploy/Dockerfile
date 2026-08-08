# syntax=docker/dockerfile:1
#
# Four stages. The runtime image carries the trading dependencies and nothing
# else: no pytest, no linters, no security tooling.
#
# That separation is not only hygiene. Rust-built dev wheels (ruff, hypothesis)
# ship PEP 770 SBOMs in .dist-info/sboms/ declaring the crates vendored inside
# them, and scanners read those. A dev toolchain in the production image means
# every vendored crate in every dev wheel becomes a finding against the process
# that places orders.
#
#   docker build -f deploy/Dockerfile -t shortcircuit .                # runtime
#   docker build -f deploy/Dockerfile --target test -t shortcircuit:test .

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc python3-dev libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


# Dev dependencies layered on top, for the test image only. Installed at build
# time rather than container start because /opt/venv is root-owned and the
# runtime user is not.
FROM builder AS builder-dev

COPY requirements-dev.txt ./
RUN /opt/venv/bin/pip install -r requirements-dev.txt


# Everything both images share except the virtualenv.
FROM python:3.12-slim AS base

# Non-root. A process that places orders should not also be able to rewrite its
# own code.
RUN groupadd --system --gid 1001 shortcircuit \
 && useradd  --system --uid 1001 --gid shortcircuit --create-home shortcircuit \
 && apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=shortcircuit:shortcircuit . /app

# Writable runtime dirs. .dockerignore keeps the host's logs/ and data/ out of
# the image, so these are created empty.
RUN mkdir -p /app/logs /app/data /app/reports \
 && chown -R shortcircuit:shortcircuit /app/logs /app/data /app/reports


FROM base AS test

COPY --from=builder-dev /opt/venv /opt/venv
USER shortcircuit

# Both tools write state, and /app is root-owned by design.
ENV COVERAGE_FILE=/tmp/.coverage \
    HYPOTHESIS_STORAGE_DIRECTORY=/tmp/.hypothesis

ENTRYPOINT ["python", "-m", "pytest"]


# Last stage, so a plain `docker build` produces the runtime image.
FROM base AS runtime

COPY --from=builder /opt/venv /opt/venv
USER shortcircuit

# Credentials are injected at run time, never baked in.
ENV FYERS_NO_INTERACTIVE=1

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import shortcircuit.config, shortcircuit.strategy.features" || exit 1

ENTRYPOINT ["python", "-m", "shortcircuit"]
