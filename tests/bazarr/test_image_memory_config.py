# coding=utf-8
"""The shipped image has to cap glibc's malloc arenas.

glibc gives each thread its own malloc arena, up to eight per core, and each one
reserves address space that becomes resident as it is touched. Bazarr runs well
over a hundred threads in normal operation, because the web server parks one per
open browser tab for its event stream, so without a cap the process pays for a
heap arena on most of them.

Measured on a real deployment, same database and same workload: capping arenas
at two took the container from 514 MiB to 347 MiB and the backend process from
388 MiB to 278 MiB, with the thread count unchanged at 123 at the time. The
web-server pool has since been right-sized separately (general.web_server_threads,
default 32; see app/server.py), which is complementary: the arena cap bounds
what each thread costs, the pool size bounds how many exist.

Asserted here because it is one environment variable in the Dockerfile with
nothing else referring to it, so it would be removed by a tidy-up without
anything noticing until someone measured again.
"""
import os
import re

DOCKERFILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'Dockerfile')


def _env_values():
    """Every KEY=VALUE the Dockerfile sets in an ENV instruction."""
    text = open(DOCKERFILE).read()
    # ENV blocks continue across escaped newlines
    text = text.replace('\\\n', ' ')
    values = {}
    for line in text.splitlines():
        if not line.startswith('ENV '):
            continue
        for key, value in re.findall(r'(\w+)=("[^"]*"|\S+)', line[4:]):
            values[key] = value.strip('"')
    return values


def test_the_image_caps_the_malloc_arenas():
    values = _env_values()
    assert 'MALLOC_ARENA_MAX' in values, (
        'MALLOC_ARENA_MAX is not set in the image. Without it resident memory '
        'scales with thread count, which cost about 110 MiB when measured.')
    assert values['MALLOC_ARENA_MAX'].isdigit(), values['MALLOC_ARENA_MAX']
    assert 1 <= int(values['MALLOC_ARENA_MAX']) <= 4, (
        f"MALLOC_ARENA_MAX is {values['MALLOC_ARENA_MAX']}. Two is the usual "
        'server setting; much higher gives the saving back.')
