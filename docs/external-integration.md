# External Integration — Subtitle API Endpoint

Bazarr+ can expose a REST API at `/api/v1/*` that lets external subtitle
clients (VLC's VLSub extension, Kodi's OpenSubtitles-com addon, Jellyfin's
OpenSubtitles plugin, and Stremio OpenSubtitles-compatible addons) search
and download subtitles through your configured Bazarr+ providers.

See the compatibility notice in `NOTICES.md` for the legal posture.

## Enabling the endpoint

1. Open Settings → External Integration.
2. Read the warning above the Enable toggle. Confirm the acknowledgement
   checkbox.
3. Flip the Enable toggle and Save.
4. Restart Bazarr+. The endpoint is active after restart.
5. Copy the API token displayed in the Settings tab.

## Pointing a VLSub (VLC) extension at Bazarr+

The upstream VLSub-OpenSubtitles-com extension supports changing the base
URL. In the extension config:

- Base URL: `http(s)://your-bazarr-host:port/api/v1/`
- API key: paste the Bazarr+ token
- Username / password: any non-empty value (ignored by Bazarr+)

Repeat for Kodi's `service.subtitles.opensubtitles-com` addon and Jellyfin's
OpenSubtitles plugin — each supports a base URL override.

## Security posture

- This endpoint is intended for **private network use only**. Do not expose
  it to the public internet.
- The compat token grants read access to your configured subtitle providers'
  search and download capabilities via your provider credentials. Leaking it
  lets a third party consume your provider quotas.
- Rotating the token invalidates all outstanding client sessions. Use
  "Regenerate all secrets" in the Settings tab.
- Auth failures are logged at WARNING with the source IP. Logs are rotated
  per Bazarr's existing 30-day rotation; no long-term retention.

## Troubleshooting

- **Clients receive 302 redirects** → Compat endpoint is disabled; enable it
  and restart.
- **Clients receive 401** → API token mismatch; re-paste or regenerate.
- **Clients receive 404 on `/download`** → `file_id` expired (60-min TTL);
  client should re-search.
- **503 with `x-reason: upstream`** → all configured providers errored or
  throttled; retry in a minute.

## DMCA designated agent

(Operator-specific contact goes here.)
