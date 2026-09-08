# Sports recordings in the Distribution Hub

VLSub Bazarr+ and the Jellyfin Bazarr+ plugin can retrieve indexed subtitles for sports recordings when their ordinary OpenSubtitles-compatible request sends a usable recording hash or an exact matching filename. No companion update or sports-specific endpoint is required. Enable the Distribution Hub and local subtitle serving, connect an enabled Sportarr instance, and give the client key access to the `local` provider.

## Finding a recording

Use `/api/v1/subtitles` with `languages` and either:

- `moviehash`: a 16-character hexadecimal OpenSubtitles file hash. A hash alone is sufficient. `moviebytesize` may accompany it.
- `query`: the recording's exact basename or recorded release filename, including its extension when present in the library. Path prefixes, surrounding whitespace, letter case and Unicode normalization do not affect filename comparison. Punctuation, release components and extensions are otherwise significant.

An actual hash match takes precedence over a filename-only match. With `moviehash_match=only`, a filename cannot substitute for a missing or mismatching hash. A bare event title is not a fuzzy search across sports events.

If several enabled instances contain matching copies, filename agreement wins first, followed by the enabled default Sportarr instance, stable instance key and local event ID. Identical hashes cannot reveal which physical copy a client intended. Disabled owners and missing files do not participate. An IMDb ID that already resolves a native movie or episode retains its existing native lookup behavior.

Sports results use the compatible `Movie` feature envelope with descriptive event title/date and `imdb_id: 0`. This does not assign the event a movie identity. Clients that send only IMDb, TMDB or TVDB IDs cannot identify sports recordings through this contract. Clients that insist on a nonzero IMDb ID may not display sports results.

This feature serves indexed local subtitles. It leaves the existing remote-provider fanout unchanged. Remote services may require metadata or accounts that a sports recording does not have, and sports results from those services are not guaranteed. When there is no matching sports library file, the request follows the existing native/nonlibrary fallback.

## Access, download and limits

Sports use the same login, search, integer file ID, `/api/v1/download` and signed stream routes as other Hub results. The normal language, format, provider filters, request caps, tier limits, timeouts and usage metering apply. Local subtitles retain the existing 5 MiB size limit and must belong to the selected owner's indexed recording and configured subtitle location. Ambiguous physical ownership is rejected.

A key's local exclusion cannot be overridden with `only_providers=local`. A nonempty key allow-list must include `local` to serve local subtitles. An empty key allow-list means unrestricted access; an explicitly empty request filter, `only_providers=`, selects nothing.

Searches consume quota on admission, including cache hits and empty results. Download quota is consumed when the download request is admitted, before its stream is fetched. A later missing file can therefore remain charged. Following the signed stream does not consume quota a second time. Streams return subtitle content, never a direct disk URL.

Sports stream links bind the local event, owning instance and exact video/subtitle file identity. Owner changes, remapped paths, removed or replaced files, removed index entries and reuse of an integer file ID cannot redirect an old sports link to another file. After such changes, search again for a fresh result.

Stream links remain time-limited bearer capabilities. Logging out, rotating a key, disabling it or deleting it does not revoke a previously issued link. This existing Hub behavior also applies to sports links whose bound file remains valid. Disabling the Hub itself blocks streaming. A restart is not a general revocation guarantee for native movie/episode links.
