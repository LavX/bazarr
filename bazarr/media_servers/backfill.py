# coding=utf-8
"""One durable import marker per native kind, atomic with its legacy row."""

import logging

from app.database import TableMediaServerImports
from .instances import VALID_KINDS
from .repository import MediaServerInstanceRepository, atomic


def _record_import(session, kind):
    session.add(TableMediaServerImports(kind=kind))
    session.flush()


def _legacy_values(settings, kind):
    section = getattr(settings, kind)
    enabled = getattr(settings.general, 'use_' + kind)
    values = dict(kind=kind, name=kind.capitalize(), enabled=enabled,
                  url=section.url, api_key=section.apikey, verify_ssl=section.verify_ssl,
                  path_mappings=[dict(row) for row in section.path_mappings])
    # Preserve incomplete drafts and historical mapping values. Only malformed
    # storage types prevent import; current API validation governs future edits.
    if (type(enabled) is not bool or type(values['verify_ssl']) is not bool
            or not isinstance(values['url'], str) or not isinstance(values['api_key'], str)
            or not isinstance(section.path_mappings, list)
            or any(not isinstance(key, str) or not isinstance(value, str)
                   for row in values['path_mappings'] for key, value in row.items())):
        raise ValueError('invalid legacy settings')
    return values


def backfill_instances(session, settings):
    repo = MediaServerInstanceRepository(session)
    results = {}
    for kind in VALID_KINDS:
        try:
            with atomic(session, durable=True):
                if session.get(TableMediaServerImports, kind) is not None:
                    results[kind] = {'created': False}
                    continue
                values = _legacy_values(settings, kind)
                configured = (values['enabled'] or values['url'] or values['api_key']
                              or values['path_mappings'] or values['verify_ssl'] is False)
                row = repo.import_values(values) if configured else None
                _record_import(session, kind)
            results[kind] = {'created': row is not None}
        except Exception:
            session.rollback()
            logging.warning('BAZARR native media server import failed for %s; retrying on next startup', kind)
            results[kind] = {'created': False, 'error_code': 'migration_failed'}
    return results
