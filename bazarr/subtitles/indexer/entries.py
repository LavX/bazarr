# coding=utf-8
"""One entry of a media row's stored ``subtitles`` list.

The column holds the repr of a Python list and every reader parses it with
``ast.literal_eval``. An external file is ``[language, path, size]``. An
embedded track is ``[language, None, None, codec]``: no path, no size, and the
subtitle format knowit reported for the stream, lowercased, or None when knowit
could not name it. Rows indexed before the codec was stored still hold the
three element ``[language, None, None]`` until the media is indexed again.

Readers index an entry by position behind a length check and never unpack it
into a fixed number of names. That is what keeps a trailing field harmless.
"""


def normalize_codec(value):
    """The codec as stored: knowit's format string lowercased, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value or None


def embedded_entry(language, codec):
    """The stored entry for an in-container track."""
    return [language, None, None, normalize_codec(codec)]


def entry_codec(entry):
    """The codec an embedded entry carries, or None.

    None for an external file, for an entry indexed before the codec was
    stored, and for a track knowit could not name.
    """
    if not isinstance(entry, (list, tuple)) or len(entry) < 4 or entry[1]:
        return None
    return normalize_codec(entry[3])
