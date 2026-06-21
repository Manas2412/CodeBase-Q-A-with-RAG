# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the codebase review agent backend.
#
# Stage 1 (uv)        : grab the uv binary from its official image
# Stage 2 (builder)   : create a frozen venv from pyproject.toml + uv.lock
# Stage 3 (runtime)   : minimal Debian slim with git + the prebuilt venv
#
# The same image runs the FastAPI server, Celery worker, and Celery Beat —
# docker-compose picks the role via CMD override.

# ── Stage 1: pull uv binary once ──────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:0.5 AS uv


# ── Stage 2: builder ──────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv

# Resolve and install dependencies FIRST so the slow layer caches well.
# We only copy lockfiles at this point so any source change won't bust the
# dep cache. --no-install-project skips the project itself for now.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now bring in the project source and install it.
# README.md is required by hatchling at build time (referenced in [project].readme).
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ── Stage 3: runtime ──────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

# git: required by GitPython + the future polling agent for clone/fetch
# ca-certificates: for HTTPS clones against OpenForge/GitHub
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. UID 1000 matches the typical host user, which avoids
# permission headaches when bind-mounting the persistent clone dir in dev.
RUN useradd --create-home --uid 1000 --shell /bin/bash app \
    && mkdir -p /var/lib/codereview/repos \
    && chown -R app:app /var/lib/codereview

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

USER app

EXPOSE 8000

# Default to the API server. Worker / Beat override this in docker-compose.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
