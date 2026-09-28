# coding=utf-8
"""OCR for bitmap subtitle tracks (PGS and VobSub) into SRT text.

decode renders a stream's cues to images with ffmpeg, an engine reads them,
and pipeline ties the two together. Nothing here writes next to a video or
touches the database; the caller decides where the text goes.
"""
