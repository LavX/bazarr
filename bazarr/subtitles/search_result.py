# coding=utf-8
"""The shape a manual subtitle search hands to the user.

The library routes, the sports route and the search job all publish the same
rows, so the field list lives here once. Marshalling is what turns the raw
``manual_search`` dicts into the strings and integers the frontend's
``SearchResultType`` reads: left alone, ``original_format`` would come back as a
JSON boolean from one route and as ``"False"`` from another.
"""

from flask_restx import fields, marshal

SEARCH_RESULT_FIELDS = {
    'dont_matches': fields.List(fields.String),
    'forced': fields.String(),
    'hearing_impaired': fields.String(),
    'language': fields.String(),
    'matches': fields.List(fields.String),
    'original_format': fields.String(),
    'orig_score': fields.Integer(),
    'provider': fields.String(),
    'release_info': fields.List(fields.String),
    'score': fields.Integer(),
    'score_without_hash': fields.Integer(),
    'subtitle': fields.String(),
    'uploader': fields.String(),
    'url': fields.String(),
}


def marshal_search_results(rows):
    """The rows of a search as the API sends them, ready to publish on a job."""
    return marshal(rows, SEARCH_RESULT_FIELDS)
