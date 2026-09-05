from collections import namedtuple
from difflib import SequenceMatcher
import io
import logging
import os
import re
import tempfile
from typing import Iterable, Union
import zipfile

from guessit import guessit
import pysubs2
import rarfile
from subliminal.subtitle import fix_line_ending
from subliminal.utils import sanitize_release_group
from subliminal_patch.exceptions import MustGetBlacklisted
from subliminal_patch.core import SUBTITLE_EXTENSIONS, Episode
from subliminal_patch.subtitle import guess_matches

from ._agent_list import FIRST_THOUSAND_OR_SO_USER_AGENTS

USER_AGENTS = FIRST_THOUSAND_OR_SO_USER_AGENTS

logger = logging.getLogger(__name__)


_MatchingSub = namedtuple("_MatchingSub", ("file", "priority", "context"))

# Extensions the codebase calls a subtitle but which a scene archive also uses for
# something else. A release-info text file sits next to the subtitle in the same
# archive, and the movie selector takes the first match and stops, so an ambiguous
# member is only ever considered when the archive holds nothing better.
AMBIGUOUS_SUBTITLE_EXTENSIONS = (".txt",)

# The conservative default for the built-in providers. Callers that want the whole
# canonical set pass subliminal_patch.core.SUBTITLE_EXTENSIONS explicitly rather
# than growing their own copy of it.
DEFAULT_ARCHIVE_EXTENSIONS = (".srt", ".sub", ".ssa", ".ass")


def is_subtitle_member(name, extensions=DEFAULT_ARCHIVE_EXTENSIONS):
    """Whether an archive member is a subtitle file we are willing to extract.

    The extension is compared on its own and case-insensitively, so a member named
    ``MOVIE.SRT`` matches. Only the extension is lowercased: the rest of the member
    name carries the language and the HI/forced tags, and a lowercased copy bleeding
    into that parsing is how a name resolves as a language it never claimed. For the
    same reason this never substring-tests the whole name.

    Directories and dot-prefixed members (``.DS_Store``, the ``__MACOSX/._name``
    resource forks zip adds) are not subtitles.
    """
    if not name:
        return False
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not base or base.startswith("."):
        return False
    return os.path.splitext(base)[1].lower() in tuple(
        extension.lower() for extension in extensions
    )


def list_subtitle_members(archive, extensions=DEFAULT_ARCHIVE_EXTENSIONS):
    """The subtitle members of ``archive``, in archive order.

    Single source of truth for "what counts as a subtitle member in this archive":
    the list offered to a plugin for language selection and the list the selector
    picks from both come from here, so the two can never disagree about a given
    archive the way they did when each filtered for itself.

    Members whose extension is ambiguous come last, in archive order among
    themselves. Last rather than absent: a caller that just takes the first
    member gets the real subtitle instead of the release-info text file, which
    is what the ordering is for, while language and episode matching can still
    reach a .txt member. The ex-Yugoslav providers ship MicroDVD in .txt files,
    so an archive holding movie.sr.txt beside movie.en.srt is a real shape and
    the .txt is the only copy of that language.
    """
    unambiguous = []
    ambiguous = []
    for name in archive.namelist():
        if not is_subtitle_member(name, extensions):
            continue
        if os.path.splitext(name)[1].lower() in AMBIGUOUS_SUBTITLE_EXTENSIONS:
            ambiguous.append(name)
        else:
            unambiguous.append(name)
    return unambiguous + ambiguous


def _archive_member_names(archive):
    """Member names for a log line, best effort: a broken archive must not turn a
    failed extraction into a traceback."""
    try:
        return list(archive.namelist())
    except Exception:  # pragma: no cover - exotic archive objects
        return []


def blacklist_on(*exc_types):
    "Raise MustGetBlacklisted if any of the exc_types are raised."

    def decorator(method):
        def wrapper(self, subtitle):
            try:
                return method(self, subtitle)
            except exc_types:
                logger.error("Sending blacklist exception", exc_info=True)
                raise MustGetBlacklisted(subtitle.id, subtitle.media_type)

        return wrapper

    return decorator


