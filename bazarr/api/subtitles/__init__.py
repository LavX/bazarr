# coding=utf-8

from .subtitles import api_ns_subtitles
from .subtitles_info import api_ns_subtitles_info
from .batch import api_ns_batch
from .content import api_ns_subtitle_content
from .subtitles_contents import api_ns_subtitle_contents
from .archive import api_ns_subtitle_archive


api_ns_list_subtitles = [
    api_ns_subtitles,
    api_ns_subtitles_info,
    api_ns_batch,
    api_ns_subtitle_content,
    api_ns_subtitle_contents,
    api_ns_subtitle_archive,
]
