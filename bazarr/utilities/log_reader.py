# coding=utf-8
"""Read bazarr.log newest first, one page at a time.

The system log endpoint used to read the whole file into one string, build a
dict for every record and reverse the list, so a single request on a busy day's
log held the file several times over in memory. This module reads the file
backwards in fixed-size blocks instead. The first page of a log of any size is
one or two blocks, and entries are built only for the page that was asked for.

The total is exact. Below the page, records are only counted, by the same pass
over the same blocks, which holds one block and a few counters at a time.
Because the log only ever grows at its end, the count below a known record is
remembered per file and filter. The next request, typically a refresh of the
newest page, counts only what was appended since and reads back only as far as
its page reaches. For a filter that matches rarely, the page itself can reach
far back: the newest 500 critical lines may span most of the file.

The file format is the one FileHandlerFormatter writes, one record per line:

    2026-09-25 10:00:00|INFO    |logger name                     |message|

A record that carries an exception has the traceback, repr'd onto the same
line, as a fifth field. Lines are split on the "|" + newline that ends every
record, exactly as the endpoint always split them, so a record and its fields
parse the same as before. A line that does not start a record belongs to the
record above it in the file, and lines ahead of the file's first record form
one entry of their own, both as before.
"""

import hashlib
import os
import re
import threading
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field

BLOCK_SIZE = 256 * 1024

# The worst legal response is then about 1MB at a typical 212 bytes a record.
# Anyone who wants the whole file uses the download route, which streams it.
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000

LEVEL_NUMBERS = {'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50}
LEVEL_CHOICES = tuple(name.lower() for name in LEVEL_NUMBERS)

_RECORD_START = re.compile(rb'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')
_NUMBERED_LEVEL = re.compile(r'Level (\d+)')
_LF = b'|\n'
_CRLF = b'|\r\n'
# The level of the lines that come before the file's first record, which have
# always been shown together as one entry of their own.
_ORPHAN_LEVEL = b'ERROR'


class StoredFilter:
    """The include and exclude filters kept in settings.log.

    The rules are the ones the endpoint has always applied. Each line of the
    file is tested on its own. A plain filter is a substring test, which
    ignore_case turns into a case-folded one. A regex filter is a search, with
    ignore_case as re.IGNORECASE. An invalid regex is still not applied, but it
    is recorded in ``errors`` so the reader is told the log is not filtered.
    """

    def __init__(self, include='', exclude='', ignore_case=False, use_regex=False):
        self.errors = []
        self.signature = (include, exclude, bool(ignore_case), bool(use_regex))
        self._include = self._compile('include', include, ignore_case, use_regex)
        self._exclude = self._compile('exclude', exclude, ignore_case, use_regex)

    def _compile(self, name, pattern, ignore_case, use_regex):
        if not pattern:
            return None
        if use_regex:
            try:
                return re.compile(pattern, re.IGNORECASE if ignore_case else 0).search
            except (re.error, OverflowError, RecursionError) as error:
                # re.error is not the only refusal: a repeat count too large
                # to hold raises OverflowError and very deep nesting raises
                # RecursionError. The endpoint caught every one of them before.
                message = str(error) or type(error).__name__
                self.errors.append({'filter': name, 'pattern': pattern, 'message': message})
                return None
        if ignore_case:
            folded = pattern.casefold()
            return lambda text: folded in text.casefold()
        return lambda text: pattern in text

    @property
    def active(self):
        return self._include is not None or self._exclude is not None

    def keeps(self, text):
        if self._include is not None and not self._include(text):
            return False
        if self._exclude is not None and self._exclude(text):
            return False
        return True


class _Contains:
    """A case-insensitive substring test, on one line or on a whole block.

    An ASCII needle is matched against the bytes with ASCII case folding, which
    is fast enough to run on every line of a large file. Only a needle with
    other characters needs the lines decoded and Unicode case folding. Either
    way the block test is the line test run on the block's bytes: every line of
    a block is inside it, so a block without a match holds no matching line.
    """

    def __init__(self, contains):
        try:
            self._needle = contains.encode('ascii').lower()
            self._ascii = True
        except UnicodeEncodeError:
            self._needle = contains.casefold()
            self._ascii = False
        # Needles that fold to the same text match the same lines, so they
        # share one remembered count.
        self.key = self._needle

    def test(self, raw):
        if self._ascii:
            return self._needle in raw.lower()
        return self._needle in raw.decode('utf-8', 'replace').casefold()


@dataclass
class LogPage:
    entries: list = field(default_factory=list)
    total: int = 0


@dataclass
class _Known:
    """What an earlier request counted below one record of a file."""
    anchor: int
    digest: bytes
    counts: Counter


class CountCache:
    """Counts remembered between requests, keyed by file and filter.

    An entry holds how many records, by level, lie below the record that starts
    at ``anchor``. It is used only after that record is found again at the
    same offset with the same bytes, which fails for a file that was truncated
    or replaced, and a failed check falls back to counting from the start.
    """

    def __init__(self, size=16):
        self._size = size
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, key, entry):
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._size:
                self._entries.popitem(last=False)

    def discard(self, key):
        with self._lock:
            self._entries.pop(key, None)


