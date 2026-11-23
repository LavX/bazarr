# coding=utf-8
"""Bounded collection and exact ID responses shared by Jellyfin and Emby."""
from dataclasses import dataclass, field

from .http import MediaServerError

PAGE_SIZE = 500
LOOKUP_SIZE = 100
_MAX_PAGES = 20_000
# Episode indexes, SeriesId, production year and premiere date are base DTO fields.
_FIELDS = 'Path,ProviderIds'


def item_id(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 255
            or ',' in value or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise MediaServerError('invalid_item_id')
    return value


def _result(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('Items'), list):
        raise MediaServerError('invalid_response')
    items, total = payload['Items'], payload.get('TotalRecordCount')
    if type(total) is not int or total < 0:
        raise MediaServerError('invalid_response')
    for row in items:
        item_id(row.get('Id') if isinstance(row, dict) else None)
    return items, total


@dataclass
class ItemPages:
    items: dict = field(default_factory=dict)
    complete: bool = False
    drifted: bool = False
    error: str | None = None


def collect_items(fetch, parent_id, item_types, cancel=None):
    """Keep data from pages already received and report how the collection ended."""
    parent_id = item_id(parent_id)
    result = ItemPages()
    total_at_start = offset = 0
    for page in range(_MAX_PAGES):
        if cancel is not None:
            cancel()
        try:
            items, total = _result(fetch({
                'ParentId': parent_id, 'Recursive': 'true', 'IncludeItemTypes': ','.join(item_types),
                'Fields': _FIELDS, 'SortBy': 'SortName', 'SortOrder': 'Ascending',
                'StartIndex': offset, 'Limit': PAGE_SIZE,
            }))
        except MediaServerError as error:
            result.error = error.code
            return result
        if len(items) > PAGE_SIZE:
            result.error = 'invalid_response'
            return result
        if page == 0:
            total_at_start = total
        else:
            result.drifted |= total_at_start != total
        previously_seen = len(result.items)
        for row in items:
            # A repeated id leaves the first view of it in place for this scan.
            result.items.setdefault(row['Id'], row)
        offset += len(items)
        if offset >= total:
            result.complete = True
            result.drifted |= len(result.items) != total or offset != total
            return result
        if len(items) < PAGE_SIZE or len(result.items) == previously_seen:
            result.error = 'incomplete_listing'
            return result
    result.error = 'page_limit'
    return result


def find_ids(fetch, ids):
    """A targeted lookup is evidence only when every returned row is accounted for."""
    asked = set(map(item_id, ids))
    if not asked:
        return set()
    if len(asked) > LOOKUP_SIZE:
        raise MediaServerError('lookup_limit')
    items, total = _result(fetch({
        'Ids': ','.join(sorted(asked)), 'StartIndex': 0, 'Limit': len(asked),
    }))
    found = {row['Id'] for row in items}
    if total != len(items) or len(found) != total or not found <= asked:
        raise MediaServerError('invalid_response')
    return found
