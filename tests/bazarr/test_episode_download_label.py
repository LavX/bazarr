# coding=utf-8
"""An episode download job is labelled with the series that owns the episode.

The label used to be looked up as if the episode's upstream id were a series
id, so it named whichever series happened to carry that number, or none at
all. The lookup now follows the episode's local series relationship within its
owning instance. The queue re-binds the caller's locals as the job's keyword
arguments, so these tests go through the real queue entry point: a helper
variable in the labelling branch would break every queued episode search.
"""
import inspect

import pytest
from sqlalchemy.orm import sessionmaker

from test_sportarr_kind_migration import migration_engine  # noqa: F401


@pytest.fixture
def library(migration_engine):  # noqa: F811
    from app.database import Base, TableShows, TableEpisodes

    Base.metadata.create_all(migration_engine)
    with sessionmaker(bind=migration_engine)() as session:
        for owner in (7, 8):
            session.add(TableShows(id=owner, sonarrSeriesId=10, arr_instance_id=owner,
                                   path=f'/tv/{owner}', title=f'Owner {owner}'))
            # A series whose upstream id is the episode's upstream id.
            session.add(TableShows(id=owner + 10, sonarrSeriesId=99, arr_instance_id=owner,
                                   path=f'/tv/decoy-{owner}', title='Wrong series'))
        session.flush()
        for owner in (7, 8):
            session.add(TableEpisodes(id=owner, series_id=owner, arr_instance_id=owner,
                                      sonarrSeriesId=10, sonarrEpisodeId=99,
                                      path=f'/tv/{owner}/one.mkv', title='One', season=1, episode=1))
        # Only the first instance has this episode.
        session.add(TableEpisodes(id=20, series_id=7, arr_instance_id=7,
                                  sonarrSeriesId=10, sonarrEpisodeId=55,
                                  path='/tv/7/two.mkv', title='Two', season=1, episode=2))
        session.commit()
        yield session


@pytest.fixture
def queued(library, monkeypatch):
    from subtitles.mass_download import series

    monkeypatch.setattr(series, 'database', library)
    jobs = []

    def feed(**job):
        jobs.append(job)
        return len(jobs)

    monkeypatch.setattr(series.jobs_queue, 'feed_jobs_pending_queue', feed)
    return series, jobs


@pytest.mark.parametrize('owner', [7, 8])
def test_download_label_uses_the_episodes_own_series(queued, owner):
    series, jobs = queued

    series.episode_download_subtitles(99, arr_instance_id=owner)

    assert [job['job_name'] for job in jobs] == [f'Downloading missing subtitles for Owner {owner}']
    # The queued job re-runs this function with exactly its own parameters.
    assert jobs[0]['module'] == 'subtitles.mass_download.series'
    assert jobs[0]['func'] == 'episode_download_subtitles'
    parameters = inspect.signature(series.episode_download_subtitles).parameters
    assert set(jobs[0]['kwargs']) == set(parameters)
    assert jobs[0]['kwargs']['no'] == 99
    assert jobs[0]['kwargs']['arr_instance_id'] == owner


def test_single_instance_episode_is_labelled_without_an_owner(queued):
    series, jobs = queued

    series.episode_download_subtitles(55)

    assert [job['job_name'] for job in jobs] == ['Downloading missing subtitles for Owner 7']


def test_ambiguous_episode_label_does_not_pick_an_owner(queued):
    series, jobs = queued

    series.episode_download_subtitles(99)

    assert [job['job_name'] for job in jobs] == ['Downloading missing subtitles for Unknown Series']


@pytest.mark.parametrize('episode, owner', [(12345, None), (55, 8)])
def test_unknown_episode_is_labelled_unknown(queued, episode, owner):
    series, jobs = queued

    series.episode_download_subtitles(episode, arr_instance_id=owner)

    assert [job['job_name'] for job in jobs] == ['Downloading missing subtitles for Unknown Series']