class _FileShrank(Exception):
    pass


def level_number(name):
    """The numeric severity of a level name as the file spells it, or None."""
    number = LEVEL_NUMBERS.get(name)
    if number is None:
        numbered = _NUMBERED_LEVEL.fullmatch(name)
        if numbered:
            number = int(numbered.group(1))
    return number


def _level_name(level_field):
    return level_field.rstrip().decode('utf-8', 'replace')


def _level_field(line):
    """The raw level field of a record's line, or None when the line starts no record.

    A record's line starts with a timestamp and has at least four fields. A
    timestamped line with fewer was never shown as an entry, and the lines
    after it still belong to the record above it. _scan inlines this for speed
    and a test holds the two to the same answers.
    """
    if not _RECORD_START.match(line):
        return None
    first = line.find(b'|')
    if first < 0:
        return None
    second = line.find(b'|', first + 1)
    if second < 0 or line.find(b'|', second + 1) < 0:
        return None
    return line[first + 1:second]


def _text(raw):
    text = raw.decode('utf-8', 'replace')
    if '\r' in text:
        # The endpoint used to read the file in text mode, which turns a
        # Windows line ending into "\n" before anything else sees it.
        text = text.replace('\r\n', '\n')
    return text


def _detect_delimiter(f, end):
    """Whether this file's records end in "|\\n" or, as on Windows, "|\\r\\n"."""
    size = min(end, 8 * 1024)
    f.seek(end - size)
    sample = f.read(size)
    return _CRLF if sample.count(_CRLF) > sample.count(_LF) else _LF


def _iter_blocks(f, end, delimiter, block_size, stop=0):
    """Read f[stop:end] backwards, yielding the complete lines of each block.

    Yields (lines_end, lines, tail, buf): the lines in file order, the file
    offset just past the last of them, whether that last one is the file's
    unterminated tail, and the bytes they were split from, which may also hold
    the start of the line below them. ``stop`` must be the start of a line. A
    line longer than a block is gathered across blocks and joined once.
    """
    reach = len(delimiter) - 1
    pos = end
    # Chunks, in file order, of the line that continues into the block below.
    pending = deque()
    # Its first bytes, enough to see a delimiter split across two blocks.
    pending_head = b''
    tail = True
    while pos > stop:
        size = min(block_size, pos - stop)
        pos -= size
        f.seek(pos)
        chunk = f.read(size)
        if len(chunk) != size:
            # The file shrank under the read. What was read is still right, the
            # rest no longer exists, so stop rather than guess.
            raise _FileShrank()
        probe = chunk + pending_head
        pending.appendleft(chunk)
        if pos > stop and delimiter not in probe:
            pending_head = probe[:reach]
            continue
        buf = b''.join(pending)
        pending.clear()
        lines = buf.split(delimiter)
        if pos > stop:
            head = lines.pop(0)
            pending.append(head)
            pending_head = head[:reach]
        yield pos + len(buf), lines, tail, buf
        tail = False


def _to_entry(header, continuations):
    """The entry the endpoint has always returned for a record.

    ``continuations`` are the record's other lines in file order. ``header``
    is None for the lines ahead of the file's first record.
    """
    lines = [_text(raw).strip() for raw in continuations]
    if header is None:
        return {'timestamp': None, 'type': 'ERROR', 'message': 'See exception',
                'exception': '\n'.join(lines)}
    fields = _text(header).split('|')
    exception = None
    if len(fields) > 4 and fields[4] != '\n':
        exception = fields[4].strip('\'').replace('  ', '\u2003\u2003')
    if lines:
        exception = '\n'.join(([exception] if exception else []) + lines)
    return {'timestamp': fields[0], 'type': fields[1].rstrip(), 'message': fields[3],
            'exception': exception}


