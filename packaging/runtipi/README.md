# Runtipi package for Bazarr+

This package defines a standalone Bazarr+ app for a custom or community Runtipi app store. The official Runtipi App Store currently says that it accepts bug fixes only and directs new apps to custom or community stores. This package is therefore not represented as an accepted official-store submission.

To use it, place `bazarr-plus/` under `apps/bazarr-plus/` in an app store based on Runtipi's official template. The folder and `config.json` ID are both `bazarr-plus`, so the app remains distinct from the official `bazarr` entry.

Before starting it, make sure host port `6767` is available. If migrating from another Bazarr instance, back up its configuration and stop it before opening that configuration with this image. Never run two Bazarr containers against the same live `/config` directory. This package does not perform an automatic configuration migration.

## Runtime definition

- Image: `ghcr.io/lavx/bazarr:2.7.0`, pinned to the requested release tag.
- Web interface: container port `6767`, exposed through Runtipi's main-service routing.
- Persistent configuration: `${APP_DATA_DIR}/config` mounted at `/config`.
- Media: Runtipi's shared `${ROOT_FOLDER_HOST}/media` mounted once at `/media`.
- File identity: install fields supply `PUID` and `PGID` to the image entrypoint. Do not set Compose `user`; the image starts its entrypoint as root, prepares `/config`, then switches to the requested numeric IDs with `gosu`.
- Hardening: read-only image filesystem, a size-limited `/tmp` tmpfs, `no-new-privileges`, all capabilities dropped except `CHOWN`, `SETUID`, and `SETGID` for the entrypoint's ownership and user switch.
- Dependencies: none. The package installs only Bazarr+. Sonarr, Radarr, Sportarr, a translation service, and FlareSolverr are not required or provisioned.

Runtipi supplies one shared media root to this app definition, not three independently selectable host paths. Configure any movie, TV, or sports roots in Bazarr+ using their existing paths beneath `/media`, for example `/media/Movies`, `/media/TV`, or `/media/Sports`. The host folders are optional, but this template does not create separate optional bind fields for them.

Native testing on Runtipi 4.10.2 found that the documented `RUNTIPI_MEDIA_DIR` variable is not supplied to app Compose files. This package uses `ROOT_FOLDER_HOST/media`, matching the bundled upstream Bazarr package, so installation does not depend on an undefined variable.

If you already run an optional translator service or FlareSolverr, configure it from Bazarr+ after installation. This app definition does not install, configure, or depend on either service.

## Assets

Runtipi requires a square `metadata/logo.jpg`. It is made from the repository's existing `site/screenshots/logo128.png`. The source URL returned HTTP 200 with `image/png` on 2026-09-28. The existing social preview is also available at [github-social-preview.png](https://lavx.github.io/bazarr/screenshots/github-social-preview.png), verified with HTTP 200 and `image/png` on the same date. The social preview is a reference asset and is not copied into the app because Runtipi's app metadata uses the square logo.

## Validation status and remaining platform checks

The config is checked against Runtipi's current `app-info-schema.json`; the Compose file is parsed by Docker Compose and its `x-runtipi` fields are reviewed against the current dynamic Compose reference. These checks validate the package format, not behavior on a Runtipi server.

At the pinned app-store revision below, `scripts/validate-json.js` fetches `https://schemas.runtipi.io/dynamic-compose.json` but scans legacy `docker-compose.json` files only. It does not validate this package's `docker-compose.yml`. The current documented toolchain did not provide a separate YAML schema validator, so native Runtipi installation remains required to verify platform interpretation.

Native testing on Runtipi 4.10.2 passed store discovery, UI installation, onboarding, startup health, UID/GID and mount permissions, stop/start persistence, backup/restore, and uninstall after correcting the media variable. See [native validation](native-validation.md) for the failed first attempt, exact checks, and remaining gaps. An application-version upgrade, upstream database migration, arm64 runtime, and older Runtipi versions remain untested.

## Upstream references

The upstream repository branch heads were read on 2026-09-28. The links below pin the exact revisions used for this package review.

- Official app store README at [`5d30931cd764c29fcbaa092b4197fc6dce530e3b`](https://github.com/runtipi/runtipi-appstore/blob/5d30931cd764c29fcbaa092b4197fc6dce530e3b/README.md). It states that new apps are not accepted and points maintainers to custom or community stores.
- Official app-info JSON Schema at [`5d30931cd764c29fcbaa092b4197fc6dce530e3b`](https://github.com/runtipi/runtipi-appstore/blob/5d30931cd764c29fcbaa092b4197fc6dce530e3b/apps/app-info-schema.json).
- Official app-store JSON validation script at [`5d30931cd764c29fcbaa092b4197fc6dce530e3b`](https://github.com/runtipi/runtipi-appstore/blob/5d30931cd764c29fcbaa092b4197fc6dce530e3b/scripts/validate-json.js); it validates legacy `docker-compose.json` entries.
- Existing official Bazarr metadata at [`5d30931cd764c29fcbaa092b4197fc6dce530e3b`](https://github.com/runtipi/runtipi-appstore/blob/5d30931cd764c29fcbaa092b4197fc6dce530e3b/apps/bazarr/config.json) and its [Compose definition](https://github.com/runtipi/runtipi-appstore/blob/5d30931cd764c29fcbaa092b4197fc6dce530e3b/apps/bazarr/docker-compose.yml). That entry uses port 6767, maps one media root to `/media`, and sets PUID/PGID.
- Current [config.json reference](https://runtipi.io/docs/reference/config-json), [dynamic Compose reference](https://runtipi.io/docs/reference/dynamic-compose), and [custom app store guide](https://runtipi.io/docs/guides/create-your-own-app-store), from the Runtipi docs repository head [`c063ce97588495d06152824719b15c5b607664be`](https://github.com/runtipi/runtipi-docs/tree/c063ce97588495d06152824719b15c5b607664be).
- The [Runtipi example app store](https://github.com/runtipi/example-appstore) is the official store template referenced by the custom app store guide.
