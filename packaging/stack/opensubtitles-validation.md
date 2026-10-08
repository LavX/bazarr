# OpenSubtitles.org stack verification

Validated anonymously on September 29, 2026 in the isolated Compose stack, with no real media mounts or provider account.

- Installed the official Provider Hub `opensubtitles` plugin, version `0.1.13`, through the normal installation API. After activation, it was enabled with no restart pending.
- Bazarr's effective runtime provider config resolved `flaresolverr_url` to `http://flaresolverr:8191/v1`.
- An OpenSubtitles-only temporary client key exercised `/api/v1/subtitles`: HTTP 200 in 12.9 seconds, with 40 results.
- Login, download and local stream returned HTTP 200. The stream contained 86,705 non-empty bytes with subtitle timestamps. No stream hash was captured.
- Logout returned HTTP 204 and the temporary client key was deleted. Compat was restored to disabled with consent false, followed by a Bazarr restart. The internally generated compat token remains stored but was never printed or copied into this package.
- All three stack services were healthy after cleanup. The normal Bazarr-to-translator authentication and FlareSolverr browser checks also passed afterward.

FlareSolverr logs contained no OpenSubtitles request during this test. The successful search/download did not require the fallback. This proves installation, effective wiring and anonymous provider operation, not that FlareSolverr solved an OpenSubtitles challenge.

The GitHub API was rate-limited during catalog refresh while the official raw catalog remained reachable. The normal official source was temporarily pinned to stable commit `a7a849b234b16fbe75d79e364aec62e92c4053c5` through its supported source API. It is unpinned again (`dev_ref=null`). Its final automatic refresh encountered a renewed rate limit, with retry time 01:16:12 UTC; the cached official v0.1.13 entry remained available. No source trust checks were bypassed.

This provider is installed in the test stack, not automatically installed for every package user. New installations still choose their providers through Subtitle Hub.
