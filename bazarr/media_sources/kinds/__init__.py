# coding=utf-8
"""What a source kind supplies to the shared scanner.

A kind says what is in a library and whether that answer is complete. Everything after enumeration, the
upsert, gone detection, the root guard, indexing and search, is shared. Today there is one kind, ``folder``.
"""
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, Protocol


class UnknownSourceKind(ValueError):
    """The source kind has no implementation in this build."""


@dataclass(frozen=True)
class Problem:
    """Something wrong with a library as configured. ``code`` is machine readable, ``message`` is for the user."""
    code: str
    message: str


@dataclass(frozen=True)
class SourceHealth:
    ok: bool
    message: Optional[str] = None


@dataclass(frozen=True)
class IdentityHints:
    """What a kind knows about an item or a group without parsing a file name. A folder kind gives none."""
    title: Optional[str] = None
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    tvdb_id: Optional[int] = None
    external_id: Optional[str] = None
    premiere_date: Optional[str] = None


@dataclass(frozen=True)
class DiscoveredFile:
    """One video file a listing returned. The path is the Bazarr-visible one, as found."""
    path: str
    size: int
    mtime_ns: int
    dev: str
    ino: str
    group_key: Optional[str] = None
    hints: Optional[IdentityHints] = None


@dataclass
class Listing:
    """An iterator of discovered files plus a flag the kind sets only once the whole library was listed.

    ``complete`` is meaningful after the iterator is exhausted: a walk that skipped an unreadable directory or
    stopped on a cancel leaves it False, and nothing is deleted on the strength of an incomplete listing.
    ``problems`` collects what the walk could not read, one line each.
    """
    files: Iterator[DiscoveredFile]
    complete: bool = False
    problems: list = field(default_factory=list)
    #: A walked directory vanished, became inaccessible or changed identity during the walk.
    unstable: bool = False
    #: Directory identities recorded by a folder walk, for validating later foreign releases.
    directory_states: dict = field(default_factory=dict)
    #: Files left out because their name cannot be stored (bytes that are not valid UTF-8).
    skipped_names: int = 0
    #: Files directly in the root of a series or sports library: they belong to no show or league.
    ungrouped: int = 0

    def __iter__(self):
        return iter(self.files)


class SourceKind(Protocol):
    kind: str
    #: True when a group is a directory, so the scanner resolves groups against the disk (the folder kind). A
    #: kind that keys its groups by the server's own id has no directories to follow.
    groups_are_directories: bool

    def validate_library(self, source, library) -> list[Problem]: ...

    def enumerate(self, source, library, cancel: Optional[Callable[[], None]] = None) -> Listing: ...

    def health(self, source) -> SourceHealth: ...

    def series_identity(self, source, group_key) -> Optional[IdentityHints]: ...


def get_kind(name) -> SourceKind:
    if name == 'folder':
        from .folder import FolderKind
        return FolderKind()
    raise UnknownSourceKind(f"No media source kind named {name!r} is available")
