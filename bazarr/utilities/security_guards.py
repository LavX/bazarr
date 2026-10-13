# coding=utf-8
"""Pure security-guard helpers shared by the API/UI hardening fixes.

These functions hold the security *decisions* (and nothing else) so they can be
unit-tested in isolation: no Flask, no DB, no network. The matching route
handlers call them.

Design note: Bazarr is overwhelmingly run on localhost / a private LAN, so these
guards deliberately ALLOW loopback and RFC1918 targets. The goal is not to ban
local traffic (that would break the common case) but to stop credentials/secrets
from leaking to *arbitrary public* hosts and to keep request-controlled file
paths inside their intended areas.
"""
import hmac
import os
from urllib.parse import urlparse

# Mirrors subliminal_patch.core.SUBTITLE_EXTENSIONS (kept local to avoid a heavy
# import in this widely-imported, dependency-free module).
SUBTITLE_FILE_EXTENSIONS = (
    ".srt", ".sub", ".smi", ".txt", ".ssa", ".ass", ".mpl", ".vtt",
)


def api_key_matches(provided, expected) -> bool:
    """Constant-time check that ``provided`` equals the configured API key.

    Returns False if either side is empty so an unset server key can never
    authorize a request.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def normalized_origin(uri):
    """(scheme, host, port) of an http(s) address, or None.

    The three parts that decide which server a request reaches, with the
    scheme's default port filled in. Plex destinations are compared on exactly
    this triple, so a path or query never hides a different server and a bare
    hostname never passes for the address it was taken from.

    Anything a URL parser can read two ways is refused outright: a backslash,
    whitespace or a control character ends the authority for one parser and not
    for another, and userinfo hides the real host from whichever parser reads
    the text after the last ``@``. The HTTP library's own parser has to agree
    on the host and the port before an origin is returned.
    """
    if not uri or not isinstance(uri, str):
        return None
    if any(character in uri for character in ("\\", "@")) or any(
            ord(character) < 33 or ord(character) == 127 for character in uri):
        return None
    try:
        parsed = urlparse(uri if "://" in uri else "https://" + uri)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
    except ValueError:
        # A malformed address, a bracketed IPv6 literal without its closing
        # bracket among them, raises out of the parser itself: that is not an
        # origin any credential goes to, so the caller hears None rather than
        # an unhandled error from a route that asked.
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    from urllib3.util import parse_url
    try:
        library_parsed = parse_url(uri if "://" in uri else "https://" + uri)
        library_host = library_parsed.host
        library_port = library_parsed.port
    except ValueError:
        return None
    if library_host is None:
        return None
    # A bracketed IPv6 literal is the one host the two parsers spell
    # differently, so the brackets come off both sides before the hosts are
    # compared; an address that only matches with them on is still the same
    # server.
    if (library_host.lower().rstrip(".").strip("[]")
            != parsed.hostname.lower().rstrip(".").strip("[]")):
        return None
    if library_port is not None and library_port != port:
        return None
    return parsed.scheme, parsed.hostname.lower().rstrip("."), port


def subtitle_path_within_area(subtitle_path, video_path,
                              subfolder_mode=None, custom_subfolder=None) -> bool:
    """True if ``subtitle_path`` lives where Bazarr stores subtitles for
    ``video_path``: alongside the video (or a subfolder under it), or under the
    configured absolute custom subtitle folder. Symlinks / ``..`` are resolved
    via realpath before the containment check.
    """
    if not subtitle_path or not video_path:
        return False

    real_sub = os.path.realpath(subtitle_path)
    try:
        video_dir = os.path.realpath(os.path.dirname(video_path))
        if os.path.commonpath([video_dir, real_sub]) == video_dir:
            return True
        if subfolder_mode == "absolute" and custom_subfolder:
            custom_dir = os.path.realpath(str(custom_subfolder).strip())
            if custom_dir and os.path.commonpath([custom_dir, real_sub]) == custom_dir:
                return True
    except ValueError:
        # commonpath raises across different Windows drives -> not contained.
        return False
    return False


def is_subtitle_path_extension(path) -> bool:
    """True if ``path`` ends with a recognised subtitle extension. Used to keep
    the subtitle-contents reader from opening arbitrary files (config, keys, db).
    """
    if not path:
        return False
    return str(path).lower().endswith(SUBTITLE_FILE_EXTENSIONS)
