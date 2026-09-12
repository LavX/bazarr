"""Destination-query completeness against the existing path mappers."""

import ast
import json
import os
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from test_sportarr_kind_migration import migration_engine  # noqa: F401
from test_sportarr_indexer import indexed_library  # noqa: F401
from test_sportarr_manual import manual_library  # noqa: F401


@pytest.fixture
def output_library(migration_engine, monkeypatch, tmp_path):  # noqa: F811
    from app.config import settings
    from app.database import Base, TableArrInstances, TableSportsLeagues

    Base.metadata.create_all(migration_engine)
    session = Session(bind=migration_engine)
    monkeypatch.setattr(settings.general, "subfolder", "current")
    for owner in (1, 2):
        session.execute(
            sa.insert(TableArrInstances).values(
                id=owner,
                kind="sportarr",
                name=str(owner),
                stable_key=str(owner),
                port=1867,
            )
        )
    session.execute(
        sa.insert(TableSportsLeagues).values(
            id=2, arr_instance_id=2, sportarrLeagueId=2, title="Foreign"
        )
    )
    yield session, tmp_path
    session.close()


def _cases(folder):
    names = [
        "event",
        "Évent",
        "Kickoff",
        "İstanbul",
        "ΟΣ",
        "οσ",
        "ος",
        "東京戦",
        'A "director\'s" event',
        "Event_[%].Part",
    ]
    for stem in names:
        for extension in (".mkv", ".recording", ""):
            yield (
                "movie",
                stem,
                f"/remote/{stem}{extension}",
                [["/remote", str(folder)]],
                "root",
            )
        yield (
            "movie",
            stem,
            r"\\server\share\film.mkv",
            [[r"\\server\share\film.mkv", str(folder / f"{stem}.mkv")]],
            "native_unc",
        )
        yield "movie", stem, "/remote/film.mkv", [["film", stem]], "native_fragment"
        yield (
            "movie",
            stem,
            "/remote/film.mkv",
            [["/remote", str(folder)], ["film", stem]],
            "native_first_match",
        )
        yield (
            "sports",
            stem,
            f"/remote/season/{stem}.mkv",
            [["/remote", "/unrelated"], ["/remote/season/", str(folder)]],
            "longest",
        )
        yield (
            "sports",
            stem,
            r"\\server\share\film.mkv",
            [["//server/share/film.mkv/", str(folder / f"{stem}.mkv")]],
            "sports_unc_exact",
        )
        yield (
            "sports",
            stem,
            f"//server/share/{stem}.mkv",
            [[r"\\server\share" + "\\", str(folder)]],
            "sports_unc_root",
        )
        yield (
            "sports",
            stem,
            "/remote/film.mkv",
            [["/remote/film.mkv/", str(folder / f"{stem}.mkv")]],
            "sports_trailing",
        )
        yield "sports", stem, f"/{stem}.mkv", [["/", str(folder)]], "sports_root"
        for quotes in (
            'A "director\'s"',
            "Parent's",
            'Parent"quoted',
            r"Parent\escaped",
        ):
            source = f"/remote/{quotes}/unrelated.srt"
            yield (
                "movie",
                stem,
                source,
                [[source, str(folder / f"{stem}.en.srt")]],
                "recorded_exact",
            )
            yield (
                "sports",
                stem,
                source,
                [[source + "/", str(folder / f"{stem}.en.srt")]],
                "recorded_sports_exact",
            )


def test_query_admits_every_canonical_conflict_in_generated_matrix(output_library):
    from app.database import TableArrInstances, TableMovies, TableSportsEvents
    from sportarr.output import SportsOutputNamespace
    from utilities.path_mappings import _apply_mapping, apply_sports_mapping

    session, folder = output_library
    failures = []
    checked = conflicts = 0
    for media_type, stem, path, mapping, label in _cases(folder):
        table = TableMovies if media_type == "movie" else TableSportsEvents
        session.execute(sa.delete(table))
        session.execute(
            sa.update(TableArrInstances)
            .where(TableArrInstances.id == 2)
            .values(
                kind="radarr" if media_type == "movie" else "sportarr",
                path_mappings=json.dumps(mapping),
            )
        )
        for mode in (
            "media",
            "repr",
            "json_unicode",
            "json_ascii",
            "adjacent",
            "commented",
        ):
            payload = [["en", path, 10]]
            encoded = (
                repr(payload)
                if mode == "repr"
                else json.dumps(payload, ensure_ascii=mode == "json_ascii")
            )
            if mode in ("adjacent", "commented"):
                separator = " " if mode == "adjacent" else " # continued\n"
                encoded = (
                    "[['en', ("
                    + separator.join("u" + repr(char) for char in path)
                    + "), 10]]"
                )
            source = path if mode == "media" else ast.literal_eval(encoded)[0][1]
            mapped = (
                _apply_mapping(source, mapping, False)
                if media_type == "movie"
                else apply_sports_mapping(source, mapping)
            )
            actual_stem = os.path.splitext(os.path.basename(mapped))[0].lower()
            wanted = stem.lower()
            conflict = (
                actual_stem == wanted
                or actual_stem.startswith(wanted + ".")
                or wanted.startswith(actual_stem + ".")
            )
            values = dict(
                id=81,
                arr_instance_id=2,
                path=source if mode == "media" else "/unrelated/recording.mkv",
                subtitles="[]" if mode == "media" else encoded,
            )
            session.execute(sa.delete(table))
            if media_type == "movie":
                values.update(radarrId=81, title="Foreign", tmdbId="matrix")
            else:
                values.update(
                    league_id=2, sportarrEventId=81, file_id=81, title="Foreign"
                )
            session.execute(sa.insert(table).values(**values))
            context = SimpleNamespace(
                mapped_path=str(folder / f"{stem}.mkv"), event_id=1, arr_instance_id=1
            )
            physical_conflict = conflict and os.path.realpath(
                os.path.dirname(mapped)
            ) == os.path.realpath(folder)
            try:
                SportsOutputNamespace(context, session).validate(session)
                admitted = False
            except ValueError:
                admitted = True
            checked += 1
            conflicts += int(conflict)
            if physical_conflict and not admitted:
                failures.append((media_type, label, mode, stem, source))
    print(
        f"Candidate matrix: {checked} cases, {conflicts} canonical conflicts, {len(failures)} omissions"
    )
    assert checked >= 500 and conflicts >= 300
    assert not failures, failures[:20]


