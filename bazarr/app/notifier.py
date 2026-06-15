# coding=utf-8

from apprise import Apprise, AppriseAsset
import logging
import re
from urllib.parse import quote

from .database import TableSettingsNotifier, TableEpisodes, TableShows, TableMovies, database, insert, delete, select
from arr_instances.resolution import scoped


# Notifier providers that accept `{bazarr_*}` placeholders in the URL and
# need the full media record's columns to expand them. Any other provider
# uses the URL verbatim, so the variable expansion + full-row fetch is
# pure overhead - notably TableEpisodes carries a large ffprobe_cache
# blob that we should not pull on every sub download for non-custom
# notifier deployments.
_CUSTOM_NOTIFIER_NAMES = frozenset({"Form", "XML", "JSON"})


def _has_custom_notifier(providers):
    """True iff any enabled provider is one of Form / XML / JSON, the
    only notifiers whose URL accepts `{bazarr_*}` placeholders."""
    return any(p.name in _CUSTOM_NOTIFIER_NAMES for p in providers)


def _format_year_suffix(year):
    """Render a `(year)` suffix only when the year is actually populated.
    Tracks the existing notification body shape so behaviour stays
    identical to the pre-refactor code."""
    if year in (None, '', '0'):
        return ''
    return f' ({year})'


def update_notifier():
    # define apprise object
    a = Apprise()

    # Retrieve all the details
    results = a.details()

    notifiers_added = []
    notifiers_kept = []

    notifiers_in_db = [row.name for row in
                       database.execute(
                           select(TableSettingsNotifier.name))
                       .all()]

    for x in results['schemas']:
        if x['service_name'] not in notifiers_in_db:
            notifiers_added.append({'name': str(x['service_name']), 'enabled': 0})
            logging.debug(f'Adding new notifier agent: {x["service_name"]}')  # noqa: G004
        else:
            notifiers_kept.append(x['service_name'])

    notifiers_to_delete = [item for item in notifiers_in_db if item not in notifiers_kept]

    for item in notifiers_to_delete:
        database.execute(
            delete(TableSettingsNotifier)
            .where(TableSettingsNotifier.name == item))

    database.execute(
        insert(TableSettingsNotifier)
        .values(notifiers_added)
        .on_conflict_do_nothing())


def get_notifier_providers():
    return database.execute(
        select(TableSettingsNotifier.name, TableSettingsNotifier.url)
        .where(
            TableSettingsNotifier.enabled == 1,
            TableSettingsNotifier.url.is_not(None),
        ))\
        .all()


