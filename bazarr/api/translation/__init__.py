# coding=utf-8

from .history import api_ns_translation_history
from .summary import api_ns_translation_summary
from .translation import api_ns_translation


api_ns_list_translation = [
    api_ns_translation_history,
    api_ns_translation_summary,
    api_ns_translation,
]
