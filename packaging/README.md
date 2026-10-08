# Turnkey platform packages

Submission candidates for Bazarr+, separate from upstream Bazarr. These files do not mean the app is already listed in a platform's catalog.

| Platform | Package and handoff |
| --- | --- |
| Combined Bazarr+ / translator / FlareSolverr stack | [One-package install](stack/README.md) |
| Unraid Community Apps | [Unraid](unraid/README.md) |
| Runtipi | [Runtipi](runtipi/README.md) |
| CasaOS / ZimaOS | [CasaOS](casaos/README.md) |
| TrueNAS Apps | [TrueNAS](truenas/README.md) |

Each package uses the current released stable `ghcr.io/lavx/bazarr` image, pinned per the release lock, with persistent config storage and a distinct Bazarr+ app identity. The published image supports amd64 and arm64. Platform support is narrower where a catalog requires it.

Registry verification on September 28, 2026 resolved that tag to index digest `sha256:90a5c184b0af41602ff78ea7286e0c5f2c4c284c9b18f71427d9ddbeb0b6531c`. Recheck the tag before publishing the packages.

Choose the combined stack for one install with the translator and FlareSolverr already connected, or a standalone package if you only want Bazarr+. Movies, TV, and sports folders are optional. Add only the media storage you need, with write permissions for subtitle downloads.

Runtipi uses its shared media directory at `/media`; its package does not provide three separate optional host-path fields. The CasaOS/ZimaOS package targets the current ZimaOS v2 store format, with older CasaOS catalog compatibility still unverified. Runtipi currently directs new apps to custom/community stores rather than its official catalog.

## Acceptance before submission

- Validate the package with the target catalog's current tools.
- Install on the actual platform and open the WebUI.
- Confirm the application runs as the selected non-root UID/GID.
- Save settings, restart, and confirm persistence.
- Add a movie, TV, or sports folder and prove a subtitle can be written.
- Test a backed-up upstream config with the upstream container stopped.
- Verify uninstall behavior and tell users how to preserve their config.
- Publish the required package assets and submit through the platform's review process.

Local format checks are not a substitute for native-platform acceptance. See each package README for completed checks and remaining steps. No upstream submissions or production deployments are part of this preparation.

## Native test evidence

The [integration validation](integration-validation.md) covers authenticated Sonarr/Radarr connections, full metadata sync, and free-provider search/download checks in isolated native package installations.

- [Runtipi 4.10.2](runtipi/native-validation.md): native store import and UI installation, onboarding, runtime identity, media writes, stop/start, backup/restore, and uninstall passed after correcting an undefined media-path variable.
- [TrueNAS 25.10.7](truenas/native-validation.md): rendered Compose deployed through native Apps as a custom app. This is distinct from catalog ingestion.
- [CasaOS 0.4.15](casaos/casaos-native-validation.md): exact Compose installed through the native CLI/API; runtime identity, config path, security settings, and saved Arr connections after native restart passed. Catalog ingestion remains separate.
- [ZimaOS 1.7.1](casaos/native-validation.md): OS installation passed; first-run privacy consent is still required before package import.

Unraid native testing requires a user-provided licensed test environment. Its Docker smoke check and XML validation do not establish native Unraid acceptance. Application-version upgrades, existing upstream database migration, and real provider downloads are separate checks, not implied by container lifecycle or synthetic-file tests.

## Release maintenance

Use the [stable-release routine](RELEASING.md) to generate versioned packages from a reviewed lock. It aligns image pins and metadata, records provenance and checksums, and prepares workflow artifacts without writing external catalogs. Keep platform revisions separate from the application version. Re-run native install and upgrade checks before publication. Never replace upstream Bazarr's listing or silently reuse its config.