def send_notifications(sonarr_series_id, sonarr_episode_id, message, arr_instance_id=None):
    # ``arr_instance_id`` (#156) scopes the media lookups to the OWNING instance.
    # In multi-instance the upstream ids (sonarrSeriesId/sonarrEpisodeId) are no
    # longer globally unique, so an unscoped lookup could expand the wrong
    # instance's title/path into a custom-notifier URL. None = legacy unscoped
    # path (single default instance), byte-identical to before.
    providers = get_notifier_providers()
    if not len(providers):
        return

    # When no custom notifier is enabled, only the title/year/season/
    # episode/title fields land in the notification body - we MUST NOT
    # SELECT * on TableEpisodes (which includes the heavy ffprobe_cache
    # blob) just to throw the rest away. Codex flagged this as a
    # noticeable cost on bulk subtitle ops.
    custom_notifier_used = _has_custom_notifier(providers)

    if custom_notifier_used:
        series = database.execute(
            scoped(
                select(TableShows)
                .where(TableShows.sonarrSeriesId == sonarr_series_id),
                TableShows.arr_instance_id, arr_instance_id))\
            .scalars()\
            .first()
        if not series:
            return
        series_title = series.title
        series_year = series.year
        episode = database.execute(
            scoped(
                select(TableEpisodes)
                .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id),
                TableEpisodes.arr_instance_id, arr_instance_id))\
            .scalars()\
            .first()
        if not episode:
            return
        episode_season = episode.season
        episode_number = episode.episode
        episode_title = episode.title
        media_variables = {}
        media_variables.update(_build_media_variables(series, 'series'))
        media_variables.update(_build_media_variables(episode, 'episode'))
    else:
        series_row = database.execute(
            scoped(
                select(TableShows.title, TableShows.year)
                .where(TableShows.sonarrSeriesId == sonarr_series_id),
                TableShows.arr_instance_id, arr_instance_id))\
            .first()
        if not series_row:
            return
        series_title, series_year = series_row
        episode_row = database.execute(
            scoped(
                select(TableEpisodes.season, TableEpisodes.episode, TableEpisodes.title)
                .where(TableEpisodes.sonarrEpisodeId == sonarr_episode_id),
                TableEpisodes.arr_instance_id, arr_instance_id))\
            .first()
        if not episode_row:
            return
        episode_season, episode_number, episode_title = episode_row
        media_variables = None  # not consulted on this path

    series_year_suffix = _format_year_suffix(series_year)

    asset = AppriseAsset(async_mode=False)

    apobj = Apprise(asset=asset)

    for provider in providers:
        if provider.name in _CUSTOM_NOTIFIER_NAMES:
            apobj.add(_expand_notifier_url(provider.url, media_variables))
        else:
            apobj.add(provider.url)

    apobj.notify(
        title='Bazarr notification',
        body=f"{series_title}{series_year_suffix} - S{episode_season:02d}E{episode_number:02d} - {episode_title} : {message}",
    )


def send_notifications_movie(radarr_id, message, arr_instance_id=None):
    # ``arr_instance_id`` (#156) scopes the lookup to the OWNING instance so a
    # colliding radarrId under another instance never leaks its metadata into a
    # custom-notifier URL. None = legacy unscoped path.
    providers = get_notifier_providers()
    if not len(providers):
        return

    custom_notifier_used = _has_custom_notifier(providers)

    if custom_notifier_used:
        movie = database.execute(
            scoped(
                select(TableMovies)
                .where(TableMovies.radarrId == radarr_id),
                TableMovies.arr_instance_id, arr_instance_id))\
            .scalars()\
            .first()
        if not movie:
            return
        movie_title = movie.title
        movie_year = movie.year
        media_variables = _build_media_variables(movie, 'movie')
    else:
        movie_row = database.execute(
            scoped(
                select(TableMovies.title, TableMovies.year)
                .where(TableMovies.radarrId == radarr_id),
                TableMovies.arr_instance_id, arr_instance_id))\
            .first()
        if not movie_row:
            return
        movie_title, movie_year = movie_row
        media_variables = None  # not consulted on this path

    movie_year_suffix = _format_year_suffix(movie_year)

    asset = AppriseAsset(async_mode=False)

    apobj = Apprise(asset=asset)

    for provider in providers:
        if provider.name in _CUSTOM_NOTIFIER_NAMES:
            apobj.add(_expand_notifier_url(provider.url, media_variables))
        else:
            apobj.add(provider.url)

    apobj.notify(
        title='Bazarr notification',
        body=f"{movie_title}{movie_year_suffix} : {message}",
    )


def _build_media_variables(record, prefix):
    if record is None or not prefix:
        return {}

    return {f'bazarr_{prefix}_{key}': value for key, value in record.to_dict().items()}


def _expand_notifier_url(url, media_variables):
    if url is None or not media_variables:
        return url

    # Looks for {bazarr_*} placeholders in the URL string
    placeholder_pattern = re.compile(r'\{(bazarr_[A-Za-z0-9_]+)\}')

    def replace(match):
        key = match.group(1)
        if key not in media_variables:
            return ''

        value = media_variables[key]
        if value is None:
            return ''

        return quote(str(value), safe='')

    return placeholder_pattern.sub(replace, url)
