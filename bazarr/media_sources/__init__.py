# coding=utf-8
"""Media sources: libraries of series, movies and sports that do not come from an arr.

A source owns rows in its own tables (``media_sources``, ``table_source_*``). It never writes an arr or Sportarr
table, and no arr code writes a source table. The shared download pipeline learns about a source item through
one object, ``context.SourceItemContext``, handed to it on a keyword of its own (``source=``).
"""
