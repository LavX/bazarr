# coding=utf-8
"""GET system/logs returns one page, newest first, read from the end of the file.

The endpoint used to read the whole of bazarr.log into memory, build a dict per
record, reverse the list and return every record, and the Logs page asked for
all of it once a minute. It now takes limit, offset, level and contains, and
reads the file backwards in blocks.

Three things are held here. The page is what the old endpoint returned for the
same records, so nothing about a record or the stored filters changed. The
reverse reader agrees with a plain forward reading of the same file for every
block size, filter and append, which is where a reverse reader goes wrong. And
the first page of a 200MB file is read in bounded memory, with a refresh that
reads only the end of the file.
"""
import json
import logging
import os
import random
import re
import subprocess
import sys
import textwrap

import pytest
from flask import Flask

from utilities import log_reader
from utilities.log_reader import (MAX_LIMIT, CountCache, StoredFilter, read_log_page)

BAZARR_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'bazarr'))
TEST_API_KEY = 'synthetic-logs-key'
EVERYTHING = {'limit': MAX_LIMIT, 'offset': 0}


# ---------------------------------------------------------------------------
# Fixtures written by the real formatter
# ---------------------------------------------------------------------------

def _write_real_log(path):
    """A log written through FileHandlerFormatter, as the application writes it."""
    from app.logger import FileHandlerFormatter

    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(FileHandlerFormatter('%(asctime)s|%(levelname)-8s|%(name)-32s|%(message)s|',
                                              '%Y-%m-%d %H:%M:%S'))
    logger = logging.getLogger('test_system_logs_api.writer')
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.handlers = [handler]
    try:
        other = logger.getChild('provider')
        for index in range(40):
            logger.debug('debug line %d', index)
            logger.info('info line %d for whisperai', index)
            if index % 5 == 0:
                other.warning("Provider 'whisperai' is discarded")
            if index % 7 == 0:
                try:
                    raise ValueError(f'broken value {index}')
                except ValueError:
                    logger.exception('failed to score subtitle %d', index)
            if index % 11 == 0:
                logger.critical('Árvíztűrő tükörfúrógép %d', index)
            if index % 13 == 0:
                logger.info('a message with | a pipe and\na newline %d', index)
                logger.error('an empty message follows')
                logger.info('')
    finally:
        handler.close()
        logger.handlers = []


def _previous_endpoint(text, include='', exclude='', ignore_case=False, regex=False):
    """The endpoint's parsing loop before this change, verbatim but for the plumbing.

    Kept as the oracle for what a record and a stored filter mean. It cannot
    read a line that continues a record without an exception (it raises), so
    it is only given files the application's formatter writes.
    """
    logs = []
    include_compiled = exclude_compiled = None
    if regex:
        flags = re.IGNORECASE if ignore_case else 0
        if len(include) > 0:
            try:
                include_compiled = re.compile(include, flags)
            except Exception:
                include_compiled = None
        if len(exclude) > 0:
            try:
                exclude_compiled = re.compile(exclude, flags)
            except Exception:
                exclude_compiled = None
    elif ignore_case:
        include = include.casefold()
        exclude = exclude.casefold()
    record_start_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
    for line in text.split('|\n'):
        if line == '':
            continue
        compare_line = line.casefold() if ignore_case and not regex else line
        if len(include) > 0:
            if regex:
                keep = True if include_compiled is None else include_compiled.search(compare_line)
            else:
                keep = include in compare_line
            if not keep:
                continue
        if len(exclude) > 0:
            if regex:
                skip = False if exclude_compiled is None else exclude_compiled.search(compare_line)
            else:
                skip = exclude in compare_line
            if skip:
                continue
        if record_start_pattern.match(line):
            raw_message = line.split('|')
            if len(raw_message) > 3:
                log = {'timestamp': raw_message[0], 'type': raw_message[1].rstrip(), 'message': raw_message[3]}
                if len(raw_message) > 4 and raw_message[4] != '\n':
                    log['exception'] = raw_message[4].strip('\'').replace('  ', '\u2003\u2003')
                else:
                    log['exception'] = None
                logs.append(log)
        else:
            raise AssertionError('the oracle only reads files the formatter writes')
    logs.reverse()
    return logs