def _contains_number(guessed, requested):
    guessed_numbers = guessed if isinstance(guessed, (list, tuple)) else (guessed,)
    requested_numbers = requested if isinstance(requested, (list, tuple)) else (requested,)
    return any(number in guessed_numbers for number in requested_numbers)


def _get_matching_sub(
    sub_names, forced=False, episode=None, episode_title=None, log_member_names=True,
    match_episode_context=False, season=None, **kwargs
):
    guess_options = {} if match_episode_context else {"single_value": True}
    if episode is not None or (match_episode_context and season is not None):
        guess_options["type"] = "episode"  # type: ignore

    matching_subs = []
    movie_selection = episode is None and episode_title is None

    for sub_name in sub_names:
        if not forced and os.path.splitext(sub_name.lower())[0].endswith("forced"):
            if log_member_names:
                logger.debug("Ignoring forced subtitle: %s", sub_name)
            continue

        guess = {}
        if not movie_selection or (match_episode_context and season is not None):
            guess = guessit(sub_name, options=guess_options)
        if (match_episode_context and season is not None and guess.get("season") is not None
                and not _contains_number(guess["season"], season)):
            continue

        # Without episode context, get the first member that passed the season check.
        if movie_selection:
            if log_member_names:
                logger.debug("Movie subtitle found: %s", sub_name)
            matching_subs.append(_MatchingSub(sub_name, 2, "Movie subtitle"))
            break

        matched_episode_num = guess.get("episode")
        if not matched_episode_num and log_member_names:
            logger.debug("No episode number found in file: %s", sub_name)

        if episode_title is not None:
            from_name = _analize_sub_name(sub_name, episode_title, log_member_names)
            if from_name is not None:
                matching_subs.append(from_name)

        if (_contains_number(matched_episode_num, episode) if match_episode_context
                else episode == matched_episode_num):
            if log_member_names:
                logger.debug("Episode matched from number: %s", sub_name)
            matching_subs.append(_MatchingSub(sub_name, 2, "Episode number matched"))

    if matching_subs:
        matching_subs.sort(key=lambda x: x.priority, reverse=True)
        if log_member_names:
            logger.debug("Matches: %s", matching_subs)
        return matching_subs[0].file
    else:
        logger.debug("Nothing matched")
        return None


def _analize_sub_name(sub_name: str, title_: str, log_member_names=True):
    titles = re.split(r"[\s_\.\+]?[.-][\s_\.\+]?", os.path.splitext(sub_name)[0])

    for title in titles:
        title = title.strip()
        ratio = SequenceMatcher(None, title.lower(), title_.lower()).ratio()

        if ratio > 0.85:
            if log_member_names:
                logger.debug(
                    "Episode title matched: '%s' -> '%s' [%s]", title, sub_name, ratio
                )

            # Avoid false positives with short titles
            if len(title_) > 4 and ratio >= 0.98:
                return _MatchingSub(sub_name, 3, "Perfect title ratio")

            return _MatchingSub(sub_name, 1, "Normal title ratio")

    if log_member_names:
        logger.debug("No episode title matched from file: %s", sub_name)
    return None


