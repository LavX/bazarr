# coding=utf-8
"""Immutable destination snapshots and strict request validation."""

import re
from dataclasses import dataclass

from .http import MediaServerError, validate_server_url
from .paths import validate_path_mappings

VALID_KINDS = ('emby', 'silo')
FIELDS = {'name', 'enabled', 'url', 'verify_ssl', 'api_key', 'clear_api_key', 'path_mappings'}
PROBE_FIELDS = {'url', 'verify_ssl', 'api_key', 'clear_api_key'}


@dataclass(frozen=True, repr=False)
class ConnectionSnapshot:
    id: str
    kind: str
    name: str
    instance_enabled: bool
    master_enabled: bool
    url: str
    apikey: str
    verify_ssl: bool
    path_mappings: tuple
    revision: int = 1
    configuration_error: str | None = None

    @property
    def enabled(self):
        return self.instance_enabled and self.master_enabled

    def mappings(self):
        return [dict(row) for row in self.path_mappings]


def validate_fields(body, *, create=False, probe=False):
    allowed = PROBE_FIELDS if probe else FIELDS | ({'kind'} if create else set())
    if not isinstance(body, dict) or set(body) - allowed:
        raise MediaServerError('invalid_settings')
    if create and body.get('kind') not in VALID_KINDS:
        raise MediaServerError('invalid_kind')
    for name in ('name', 'url', 'api_key'):
        if name in body and not isinstance(body[name], str):
            raise MediaServerError('invalid_settings')
    for name in ('enabled', 'verify_ssl', 'clear_api_key'):
        if name in body and type(body[name]) is not bool:
            raise MediaServerError('invalid_settings')
    if body.get('api_key') and body.get('clear_api_key'):
        raise MediaServerError('invalid_settings')
    if 'name' in body and (not body['name'].strip() or any(ord(c) < 32 for c in body['name'])):
        raise MediaServerError('invalid_name')
    if 'url' in body:
        validate_server_url(body['url'])
    if 'path_mappings' in body:
        validate_path_mappings(body['path_mappings'])
    return body


def validate_connection(values, *, probe=False):
    validate_server_url(values['url'])
    if not values['name'].strip():
        raise MediaServerError('invalid_name')
    mappings = validate_path_mappings(values['path_mappings'])
    if values['kind'] == 'silo':
        for row in mappings:
            value = row.get('library_id')
            if not isinstance(value, str) or re.fullmatch(r'[0-9]+', value) is None or not value.strip('0'):
                raise MediaServerError('library_invalid')
    if values['enabled'] or probe:
        if not values['api_key'].strip():
            raise MediaServerError('missing_credentials')
        if not mappings and not probe:
            raise MediaServerError('mapping_missing')
    return values
