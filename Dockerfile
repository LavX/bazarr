# =============================================================================
# Bazarr LavX Fork - Production Docker Image
# =============================================================================
# Multi-stage build for optimized image size
# Based on Debian Slim for better compatibility (unrar, etc.)
# =============================================================================

ARG BAZARR_VERSION=latest
ARG BUILD_DATE
ARG VCS_REF

# =============================================================================
# Stage 1: Build Frontend
# =============================================================================
FROM node:20-slim AS frontend-builder

WORKDIR /app

# Install dependencies first for better caching
COPY frontend/package*.json ./frontend/
RUN cd frontend && npm ci

# Copy frontend source and build
COPY frontend ./frontend/
RUN cd frontend && npm run build

# =============================================================================
# Stage 2: Install Python Dependencies
# =============================================================================
FROM python:3.12-slim-bookworm AS python-builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libpq-dev \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install Python packages
COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# =============================================================================
# Stage 3: Production Image
# =============================================================================
FROM python:3.12-slim-bookworm AS production

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

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
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
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/bazarr/bin /config /defaults \
    && groupadd -g 1000 bazarr \
    && useradd -u 1000 -g bazarr -d /config -s /bin/bash bazarr

# Copy Python packages from builder
COPY --from=python-builder /install /usr/local

# Copy application code
WORKDIR /app/bazarr
COPY bazarr.py ./
COPY libs ./libs
COPY custom_libs ./custom_libs
COPY bazarr ./bazarr
COPY migrations ./migrations

# Copy fork identification file (shows "LavX Fork" in System Status)
COPY package_info /app/bazarr/package_info

# Copy frontend build
COPY --from=frontend-builder /app/frontend/build ./frontend/build

# Copy entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Set environment variables
ENV HOME="/config" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Volume for persistent data
VOLUME /config

# Expose port
EXPOSE 6767

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:6767/api/system/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "bazarr.py", "--no-update", "--config", "/config"]