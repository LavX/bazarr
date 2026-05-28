# =============================================================================
# Bazarr+ Production Docker Image
# =============================================================================
# Multi-stage build optimized for layer caching
# Based on Debian Slim for better compatibility (unrar, etc.)
# =============================================================================

ARG BAZARR_VERSION=latest
ARG BUILD_DATE
ARG VCS_REF
ARG ALASS_CLI_VERSION=2.0.0

# =============================================================================
# Stage 1: Install Python Dependencies (cached heavily)
# =============================================================================
FROM python:3.14-slim-trixie AS python-builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libffi-dev \
    libpq-dev \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy ONLY requirements first for maximum caching
# This layer will only rebuild when requirements files change
COPY requirements.txt postgres-requirements.txt ./

# Use pip cache mount to avoid re-downloading packages across builds
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install --ignore-installed "setuptools>=82.0.1" \
    && pip install --prefix=/install -r requirements.txt -r postgres-requirements.txt

# =============================================================================
# Stage 2: Build ALASS CLI
# =============================================================================
FROM rust:1.95-slim-trixie AS alass-builder

ARG ALASS_CLI_VERSION

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo install alass-cli --version "${ALASS_CLI_VERSION}" --locked

# =============================================================================
# Stage 3: Production Image
# =============================================================================
FROM python:3.14-slim-trixie AS production

ARG BAZARR_VERSION
ARG BUILD_DATE
ARG VCS_REF

LABEL org.opencontainers.image.title="Bazarr+" \
      org.opencontainers.image.description="Bazarr+ - enhanced subtitle management" \
      org.opencontainers.image.version="${BAZARR_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.url="https://github.com/LavX/bazarr" \
      org.opencontainers.image.source="https://github.com/LavX/bazarr" \
      org.opencontainers.image.vendor="LavX" \
      org.opencontainers.image.licenses="GPL-3.0"

# Install runtime dependencies. RAR archives from subtitle providers are
# extracted via p7zip's 7z binary, so no non-free repo or unrar package needed.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libxml2 \
    libxslt1.1 \
    libpq5 \
    mediainfo \
    p7zip-full \
    bash \
    gosu \
    curl \
    && mkdir -p /app/bazarr/bin /config /defaults \
    && groupadd -g 1000 bazarr \
    && useradd -u 1000 -g bazarr -d /config -s /bin/bash bazarr

# Copy Python packages from builder (changes rarely)
COPY --from=python-builder /install /usr/local
COPY --from=alass-builder /usr/local/cargo/bin/alass-cli /usr/local/bin/alass-cli
RUN ln -s /usr/local/bin/alass-cli /usr/local/bin/alass

# Copy entrypoint and supervisor scripts (changes rarely)
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/supervisor.py /app/bazarr/docker/supervisor.py
RUN chmod +x /entrypoint.sh

# Set work directory
WORKDIR /app/bazarr

# Copy Bazarr-owned compatibility libraries before main app code
COPY custom_libs ./custom_libs
COPY migrations ./migrations

# Copy main application code (changes most frequently - keep at end)
COPY bazarr.py ./
COPY bazarr ./bazarr

# Write version to VERSION file so bazarr/main.py can read it
RUN echo "${BAZARR_VERSION}" > /app/bazarr/VERSION

# Copy package identification file (shows version in System Status)
COPY package_info /app/bazarr/package_info

# Copy pre-built frontend (built in GitHub Actions workflow for caching)
# This layer only rebuilds when frontend/build changes
COPY frontend/build ./frontend/build

# Set environment variables
ENV HOME="/config" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Volume for persistent data
VOLUME /config

# Expose port
EXPOSE 6767

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:6767/_supervisor/status | grep -q '"running"' || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "docker/supervisor.py", "--no-update", "--config", "/config"]
