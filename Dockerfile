# syntax=docker/dockerfile:1.7
#
# AutoBuddy production image.
#
# Two stages: wheels are built with compilers in the builder, the runtime stage
# carries only the wheels' result + libpq/ca-certificates/tzdata. The image is
# non-root (uid/gid 10001), read-only friendly, and has no build tooling.
#
# Release identity is injected as build args and OCI labels. The host updater
# cross-checks these labels against the signed release manifest before it will
# roll a release out; see scripts/auto_buddy_updater.py and
# deployment/release-manifest.schema.json.
#
# Required build context:
#   requirements.txt
#   autobuddy/, portal/, manage.py
#   workbuddy-auto-signin/signin.py   (git submodule, checked out)
#   scripts/docker-entrypoint.sh
#   deployment/healthcheck.py

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so application edits do not invalidate the wheel layer.
COPY requirements.txt /build/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip wheel --wheel-dir /wheels -r /build/requirements.txt


FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG APP_VERSION=development
ARG VCS_REF=unknown
ARG CREATED=unknown
ARG SOURCE_URL=""
ARG MIGRATIONS_BACKWARD_COMPATIBLE=false
ARG MANUAL_APPROVAL_REQUIRED=true
ARG UPSTREAM_COMMIT=unknown

LABEL org.opencontainers.image.title="AutoBuddy" \
      org.opencontainers.image.description="WorkBuddy automatic sign-in portal and worker" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.licenses="NOASSERTION" \
      org.auto-buddy.migrations-backward-compatible="${MIGRATIONS_BACKWARD_COMPATIBLE}" \
      org.auto-buddy.manual-approval-required="${MANUAL_APPROVAL_REQUIRED}" \
      org.auto-buddy.upstream-commit="${UPSTREAM_COMMIT}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    PORT=8000

# libpq5: psycopg2 runtime; ca-certificates: outbound HTTPS to WorkBuddy;
# tzdata: Django TIME_ZONE support on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libpq5 tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 autobuddy \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp \
       --shell /usr/sbin/nologin autobuddy

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    python -m pip install --no-index --find-links=/wheels -r /app/requirements.txt \
    && python -m pip check

# Application source. The submodule is part of the build context on purpose:
# the worker executes the pinned upstream signin.py in place.
COPY --chown=10001:10001 . /app

RUN test -f /app/workbuddy-auto-signin/signin.py \
       || { echo "FATAL: workbuddy-auto-signin submodule is not checked out" >&2; exit 1; } \
    && test -f /app/manage.py \
       || { echo "FATAL: manage.py missing from build context" >&2; exit 1; } \
    && chmod 0755 /app/scripts/docker-entrypoint.sh /app/deployment/healthcheck.py \
    && mkdir -p /app/staticfiles /run/auto-buddy \
    && chown 10001:10001 /app/staticfiles /run/auto-buddy

USER 10001:10001

EXPOSE 8000

# Compose overrides this with the same script; the image-level check keeps a
# bare `docker run` honest too.
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=4 \
    CMD ["python", "/app/deployment/healthcheck.py"]

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["web"]
