# Native CasaOS validation receipt

Date: 2026-09-29

## Result

CasaOS v0.4.15 installed successfully on Debian 12 genericcloud amd64 build
`20260923-2610`, using the official CasaOS installer. The installer returned
success and reported the CasaOS, Gateway, UserService, AppManagement,
local-storage, message-bus, and Rclone services running. The native UI returned
HTTP 200 and the App Store opened.

The installer also printed nonfatal first-install migration and setup warnings
(`CURRENT_BIN_FILE_LEGACY_NOT_FOUND` and missing `debian/bookworm` working
directories). It nevertheless returned success and its service checks passed.

The App Store's `Custom Install` screen remains a field-by-field `Manual App
Install` form. It did not expose a Compose/YAML upload control. The official
CasaOS AppManagement API and guest CLI do support a separate native Compose
route: `POST /v2/app_management/compose` accepts `application/yaml`, and
`casaos-cli app-management install --file <compose.yml>` invokes that install
path. The official [AppManagement OpenAPI specification](https://github.com/IceWhaleTech/CasaOS-AppManagement/blob/main/api/app_management/openapi.yaml)
documents the API, and the [CasaOS CLI](https://github.com/IceWhaleTech/CasaOS-CLI)
documents its app-management commands.

This successful CLI/API acceptance supersedes the earlier UI-only outcome. The
manual form remains field-based; the native Compose route is separate.

The exact package manifest was copied into the guest and its SHA-256 matched
the worktree file (`b62ec4e90688276f081f598b64adec02ad31cf372a3e27e95acbca0eedfcf58d`).
The CLI dry-run passed, followed by a successful native install. CasaOS listed
`bazarr-plus` as running; Docker reported the container healthy and its web
interface returned HTTP 200. CasaOS resolved the config mount to
`/DATA/AppData/bazarr-plus/config:/config`. The container received `PUID=1000`,
`PGID=1000`, and `TZ=Etc/UTC`; its Python processes ran as UID/GID 1000. The
read-only root filesystem, `no-new-privileges`, dropped capabilities, and the
three required startup capabilities were preserved.

Connections to the owner's real Sonarr and Radarr through a read-only test
bridge passed their connection tests. Both
records and the `use_sonarr` / `use_radarr` settings survived a native CasaOS
app restart, and both connection tests passed again afterward. No media bind
mount was included in the exact package, so media-file writes were not tested.
After independent HTTP and container-health inspection, the VM was shut down
gracefully. Its disk and installed test app/config were retained; its forwarded
ports were confirmed closed.
Native uninstall and upgrade behavior were not tested.

No privacy-policy or terms consent prompt appeared during first-run setup. A
disposable local CasaOS test account was used; no external account was created.
The App Store v2 catalog build/ingestion route was not exercised and remains
separate from manual native Compose deployment.

## Host and installer evidence

- Debian image: [official pinned Debian 12 genericcloud amd64 QCOW2](https://cloud.debian.org/images/cloud/bookworm/20260923-2610/debian-12-genericcloud-amd64-20260923-2610.qcow2)
- Checksum manifest: [official SHA512SUMS](https://cloud.debian.org/images/cloud/bookworm/20260923-2610/SHA512SUMS). Verified SHA-512: `3d94c9dd66d8a283fde060b8810373b7ae04038b956ee00d553d1ae564f6fbdeaa2fd6e7404e87e53348b5a9509170f3ea7c0731ba737590c5c4cb8d559a47f8`.
- CasaOS installer: [official installer source](https://github.com/IceWhaleTech/get/blob/main/casaos.sh), invoked inside the guest with `curl -fsSL https://get.casaos.io | sudo bash`.
- CasaOS version reported by the installer and guest CLI: `v0.4.15`.
- Resources: 2 vCPU, 2 GiB RAM, 24 GiB virtual disk.
- The test used fresh VM storage and no real media or application configuration.

## Separate catalog boundary

The App Store v2 catalog build and ingestion route was not exercised. The native
Compose install above does not establish v2 catalog acceptance. No external
account or host media was used.
