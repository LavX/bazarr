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
import ipaddress
import os
import socket
from urllib.parse import urlparse

# Mirrors subliminal_patch.core.SUBTITLE_EXTENSIONS (kept local to avoid a heavy
# import in this widely-imported, dependency-free module).
SUBTITLE_FILE_EXTENSIONS = (
    ".srt", ".sub", ".smi", ".txt", ".ssa", ".ass", ".mpl", ".vtt",
)

# Domains that Plex itself owns; sending a Plex token to these is expected
# (covers plex.tv and the *.plex.direct TLS hostnames used for remote access).
_PLEX_OWNED_SUFFIXES = (".plex.tv", ".plex.direct")


def api_key_matches(provided, expected) -> bool:
    """Constant-time check that ``provided`` equals the configured API key.

    Returns False if either side is empty so an unset server key can never
    authorize a request.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def is_trusted_plex_target(uri, trusted_hosts=()) -> bool:
    """True if it is safe to send the Plex token to ``uri``.

    Trusted = a Plex-owned domain (plex.tv / *.plex.direct), an explicitly
    configured host (the user's own server), or any host that resolves only to
    loopback / link-local / RFC1918 addresses (a home Plex). Any other public
    destination is refused so the token cannot be exfiltrated.
    """
    if not uri:
        return False

    parsed = urlparse(uri if "://" in uri else "https://" + uri)
    if parsed.scheme not in ("http", "https"):
        return False

    host = parsed.hostname
    if not host:
        return False
    host = host.lower().rstrip(".")

    if host == "plex.tv" or host.endswith(_PLEX_OWNED_SUFFIXES):
        return True

    if host in {h.lower() for h in trusted_hosts if h}:
        return True

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError, OSError):
        return False
    if not infos:
        return False

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        # Any single public address makes the destination untrusted.
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return True


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
