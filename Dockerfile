# =============================================================================
# Bazarr LavX Fork - Production Dockerfile
# =============================================================================
# Multi-stage build for optimal image size
# Includes: Python backend + Pre-built React frontend
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Frontend Builder (if not pre-built externally)
# -----------------------------------------------------------------------------
FROM node:20-alpine AS frontend-builder

WORKDIR /app

# Copy frontend package files
COPY frontend/package*.json ./
COPY frontend/.nvmrc ./

# Install dependencies
RUN npm ci

# Copy frontend source
COPY frontend/ ./

# Build production frontend
RUN npm run build

# -----------------------------------------------------------------------------
# Stage 2: Python Dependencies Builder
# -----------------------------------------------------------------------------
FROM alpine:3.22 AS python-builder

# Install build dependencies
RUN apk add --no-cache \
    build-base \
    cargo \
    libffi-dev \
    libpq-dev \
    libxml2-dev \
    libxslt-dev \
    python3-dev \
    py3-pip

WORKDIR /build

# Copy requirements
COPY requirements.txt postgres-requirements.txt ./

# Create virtual environment and install dependencies
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir \
        --find-links https://wheel-index.linuxserver.io/alpine-3.22/ \
        -r requirements.txt \
        -r postgres-requirements.txt

# -----------------------------------------------------------------------------
# Stage 3: Production Runtime
# -----------------------------------------------------------------------------
FROM alpine:3.22 AS production

# Build arguments
ARG BAZARR_VERSION=dev
ARG BUILD_DATE
ARG VCS_REF

# Labels for container metadata
LABEL org.opencontainers.image.title="Bazarr (LavX Fork)" \
      org.opencontainers.image.description="Bazarr with OpenSubtitles.org scraper support" \
      org.opencontainers.image.version="${BAZARR_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="LavX" \
      org.opencontainers.image.source="https://github.com/LavX/bazarr" \
      org.opencontainers.image.licenses="GPL-3.0"

# Install runtime dependencies only
RUN apk add --no-cache \
    ffmpeg \
    libxml2 \
    libxslt \
    libpq \
    mediainfo \
    python3 \
    p7zip \
    unrar \
    bash \
    su-exec \
    tzdata && \
    # Create directories
    mkdir -p \
        /app/bazarr/bin \
        /config \
        /defaults && \
    # Create non-root user
    addgroup -g 1000 bazarr && \
    adduser -u 1000 -G bazarr -h /config -D bazarr

# Copy Python virtual environment from builder
COPY --from=python-builder /opt/venv /opt/venv

# Set Python path to use virtual environment
ENV PATH="/opt/venv/bin:$PATH"
ENV VIRTUAL_ENV="/opt/venv"

# Set working directory
WORKDIR /app/bazarr/bin

# Copy application code
COPY bazarr.py ./
COPY libs ./libs
COPY custom_libs ./custom_libs
COPY bazarr ./bazarr
COPY migrations ./migrations

# Copy fork identification file (shows "LavX Fork" in System Status)
COPY package_info /app/bazarr/package_info

# Copy pre-built frontend from builder stage
# Note: In CI, this might be replaced by artifact download
COPY --from=frontend-builder /app/build ./frontend/build

# Alternative: Copy from local if pre-built (used in CI workflow)
# The frontend build directory should be at ./frontend/build
COPY frontend/build ./frontend/build 2>/dev/null || true

# Set Python path - custom_libs takes precedence over libs
ENV PYTHONPATH="/app/bazarr/bin/custom_libs:/app/bazarr/bin/libs:/app/bazarr/bin/bazarr:/app/bazarr/bin"

# Environment variables
ENV BAZARR_VERSION="${BAZARR_VERSION}" \
    SZ_USER_AGENT="bazarr-lavx" \
    # Config directory
    BAZARR_CONFIG="/config" \
    # Timezone (can be overridden)
    TZ="UTC" \
    # Python settings
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Volume for persistent data
VOLUME ["/config"]

# Expose Bazarr port
EXPOSE 6767

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:6767/api/system/health || exit 1

# Entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]