STORED_FILTERS = [
    {},
    {'include': 'whisperai'},
    {'include': 'WHISPERAI', 'ignore_case': True},
    {'exclude': 'debug line'},
    {'include': r'\|(ERROR|CRITICAL) ', 'use_regex': True},
    {'include': 'info', 'exclude': r'line \d*3\b', 'use_regex': True, 'ignore_case': True},
]


@pytest.mark.parametrize('stored', STORED_FILTERS)
@pytest.mark.parametrize('block_size', [1, 7, 97, 4096, log_reader.BLOCK_SIZE])
def test_entries_are_the_ones_the_previous_endpoint_returned(tmp_path, stored, block_size):
    path = tmp_path / 'bazarr.log'
    _write_real_log(path)
    text = path.read_text(encoding='utf-8')
    expected = _previous_endpoint(text, include=stored.get('include', ''), exclude=stored.get('exclude', ''),
                                  ignore_case=stored.get('ignore_case', False),
                                  regex=stored.get('use_regex', False))
    assert expected, 'the fixture must leave something to compare'
    page = read_log_page(path, stored=StoredFilter(**stored), block_size=block_size, **EVERYTHING)
    assert page.entries == expected
    assert page.total == len(expected)


def test_an_exception_record_arrives_whole_across_the_block_boundary(tmp_path):
    path = tmp_path / 'bazarr.log'
    _write_real_log(path)
    expected = _previous_endpoint(path.read_text(encoding='utf-8'))
    tracebacks = [entry for entry in expected if 'Traceback' in (entry['exception'] or '')]
    assert len(tracebacks) == 6
    # Blocks far smaller than one traceback line, so every one of them is split.
    for block_size in (16, 33, 64):
        page = read_log_page(path, block_size=block_size, **EVERYTHING)
        assert [entry for entry in page.entries if 'Traceback' in (entry['exception'] or '')] == tracebacks


# ---------------------------------------------------------------------------
# Paging, level and contains
# ---------------------------------------------------------------------------

def _numbered_log(path, count, delimiter='|\n'):
    levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
    lines = [f'2026-09-25 10:{index // 60 % 60:02d}:{index % 60:02d}|{levels[index % 5]:<8}|'
             f'{"bazarr.test":<32}|record {index}' for index in range(count)]
    path.write_bytes((delimiter.join(lines) + delimiter).encode('utf-8'))
    return levels


def test_pages_run_newest_first_without_gaps_or_repeats(tmp_path):
    path = tmp_path / 'bazarr.log'
    _numbered_log(path, 1000)
    seen = []
    for offset in range(0, 1000, 70):
        page = read_log_page(path, limit=70, offset=offset, block_size=512)
        assert page.total == 1000
        seen.extend(entry['message'] for entry in page.entries)
    assert seen == [f'record {index}' for index in range(999, -1, -1)]
    assert read_log_page(path, limit=10, offset=1000).entries == []


def test_level_is_a_minimum_severity(tmp_path):
    path = tmp_path / 'bazarr.log'
    _numbered_log(path, 500)
    page = read_log_page(path, level='warning', **EVERYTHING)
    assert {entry['type'] for entry in page.entries} == {'WARNING', 'ERROR', 'CRITICAL'}
    assert page.total == len(page.entries) == 300
    assert read_log_page(path, level='critical', **EVERYTHING).total == 100
    assert read_log_page(path, level='debug', **EVERYTHING).total == 500


def test_contains_is_case_insensitive_and_reads_the_whole_line(tmp_path):
    path = tmp_path / 'bazarr.log'
    _numbered_log(path, 500)
    page = read_log_page(path, contains='RECORD 12', **EVERYTHING)
    assert [entry['message'] for entry in page.entries] == \
        ['record 129', 'record 128', 'record 127', 'record 126', 'record 125', 'record 124', 'record 123',
         'record 122', 'record 121', 'record 120', 'record 12']
    assert page.total == 11
    # The logger name is not shown in the table but is on the line, and it is
    # the quickest way to find one component's output.
    assert read_log_page(path, contains='bazarr.test', limit=1).total == 500
    # 123, 124, 128 and 129 are ERROR or CRITICAL; 12 itself is a WARNING.
    assert read_log_page(path, contains='record 12', level='error', **EVERYTHING).total == 4


