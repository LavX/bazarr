# Bazarr LavX Fork - Maintenance Guide

This document describes the automated workflow for maintaining this fork of [Bazarr](https://github.com/morpheus65535/bazarr).

## Overview

This fork contains custom modifications (primarily the OpenSubtitles.org web scraper provider) that are automatically kept in sync with the upstream Bazarr repository. The workflow handles:

1. **Upstream Synchronization** - Daily automatic merging of upstream changes
2. **Conflict Resolution** - Automatic PR creation when merge conflicts occur
3. **Docker Builds** - Automated multi-architecture Docker image builds
4. **Publishing** - Images published to GitHub Container Registry

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           GitHub Actions Workflows                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────┐         ┌─────────────────────────────────────┐   │
│  │  sync-upstream.yml  │         │       build-docker.yml              │   │
│  │                     │         │                                     │   │
│  │  • Daily 4 AM UTC   │────────>│  • Build frontend                   │   │
│  │  • Fetch upstream   │ trigger │  • Build Docker image               │   │
│  │  • Auto merge       │         │  • Push to ghcr.io                  │   │
│  │  • Conflict PR      │         │  • multi-arch (amd64/arm64)         │   │
│  └─────────────────────┘         └─────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Workflows

### 1. Upstream Sync (`sync-upstream.yml`)

**Schedule:** Daily at 4:00 AM UTC (5:00 AM Budapest time)

**Triggers:**
- Scheduled cron job
- Manual dispatch from GitHub Actions UI

**Process:**
1. Fetches latest commits from `morpheus65535/bazarr:master`
2. Compares with current fork
3. Attempts automatic merge
4. If successful: pushes changes and triggers Docker build
5. If conflicts: creates a PR with `sync-conflict` label

**Manual Trigger:**
```bash
# Via GitHub CLI
gh workflow run sync-upstream.yml

# With force sync (even if no new commits)
gh workflow run sync-upstream.yml -f force_sync=true
```

### 2. Docker Build (`build-docker.yml`)

**Triggers:**
- Push to `main` branch
- Called by sync workflow after successful merge
- Manual dispatch
- Tag creation (for releases)

**Output:**
- `ghcr.io/lavx/bazarr:latest` - Latest build
- `ghcr.io/lavx/bazarr:vX.Y.Z-lavx.YYYYMMDD` - Versioned build
- `ghcr.io/lavx/bazarr:sha-XXXXXXX` - Git SHA reference

## Using the Docker Image

### Quick Start

```bash
docker run -d \
  --name bazarr \
  -p 6767:6767 \
  -v /path/to/config:/config \
  -v /path/to/movies:/movies \
  -v /path/to/tv:/tv \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=Europe/Budapest \
  ghcr.io/lavx/bazarr:latest
```

### Docker Compose

Create a `docker-compose.yml`:

```yaml
services:
  bazarr:
    image: ghcr.io/lavx/bazarr:latest
    container_name: bazarr
    restart: unless-stopped
    ports:
      - "6767:6767"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/Budapest
    volumes:
      - ./config:/config
      - /path/to/movies:/movies
      - /path/to/tv:/tv
```

Then run:
```bash
docker compose up -d
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | `1000` | User ID for file permissions |
| `PGID` | `1000` | Group ID for file permissions |
| `TZ` | `UTC` | Timezone (e.g., `Europe/Budapest`) |

## Handling Merge Conflicts

When upstream changes conflict with fork modifications:

1. **Notification:** A PR is automatically created with the `sync-conflict` label
2. **Review:** Check the PR for conflict markers
3. **Fix locally:**
   ```bash
   git fetch origin sync/upstream-XXXXXXXX
   git checkout sync/upstream-XXXXXXXX
   # Resolve conflicts in your editor
   git add .
   git commit -m "Resolve merge conflicts"
   git push origin sync/upstream-XXXXXXXX
   ```
4. **Merge:** Merge the PR via GitHub UI
5. **Build:** Docker build will trigger automatically

## Fork-Specific Files

These files are unique to this fork and should be preserved during merges:

| File | Purpose |
|------|---------|
| `custom_libs/subliminal_patch/providers/opensubtitles_scraper.py` | OpenSubtitles.org scraper mixin |
| `custom_libs/subliminal_patch/providers/opensubtitles.py` | Modified provider with scraper support |
| `opensubtitles-scraper/` | Git submodule - web scraper service |
| `package_info` | Fork identification (shown in System Status) |
| `bazarr/app/check_update.py` | Modified to use fork's releases |
| `.github/workflows/sync-upstream.yml` | Upstream sync workflow |
| `.github/workflows/build-docker.yml` | Docker build workflow |
| `Dockerfile` | Production Docker image |
| `docker-compose.yml` | User deployment template |
| `.gitattributes` | Merge conflict protection rules |

## OpenSubtitles Scraper Service

This fork includes the [OpenSubtitles Scraper](https://github.com/LavX/opensubtitles-scraper) as a git submodule. The scraper is a standalone service that provides web scraping capabilities for OpenSubtitles.org.

### Architecture

```
┌────────────────────┐     HTTP API     ┌─────────────────────────┐
│      Bazarr        │ ───────────────> │  OpenSubtitles Scraper  │
│  (LavX Fork)       │                  │    (Port 8765)          │
│                    │ <─────────────── │                         │
│  Uses provider:    │   JSON Response  │  Scrapes:               │
│  opensubtitles.org │                  │  - opensubtitles.org    │
└────────────────────┘                  └─────────────────────────┘
```

### Docker Compose Deployment

The `docker-compose.yml` includes both services:

```yaml
services:
  opensubtitles-scraper:
    image: ghcr.io/lavx/opensubtitles-scraper:latest
    ports:
      - "8765:8765"
    
  bazarr:
    image: ghcr.io/lavx/bazarr:latest
    depends_on:
      - opensubtitles-scraper
    environment:
      - OPENSUBTITLES_SCRAPER_URL=http://opensubtitles-scraper:8765
```

### Updating the Scraper Submodule

To update the scraper to the latest version:

```bash
cd opensubtitles-scraper
git pull origin main
cd ..
git add opensubtitles-scraper
git commit -m "Update opensubtitles-scraper submodule"
git push
```

## Versioning

This fork uses a versioning scheme that combines upstream version with fork identifier:

```
{upstream_version}-lavx.{date}

Example: v1.5.3-lavx.20241214
```

This makes it clear:
- Which upstream version the build is based on
- When the fork build was created
- That it contains fork-specific modifications

## Auto-Update Behavior

### Important: Auto-Update is Disabled in Docker

The Docker image runs with `--no-update` flag to prevent Bazarr's built-in update mechanism from overwriting your fork modifications. **This is intentional.**

### How Updates Work

| Scenario | Behavior |
|----------|----------|
| **Docker Container** | Auto-update disabled; use new Docker image for updates |
| **Manual Installation** | Auto-update can be enabled, but will pull from this fork |
| **Release Info in UI** | Shows releases from this fork (LavX/bazarr) |

### Release Repository Configuration

The fork is configured to check releases from `LavX/bazarr` instead of upstream. This is controlled by:

```python
# In bazarr/app/check_update.py
RELEASES_REPO = os.environ.get('BAZARR_RELEASES_REPO', 'LavX/bazarr')
```

To change the release source (e.g., for debugging), set the environment variable:

```yaml
# docker-compose.yml
environment:
  - BAZARR_RELEASES_REPO=morpheus65535/Bazarr  # Use upstream releases
```

### Updating Docker Containers

To update to a new version:

```bash
# Pull the latest image
docker compose pull

# Recreate the container
docker compose up -d

# Or in one command
docker compose up -d --pull always
```

### Why Docker Doesn't Auto-Update

1. **Preservation of modifications**: Auto-update would download vanilla Bazarr, losing the OpenSubtitles scraper
2. **Immutable containers**: Docker best practices recommend replacing containers rather than modifying them
3. **Reproducibility**: Pinned versions ensure consistent behavior
4. **Rollback capability**: Easy to rollback by pulling a specific tag

## Troubleshooting

### Sync Workflow Fails

1. Check the workflow logs in GitHub Actions
2. Verify upstream repository is accessible
3. Check if there are unresolved conflicts from previous sync

### Docker Build Fails

1. Check if frontend build succeeded
2. Verify all required files are present
3. Check for syntax errors in Dockerfile

### Image Pull Issues

```bash
# Login to GitHub Container Registry
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

# Pull the image
docker pull ghcr.io/lavx/bazarr:latest
```

## Contributing

When making changes to fork-specific files:

1. Test changes locally first
2. Ensure changes don't conflict with upstream structure
3. Document any new environment variables or features
4. Update this documentation if workflow changes

## Related Links

- [Upstream Bazarr Repository](https://github.com/morpheus65535/bazarr)
- [GitHub Container Registry](https://ghcr.io/lavx/bazarr)
- [Bazarr Wiki](https://wiki.bazarr.media)