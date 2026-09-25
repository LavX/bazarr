# coding=utf-8
"""One durable import marker per native kind, atomic with its legacy row."""

import logging

from app.database import TableMediaServerImports
from .instances import LIBRARY_KEYS, REFRESH_METHODS, VALID_KINDS
from .repository import MediaServerInstanceRepository, atomic


def _record_import(session, kind):
    session.add(TableMediaServerImports(kind=kind))
    session.flush()


def _string_list(value):
    """A scalar setting that has held a bare string as often as a list."""
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        raise ValueError('invalid legacy settings')
    return [item for item in value if isinstance(item, str) and item.strip()]


def _legacy_values(settings, kind):
    section = getattr(settings, kind)
    enabled = getattr(settings.general, 'use_' + kind)
    if kind == 'plex':
        from .plex_account import account_credential, account_url
        url, api_key = account_url(section), account_credential(section)
    else:
        url, api_key = section.url, section.apikey
    values = dict(kind=kind, name=kind.capitalize(), enabled=enabled,
                  url=url, api_key=api_key, verify_ssl=section.verify_ssl,
                  path_mappings=[dict(row) for row in section.path_mappings]
                  if kind in ('emby', 'silo') else [],
                  refresh_movies=True, refresh_episodes=True, options={})
    if kind in LIBRARY_KEYS:
        # The per-type opt-ins move onto the instance; the library scoping and
        # Jellyfin's refresh method are what stays kind-specific.
        values['refresh_movies'] = section.update_movie_library
        values['refresh_episodes'] = section.update_series_library
        legacy = (('movie_library_ids', 'series_library_ids', 'sports_library_ids')
                  if kind == 'jellyfin' else ('movie_library', 'series_library', 'sports_library'))
        values['options'] = {key: _string_list(getattr(section, source))
                             for key, source in zip(LIBRARY_KEYS[kind], legacy)}
        if kind == 'jellyfin':
            method = getattr(section, 'refresh_method', 'immediate')
            values['options']['refresh_method'] = method if method in REFRESH_METHODS else 'immediate'
        if any(type(values[toggle]) is not bool for toggle in ('refresh_movies', 'refresh_episodes')):
            raise ValueError('invalid legacy settings')
    # Preserve incomplete drafts and historical mapping values. Only malformed
    # storage types prevent import; current API validation governs future edits.
    if (type(enabled) is not bool or type(values['verify_ssl']) is not bool
            or not isinstance(values['url'], str) or not isinstance(values['api_key'], str)
            or not isinstance(values['path_mappings'], list)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for row in values['path_mappings'] for key, value in row.items())):
        raise ValueError('invalid legacy settings')
    return values


def _configured(values):
    """Whether these scalars are a destination or just the shipped defaults."""
    libraries = any(values['options'].get(key) for key in LIBRARY_KEYS.get(values['kind'], ()))
    return bool(values['enabled'] or values['url'] or values['api_key']
                or values['path_mappings'] or values['verify_ssl'] is False or libraries)


def backfill_instances(session, settings):
    """Import each kind's scalar settings once, and keep Plex's row in step.

    The marker is two things at once, and both matter. It is the one-time
    import latch, and it is the dispatcher's per-kind gate: a kind with no
    marker is blocked, so `NativeConfiguration.publish` drops its snapshots and
    every destination of that kind is invisible to refreshes, whether the
    import created it or a user did in Connections. So each kind is stamped
    once its step has run without error, configured or not. Only a failure
    leaves a kind unstamped, which is the one case where its destinations
    should not be refreshed from settings that could not be read.

    Plex's step is not an import. Its account panel still writes the scalars
    every time the user signs in, saves a key or picks a server, so its row is
    reconciled here on every startup instead, which is idempotent and never
    duplicates. It runs after the others so that a publication from it, if one
    ever happens again, cannot see a partial marker set.
    """
    repo = MediaServerInstanceRepository(session)
    results = {}
    for kind in VALID_KINDS:
        if kind == 'plex':
            continue
        try:
            with atomic(session, durable=True):
                if session.get(TableMediaServerImports, kind) is not None:
                    results[kind] = {'created': False}
                    continue
                values = _legacy_values(settings, kind)
                row = repo.import_values(values) if _configured(values) else None
                _record_import(session, kind)
            results[kind] = {'created': row is not None}
        except Exception:
            session.rollback()
            logging.warning('BAZARR native media server import failed for %s; retrying on next startup', kind)
            results[kind] = {'created': False, 'error_code': 'migration_failed'}
    results['plex'] = _reconcile_plex(session, settings)
    return results


def _reconcile_plex(session, settings):
    """Bring the Plex account's row in step, and stamp only if that worked.

    An account with nothing configured is not a failure: there is simply no
    destination yet, and the kind is stamped so its rows are dispatched. A
    database error is, and so is a new row whose owner id the config could not
    save. Either leaves it unstamped and therefore blocked, which is the same
    rule the other three follow.
    """
    from .plex_account import apply_plex_account
    try:
        _row, created = apply_plex_account(session, settings, persist=_persist)
        _stamp_once(session, 'plex')
        return {'created': created}
    except Exception:
        session.rollback()
        logging.warning('BAZARR native media server import failed for plex; retrying on next startup')
        logging.debug('Plex destination reconcile failed', exc_info=True)
        return {'created': False, 'error_code': 'migration_failed'}


def _stamp_once(session, kind):
    with atomic(session, durable=True):
        if session.get(TableMediaServerImports, kind) is None:
            _record_import(session, kind)


def _persist():
    """Write the config, so a recorded owner id survives the restart."""
    from app.config import write_config
    return write_config()