def get_subtitle_from_archive(
    archive,
    forced=False,
    episode=None,
    get_first_subtitle=False,
    extensions=DEFAULT_ARCHIVE_EXTENSIONS,
    match_episode_context=False,
    log_member_names=True,
    season=None,
    **kwargs,
):
    """Return subtitle bytes, or None if no member matches.

    The host can match all included episodes and reject explicit season mismatches
    while supplying sanitized diagnostics. Defaults preserve provider behavior.
    An explicit first-subtitle request remains authoritative.
    """
    subs_in_archive = list_subtitle_members(archive, extensions)

    if not subs_in_archive:
        # Name the members: without them a user whose download failed here has no
        # way to tell an archive full of the wrong extensions from a broken one.
        if log_member_names:
            logger.warning(
                "No subtitles found in archive. Members: %s", _archive_member_names(archive)
            )
        return None

    if log_member_names:
        logger.debug("Subtitles in archive: %s", subs_in_archive)

    if len(subs_in_archive) == 1 or get_first_subtitle:
        if match_episode_context and (episode is not None or season is not None) and not get_first_subtitle:
            guess = guessit(
                subs_in_archive[0], options={"type": "episode"}
            )
            for key, requested in (("season", season), ("episode", episode)):
                if requested is not None and guess.get(key) is not None and not _contains_number(guess[key], requested):
                    return None
        if log_member_names:
            logger.debug("Getting first subtitle in archive: %s", subs_in_archive)
        return fix_line_ending(archive.read(subs_in_archive[0]))

    matching_sub = _get_matching_sub(
        subs_in_archive, forced, episode, log_member_names=log_member_names,
        match_episode_context=match_episode_context, season=season, **kwargs
    )

    if matching_sub is not None:
        if log_member_names:
            logger.info("Using %s from archive", matching_sub)
        return fix_line_ending(archive.read(matching_sub))

    if log_member_names:
        logger.warning(
            "No subtitle in archive matched this episode. Members: %s", subs_in_archive
        )
    return None


def is_episode(content):
    return "episode" in guessit(content, {"type": "episode"})


_ENCS = ("utf-8", "ascii", "iso-8859-1", "iso-8859-2", "iso-8859-5", "cp1252")


def _zip_from_subtitle_file(content):
    with tempfile.NamedTemporaryFile(prefix="spsub", suffix=".srt") as tmp_f:
        tmp_f.write(content)
        sub = None
        for enc in _ENCS:
            try:
                logger.debug("Trying %s encoding", enc)
                sub = pysubs2.load(tmp_f.name, encoding=enc)
            except Exception as error:
                logger.debug("%s: %s", type(error).__name__, error)
                continue
            else:
                break

        if sub is not None:
            logger.debug("Identified subtitle file: %s", sub)
            zip_obj = zipfile.ZipFile(io.BytesIO(), mode="x")
            zip_obj.write(tmp_f.name, os.path.basename(tmp_f.name))
            return zip_obj

        logger.debug("Couldn't load subtitle file")
        return None


def get_archive_from_bytes(content: bytes):
    """Get RarFile/ZipFile object from bytes. A ZipFile instance will be returned
    if a subtitle-like stream is found. Return None if something else is found."""
    archive_stream = io.BytesIO(content)

    if rarfile.is_rarfile(archive_stream):
        logger.debug("Identified rar archive")
        return rarfile.RarFile(archive_stream)
    elif zipfile.is_zipfile(archive_stream):
        logger.debug("Identified zip archive")
        return zipfile.ZipFile(archive_stream)

    logger.debug("No compression format found. Trying with subtitle-like files")
    return _zip_from_subtitle_file(content)


def update_matches(
    matches,
    video,
    release_info: Union[str, Iterable[str]],
    split="\n",
    **guessit_options,
):
    """Update matches set from release info string or Iterable.

    Use the split parameter to iterate over the set delimiter; set None to avoid split.
    """

    guessit_options["type"] = "episode" if isinstance(video, Episode) else "movie"

    logger.debug("Guessit options to update matches: %s", guessit_options)

    if isinstance(release_info, str):
        release_info = release_info.split(split)

    sanitized_video_release_group = sanitize_release_group(video.release_group)

    for release in release_info:
        for release_split in release.split(split):
            sanitized_release_info = sanitize_release_group(release_split.strip())
            logger.debug("Updating matches from sanitized release info: %s", sanitized_release_info)
            matches |= guess_matches(
                video, guessit(sanitized_release_info, guessit_options)
            )
            if ("release_group" not in matches and sanitized_video_release_group and
                    re.search(f"\\b({re.escape(sanitized_video_release_group)})\\b", sanitized_release_info)):
                matches.add("release_group")
            logger.debug("New matches: %s", matches)

    return matches