@pytest.mark.parametrize(
    "kind",
    [
        "sports_unc",
        "native_unc",
        "sports_trailing_prefix",
        "recorded_quotes",
        "unicode",
    ],
)
@pytest.mark.parametrize("existing", [False, True])
def test_real_saver_preserves_normalized_or_encoded_foreign_owner(
    manual_library,  # noqa: F811
    monkeypatch,
    kind,
    existing,
):
    from app.config import settings
    from app.database import (
        TableArrInstances,
        TableSportsEvents,
        TableMovies,
        TableHistorySports,
    )

    service, session, folder = manual_library
    target = folder / "shared"
    target.mkdir()
    stem = "Évent" if kind == "unicode" else "event"
    destination = target / f"{stem}.en.srt"
    expected = b"Foreign owner edited subtitle"
    if existing:
        destination.write_bytes(expected)
    monkeypatch.setattr(settings.general, "subfolder", "absolute")
    monkeypatch.setattr(settings.general, "subfolder_custom", str(target))
    session.execute(
        sa.update(TableSportsEvents)
        .where(TableSportsEvents.id == 62)
        .values(path="/sports/other.mkv")
    )
    if kind == "unicode":
        for owner in (1, 2):
            (folder / str(owner) / "event.mkv").rename(
                folder / str(owner) / f"{stem}.mkv"
            )
            session.execute(
                sa.update(TableSportsEvents)
                .where(TableSportsEvents.id == 60 + owner)
                .values(path=f"/sports/{stem}.mkv")
            )
    else:
        foreign = r"\\server\share\film.mkv" if "unc" in kind else "/sports/other.mkv"
        source = foreign + "/" if kind == "sports_trailing_prefix" else foreign
        if kind == "recorded_quotes":
            source = '/remote/A "director\'s" subtitle.srt'
        mapping = [
            [
                source,
                str(
                    destination if kind == "recorded_quotes" else folder / "2/event.mkv"
                ),
            ]
        ]
        if kind in ("native_unc", "recorded_quotes"):
            session.execute(
                sa.insert(TableArrInstances).values(
                    id=3,
                    kind="radarr",
                    name="Native",
                    stable_key="native",
                    port=7878,
                    path_mappings=json.dumps(mapping),
                )
            )
            session.execute(
                sa.insert(TableMovies).values(
                    id=81,
                    arr_instance_id=3,
                    radarrId=81,
                    title="Native",
                    tmdbId="native-fixture",
                    path="/unrelated/recording.mkv"
                    if kind == "recorded_quotes"
                    else foreign,
                    subtitles=repr([["en", source, 10]])
                    if kind == "recorded_quotes"
                    else "[]",
                )
            )
        else:
            session.execute(
                sa.update(TableArrInstances)
                .where(TableArrInstances.id == 2)
                .values(path_mappings=json.dumps(mapping))
            )
            session.execute(
                sa.update(TableSportsEvents)
                .where(TableSportsEvents.id == 62)
                .values(path=foreign)
            )
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    caught = None
    try:
        service.manual_download_sports(61, candidate, arr_instance_id=1)
    except (ValueError, OSError) as exc:
        caught = exc
    assert (
        destination.read_bytes() == expected if existing else not destination.exists()
    )
    assert caught is not None
    assert session.execute(sa.select(TableHistorySports)).first() is None


