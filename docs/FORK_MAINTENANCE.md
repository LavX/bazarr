# Bazarr+ Maintenance Guide

Bazarr+ is a hard fork of [Bazarr](https://github.com/morpheus65535/bazarr). It shares the original codebase as a starting point but is developed independently. There is no automatic upstream synchronization.

## Relationship with Upstream

Bazarr+ may selectively cherry-pick bug fixes from upstream when relevant, but does not merge upstream releases wholesale. The codebases have diverged significantly in security model, UI, features, and architecture.

**When cherry-picking from upstream:**
1. Evaluate whether the fix applies to Bazarr+ (upstream may fix things we've already addressed differently)
2. Cherry-pick individual commits: `git cherry-pick <commit-sha>`
3. Review for conflicts with fork-specific code (security hardening, telemetry removal, UI changes)
4. Test thoroughly before merging

## Versioning

Bazarr+ uses independent semantic versioning, starting at v2.0.0. Versions are not tied to upstream Bazarr release numbers.

```
v{major}.{minor}.{patch}

Example: v2.0.0, v2.1.0, v2.0.1
```

- Patch (v2.0.1): bug fixes
- Minor (v2.1.0): new features, backwards-compatible
- Major (v3.0.0): breaking changes

## Docker Build (`build-docker.yml`)

**Triggers:**
- Push to `master` branch
- Manual dispatch
- Tag creation (for releases)

**Output:**
- `ghcr.io/lavx/bazarr:latest` - Latest build
- `ghcr.io/lavx/bazarr:X.Y.Z` - Versioned build
- `ghcr.io/lavx/bazarr:sha-XXXXXXX` - Git SHA reference

## Branch Model

- `master` contains stable releases
- `development` is the integration branch where new features land
- Feature branches are created from `development` and merged back via PR

## Fork-Specific Files

Key files that define Bazarr+ and differentiate it from upstream:

| File | Purpose |
|------|---------|
| `custom_libs/subliminal_patch/providers/opensubtitles_scraper.py` | OpenSubtitles.org scraper mixin |
| `custom_libs/subliminal_patch/providers/opensubtitles.py` | Modified provider with scraper support |
| `opensubtitles-scraper/` | Git submodule: web scraper service |
| `ai-subtitle-translator/` | Git submodule: AI translator service |
| `package_info` | Fork identification (shown in System Status) |
| `bazarr/app/check_update.py` | Uses fork's releases, not upstream |
| `.github/workflows/build-docker.yml` | Docker build workflow |
| `Dockerfile` | Production Docker image (Python 3.14) |
| `docker-compose.yml` | User deployment template |
| `bazarr/utilities/analytics.py` | Deleted (contained GA4 + UA telemetry) |

## Auto-Update Behavior

### Docker: Auto-Update is Disabled

The Docker image runs with `--no-update` flag. Update by pulling new images:

```bash
docker compose pull
docker compose up -d
```

### Release Repository

The fork checks releases from `LavX/bazarr`:

```python
# In bazarr/app/check_update.py
RELEASES_REPO = os.environ.get('BAZARR_RELEASES_REPO', 'LavX/bazarr')
```

## Troubleshooting

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

## Related Links

- [Upstream Bazarr Repository](https://github.com/morpheus65535/bazarr) (original, not synced)
- [GitHub Container Registry](https://ghcr.io/lavx/bazarr)
- [Contributing Guide](../CONTRIBUTING.md)
