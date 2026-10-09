# Native package integration validation

Test date: September 29, 2026. Application image: `ghcr.io/lavx/bazarr:2.7.0`.

## Isolation and scope

The retained disposable Runtipi 4.10.2 and TrueNAS 25.10.7 VMs were used for deeper integration testing. Sonarr 4.0.19.2979 and Radarr 6.3.0.10514 ran on the owner's confirmed test server. Its existing Bazarr API also returned HTTP 200; that deployment was not replaced or reconfigured.

The test apps reached Arr through a localhost-bound, read-only bridge. Authentication to the original services stayed in host process memory. The bridge allowed selected metadata GET routes and blocked POST, PUT, PATCH, and DELETE. No real media or original application configuration was mounted in either VM. These tests establish authenticated connection and metadata behavior, not direct LAN routing, SignalR, or successful Arr rescan notifications.

## Connection and library results

| Check | Runtipi | TrueNAS custom app |
| --- | --- | --- |
| Saved Sonarr connection test | Passed | Passed |
| Saved Radarr connection test | Passed | Passed |
| Native stop/start and stored-key connection retest | Passed | Passed |
| Imported movies | 744 | 744 |
| Imported series | 285 | 285 |
| Imported episodes | 3,951 | 3,951 |
| Automatic download History entries during metadata sync | 0 | 0 |

Radarr returned 763 movies, of which 744 had files. The imported movie count matches that eligible set. Sonarr returned 285 series, including 273 with episode files.

The Runtipi browser rendered both saved connection cards. Clicking each card's Test button returned HTTP 200 with `ok=true` and the corresponding upstream version. The Subtitle Hub rendered both successfully tested provider names.

A second Runtipi stop/start retained all three downloaded subtitle fixtures with identical byte counts, hashes, and UID/GID 1000, as well as all imported rows and both working saved connections. Runtipi's UI reported Running afterward.

## CasaOS native integration

CasaOS 0.4.15's native `app-management install --file` CLI accepted the exact package Compose, SHA-256 `b62ec4e90688276f081f598b64adec02ad31cf372a3e27e95acbca0eedfcf58d`. Its native API is `POST /v2/app_management/compose`. This supersedes the earlier inconclusive manual-install UI check; it does not establish catalog ingestion.

The app was healthy and returned HTTP 200. `$AppID` resolved to the app-specific config directory, UID/GID resolved to 1000, and the read-only root, capability restrictions, and no-new-privileges setting were retained. Both saved Arr connections passed before and after the native CasaOS restart. See the [CasaOS receipt](casaos/casaos-native-validation.md).

## Free provider installation and downloads

All three providers were installed through Provider Hub from the official catalog at commit `a7a849b234b16fbe75d79e364aec62e92c4053c5`. Installation jobs completed, native restart activated each version 0.1.3, and active state had no pending restart. No provider account or API key was required.

Each successful canary enabled only its target provider, cleared the compatibility cache, searched for English subtitles, logged into the compatibility API, requested a download link, and fetched its same-origin stream without following redirects. It checked subtitle timestamps and logged out successfully with HTTP 204.

| Provider | Media | Results | Search / download / stream | Subtitle bytes |
| --- | --- | --- | --- | --- |
| My-Subs | Movie | 50 | 200 / 200 / 200 | 114,209 |
| My-Subs | Episode | 4 | 200 / 200 / 200 | 42,834 |
| YIFYSubtitles | Movie | 50 | 200 / 200 / 200 | 87,906 |
| TVsubtitles | Episode | 0 | Search 200, worker failed | Not reached |

The three successful streams were written and read back in disposable Runtipi media folders as UID/GID 1000. Their saved hashes matched the downloaded bytes:

- My-Subs movie: `51e36eec62d72f6fd158c3cd2578c785fa188b8f10349dd703e497111987790a`.
- My-Subs episode: `26733660f364aa6d5fb1ac7e2c8f41fdf6d10f6f11708dae72a9efefaf03445b`.
- YIFYSubtitles movie: `b1a4911498a32257119eb4ecd1ad2e4aea383b419edc91b3a62d68e3cf2ad76c`.

Those permission writes were performed by the test client inside the app container. They are distinct from Bazarr's manual-download writer, indexing, and History workflow.

## Actual Bazarr subtitle save

The [TrueNAS integration receipt](truenas/integration-validation.md) records a generated, valid movie fixture, exact isolated path mapping, an English profile for only that item, and activated My-Subs installation. After explicit user approval to send the selected movie metadata to My-Subs, the normal Bazarr manual search and download path succeeded using anonymous provider access.

Independent read-only verification confirmed a 118,792-byte SRT owned by UID/GID 568, valid subtitle timestamps, and SHA-256 `e5e76b19b3832d992d760361a6838e308a765ea147d043964c3ee8edc453b204`. Bazarr indexed it as English with the matching byte count. The corresponding History row recorded manual download action 2, provider `my_subs`, language `en`, and the correct owning Arr instance. Its subtitle path matched the indexed path.

The earlier approval denial was respected; this run occurred only after the user explicitly approved it. No external provider account was registered or used. Account-based provider testing is out of scope at the user's request.

## Provider failure

TVsubtitles failed with a worker name-resolution error for its configured `www.tvsubtitles.net` origin. Independent host resolution also failed for that hostname and the bare domain, while My-Subs and YIFYSubtitles resolved. HTTP 200 with an empty result list was not counted as provider success. TVsubtitles was left disabled in the disposable Runtipi app; My-Subs and YIFYSubtitles were enabled.

## Harness corrections and limitations

- The private Runtipi fixture Git server needed restarting after its VM boot before native store refresh and reinstall.
- The Runtipi CLI could not restore a backup while the app was uninstalled. Native store reinstall worked. A later CLI lifecycle call reported a queue connection error after stopping the app; the native UI Start action succeeded.
- Initial provider probes reached HTTP 503 during application startup. They were rerun after readiness; these were not classified as provider failures.
- The read-only bridge initially rejected Sonarr's trailing slash in `/api/v3/series/`. Allowing that slash and rerunning sync corrected the harness failure.
- The first Runtipi Arr instances were initially non-default, leaving legacy version lookups pointed at absent local services and slowing import. Marking the first instance of each kind as default through the normal API corrected the setup; the complete counts above were then verified.
- No paid provider, external account registration, email verification, real-media replacement, image-version upgrade, or existing upstream database migration was performed.
- TrueNAS remains a native custom-app deployment, not catalog ingestion. ZimaOS privacy consent and a licensed Unraid environment remain separate gates.

## Cleanup

All disposable test VMs were shut down gracefully, and the read-only bridge and
its SSH tunnels were stopped. No QEMU test processes or assigned SSH/bridge
listeners remained. Virtual disks, test configuration, and synthetic fixtures
were retained for reproducibility. The original Sonarr, Radarr, and
Bazarr container identities were unchanged and remained running. No package was
published and no original service was replaced or reconfigured.