def test_contains_folds_case_beyond_ascii(tmp_path):
    path = tmp_path / 'bazarr.log'
    _write_real_log(path)
    page = read_log_page(path, contains='ÁRVÍZTŰRŐ TÜKÖR', **EVERYTHING)
    assert page.total == 4
    assert all(entry['type'] == 'CRITICAL' for entry in page.entries)


def test_a_missing_or_empty_log_is_an_empty_page(tmp_path):
    assert read_log_page(tmp_path / 'absent.log').total == 0
    (tmp_path / 'empty.log').write_bytes(b'')
    assert read_log_page(tmp_path / 'empty.log').entries == []


# ---------------------------------------------------------------------------
# The reverse reader against a plain forward reading
# ---------------------------------------------------------------------------

_TIMESTAMP = re.compile(rb'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')


def _forward_reference(data, delimiter, stored, level, contains):
    """What the page should hold, read front to back with no blocks and no cache."""
    records = []
    orphan = []
    for line in data.split(delimiter):
        if not line:
            continue
        if stored.active and not stored.keeps(log_reader._text(line)):
            continue
        if _TIMESTAMP.match(line):
            if log_reader._level_field(line) is not None:
                records.append((line, []))
            continue
        if records:
            records[-1][1].append(line)
        else:
            orphan.append(line)
    if orphan:
        records.insert(0, (None, orphan))
    wanted = []
    minimum = log_reader.LEVEL_NUMBERS[level.upper()] if level else None
    for header, continuations in records:
        name = 'ERROR' if header is None else log_reader._level_name(log_reader._level_field(header))
        if minimum is not None:
            number = log_reader.level_number(name)
            if number is None or number < minimum:
                continue
        if contains:
            folded = contains.casefold()
            if not any(folded in line.decode('utf-8', 'replace').casefold()
                       for line in ([header] if header else []) + continuations):
                continue
        wanted.append(log_reader._to_entry(header, continuations))
    wanted.reverse()
    return wanted


def _random_line(rng, index):
    kind = rng.random()
    stamp = f'2026-09-25 10:{index // 60 % 60:02d}:{index % 60:02d}'
    level = rng.choice(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'Level 5', 'NOTICE'])
    word = rng.choice(['whisperai', 'Árvíztűrő', 'opensubtitles', 'score', 'x|y', 'multi\nline'])
    if kind < 0.55:
        return f'{stamp}|{level:<8}|{"subliminal_patch.core":<32}|{word} {index}'
    if kind < 0.7:
        return f"{stamp}|{level:<8}|{'root':<32}|failed {index}|'Traceback  (most recent call last):\\n  {word}'"
    if kind < 0.85:
        # A line that does not start a record: it belongs to the one above.
        return rng.choice(['  File "x.py", line 1', 'continued ' + word, 'ValueError: nope', ' '])
    if kind < 0.92:
        # Timestamped but too few fields to be a record.
        return f'{stamp}|{level}'
    return ''


def _random_log(rng, lines, delimiter, start=0, terminated=True):
    body = delimiter.join(_random_line(rng, start + index) for index in range(lines))
    data = body.encode('utf-8')
    if delimiter == '|\r\n':
        data = data.replace(b'\n', b'\r\n').replace(b'|\r\r\n', b'|\r\n')
    if terminated:
        data += delimiter.encode()
    return data


RANDOM_FILTERS = [
    ({}, None, ''),
    ({}, 'warning', ''),
    ({}, None, 'WHISPER'),
    ({}, 'error', 'árvíz'),
    ({'include': 'e', 'ignore_case': True}, None, ''),
    ({'exclude': r'^\s*$|DEBUG', 'use_regex': True}, 'info', 'o'),
]


