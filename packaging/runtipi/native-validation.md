# Native Runtipi validation

Tested September 28, 2026 with Runtipi **4.10.2** on a fresh Ubuntu **24.04.5** KVM guest, Docker **29.8.1**, amd64, 2 vCPUs and 4 GiB RAM. All app state and media were disposable guest data. No host media, existing app config, external integrations, or provider credentials were used.

## Package identity

- Image: `ghcr.io/lavx/bazarr:2.7.0`.
- Tested Compose SHA-256: `eb1a4f04b0cf4fc034439bdf840a3889a7f10012558fb09e46e4a3c4ee6ef03b`.
- Source app folder was copied unchanged into a private Git fixture with `apps/bazarr-plus/`. A guest-local, read-only smart-HTTP Git endpoint served that fixture. No repository or catalog listing was published.
- The native Runtipi App Stores UI cloned the fixture and displayed Bazarr+ as a separate entry from upstream Bazarr.

## Failure caught and corrected

The original native installation failed before container creation:

```text
The "RUNTIPI_MEDIA_DIR" variable is not set. Defaulting to a blank string.
invalid spec: :/media: empty section between colons
```

Runtipi 4.10.2's generated app environment contained `ROOT_FOLDER_HOST`, but not `RUNTIPI_MEDIA_DIR`. The bundled upstream Bazarr definition used `${ROOT_FOLDER_HOST}/media:/media`. This candidate now uses that same bind. After refreshing the native store, installation succeeded without adding a compensating environment variable or editing the generated Compose.

The earlier standalone Compose parse used a supplied `RUNTIPI_MEDIA_DIR` value and therefore could not reveal this integration failure. Native testing is the acceptance evidence for the correction.

## Results

| Check | Result |
| --- | --- |
| Native store discovery, version and app description | Passed |
| Native install form with PUID/PGID and port defaults | Passed |
| Install from native UI | Passed after the media-path correction |
| Runtipi Running status and container health | Passed |
| Bazarr+ browser onboarding without companions | Passed, created an English language profile and opened Discover |
| Supervisor readiness | `state=running` |
| Runtime identity | UID 1000, GID 1000 |
| Runtime effective capabilities | Zero; `NoNewPrivs=1` |
| Container security settings | Read-only root, tmpfs `/tmp`, all caps dropped except entrypoint CHOWN/SETUID/SETGID |
| Config and media mounts | `/config` and `/media`, writable |
| Synthetic subtitle write/read as UID 1000 | Passed for Movies, TV and Sports folders |
| Native Stop followed by Start | Passed; config hash unchanged, marker and all three media fixtures retained |
| Browser after restart | Opened Discover, did not reopen onboarding |
| Native Backup now | Created a 37,307-byte archive; platform stopped and restarted the app |
| Native restore | Recovered the original config marker after deliberately changing it; config hash unchanged |
| Sync with Template check | Reported already in sync |
| Native uninstall | Removed app container and app config; retained the backup with Remove backups disabled; retained all three external media fixtures |

Uninstall **deletes app config**. Create a backup first and leave **Remove backups** disabled if you need to recover it. External shared media is not part of the app-data backup.

## Limits and cleanup

- GitHub's unauthenticated API limit initially prevented the platform installer from resolving its latest release. Installing the verified explicit `v4.10.2` release bypassed that lookup. The same shared API limit prevented the optional provider catalog from loading during onboarding; the UI exposed the rate-limit error and allowed continuing without providers.
- Subtitle-file checks prove mount permissions, not a provider search/download or automatic library workflow. No real media or provider account was used.
- Native backup/restore and same-version restart were tested. An application-version upgrade and migration of an existing upstream Bazarr database were not tested.
- amd64 was exercised; arm64 was not.
- The test app was uninstalled and the VM shut down. Its virtual disk and synthetic backup were retained for reproduction. No live services were replaced.