class _Page:
    """The entries being collected, and how many matching records came before them."""

    def __init__(self, offset, limit, minimum):
        self.offset = offset
        self.wanted = offset + limit
        self.minimum = minimum
        self.matched = 0
        self.entries = []
        # Whether the level filter passes each raw level field seen so far.
        self.chosen = {}

    @property
    def full(self):
        return self.matched >= self.wanted

    def selects(self, level_field):
        chosen = self.chosen.get(level_field)
        if chosen is None:
            chosen = self.selects_name(_level_name(level_field))
            self.chosen[level_field] = chosen
        return chosen

    def selects_name(self, name):
        if self.minimum is None:
            return True
        number = level_number(name)
        return number is not None and number >= self.minimum


def _bottom_lines(lines, keeps):
    """The lines of a block ahead of its first record, and whether it has one.

    They belong to a record in a block further down.
    """
    bottom = []
    for line in lines:
        if not line:
            continue
        if keeps is not None and not keeps(_text(line)):
            continue
        if _level_field(line) is not None:
            return True, bottom
        if not _RECORD_START.match(line):
            bottom.append(line)
    return False, bottom


def _scan(f, lo, hi, delimiter, block_size, stored, contains, page=None, count=False):
    """Walk the records that start in f[lo:hi], newest first.

    With ``page`` each matching record goes to it, and a walk that does not
    count stops as soon as the page is full. With ``count`` the matching
    records are counted by raw level field, before the level filter, and the
    newest complete record the stored filter keeps is taken as the anchor a
    count can be remembered against. Returns (counts, anchor), where anchor is
    (start, digest, counts through it) or None.

    ``lo`` is 0 or the start of a record and ``hi`` the start of a record or
    the end of the file, so no record crosses either end.

    This loop runs once per line of the file on a cold count, so the page's
    state is held in locals rather than reached through methods.
    """
    keeps = stored.keeps if stored.active else None
    test = contains.test if contains is not None else None
    match = _RECORD_START.match
    width = len(delimiter)
    counts = Counter() if count else None
    anchor = None
    need_anchor = count
    if page is not None:
        chosen = page.chosen
        selects = page.selects
        offset = page.offset
        wanted = page.wanted
        matched = page.matched
        entries = page.entries
    else:
        chosen = selects = entries = None
        offset = matched = 0
        wanted = -1
    # The lines seen since the last record's line, newest first, which belong
    # to the next record found below them. They are kept only while the page
    # may still need them.
    pending = []
    pending_any = False
    pending_match = False
    try:
        for lines_end, lines, tail, buf in _iter_blocks(f, hi, delimiter, block_size, stop=lo):
            if not count and matched >= wanted:
                return counts, anchor
            if test is not None and not pending_match and not need_anchor and not test(buf):
                # No line here contains the needle and nothing above is waiting
                # on a record here, so no record in this block matches. Its
                # first lines can still belong to a matching record below.
                if matched >= wanted:
                    pending = []
                    pending_any = False
                    continue
                has_record, bottom = _bottom_lines(lines, keeps)
                bottom.reverse()
                if has_record:
                    pending = bottom
                else:
                    pending.extend(bottom)
                pending_any = bool(pending)
                continue
            # The index of the line below, kept only while an anchor is wanted.
            index = len(lines)
            for line in reversed(lines):
                if need_anchor:
                    index -= 1
                if not line:
                    continue
                if keeps is not None and not keeps(_text(line)):
                    continue
                if match(line):
                    # _level_field, inline.
                    first = line.find(b'|')
                    if first < 0:
                        continue
                    second = line.find(b'|', first + 1)
                    if second < 0 or line.find(b'|', second + 1) < 0:
                        continue
                    if test is None or pending_match or test(line):
                        level = line[first + 1:second]
                        if counts is not None:
                            counts[level] += 1
                        if page is not None:
                            selected = chosen.get(level)
                            if selected is None:
                                selected = selects(level)
                            if selected:
                                if offset <= matched < wanted:
                                    entries.append(_to_entry(line, pending[::-1]))
                                matched += 1
                                if not count and matched >= wanted:
                                    return counts, anchor
                    if need_anchor and not (tail and index == len(lines) - 1):
                        # The newest complete record: everything below it is
                        # final, so a count below it stays true as the file grows.
                        start = lines_end - sum(len(later) for later in lines[index:]) \
                            - width * (len(lines) - 1 - index)
                        anchor = (start, _digest(line), counts.copy())
                        need_anchor = False
                    if pending:
                        pending = []
                    pending_any = pending_match = False
                    continue
                pending_any = True
                if matched < wanted:
                    pending.append(line)
                if test is not None and not pending_match and test(line):
                    pending_match = True
        if lo == 0 and pending_any and (test is None or pending_match):
            if counts is not None:
                counts[_ORPHAN_LEVEL] += 1
            if page is not None and selects(_ORPHAN_LEVEL):
                if offset <= matched < wanted:
                    entries.append(_to_entry(None, pending[::-1]))
                matched += 1
        return counts, anchor
    finally:
        if page is not None:
            page.matched = matched


