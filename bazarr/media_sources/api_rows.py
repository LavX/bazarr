# coding=utf-8
"""Post-process one page while keeping Arr enrichment scoped to Arr rows."""

from flask_restx import fields

from api.utils import postprocess
from subtitles.mismatch import flagged_media_ids


source_row_fields = {
    'origin': fields.String(),
    'source_key': fields.String(),
    'source_id': fields.Integer(),
    'source_name': fields.String(),
    'source_kind': fields.String(),
    'identity_state': fields.String(),
    'shadowed_by': fields.String(),
    'monitored': fields.Boolean(),
    'poster': fields.String(),
}


def postprocess_source(item):
    """Format a standalone item's stored paths, poster and integer monitored state."""
    return postprocess(item, source=True)


def wanted_rows(session, kind, rows, *, mixed=False):
    def is_arr(row):
        return not mixed or row.origin == 'arr'

    mismatched = flagged_media_ids(session, kind, [row.id for row in rows if is_arr(row)])
    results = []
    for row in rows:
        data = dict(row._mapping)
        if is_arr(row):
            data['release_mismatch'] = row.id in mismatched
            results.append(postprocess(data))
        else:
            data['release_mismatch'] = False
            results.append(postprocess_source(data))
    return results