@pytest.mark.parametrize('seed', range(24))
def test_the_reverse_reader_agrees_with_a_forward_reading(tmp_path, seed):
    rng = random.Random(seed)
    delimiter = '|\r\n' if seed % 4 == 3 else '|\n'
    data = _random_log(rng, rng.randint(0, 60), delimiter, terminated=seed % 5 != 0)
    if seed % 6 == 1:
        # Lines ahead of the first record.
        data = b'orphan line one' + delimiter.encode() + b'orphan two' + delimiter.encode() + data
    path = tmp_path / 'bazarr.log'
    path.write_bytes(data)
    reference_data = data.replace(b'\r\n', b'\n') if delimiter == '|\r\n' else data
    for stored_settings, level, contains in RANDOM_FILTERS:
        stored = StoredFilter(**stored_settings)
        expected = _forward_reference(reference_data, b'|\n', stored, level, contains)
        for block_size in (1, 5, 64, 4096):
            page = read_log_page(path, level=level, contains=contains, stored=stored, block_size=block_size,
                                 **EVERYTHING)
            assert page.total == len(expected), (stored_settings, level, contains, block_size)
            assert page.entries == expected, (stored_settings, level, contains, block_size)
        # And one page at a time.
        gathered = []
        for offset in range(0, len(expected) + 3, 3):
            gathered.extend(read_log_page(path, limit=3, offset=offset, level=level, contains=contains,
                                          stored=stored, block_size=7).entries)
        assert gathered == expected


@pytest.mark.parametrize('seed', range(16))
def test_a_remembered_count_stays_exact_as_the_log_grows(tmp_path, seed):
    rng = random.Random(1000 + seed)
    path = tmp_path / 'bazarr.log'
    data = _random_log(rng, rng.randint(1, 40), '|\n')
    path.write_bytes(data)
    cache = CountCache()
    written = 40
    for _ in range(5):
        for stored_settings, level, contains in RANDOM_FILTERS:
            stored = StoredFilter(**stored_settings)
            expected = _forward_reference(data, b'|\n', stored, level, contains)
            for limit, offset in ((5, 0), (5, 5), (MAX_LIMIT, 0)):
                page = read_log_page(path, limit=limit, offset=offset, level=level, contains=contains,
                                     stored=stored, cache=cache, block_size=rng.choice([3, 16, 256]))
                assert page.total == len(expected), (stored_settings, level, contains, limit, offset)
                assert page.entries == expected[offset:offset + limit]
        # Sometimes an unterminated line, as a record still being written,
        # which the next append finishes before adding more.
        addition = _random_log(rng, rng.randint(0, 12), '|\n', start=written, terminated=rng.random() < 0.7)
        written += 12
        if data and not data.endswith(b'|\n'):
            addition = rng.choice([b'', b' and the rest of it']) + b'|\n' + addition
        data += addition
        with open(path, 'ab') as handle:
            handle.write(addition)


def test_a_rewritten_file_is_counted_again_rather_than_trusted(tmp_path):
    path = tmp_path / 'bazarr.log'
    _numbered_log(path, 300)
    cache = CountCache()
    assert read_log_page(path, cache=cache).total == 300
    # Same inode, different content that runs past the remembered record.
    with open(path, 'r+b') as handle:
        handle.truncate(0)
        levels = ['INFO'] * 700
        handle.write(''.join(f'2026-09-26 11:00:00|{level:<8}|{"other":<32}|rewritten {index}|\n'
                             for index, level in enumerate(levels)).encode())
    page = read_log_page(path, cache=cache, limit=1)
    assert page.total == 700
    assert page.entries[0]['message'] == 'rewritten 699'


def test_a_refresh_reads_only_the_end_of_the_file(tmp_path, monkeypatch):
    path = tmp_path / 'bazarr.log'
    _numbered_log(path, 20000)
    size = path.stat().st_size
    reads = []

    class CountingFile:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            data = self._handle.read(size)
            reads.append(len(data))
            return data

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()

    monkeypatch.setattr(log_reader, '_open_log', lambda p: CountingFile(open(p, 'rb')))
    cache = CountCache()
    read_log_page(path, limit=50, cache=cache, block_size=4096)
    with open(path, 'ab') as handle:
        handle.write(b'2026-09-25 23:59:59|ERROR   |bazarr.test                     |appended|\n')
    reads.clear()
    page = read_log_page(path, limit=50, cache=cache, block_size=4096)
    assert page.total == 20001
    assert page.entries[0]['message'] == 'appended'
    assert sum(reads) < 64 * 1024 < size


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch, tmp_path):
    from api import api_bp
    from api.system import logs as logs_api
    from app.config import settings

    monkeypatch.setitem(settings.auth, 'apikey', TEST_API_KEY)
    for name, value in (('include_filter', ''), ('exclude_filter', ''), ('ignore_case', False),
                        ('use_regex', False)):
        monkeypatch.setitem(settings.log, name, value)
    path = tmp_path / 'bazarr.log'
    monkeypatch.setattr(logs_api, 'get_log_file_path', lambda: str(path))
    monkeypatch.setattr(logs_api, '_log_counts', CountCache())
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    client = app.test_client()

    def get(**params):
        return client.get('/api/system/logs', query_string=params, headers={'X-API-KEY': TEST_API_KEY})

    get.path = path
    get.client = client
    get.settings = settings
    return get


