# =============================================================================
# Bazarr LavX Fork - Production Docker Image
# =============================================================================
# Multi-stage build optimized for layer caching
# Based on Debian Slim for better compatibility (unrar, etc.)
# =============================================================================

ARG BAZARR_VERSION=latest
ARG BUILD_DATE
ARG VCS_REF

# =============================================================================
# Stage 1: Install Python Dependencies (cached heavily)
# =============================================================================
FROM python:3.14-slim-bookworm AS python-builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libpq-dev \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy ONLY requirements first for maximum caching
# This layer will only rebuild when requirements.txt changes
COPY requirements.txt ./

# Use pip cache mount to avoid re-downloading packages across builds
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install -r requirements.txt

# =============================================================================
# Stage 2: Production Image
# =============================================================================
FROM python:3.14-slim-bookworm AS production

ARG BAZARR_VERSION
ARG BUILD_DATE
ARG VCS_REF

LABEL org.opencontainers.image.title="Bazarr (LavX Fork)" \
      org.opencontainers.image.description="Bazarr with OpenSubtitles.org scraper support" \
      org.opencontainers.image.version="${BAZARR_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.url="https://github.com/LavX/bazarr" \
      org.opencontainers.image.source="https://github.com/LavX/bazarr" \
      org.opencontainers.image.vendor="LavX" \
      org.opencontainers.image.licenses="GPL-3.0"

# Enable non-free repository for unrar and install runtime dependencies
# Use apt cache mount to speed up repeated builds
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    sed -i 's/Components: main/Components: main non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libxml2 \
    libxslt1.1 \
    libpq5 \
    mediainfo \
    p7zip-full \
    unrar \
    bash \
    gosu \
    curl \
    && mkdir -p /app/bazarr/bin /config /defaults \
    && groupadd -g 1000 bazarr \
    && useradd -u 1000 -g bazarr -d /config -s /bin/bash bazarr

# Copy Python packages from builder (changes rarely)
COPY --from=python-builder /install /usr/local

# Copy entrypoint script (changes rarely)
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Set work directory
WORKDIR /app/bazarr

# Copy libs directories (change less frequently than main app code)
COPY libs ./libs
COPY custom_libs ./custom_libs
COPY migrations ./migrations

# Copy main application code (changes most frequently - keep at end)
COPY bazarr.py ./
COPY bazarr ./bazarr

# Write version to VERSION file so bazarr/main.py can read it
RUN echo "${BAZARR_VERSION}" > /app/bazarr/VERSION

# Copy fork identification file (shows "LavX Fork" in System Status)
COPY package_info /app/bazarr/package_info

# Copy pre-built frontend (built in GitHub Actions workflow for caching)
# This layer only rebuilds when frontend/build changes
COPY frontend/build ./frontend/build

# Set environment variables
# PYTHON_JIT=1 enables the experimental JIT compiler (Python 3.13+)
ENV HOME="/config" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHON_JIT=1

# Volume for persistent data
VOLUME /config

# Expose port
EXPOSE 6767

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:6767/api/system/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "bazarr.py", "--no-update", "--config", "/config"]