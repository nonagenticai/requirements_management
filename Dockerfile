# ==============================================================================
# Requirements API - Multi-stage Dockerfile (aligns with standard)
# - Build stage generates a locked, reproducible venv with uv
# - Production stage is slim, non-root, and includes healthcheck
# ==============================================================================

# -----------------------------------------------------------------------------
# Base Stage
# -----------------------------------------------------------------------------
ARG BASE_REGISTRY=docker.io
FROM ${BASE_REGISTRY}/library/python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# Minimal runtime deps for curl-based healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Bring in uv binary (multi-arch)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV PATH="/usr/local/bin:$PATH"

# -----------------------------------------------------------------------------
# Builder Stage - resolve & install deps (no dev) + spaCy model
# -----------------------------------------------------------------------------
FROM base AS builder

# Build-time system deps for compiling wheels and postgres driver
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    git \
    libpq-dev \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy files needed for dependency resolution
COPY README.md pyproject.toml uv.lock ./

# Every dependency resolves from PyPI; the lockfile is the whole dependency surface.
# Install dependencies only (cached layer - not invalidated by code changes)
RUN uv sync --frozen --no-dev --no-install-project

# Install SpaCy model into the virtualenv
RUN uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

# Verify model presence (non-failing logging step)
RUN uv run python - <<'PY'
import spacy
nlp = spacy.load('en_core_web_sm')
print('SpaCy model:', nlp.meta['name'], nlp.meta['version'])
PY

# Copy runtime application code
COPY src /app/src
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini

# Install the local project (fast - deps already cached)
RUN uv sync --frozen --no-dev

# -----------------------------------------------------------------------------
# Production Stage - slim runtime image
# -----------------------------------------------------------------------------
FROM base AS production

# Runtime libs: postgres client lib; cairo (+ the DejaVu fonts the renderers name) for the
# vector PDF exports (cairosvg -> cairocffi dlopens libcairo.so.2; tools/rendering/pdf.py)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libcairo2 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Copy prepared virtualenv and app code
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/alembic /app/alembic
COPY --from=builder /app/alembic.ini /app/alembic.ini

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Environment
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PORT=8921

EXPOSE 8921

# Health check aligned with FastAPI /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8921/health || exit 1

# Launch application (production, no reload)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8921"]
