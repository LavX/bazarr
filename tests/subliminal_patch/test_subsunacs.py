import io

from py7zr import SevenZipFile
from subliminal.video import Episode
from subliminal_patch.providers.subsunacs import SubsUnacsProvider, SUBTITLE_ARCHIVE_FILE_COUNT_LIMIT
from subzero.language import Language


def test_subsunacs_extracts_subtitles_from_7z_archive():
    archive = io.BytesIO()
    with SevenZipFile(archive, "w") as archive_writer:
        archive_writer.writestr("1\n00:00:01,000 --> 00:00:02,000\nHello\n", "Show.S01E01.en.srt")
        archive_writer.writestr("subsunacs.net", "readme.txt")

    archive.seek(0)
    video = Episode("Show.S01E01.mkv", "Show", 1, 1)

    with SevenZipFile(archive, "r") as archive_reader:
        subtitles = SubsUnacsProvider().process_archive_subtitle_files(
            archive_reader,
            Language.fromalpha2("en"),
            video,
            "https://subsunacs.net/subtitle",
            None,
            1,
        )

    assert len(subtitles) == 1
    assert subtitles[0].filename == "Show.S01E01.en.srt"
    assert b"Hello" in subtitles[0].content


def test_subsunacs_ignores_archives_with_too_many_files():
    archive = io.BytesIO()
    with SevenZipFile(archive, "w") as archive_writer:
        for index in range(SUBTITLE_ARCHIVE_FILE_COUNT_LIMIT + 1):
            archive_writer.writestr("readme", f"readme-{index}.txt")

    archive.seek(0)
    video = Episode("Show.S01E01.mkv", "Show", 1, 1)

    with SevenZipFile(archive, "r") as archive_reader:
        subtitles = SubsUnacsProvider().process_archive_subtitle_files(
            archive_reader,
            Language.fromalpha2("en"),
            video,
            "https://subsunacs.net/subtitle",
            None,
            1,
        )

    assert subtitles == []