def _digest(raw):
    return hashlib.blake2b(raw, digest_size=16).digest()


def _is_line_at(f, start, end, delimiter, digest):
    """Whether the line starting at ``start`` is still the one with this digest."""
    width = len(delimiter)
    if start > 0:
        if start < width:
            return False
        f.seek(start - width)
        if f.read(width) != delimiter:
            return False
    f.seek(start)
    data = bytearray()
    searched = 0
    step = 4096
    while True:
        room = end - start - len(data)
        if room <= 0:
            return False
        # Most lines are a few hundred bytes, so start small and grow.
        chunk = f.read(min(step, room))
        step = min(step * 2, BLOCK_SIZE)
        if not chunk:
            return False
        data += chunk
        at = data.find(delimiter, searched)
        if at >= 0:
            return _digest(bytes(data[:at])) == digest
        # A delimiter can still start in the last few bytes read.
        searched = max(0, len(data) - width + 1)


def _selected_total(page, counts):
    return sum(number for name, number in counts.items() if page.selects_name(name))


def _open_log(path):
    return open(path, 'rb')


def read_log_page(path, limit=DEFAULT_LIMIT, offset=0, level=None, contains='', stored=None,
                  cache=None, block_size=BLOCK_SIZE, baseline_total=None):
    """Return one page of log entries, newest first, and how many match in total.

    ``level`` is a minimum severity name. ``contains`` is a case-insensitive
    substring of a record's lines, including the timestamp and logger name.
    Both apply on top of the stored filter. ``offset`` and ``limit`` count
    matching entries from the newest.

    ``baseline_total`` is the total a reader saw when they started paging.
    Entries that matched since are skipped, so an older page shows the same
    entries it would have shown then instead of shifting as new lines arrive.
    """
    stored = stored if stored is not None else StoredFilter()
    minimum = LEVEL_NUMBERS[level.upper()] if level else None
    contains_test = _Contains(contains) if contains else None
    page = _Page(offset, limit, minimum)

    try:
        f = _open_log(path)
    except FileNotFoundError:
        return LogPage()

    with f:
        status = os.fstat(f.fileno())
        end = status.st_size
        if end == 0:
            return LogPage()
        delimiter = _detect_delimiter(f, end)
        key = (status.st_dev, status.st_ino, delimiter, stored.signature,
               contains_test.key if contains_test is not None else None)

        def names(raw_counts):
            counted = Counter()
            for level_field, number in raw_counts.items():
                counted[_level_name(level_field)] += number
            return counted

        # Records that pass the stored filter and contains, by level. The level
        # filter is applied to the sum, so one remembered count serves every
        # level.
        counts = Counter()
        complete = True
        remember = None
        try:
            # The count below a remembered record, if that record is still the
            # same line at the same place.
            known = cache.get(key) if cache is not None else None
            if known is not None and not (known.anchor < end and _is_line_at(
                    f, known.anchor, end, delimiter, known.digest)):
                cache.discard(key)
                known = None
            floor = known.anchor if known is not None else 0

            # Count what the remembered count does not cover: the whole file
            # once on a cold read, what was appended since on a refresh. The
            # page is gathered on the same walk, unless it has to be shifted
            # by a total that is only known once the walk ends.
            shifting = baseline_total is not None
            raw_counts, anchor = _scan(f, floor, end, delimiter, block_size, stored, contains_test,
                                       page=None if shifting else page, count=True)
            counts = names(raw_counts)
            below = Counter()
            if known is not None:
                counts += known.counts
                below += known.counts
            if anchor is not None:
                start, digest, through = anchor
                below += names(raw_counts - through)
                remember = _Known(start, digest, below)

            # The total is exact by now, so a walk for the rest of the page
            # stops once the page or the matching records run out.
            total = _selected_total(page, counts)
            if shifting:
                page.offset += max(0, total - baseline_total)
                page.wanted = min(page.offset + limit, total)
                if page.offset < page.wanted:
                    _scan(f, 0, end, delimiter, block_size, stored, contains_test, page=page)
            elif known is not None:
                page.wanted = min(page.wanted, total)
                if page.offset < page.wanted:
                    _scan(f, 0, floor, delimiter, block_size, stored, contains_test, page=page)
        except _FileShrank:
            # What was read is right, but the page and the total may be short,
            # so the count is not remembered.
            complete = False

    if cache is not None and complete and remember is not None:
        cache.put(key, remember)

    return LogPage(entries=page.entries, total=_selected_total(page, counts))
