# Bazarr+ for CasaOS and ZimaOS

For **one install with Bazarr+, AI Subtitle Translator and FlareSolverr already
connected**, use [Bazarr+ Stack](Apps/BazarrPlusStack/docker-compose.yml). Its
[installation guide](../stack/README.md) explains setup and storage; its
[native test receipt](../stack/casaos-validation.md) records CasaOS verification.

The standalone Bazarr+ source package remains available at
[`Apps/BazarrPlus/docker-compose.yml`](Apps/BazarrPlus/docker-compose.yml).
It follows the current ZimaOS App Store v2 source format: standard Docker
Compose runtime settings and top-level `x-casaos` metadata. The service-level
`x-casaos` block is retained only for legacy v1 clients; the official v2 build
removes it.

## What it installs

- App identity: `com.lavx.bazarr-plus`, separate from other Bazarr images.
- Image: `ghcr.io/lavx/bazarr:2.7.0`, verified on 2026-09-28 as an OCI index
  supporting `linux/amd64` and `linux/arm64`.
- Web interface: HTTP on host port `6767` to container port `6767`.
- Persistent state: `/DATA/AppData/$AppID/config` mounted at `/config`.
- Runtime identity: `PUID` and `PGID` default to `1000`; `TZ` defaults to `UTC`.
- No translator, FlareSolverr, media manager, or media mount is required.

The container starts with only `CHOWN`, `SETUID`, and `SETGID` capabilities so
the entrypoint can prepare `/config` and drop to `PUID:PGID`. It then runs the
application without capabilities. The Compose file does not set `user:`, which
would prevent that startup sequence from working as designed.

## Optional media paths

The default package mounts only `/config`, so it can start before any library
paths are chosen. To give Bazarr+ access to media, add the relevant bind mounts
under the service's `volumes:` list in the app editor or source compose file.
Use paths that exist on the host, and keep them writable so Bazarr+ can save
subtitle files beside the media:

```yaml
      - type: bind
        source: /DATA/Media/Movies
        target: /movies
      - type: bind
        source: /DATA/Media/TV
        target: /tv
      - type: bind
        source: /DATA/Media/Sports
        target: /sports
```

Add only paths used on that system. Then select the corresponding container
paths (`/movies`, `/tv`, and `/sports`) when configuring Bazarr+.

## Existing Bazarr installations

Port `6767` is also the usual Bazarr port. If another Bazarr instance is
already running, stop it before installing this app or change the host-side
`published` port and matching `x-casaos.port_map` together. Keep the container
port at `6767`.

The package uses its own configuration directory. Do not point two running
instances at the same `/config`. Before moving an existing Bazarr configuration
to Bazarr+, stop the old instance, make a backup, and copy the configuration
into this app's directory. Start only one instance against that copied
configuration.

## Platform compatibility

The package targets the current ZimaOS App Store v2 source protocol. The
official v2 guide describes creation of a ZimaOS-compatible third-party store,
while the official repository separately retains a generated v1 artifact for
older clients. This does not establish that the v2 package is accepted by a
current CasaOS app catalog.

CasaOS v0.4.15 was installed on Debian 12 in an isolated VM. Its `Custom Install`
screen exposed a field-by-field manual form, but the official AppManagement API
and `casaos-cli app-management install` accepted the exact package Compose file.
The native app reached healthy status over HTTP. AppID config-path resolution,
numeric PUID/PGID, runtime UID/GID, security settings, and saved synthetic Arr
connections were verified, including persistence across a native app restart.
No media mount was present in the package, so media-file writes were not tested.
App Store v2 catalog ingestion, upgrade, and uninstall remain separate and
untested. See the [native CasaOS validation receipt](casaos-native-validation.md).

ZimaOS 1.7.1 was installed in an isolated VM, but package import and runtime
checks remain pending first-run privacy-policy consent. See the
[native ZimaOS validation receipt](native-validation.md). Native mount-editor
and upgrade behavior remain untested.

## Submitting or hosting the app

To propose this app to the official store, fork
[IceWhaleTech/CasaOS-AppStore](https://github.com/IceWhaleTech/CasaOS-AppStore),
copy `Apps/BazarrPlus/` into that repository's `Apps/` directory, then run
`./scripts/build_dist.sh` from the repository root. Confirm that the build
generates `dist/apps/com.lavx.bazarr-plus/` and that its summary reports no app
errors. Open a pull request with the change type, description, and validation
result.

For a separate third-party store, copy the app directory into that store's
`Apps/` tree, add the store-level `store-config.json` and
`supported-languages.json`, run the official build action, and publish the
generated `dist/` directory over HTTPS. This package is the app source only,
not a complete store repository.

## Official schema sources

The references below are pinned to IceWhaleTech/CasaOS-AppStore main commit
`0909364b800950030e71ea82355a5969a1c08b39`, read on 2026-09-28:

- [Compose and `x-casaos` schema](https://github.com/IceWhaleTech/CasaOS-AppStore/blob/0909364b800950030e71ea82355a5969a1c08b39/docs/specs/compose-and-x-casaos.md)
- [ZimaOS v2 store quick start](https://github.com/IceWhaleTech/CasaOS-AppStore/blob/0909364b800950030e71ea82355a5969a1c08b39/docs/quick-start/overview.md)
- [v1 and v2 compatibility FAQ](https://github.com/IceWhaleTech/CasaOS-AppStore/blob/0909364b800950030e71ea82355a5969a1c08b39/docs/faq/overview.md)
- [Official contribution and validation instructions](https://github.com/IceWhaleTech/CasaOS-AppStore/blob/0909364b800950030e71ea82355a5969a1c08b39/CONTRIBUTING.md)
- [Official app example with service-level legacy metadata](https://github.com/IceWhaleTech/CasaOS-AppStore/blob/0909364b800950030e71ea82355a5969a1c08b39/Apps/ActualBudget/docker-compose.yml)

The package includes `Apps/BazarrPlus/icon.png` and
`Apps/BazarrPlus/thumbnail.png`, copied from the existing Bazarr+ site assets
without editing or generating new artwork. The v2 metadata uses the reachable
site URL for its source icon and the local thumbnail filename:

- [Source icon](https://lavx.github.io/bazarr/screenshots/logo128.png)
- [Source thumbnail](https://lavx.github.io/bazarr/screenshots/github-social-preview.png)

## Validation status

- `docker compose -f packaging/casaos/Apps/BazarrPlus/docker-compose.yml config --quiet`
  passed on 2026-09-28 with `AppID=com.lavx.bazarr-plus`.
- The GHCR tag returned HTTP 200 as an OCI index at
  `sha256:90a5c184b0af41602ff78ea7286e0c5f2c4c284c9b18f71427d9ddbeb0b6531c`;
  its runnable platforms include `linux/amd64` and `linux/arm64`.
- Both packaged PNGs are byte-identical to the linked source assets.
- The official App Store build was not run. CasaOS v0.4.15 native Compose
  installation passed; App Store v2 catalog ingestion remains untested. See the
  [native CasaOS validation receipt](casaos-native-validation.md). ZimaOS 1.7.1
  package import and runtime checks remain pending first-run privacy-policy
  consent. See the [native ZimaOS validation receipt](native-validation.md).
  Native mount-editor and upgrade behavior remain untested.
