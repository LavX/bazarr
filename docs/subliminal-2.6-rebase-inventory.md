# Subliminal 2.6 Rebase Inventory

This inventory was captured before replacing Bazarr's modified `custom_libs/subliminal`
tree with the pinned upstream `subliminal 2.6.0` package from pip. Bazarr-specific
behavior now belongs in `custom_libs/subliminal_patch`.

## Upstream Artifacts

- Target: `subliminal[rar]==2.6.0`
- Source distribution SHA256: `e6e7aee1b218d543dcb3b7b2248ea0f92afc4c223ce3e7af8d2c3843e31bafe5`
- Wheel SHA256: `b03316094668cdbd7e3a08289b4cdb51d75ad62209822c089acbbf33dee7751f`
- Confirmed upstream changes: provider add/remove churn, `knowit` metadata extraction, optional `rarfile`,
  UTF-8 subtitle output default, FPS scoring changes, `Provider.hash_video`, and
  `get_default_providers()` / `get_default_refiners()` replacing the removed globals.

## Upstream Risk Review

| Upstream change | Bazarr handling in this branch |
| --- | --- |
| Provider additions and removals | Upstream 2.6 providers are imported from pip. Providers Bazarr still uses but upstream removed are restored in `subliminal_patch`. Registry tests lock both the Bazarr provider ids and the vanilla upstream `subliminal` provider ids. |
| OpenSubtitles.com changes | The upstream 2.6 provider and converter are included. Bazarr still keeps its patched provider layer and OpenSubtitles converter registration. Credential-backed tests remain environment gated. |
| Python `>=3.9` requirement | No shim is added. Runtime and image builds must stay on Python 3.9 or newer. |
| `knowit` metadata extraction | `knowit` and `pymediainfo` are installed through the pip dependency tree. Linux images should still keep `mediainfo` or `libmediainfo` available for best metadata extraction. |
| Optional RAR support | Bazarr pins `subliminal[rar]==2.6.0`, so `rarfile` remains installed for archive providers. |
| Subtitle parser/converter dependency changes | Pip Subliminal pulls its parser/converter dependencies. Bazarr also keeps direct requirement pins for dependencies it imports directly. |
| CLI refactor | The upstream CLI now comes from the pip package. Bazarr runtime tests do not currently exercise the standalone CLI. |
| Encoding and save behavior | Bazarr's patched `save_subtitles` and `Subtitle` class remain the active runtime surface, and regression tests assert the monkey patching. |
| Scoring and HI/forced handling | Upstream scoring is rebased, while Bazarr's patched scoring compatibility tests are kept in the deterministic suite. |
| Removed `default_providers` and `default_refiners` globals | The pip package uses `get_default_providers()` and `get_default_refiners()`, and Bazarr's extension layer registers the preserved provider set explicitly. |

## Bazarr Patch Surface

- `custom_libs/subliminal_patch`: compatibility layer, provider pools, scoring, HTTP/session wrappers,
  provider health, subtitle conversion, language converters, and Bazarr refiners.
- Provider modules: 63 provider Python files in `custom_libs/subliminal_patch/providers`.
- Converter modules: 9 Python files in `custom_libs/subliminal_patch/converters`.
- Refiner modules: 10 Python files in `custom_libs/subliminal_patch/refiners`.
- Monkey patches applied by `custom_libs/subliminal_patch/__init__.py`:
  - `subliminal.subtitle.Subtitle`
  - `subliminal.subtitle.guess_matches`
  - `subliminal.scan_video`
  - `subliminal.core.scan_video`
  - `subliminal.core.search_external_subtitles`
  - `subliminal.save_subtitles`
  - `subliminal.core.save_subtitles`
  - `subliminal.refine`
  - `subliminal.core.refine`
  - `subliminal.download_best_subtitles`
  - `subliminal.core.download_best_subtitles`
  - `subliminal.video.Video`
  - `subliminal.Video`
  - `subliminal.video.Episode.__bases__`
  - `subliminal.video.Movie.__bases__`
  - `subliminal.list_all_subtitles`
  - `subliminal.core.list_all_subtitles`

## Local 2.1.0 Delta To Preserve

- Cache key behavior: Bazarr replaced the old key generator with SHA1 key mangling to keep cache keys short.
- Video model compatibility: `.strm` extension, permissive guessed-title fallback, single `episode`
  attribute, anime metadata, `edition`, `other`, `streaming_service`, subtitle-language mutation,
  and Bazarr-specific runtime fields.
- Core behavior: Bazarr's scan path computes legacy provider hashes, tolerates Windows special paths,
  preserves archive handling, and applies Bazarr-specific provider error handling.
- Subtitle behavior: Bazarr keeps the patched subtitle class, `guess_matches`, custom encoding handling,
  original-format preservation, and line-ending behavior through `subliminal_patch`.
- Provider registry: Bazarr must continue registering all `subliminal_patch.providers` modules into
  `provider_registry`; upstream provider removals must not remove Bazarr providers.
- Legacy provider behavior is no longer preserved by modifying upstream `subliminal`. Providers Bazarr still
  registers but upstream removed, currently including `shooter` and `subscenter`, are owned by
  `subliminal_patch`.

## Provider Wrapper Policy

- Prefer upstream `subliminal 2.6.0` provider protocol code when a provider exists upstream. Bazarr patch
  wrappers should keep only Bazarr-specific behavior: extra language variants, HI/forced language handling,
  release metadata, custom score matches, throttling/auth behavior, and archive selection.
- `tvsubtitles`: upstream 2.6 owns most protocol code. Bazarr keeps the working `search1.php` search endpoint,
  subtitle wrapper for `release_info`, mutable `matches`, and multi-episode normalization.
- `podnapisi`: upstream 2.6 owns the JSON `/subtitles/search/advanced` protocol. Bazarr keeps `only_foreign`,
  `also_foreign`, HI/forced language rebuilding, inconsistent title normalization, relaxed TLS compatibility,
  release metadata, and archive member selection.
- `subtitulamostv`: upstream 2.6 owns the search, show/season/episode navigation, language converter,
  matching, and download flow. Bazarr keeps the existing `subtitulamostv` provider id, `release_info`, mutable
  `matches`, legacy full-download-link subtitle id, and the exact series-name guard from the old wrapper.
- Providers with larger Bazarr-specific behavior still require dedicated wrappers for now:
  `addic7ed`, `opensubtitles`, `opensubtitlescom`, `gestdown`, `bsplayer`, `subtis`, `napiprojekt`,
  `shooter`, and `subscenter`.
- The patched `Subtitle` base accepts the upstream 2.6 constructor shape while preserving legacy Bazarr provider
  calls that pass `hearing_impaired` and `page_link` positionally.

## Baseline Notes

- Broad pre-rebase provider run was not clean locally: 210 passed, 10 skipped, 58 failed, 2 errors.
- Deterministic guard suite passed before changes: 59 passed.
- Pre-existing broad failures included missing `SUBDL_TOKEN`, DNS/network provider failures,
  and existing provider behavior mismatches.
- Post-rebase deterministic guard suite: 154 passed, 4 deselected, 2 warnings.
