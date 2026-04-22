# NOTICES

## Compatibility notice

Bazarr+ v2.2.0 introduces an optional REST API endpoint at `/api/v1/*` whose
request and response shape is designed for interoperability with existing
OpenSubtitles-compatible media-center plugins (VLC's VLSub, Kodi's
service.subtitles.opensubtitles-com, Jellyfin's OpenSubtitles plugin, and
Stremio OpenSubtitles-compatible addons).

This endpoint is an independent reimplementation of the shape used by the
OpenSubtitles.com REST API. Bazarr+ does not copy source code from
OpenSubtitles.com, does not proxy requests to OpenSubtitles.com by default,
and is not affiliated with, endorsed by, or sponsored by OpenSubtitles.com.
"OpenSubtitles" and "OpenSubtitles.com" are trademarks of their respective
owners; their use in Bazarr+ documentation is limited to factual references
for interoperability purposes (nominative fair use).

## Third-party software

Bazarr+ depends on the following third-party open-source libraries. Full
license texts are included in the `libs/` directory where applicable.

- dogpile.cache — MIT — used for the compat endpoint's search result cache
- PyJWT — MIT — used for HS256 JWT synthesis on the compat endpoint
- guessit — LGPL v3 — used for filename metadata parsing
- babelfish — BSD 3-Clause — used for language code conversion
- subliminal — MIT — used as the provider framework
- subliminal_patch — fork of subliminal — modifications copyright (C) the
  respective contributors

## Metadata sources

Bazarr+ enriches subtitle search responses with title, year, and series
information from the following metadata sources when a library-local
lookup is not available:

- **TheTVDB** (https://thetvdb.com) — Metadata provided by TheTVDB.
  Please consider adding missing information or subscribing. Accessed
  via the TVDB v4 API under a project-tier license key embedded in
  Bazarr+ for no-configuration episode resolution.
- **OMDb API** (https://www.omdbapi.com) — Movie metadata is resolved
  through OMDb when an operator supplies their own API key in settings.
  No key is shipped by default.

## License

Bazarr+ is released under GPL v3. See `LICENSE` for the full text.

## Provider terms of service

Operators enabling the `/api/v1/*` endpoint are responsible for ensuring
that their use complies with each enabled subtitle provider's terms of
service. Some providers explicitly prohibit proxying their service or
sharing API credentials with third-party clients. Bazarr+ does not
enforce provider terms of service and shall not be held responsible for
operator decisions.

## DMCA / Copyright takedown

If you believe that content accessible through a Bazarr+ instance infringes
your copyright, contact the operator of that instance directly. Bazarr+ is
self-hosted software and does not operate a central service.
