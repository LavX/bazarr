# Combined-stack validation

Validated on September 29, 2026 with Docker Compose 5.5.1 on Linux amd64, using dedicated volumes, a separate network and loopback-only port 16775. No existing application configuration or media was mounted.

## Pinned images

| Service | Version | Registry index digest |
| --- | --- | --- |
| Bazarr+ | 2.7.0 | `sha256:90a5c184b0af41602ff78ea7286e0c5f2c4c284c9b18f71427d9ddbeb0b6531c` |
| AI Subtitle Translator | v2.1.1 | `sha256:e22abba9625b96df6727354f10933da312a084ffeae68f0eb1f9a82492f5e48d` |
| FlareSolverr | v3.5.2 | `sha256:c80ae007ce2ccdcd217a12426e4f039ef763ff90738c808d38810c3e59323767` |

## Passed

- Compose validation and eight behavioral bootstrap tests: fresh key generation, saved settings preservation, refusal of unmanaged data, missing/mismatched keys, and symlink rejection.
- Fresh installation: initializer exits 0; all three application containers become healthy. The final initializer explicitly disables the image's HTTP healthcheck, which is not applicable to a completed one-shot process. Recreating only that initializer preserves existing configuration and reports Docker healthcheck `NONE`.
- Bazarr and translator processes run as UID/GID 1000:1000. Only Bazarr publishes a host port. Helper ports remain on the stack network.
- Independent initialization with PUID/PGID 568:568 creates Bazarr files owned by 568:568 while the translator key stays owned by 1000:1000. Config and key files are mode 0600, with the config directory mode 0700. These checks used the pinned image, read-only metadata inspection as each owner, and separate disposable volumes, which were removed afterward.
- Authenticated `GET /api/translator/status` through Bazarr returns HTTP 200, using the saved internal URL and generated shared key.
- A direct translator status request without authentication returns HTTP 401.
- FlareSolverr `/health` returns HTTP 200 and `status: ok`.
- FlareSolverr's real browser loads the stack's Bazarr supervisor endpoint, returning HTTP 200 and the expected running state.
- All nine intended provider sections contain the helper URL. The list matches the stable catalog snapshot `a7a849b234b16fbe75d79e364aec62e92c4053c5`.
- First-run onboarding stays available.
- A translator setting saved through Bazarr's API persists after removing and recreating the stack's containers. Initialization reports existing configuration preserved, and both helper connection checks pass again.

Reproduce the focused checks from the repository with:

```sh
python packaging/stack/test_bootstrap.py -v
docker compose -f packaging/stack/compose.yaml config -q
python packaging/stack/check_connections.py --project YOUR_STACK_PROJECT
```

The connection check uses local status endpoints and one FlareSolverr browser request to Bazarr. It does not submit subtitles to an AI model or use a provider account. The nine-default-URL check is intended for the fresh package fixture, not a customized installation.

## Boundaries

The shared-key handshake is proven; AI translation output is not. No paid requests or OpenRouter credentials were used. A working FlareSolverr browser is not evidence that any particular provider's current challenge can be solved. The [anonymous OpenSubtitles.org test](opensubtitles-validation.md) passed search, download and non-empty stream checks, with the fallback URL configured but not needed during the successful request.

The initializer validates that the generated key volume matches its original package marker. It intentionally preserves later Bazarr settings, including a user-selected external translator and key. Container health indicates service readiness, not whether a later custom connection or model works.

The initializer was independently reviewed against the published image contracts and its preservation behavior. Native CasaOS results are recorded in [casaos-validation.md](casaos-validation.md). An unresolved bind-IP variable was rejected by the native importer during the first dry-run. Native update also retained PUID/PGID placeholders literally. The import-ready variant therefore resolves all environment placeholders to explicit defaults. Generic Compose supports the variables as shipped.

The final generic manifest SHA-256 is `3ec5a5edf75bde1fd1e2b6df8dad4f96eb9fded1014d77c13e9e6cdc61a11cca`. The import-ready CasaOS manifest is `4410ed7d2e13bd60663e4d7dd44f68fd16dcac244f667bb0bbc924f6c01d3e05` and differs only by resolving the port, PUID/PGID and time-zone placeholders to their defaults.

The optional shared-network/media overlay passes Compose validation. This is a configuration example, not evidence of live connections to existing media managers or media servers; no existing networks or media were attached during this package test.

No catalog submission, release, production replacement, upstream-config migration, arm64 execution, or AI-output acceptance is implied by these checks.
