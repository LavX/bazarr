"""Standalone show and movie projections for the existing library pages."""

import sqlalchemy as sa
from flask_restx import fields

from api.utils import postprocess
from . import repository
from .api_rows import postprocess_source, source_row_fields
from .union import union_page


library_row_fields = {
    **source_row_fields,
    'library_id': fields.Integer(),
    'needs_attention': fields.Boolean(),
    'resolution_note': fields.String(),
}


def _source_columns(media, sources, profile):
    return (
        media.id, media.title, media.sort_title, media.year, media.poster, media.monitored,
        media.imdb_id.label('imdbId'), profile.label('profileId'),
        media.source_id.label('_owner'), media.source_id, media.library_id,
        sources.name.label('source_name'), sources.kind.label('source_kind'), media.identity_state,
        sa.or_(media.identity_state.in_(('ambiguous', 'unmatched')), profile.is_(None)).label('needs_attention'),
    )


def movie_rows(owner_filter):
    items, libraries, sources = repository.Items, repository.Libraries, repository.Sources
    item, library, source = items.c, libraries.c, sources.c
    profile = sa.func.coalesce(item.profile_id, library.profile_id)
    return sa.select(
        *_source_columns(item, source, profile), item.path, item.audio_language, item.subtitles, item.missing_subtitles,
        item.scene_name.label('sceneName'), item.shadow_owner.label('shadowed_by'),
    ).select_from(items.join(
        libraries, sa.and_(item.library_id == library.id, item.source_id == library.source_id),
    ).join(sources, item.source_id == source.id)).where(
        item.kind == 'movie', owner_filter.source(item.source_id),
    )


def show_rows(owner_filter):
    groups, libraries, sources, items = repository.Groups, repository.Libraries, repository.Sources, repository.Items
    group, library, source, item = groups.c, libraries.c, sources.c, items.c
    profile = sa.func.coalesce(group.profile_id, library.profile_id)
    missing = sa.and_(item.missing_subtitles.is_not(None), item.missing_subtitles != '[]',
                      item.arr_shadow == 0, item.server_missing == 0, item.monitored == 1,
                      item.identity_state.in_(('identified', 'parsed')), sa.func.coalesce(item.profile_id, profile).is_not(None))
    episodes = sa.and_(item.group_id == group.id, item.kind == 'episode',
                       item.source_id == group.source_id, item.library_id == group.library_id)
    count = sa.select(sa.func.count()).where(episodes).correlate(groups).scalar_subquery()
    missing_count = sa.select(sa.func.count()).where(episodes, missing).correlate(
        groups, libraries).scalar_subquery()
    audio = sa.select(item.audio_language).where(episodes, item.audio_language.notin_(('', '[]'))).order_by(
        sa.func.coalesce(item.season, 0), sa.func.coalesce(item.episode, 0), item.part_number, item.id,
    ).limit(1).correlate(groups).scalar_subquery()
    return sa.select(
        *_source_columns(group, source, profile), group.tvdb_id.label('tvdbId'), group.resolution_note,
        count.label('episodeFileCount'),
        sa.case((sa.and_(source.enabled == 1, library.enabled == 1), missing_count), else_=0)
        .label('episodeMissingCount'),
        audio.label('audio_language'),
        sa.case((source.kind == 'folder', group.group_key)).label('path'),
    ).select_from(groups.join(
        libraries, sa.and_(group.library_id == library.id, group.source_id == library.source_id),
    ).join(sources, group.source_id == source.id)).where(
        group.kind == 'show', owner_filter.source(group.source_id),
    )


def library_page(session, arr, source, sort_title, *, start, length, needs_attention):
    arr = arr.order_by(None).add_columns(
        arr.selected_columns.arr_instance_id.label('_owner'), sort_title.label('sort_title'),
        sa.cast(sa.null(), sa.Integer).label('source_id'),
        sa.cast(sa.null(), sa.Integer).label('library_id'),
        *(sa.cast(sa.null(), sa.Text).label(key) for key in (
            'source_name', 'source_kind', 'identity_state', 'shadowed_by', 'resolution_note')),
        sa.literal(False).label('needs_attention'),
    )
    if needs_attention:
        arr = arr.where(sa.false())
        source = source.where(source.selected_columns.needs_attention.element)
    return union_page(session, arr, source, start=start, length=length,
                      sort=lambda c: (sa.func.coalesce(c.sort_title, c.title, ''), c.origin, c.id))


def library_row(row, mixed):
    data = dict(row._mapping)
    if mixed and row.origin == 'source':
        # The common column type for the Arr text flags and source integers is Text.
        data['monitored'] = row.monitored == '1'
        return postprocess_source(data)
    return postprocess(data)
