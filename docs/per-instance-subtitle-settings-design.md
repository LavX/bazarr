# Per-instance Subtitle settings: design

Status: draft for review
Tracking issue: https://github.com/LavX/bazarr/issues/227
Follow-up to: https://github.com/LavX/bazarr/issues/156 (multiple Sonarr/Radarr instances)

## 1. Problem

The multiple-instance work (merged in https://github.com/LavX/bazarr/pull/224)
scopes libraries, sync and history per Sonarr/Radarr instance. Subtitle
processing settings (post-processing command, audio sync and its thresholds,
and subtitle modifications) are still global: every instance shares one set.

Requested use case (from https://github.com/LavX/bazarr/issues/156): run a
different post-processing pipeline for an anime instance than for a kids
instance, on the same Bazarr+ install. The same argument applies to subsync
thresholds and the subzero mods (for example, "Remove HI" for a kids library
but not elsewhere).

This document proposes how to make a defined subset of subtitle settings
configurable per instance, how the data is stored and resolved, and how the
existing global configuration maps to the new model. It does not change
behaviour on its own; it is the plan to be approved before implementation.

## 2. Current state

All values below are read directly from the global `settings` object
(`from app.config import settings`).

### 2.1 Post-processing (`settings.general.*`)

| Key | Type | Default |
| --- | --- | --- |
| `use_postprocessing` | bool | False |
| `postprocessing_cmd` | str | "" |
| `use_postprocessing_threshold` | bool | False |
| `postprocessing_threshold` | int | 90 |
| `use_postprocessing_threshold_movie` | bool | False |
| `postprocessing_threshold_movie` | int | 70 |

Read at: `bazarr/subtitles/processing.py`, `bazarr/subtitles/upload.py`.

### 2.2 Audio sync (`settings.subsync.*`)

| Key | Type | Default |
| --- | --- | --- |
| `use_subsync` | bool | False |
| `use_subsync_threshold` | bool | False |
| `subsync_threshold` | int | 90 |
| `use_subsync_movie_threshold` | bool | False |
| `subsync_movie_threshold` | int | 70 |
| `enabled_engines` | list | `['ffsubsync', 'autosubsync', 'alass']` |
| `max_offset_seconds` | int | 60 |
| `gss` | bool | True |
| `no_fix_framerate` | bool | True |
| `force_audio` | bool | False |
| `use_original_language` | bool | False |
| `auto_use_original_language` | bool | False |
| `output_mode` | str | "overwrite" |
| `debug` | bool | False |
| `checker.blacklisted_providers` | list | [] |
| `checker.blacklisted_languages` | list | [] |

Read at: `bazarr/subtitles/sync.py`, `bazarr/subtitles/tools/subsyncer.py`,
`bazarr/subtitles/mass_operations.py`.

### 2.3 Subtitle modifications (`settings.general.subzero_mods`)

A comma-separated string expanded with `get_array_from(...)`. Mod ids include
`remove_HI` (and `remove_HI(keep_lyrics=1)` after
https://github.com/LavX/bazarr/issues/225), `remove_tags`, `OCR_fixes`,
`common`, `fix_uppercase`, `reverse_rtl`, `color(name=...)`, and `emoji`. Read
at: `bazarr/subtitles/download.py`, `bazarr/subtitles/manual.py`,
`bazarr/subtitles/upload.py`, `bazarr/subtitles/tools/mods.py`.

### 2.4 The instances data model

`arr_instances` (`bazarr/app/database.py`) already carries three per-instance
JSON columns, two of which set the precedent for storing per-instance config:

- `path_mappings` (text/JSON): per-instance remote -> local path map.
- `schedule` (text/JSON): per-instance sync cadence.
- `options` (text/JSON, nullable): described in code as "sync / exclusion
  options" and currently unused.

`options` is the natural home for per-instance subtitle overrides: it is
already there, already JSON, and already part of the instance CRUD payload.

### 2.5 How instance scope already flows

`arr_instance_id` is already threaded through the subtitle action layer
(download, manual, upload, sync, mods, processing) and the API endpoints, and
queries are scoped with `arr_instances.resolution.scoped(...)`. The plumbing to
know which instance an action belongs to exists; what is missing is a settings
lookup that uses it.

## 3. Goals and non-goals

Goals:

- Let an operator override a defined subset of subtitle settings per instance.
- Fall back to the current global value when an instance does not override it
  (zero behaviour change for anyone who never touches the new UI).
- Keep the standalone subtitle pipeline (subzero / subliminal_patch) free of
  any awareness of instances; resolution happens in the bazarr layer.
- First-class PostgreSQL and SQLite support (the `options` column already
  exists in both).

Non-goals:

- Per-language-profile or per-series overrides (out of scope; instance-level
  only).
- Moving global settings out of `config.yaml`; the global values remain the
  defaults.

## 4. Which settings go per-instance

Proposed split. "Per-instance" means an instance may override it; unset means
inherit the global value.

Per-instance (high value, clearly instance-specific):

- `use_postprocessing`, `postprocessing_cmd`
- `use_postprocessing_threshold` / `postprocessing_threshold`
- `use_postprocessing_threshold_movie` / `postprocessing_threshold_movie`
- `subzero_mods` (the whole mod set, including `remove_HI(keep_lyrics=1)`)
- `use_subsync`
- `use_subsync_threshold` / `subsync_threshold`
- `use_subsync_movie_threshold` / `subsync_movie_threshold`
- `subsync.enabled_engines`
- `subsync.max_offset_seconds`

Stay global (site-wide policy or rarely instance-specific):

- `subsync.gss`, `subsync.no_fix_framerate` (technical tuning)
- `subsync.output_mode` (overwrite vs keep_all is a UI/behaviour choice)
- `subsync.debug` (a global troubleshooting switch)
- `subsync.force_audio`, `subsync.use_original_language`,
  `subsync.auto_use_original_language` (can be revisited if requested)
- `subsync.checker.*` blacklists (can be revisited if requested)

This list is the main thing to confirm in review.

## 5. Data model

Store overrides under a single namespaced key inside the existing
`arr_instances.options` JSON, so other future `options` uses do not collide:

```json
{
  "subtitle_settings": {
    "use_postprocessing": true,
    "postprocessing_cmd": "/config/scripts/anime.sh \"{{subtitles}}\"",
    "subzero_mods": ["remove_HI(keep_lyrics=1)", "common"],
    "subsync": {
      "use_subsync": true,
      "subsync_threshold": 80,
      "enabled_engines": ["ffsubsync"]
    }
  }
}
```

Rules:

- A key that is absent means "inherit the global value". Only overridden keys
  are written, so the blob stays small and global changes still propagate to
  non-overriding instances.
- An instance with no `subtitle_settings` (or `options` null) behaves exactly
  as today. This is the default for every existing and newly added instance,
  giving a no-op migration.
- The default instance also inherits globals unless explicitly overridden, so
  the global Subtitles settings page keeps working unchanged.

## 6. Resolution layer

Add a small resolver next to the existing instance helpers
(`bazarr/arr_instances/resolution.py`):

```python
def resolve_subtitle_setting(arr_instance_id, dotted_key, global_default):
    """Return the per-instance override for dotted_key, else the global value.

    arr_instance_id None (single-instance / default path) always returns the
    global value, so existing call sites are unaffected until an override is set.
    """
```

Backed by a cached read of `arr_instances.options -> subtitle_settings`,
invalidated on instance update (the instance repository already emits update
events). Call sites change from, for example:

```python
use_pp = settings.general.use_postprocessing
```

to:

```python
use_pp = resolve_subtitle_setting(arr_instance_id, "general.use_postprocessing",
                                  settings.general.use_postprocessing)
```

For subzero mods, `bazarr/subtitles/tools/mods.py::get_subzero_mods()` (added in
https://github.com/LavX/bazarr/issues/225) becomes the single choke point:
extend it to take `arr_instance_id` and resolve the per-instance mod list there,
so download/manual/upload automatically pick up the per-instance value.

Affected call sites (all already receive `arr_instance_id`):
`bazarr/subtitles/processing.py`, `bazarr/subtitles/sync.py`,
`bazarr/subtitles/download.py`, `bazarr/subtitles/manual.py`,
`bazarr/subtitles/upload.py`, `bazarr/subtitles/mass_operations.py`.

## 7. API

Extend the existing instance endpoints rather than add new ones:

- `GET /api/.../arr-instances` already returns `options`; include the parsed
  `subtitle_settings` (or expose a typed `subtitle_settings` field derived from
  it) so the UI can render current overrides.
- `PATCH`/create accepts a `subtitle_settings` object; the backend validates
  each key against the allowed per-instance set (section 4) and writes only the
  overridden keys into `options.subtitle_settings`. Unknown or
  not-allowed-per-instance keys are rejected.

Validation reuses the same value constraints as the global validators in
`bazarr/app/config.py` (ranges for thresholds, the engine enum, etc.).

## 8. Frontend

The instances UI lives in `frontend/src/pages/Settings/Connections/`
(`InstanceFormModal.tsx`, `InstanceCard.tsx`). Add a collapsible "Subtitle
Settings" section to the instance modal:

- Each overridable setting gets an "Inherit global / Override" switch. When set
  to Inherit, the control is disabled and shows the resolved global value for
  reference. When set to Override, the control is enabled and its value is
  written to `subtitle_settings`.
- The section reuses the existing settings widgets (the same Check / Selector /
  Slider / Text components used on the global Subtitles page) for consistency.

On the global Subtitles page
(`frontend/src/pages/Settings/Subtitles/index.tsx`), add a short note that
these values are the defaults and can be overridden per instance under
Settings > Connections, with a link.

## 9. Config mapping and migration

- No schema migration is needed: `arr_instances.options` already exists.
- No data migration is needed: absent overrides inherit globals, so existing
  installs behave identically after upgrade.
- The current global `config.yaml` values remain the defaults and the single
  source of truth for any instance that does not override.

## 10. Rollout

1. Backend resolver + `options.subtitle_settings` read/validate/write, with
   `arr_instance_id` plumbed into `get_subzero_mods()` and the
   post-processing / subsync reads (behaviour identical while no overrides
   exist).
2. API surface for reading and writing `subtitle_settings`.
3. Frontend override UI in the instance modal + the global-page note.
4. Docs and release notes.

Each step is independently shippable and inert until an override is actually
set.

## 11. Risks and open questions

- Settings reads are spread across many call sites; the resolver must be cheap
  (cached) to avoid per-subtitle DB hits. Mitigation: cache keyed by instance,
  invalidated on instance update.
- Confirm the per-instance vs global split in section 4 (the main review item).
- `subzero_mods` as a per-instance list interacts with the parameterized mod
  format (`color(name=...)`, `remove_HI(keep_lyrics=1)`); the resolver stores
  and returns the already-expanded list, so no extra parsing is needed.
- Decide whether the default instance should be allowed to override (proposed:
  yes, for symmetry, though in practice its overrides equal the globals).

## 12. Testing strategy

- Resolver unit tests: override present, override absent (inherit), `None`
  instance id, partial override blobs, invalid/disallowed keys rejected.
- `get_subzero_mods(arr_instance_id=...)` returns the per-instance list and
  falls back to global.
- API tests: write then read round-trips a `subtitle_settings` blob; validation
  rejects out-of-range and non-allowed keys; isolation across instances (an
  override on instance A never leaks to instance B), mirroring the existing
  cross-instance isolation guard tests.
- Frontend: the inherit/override switch writes only overridden keys; inherited
  controls render the global value and are disabled.