@pytest.mark.parametrize("encoded", [False, True])
def test_root_mapping_unicode_selectivity_and_encoded_availability(
    output_library, encoded
):
    from app.database import TableArrInstances, TableMovies
    from sportarr.output import SportsOutputNamespace

    session, folder = output_library
    session.execute(
        sa.update(TableArrInstances)
        .where(TableArrInstances.id == 2)
        .values(kind="radarr", path_mappings=json.dumps([["/remote", str(folder)]]))
    )
    for index in range(140):
        path = f"/remote/library/Épisode-{index}.mkv"
        subtitles = json.dumps(
            [["en", f"/remote/library/Épisode-{index}.en.srt", 10]],
            ensure_ascii=encoded,
        )
        session.execute(
            sa.insert(TableMovies).values(
                id=index + 10,
                arr_instance_id=2,
                radarrId=index + 10,
                title="Foreign",
                tmdbId=str(index + 10),
                path=path,
                subtitles=subtitles,
            )
        )
    namespace = SportsOutputNamespace(
        SimpleNamespace(
            mapped_path=str(folder / "Évent.mkv"), event_id=1, arr_instance_id=1
        ),
        session,
    )
    namespace.validate(session)


def test_prepared_ownership_does_no_bulk_sql_decoding_or_physical_io(
    output_library, monkeypatch
):
    from sportarr import output

    session, folder = output_library
    namespace = output.SportsOutputNamespace(
        SimpleNamespace(
            mapped_path=str(folder / "event.mkv"), event_id=1, arr_instance_id=1
        ),
        session,
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Prepared ownership validation performed slow work")

    monkeypatch.setattr(output.ast, "literal_eval", forbidden)
    monkeypatch.setattr(output, "_physical", forbidden)
    statements = []
    sa.event.listen(
        session.get_bind(),
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: statements.append(
            statement
        ),
    )
    namespace.validate(session)
    assert len(statements) == 1
    assert "subtitle_ownership_revision" in statements[0]


@pytest.mark.parametrize(
    "table_name",
    ["arr_instances", "table_episodes", "table_movies", "table_sports_events"],
)
def test_direct_sql_change_invalidates_prepared_ownership(output_library, table_name):
    from sportarr.output import SportsOutputNamespace

    session, folder = output_library
    context = SimpleNamespace(
        mapped_path=str(folder / "event.mkv"), event_id=1, arr_instance_id=1
    )
    namespace = SportsOutputNamespace(context, session)
    # PostgreSQL statement-level updates also cover empty imports. SQLite uses
    # row triggers, so seed one ordinary native/sports row for this mutation.
    if table_name == "arr_instances":
        session.execute(sa.text("UPDATE arr_instances SET name='changed' WHERE id=2"))
    else:
        from app.database import TableEpisodes, TableMovies, TableSportsEvents

        table = {
            "table_episodes": TableEpisodes,
            "table_movies": TableMovies,
            "table_sports_events": TableSportsEvents,
        }[table_name]
        values = dict(
            id=80, arr_instance_id=2, path="/unrelated/recording.mkv", title="Foreign"
        )
        if table_name == "table_movies":
            values.update(radarrId=80, tmdbId="80")
        elif table_name == "table_sports_events":
            values.update(league_id=2, sportarrEventId=80, file_id=80)
        else:
            values.update(sonarrEpisodeId=80, episode=1, season=1)
        session.execute(sa.insert(table).values(**values))
    with pytest.raises(ValueError, match="changed"):
        namespace.validate(session)


def test_real_save_with_many_unrelated_windows_subtitle_records(manual_library):  # noqa: F811
    from app.database import TableMovies, TableHistorySports

    service, session, folder = manual_library
    for index in range(140):
        session.execute(
            sa.insert(TableMovies).values(
                id=1000 + index,
                radarrId=1000 + index,
                tmdbId=str(index),
                title="Unrelated",
                path=f"C:\\Movies\\movie-{index}.mkv",
                subtitles=repr([["en", f"C:\\Movies\\movie-{index}.en.srt", 10]]),
            )
        )
    candidate = service.manual_search_sports(61, "en", arr_instance_id=1)[0]
    service.manual_download_sports(61, candidate, arr_instance_id=1)
    assert (folder / "1/event.en.srt").read_bytes()
    assert session.execute(sa.select(TableHistorySports)).scalar_one().event_id == 61


def test_namespace_rejects_an_inherited_global_sports_mapping_change(output_library, monkeypatch):
    from app.config import settings
    from app.database import TableSportsEvents
    from sportarr.output import SportsOutputNamespace

    session, folder = output_library
    monkeypatch.setattr(settings.general, 'path_mappings_sports', [['/foreign', '/elsewhere']])
    session.add(TableSportsEvents(id=62, league_id=2, arr_instance_id=2,
                                 sportarrEventId=9, file_id=71,
                                 path='/foreign/event.mkv', title='Foreign', subtitles='[]'))
    session.commit()
    context = SimpleNamespace(event_id=61, arr_instance_id=1,
                              mapped_path=str(folder / 'event.mkv'))
    namespace = SportsOutputNamespace(context, session)
    monkeypatch.setattr(settings.general, 'path_mappings_sports', [['/foreign', str(folder)]])
    with pytest.raises(ValueError, match='ownership changed'):
        namespace.validate(session)
