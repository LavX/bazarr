# coding=utf-8

from .subtitles import api_ns_subtitles
from .subtitles_info import api_ns_subtitles_info
from .batch_translate import api_ns_batch_translate


api_ns_list_subtitles = [
    api_ns_subtitles,
    api_ns_subtitles_info,
    api_ns_batch_translate,
]
