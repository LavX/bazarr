# coding=utf-8
"""Which in-container subtitle stream a request means.

The text extractor, the bitmap dump and OCR all have to land on the same
stream for the same row, so the choice lives here once: the codec families,
and the two-pass match (exact language with the hi and forced flags, then the
first stream in that language). The index returned is the stream's position
among the file's subtitle streams, which is what ffmpeg's ``0:s:N`` names.
"""

from utilities.video_analyzer import _title_is_forced, embedded_track_language

# Substrings of knowit's format string (or the track name when knowit gave no
# format) that mark an image-based track. Matched as substrings, exactly as the
# text extractor has always matched them.
BITMAP_CODECS = ("pgs", "vobsub", "dvd_subtitle", "dvbsub", "hdmv_pgs_subtitle", "dvd")

# The bitmap codecs OCR reads. DVB is left out on purpose: broadcast tracks
# repaint cumulatively and are not supported.
OCR_CODECS = ("pgs", "hdmv_pgs_subtitle", "vobsub", "dvd_subtitle", "dvd")


# How an image-based track is copied out as it is: the file extension and the
# ffmpeg muxer. ffmpeg has no VobSub muxer, so VobSub goes through an MPEG-2
# program stream, which is what a .sub is, and the .idx is written beside it.
BITMAP_CONTAINERS = {
    "pgs": (".sup", "sup"),
    "vobsub": (".sub", "vob"),
    "dvb": (".mks", "matroska"),
}

# The files an extraction of each family leaves next to the video.
OUTPUT_EXTENSIONS = {
    "pgs": (".sup",),
    "vobsub": (".idx", ".sub"),
    "dvb": (".mks",),
}
TEXT_OUTPUT_EXTENSIONS = (".srt",)


def bitmap_family(codec):
    """"pgs", "vobsub" or "dvb" for an image-based codec string, else None."""
    codec = (codec or "").lower()
    if not is_bitmap_codec(codec):
        return None
    if "pgs" in codec:
        return "pgs"
    if "dvb" in codec:
        return "dvb"
    return "vobsub"


def output_extensions(codec):
    """The extensions an extraction of a track with ``codec`` writes: text is an SRT."""
    family = bitmap_family(codec)
    return OUTPUT_EXTENSIONS[family] if family else TEXT_OUTPUT_EXTENSIONS


def track_codec(track):
    """The lowercased codec string the matchers compare against."""
    return (track.get("format") or track.get("name") or "").lower()


def is_bitmap_codec(codec):
    codec = (codec or "").lower()
    return any(name in codec for name in BITMAP_CODECS)


def is_ocr_codec(codec):
    codec = (codec or "").lower()
    return "dvb" not in codec and any(name in codec for name in OCR_CODECS)


def select_subtitle_stream(tracks, target_alpha3, *, hi=False, forced=False,
                           und_default_language=None, accept=None, exact=False):
    """Index of the stream to use among ``tracks``, or None.

    ``accept`` filters on the codec string; streams it rejects still count
    towards the index, because ffmpeg numbers every subtitle stream.
    ``exact`` turns the language-only fallback off, for callers that must get
    the track a row names or nothing.
    """
    accept = accept or (lambda codec: not is_bitmap_codec(codec))
    fallback = None
    for index, track in enumerate(tracks):
        if not accept(track_codec(track)):
            continue
        # The same rule indexing used to decide the track exists.
        language = embedded_track_language(track, und_default_language)
        if language is None or language != target_alpha3:
            continue
        track_forced = track.get("forced", False) or _title_is_forced(track.get("name", ""))
        if track.get("hearing_impaired", False) == hi and track_forced == forced:
            return index
        if fallback is None:
            fallback = index
    return None if exact else fallback