def test_the_endpoint_returns_a_page_and_the_total(api):
    _numbered_log(api.path, 1200)
    response = api()
    assert response.status_code == 200
    body = response.get_json()
    assert body['total'] == 1200
    assert body['limit'] == 500 and body['offset'] == 0
    assert body['filter_errors'] == []
    assert len(body['data']) == 500
    assert body['data'][0] == {'timestamp': '2026-09-25 10:19:59', 'type': 'CRITICAL', 'message': 'record 1199',
                               'exception': None}

    body = api(limit=2, offset=3, level='ERROR', contains='record 11').get_json()
    # ERROR and CRITICAL are the records whose number ends in 3, 4, 8 or 9.
    assert [entry['message'] for entry in body['data']] == ['record 1193', 'record 1189']
    assert body['total'] == 44


def test_the_endpoint_caps_limit_and_refuses_what_it_cannot_serve(api):
    _numbered_log(api.path, 10)
    assert api(limit=999999).get_json()['limit'] == MAX_LIMIT
    for params in ({'limit': 0}, {'limit': 'many'}, {'offset': -1}, {'level': 'loud'}):
        assert api(**params).status_code == 400, params


def test_an_invalid_stored_regex_is_reported_not_silently_ignored(api, monkeypatch):
    _numbered_log(api.path, 20)
    monkeypatch.setitem(api.settings.log, 'use_regex', True)
    monkeypatch.setitem(api.settings.log, 'include_filter', '(record')
    monkeypatch.setitem(api.settings.log, 'exclude_filter', '[0-')
    body = api().get_json()
    # Neither is applied, as before, but the reader is now told.
    assert body['total'] == 20
    assert [(error['filter'], error['pattern']) for error in body['filter_errors']] == \
        [('include', '(record'), ('exclude', '[0-')]
    assert all(error['message'] for error in body['filter_errors'])

    monkeypatch.setitem(api.settings.log, 'include_filter', r'record 1\d')
    monkeypatch.setitem(api.settings.log, 'exclude_filter', '')
    body = api().get_json()
    assert body['filter_errors'] == []
    assert body['total'] == 10


def test_the_endpoint_still_needs_the_api_key(api):
    _numbered_log(api.path, 3)
    assert api.client.get('/api/system/logs').status_code == 401


def test_emptying_the_log_is_unchanged(api, monkeypatch):
    from api.system import logs as logs_api

    emptied = []
    monkeypatch.setattr(logs_api, 'empty_log', lambda: emptied.append(True))
    response = api.client.delete('/api/system/logs', headers={'X-API-KEY': TEST_API_KEY})
    assert response.status_code == 204
    assert emptied == [True]


def test_the_download_still_serves_the_whole_file(monkeypatch, tmp_path):
    from app import ui
    from app.config import settings

    path = tmp_path / 'bazarr.log'
    _numbered_log(path, 3000)
    monkeypatch.setattr(ui, 'get_log_file_path', lambda: str(path))
    monkeypatch.setitem(settings.auth, 'type', None)
    app = Flask(__name__)
    app.secret_key = 'test-secret'
    app.register_blueprint(ui.ui_bp)
    response = app.test_client().get('/bazarr.log')
    assert response.status_code == 200
    assert response.headers['Content-Disposition'].startswith('attachment')
    assert response.get_data() == path.read_bytes()


# ---------------------------------------------------------------------------
# The regression guard for the actual complaint
# ---------------------------------------------------------------------------

