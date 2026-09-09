# coding=utf-8
"""Audio track languages for sports events.

Sportarr reports no audio metadata at all (its API returns languages: [] on
every file), so ffprobe is the only source there has ever been. Sonarr and
Radarr both branch on parse_embedded_audio_track and fall back to the arr
payload; sports implemented only the fallback, so the column could never be
populated and the audio_exclude / audio_only_include profile rules were inert
for every sports event.
"""
import pytest


@pytest.fixture
def languages_loaded():
    """Seed the module-level language table the name lookups read.

    create_languages_dict() normally runs at app startup off the database;
    language_from_alpha3 raises NameError without it.
    """
    from languages import get_languages

    get_languages.languages_dict = [
        {"code3": "hun", "code2": "hu", "name": "Hungarian", "code3b": "hun"},
        {"code3": "eng", "code2": "en", "name": "English", "code3b": "eng"},
    ]
    return get_languages.languages_dict


def test_audio_languages_come_out_of_an_already_parsed_blob(languages_loaded):
    """The indexer parses the file for embedded subtitles anyway.

    Deriving the audio languages from that same blob avoids probing a multi-GB
    recording twice.
    """
    from utilities.video_analyzer import audio_languages_from_metadata

    blob = {
        "ffprobe": {
            "audio": [
                {"language": "hun"},
                {"language": "eng"},
            ]
        }
    }
    assert audio_languages_from_metadata(blob, "/media/race.mkv") == [
        "Hungarian",
        "English",
    ]


def test_audio_languages_are_names_not_codes(languages_loaded):
    """get_audio_profile_languages resolves entries by NAME.

    Handing it an ISO code makes it hand back code2 None, so this is the shape
    the rest of the pipeline requires.
    """
    from utilities.video_analyzer import audio_languages_from_metadata

    resolved = audio_languages_from_metadata(
        {"ffprobe": {"audio": [{"language": "hun"}]}}, "/media/race.mkv"
    )
    assert resolved == ["Hungarian"]
    assert "hu" not in resolved


def test_empty_and_missing_metadata_are_survivable():
    from utilities.video_analyzer import audio_languages_from_metadata

    assert audio_languages_from_metadata(None, "/media/race.mkv") == []
    assert audio_languages_from_metadata({}, "/media/race.mkv") == []
    assert audio_languages_from_metadata({"ffprobe": {}}, "/media/race.mkv") == []


def test_sync_does_not_clobber_what_the_indexer_derived():
    """The parser can only ever offer '[]', because Sportarr sends nothing.

    Letting the sync write that back erased the ffprobe result, and because
    audio_language took part in the new-file comparison it also made the next
    sync judge the file new and wipe the whole subtitle index with it.
    """
    import inspect

    from sportarr.sync import events

    source = inspect.getsource(events.sync_events)
    assert "indexer_owns_audio" in source
    assert "if key == 'audio_language' and indexer_owns_audio and not new_file" in source
    # audio_language is only compared when the arr payload actually owns it.
    assert "compared += ('audio_language',)" in source


def test_indexer_writes_audio_language_before_computing_missing():
    """The audio profile rules are evaluated off this column.

    Writing it after _missing would leave audio_exclude / audio_only_include
    reading a stale value for a whole indexing cycle.
    """
    import inspect

    from subtitles.indexer import sports

    source = inspect.getsource(sports.store_subtitles_sports)
    audio_at = source.index("row.audio_language = str(audio_languages_from_metadata")
    missing_at = source.index("row.missing_subtitles = str(_missing(")
    assert audio_at < missing_at


@pytest.mark.parametrize("flag", [True, False])
def test_metadata_is_parsed_when_either_embedded_feature_is_on(flag):
    import inspect

    from subtitles.indexer import sports

    source = inspect.getsource(sports.store_subtitles_sports)
    assert (
        "if settings.general.use_embedded_subs or settings.general.parse_embedded_audio_track:"
        in source
    )
