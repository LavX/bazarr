from subliminal_patch.refiners.filebot import _parse_filebot_output


def test_filebot_refiner_parses_original_filename_from_xattr_output():
    assert (
        _parse_filebot_output('net.filebot.filename="Original.Show.S01E01.mkv"\n')
        == "Original.Show.S01E01.mkv"
    )


def test_filebot_refiner_parses_original_filename_from_attr_output():
    assert (
        _parse_filebot_output("Attribute net.filebot.filename:\nOriginal.Movie.mkv\n")
        == "Original.Movie.mkv"
    )
