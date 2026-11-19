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
    #: Files left out because their name cannot be stored (bytes that are not valid UTF-8).
    skipped_names: int = 0

    def __iter__(self):
        return iter(self.files)


class SourceKind(Protocol):
    kind: str

    def validate_library(self, source, library) -> list[Problem]: ...

    def enumerate(self, source, library, cancel: Optional[Callable[[], None]] = None) -> Listing: ...

    def health(self, source) -> SourceHealth: ...

    def series_identity(self, source, group_key) -> Optional[IdentityHints]: ...


def get_kind(name) -> SourceKind:
    if name == 'folder':
        from .folder import FolderKind
        return FolderKind()
    raise UnknownSourceKind(f"No media source kind named {name!r} is available")
