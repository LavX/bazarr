# External Integration: Subtitle API Endpoint

Bazarr+ can expose a REST API at `/api/v1/*` that lets external subtitle
clients search and download subtitles through your configured Bazarr+
providers. It speaks the OpenSubtitles.com REST API shape, so it serves the
two first-party clients, the
[Jellyfin plugin](https://github.com/LavX/jellyfin-plugin-bazarr-plus) and the
[VLSub Bazarr+ VLC extension](https://github.com/LavX/vlsub-bazarr-plus), as
well as other OpenSubtitles.com clients that let you change their base URL.

The endpoint and its keys are managed in the Distribution Hub. The old
Settings > External Integration page has been removed, and
`/settings/external` now redirects to `/distribution-hub`.

See the compatibility notice in `NOTICES.md` for the legal posture.

## Enabling the endpoint

1. Open the Distribution Hub from the main menu and go to its Settings tab.
2. Read the acknowledgement above the Enable switch and turn it on.
3. Turn on "Enable the Distribution Hub endpoint" and click Save settings.
4. Restart Bazarr+. The `/api/v1` routes are mounted at startup, so until the
   restart the Distribution Hub shows a "Restart required" banner and the
   endpoint keeps answering 404.
5. Go to the API Keys tab. Click New key to create a key for each client and
   copy its token, which is shown only once. To reuse the shared token from an
   older setup, open the menu on the Default key and choose Reveal token.

## Pointing a client at Bazarr+

The Jellyfin plugin and VLSub Bazarr+ ask for the Bazarr+ URL and an API
token. Paste a token from the API Keys tab. Their READMEs cover installation.

For another OpenSubtitles.com client that supports a base URL override:

- Base URL: `http(s)://your-bazarr-host:port/api/v1/`
- API key: paste the Bazarr+ token
- Username / password: any non-empty value (ignored by Bazarr+)

## Security posture

- This endpoint is intended for **private network use only**. Do not expose
  it to the public internet.
- A token grants search and download through your configured subtitle
  providers and their credentials. Leaking it lets a third party consume your
  provider quotas.
- To rotate one client's token, use Rotate token in that key's menu on the
  API Keys tab. On the Default key that action is the same as Regenerate
  secrets. Regenerate secrets on the Settings tab rotates the signing
  secrets and the shared Default token, which signs every client out and
  invalidates outstanding download links. Named keys keep their tokens.
- Download links are time-limited bearer links (5 minutes by default) and are
  not consumed on use. Logging out or rotating, disabling or deleting a key
  does not revoke a link already issued.

## Troubleshooting

- **Clients receive 404 with `x-reason: compat-disabled`**: the endpoint is
  disabled, or was enabled without a restart. Enable it and restart.
- **Clients receive 503 "compat endpoint disabled"**: the endpoint was turned
  off after startup. Turn it back on in the Settings tab.
- **Clients receive 403**: the API key is missing, wrong or disabled. Re-paste
  it, re-enable the key on the API Keys tab, or create a new key.
- **Clients receive 401**: the client's session token is missing, expired or
  signed with rotated secrets. The client should log in again; the Jellyfin
  plugin does this on its own.
- **Search returns 429 or download returns 406**: the key has reached its
  limit for the current window. Wait for it to reset, or raise the key's
  limits on the API Keys tab.
- **Clients receive 404 on `/download`**: the `file_id` expired (60-minute
  TTL by default); the client should search again.
- **503 with `x-reason: upstream`**: all configured providers errored or
  throttled; retry in a minute.

## DMCA designated agent

(Operator-specific contact goes here.)