_MEASURE = textwrap.dedent('''
    import json, resource, sys, time
    sys.path.insert(0, sys.argv[1])
    from utilities import log_reader
    from utilities.log_reader import CountCache, read_log_page

    path = sys.argv[2]
    reads = []
    real_open = log_reader._open_log

    class Counting:
        def __init__(self, handle):
            self._handle = handle
        def read(self, size=-1):
            data = self._handle.read(size)
            reads.append(len(data))
            return data
        def __getattr__(self, name):
            return getattr(self._handle, name)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self._handle.close()

    log_reader._open_log = lambda p: Counting(real_open(p))

    def peak_rss_mb():
        # getrusage carries the forking parent's high-water mark across exec on
        # Linux, which would measure pytest rather than this process. VmHWM
        # belongs to this process's own address space.
        try:
            with open('/proc/self/status') as status:
                for line in status:
                    if line.startswith('VmHWM:'):
                        return int(line.split()[1]) / 1024
        except OSError:
            pass
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    cache = CountCache()
    started = time.perf_counter()
    first = read_log_page(path, limit=500, cache=cache)
    cold = time.perf_counter() - started
    cold_read = sum(reads)
    with open(path, 'ab') as handle:
        for index in range(20):
            handle.write(b'2026-09-25 23:59:59|ERROR   |bazarr.test                     |appended %d|\\n' % index)
    reads.clear()
    started = time.perf_counter()
    refresh = read_log_page(path, limit=500, cache=cache)
    warm = time.perf_counter() - started
    print(json.dumps({
        'cold_seconds': cold, 'cold_read': cold_read, 'first_total': first.total,
        'first_entries': len(first.entries), 'first_message': first.entries[0]['message'],
        'warm_seconds': warm, 'warm_read': sum(reads), 'refresh_total': refresh.total,
        'refresh_message': refresh.entries[0]['message'],
        'peak_rss_mb': peak_rss_mb(),
    }))
''')


@pytest.mark.skipif(sys.platform == 'win32', reason='peak RSS is read through the resource module')
def test_the_first_page_of_a_200mb_log_is_read_in_bounded_time_and_memory(tmp_path):
    path = tmp_path / 'bazarr.log'
    try:
        # About a megabyte of the mix a busy debug-mode day produces, written
        # until the file passes 200MB. Numbered, so the newest record is known.
        records = 0
        written = 0
        levels = ['DEBUG   ', 'INFO    ', 'DEBUG   ', 'WARNING ', 'INFO    ', 'ERROR   ']
        with open(path, 'wb') as handle:
            while written < 200 * 1024 * 1024:
                chunk = []
                for _ in range(5000):
                    level = levels[records % 6]
                    if records % 97 == 0:
                        line = (f"2026-09-25 10:00:00|ERROR   |{'root':<32}|failed {records}|"
                                f"'Traceback (most recent call last):\\n  File \"x.py\"\\nValueError: {records}'|\n")
                    else:
                        line = (f'2026-09-25 10:00:00|{level}|{"socketio.server":<32}|'
                                f'emitting event "data" to all [/] {records}|\n')
                    chunk.append(line)
                    records += 1
                data = ''.join(chunk).encode()
                handle.write(data)
                written += len(data)

        result = subprocess.run([sys.executable, '-c', _MEASURE, BAZARR_DIR, str(path)], capture_output=True,
                                text=True, timeout=300, check=True)
        measured = json.loads(result.stdout)
        print(measured)

        assert measured['first_total'] == records
        assert measured['first_entries'] == 500
        assert measured['first_message'].endswith(f' {records - 1}')
        # The file never has to fit in memory. Reading it whole, as the endpoint
        # used to, cannot stay under this: the file alone is 200MB.
        assert measured['peak_rss_mb'] < 128, measured
        # A cold read counts the whole file once, in a count-only pass. It is
        # about a second on a workstation; this bound catches a regression to
        # building records for every line, not a slow runner.
        assert measured['cold_seconds'] < 30, measured
        # The refresh the Logs page makes reads the end of the file and no more.
        assert measured['refresh_total'] == records + 20
        assert measured['refresh_message'] == 'appended 19'
        assert measured['warm_read'] < 2 * 1024 * 1024, measured
        assert measured['warm_seconds'] < 2, measured
    finally:
        path.unlink(missing_ok=True)
