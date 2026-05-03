import logging

import pytest

from utilities import video_analyzer

logging.getLogger("knowit").setLevel(logging.WARNING)

M_INFO = {
    "creatingLibrary": {
        "name": "MediaInfoLib",
        "version": "23.03",
        "url": "https://mediaarea.net/MediaInfo",
    },
    "media": {
        "@ref": "/mnt/media/Hocus.Pocus.1993.1080p.DSNP.WEB-DL.DDP5.1.H.264.DUAL-PD.mkv",
        "track": [
            {
                "@type": "General",
                "UniqueID": "177986280948425821736023466260510750529",
                "VideoCount": "1",
                "AudioCount": "2",
                "TextCount": "31",
                "FileExtension": "mkv",
                "Format": "Matroska",
                "Format_Version": "4",
                "FileSize": "6468058376",
                "Duration": "5766.219",
                "OverallBitRate_Mode": "VBR",
                "OverallBitRate": "8973726",
                "FrameRate": "23.976",
                "FrameCount": "138251",
                "StreamSize": "2607619",
                "IsStreamable": "Yes",
                "Encoded_Date": "2023-04-22 16:46:57 UTC",
                "File_Modified_Date": "2023-05-17 01:49:55 UTC",
                "File_Modified_Date_Local": "2023-05-16 21:49:55",
                "Encoded_Application": "mkvmerge v75.0.0 ('Goliath') 64-bit",
                "Encoded_Library": "libebml v1.4.4 + libmatroska v1.7.1",
            },
            {
                "@type": "Video",
                "StreamOrder": "0",
                "ID": "1",
                "UniqueID": "9393509843335289949",
                "Format": "AVC",
                "Format_Profile": "High",
                "Format_Level": "4",
                "Format_Settings_CABAC": "Yes",
                "Format_Settings_RefFrames": "4",
                "CodecID": "V_MPEG4/ISO/AVC",
                "Duration": "5766.219000000",
                "BitRate_Mode": "VBR",
                "BitRate": "8458733",
                "BitRate_Maximum": "12749952",
                "Width": "1920",
                "Height": "1080",
            },
            {
                "@type": "Audio",
                "@typeorder": "1",
                "StreamOrder": "1",
                "ID": "2",
                "UniqueID": "12329215851643269509",
                "Format": "E-AC-3",
                "Format_Commercial_IfAny": "Dolby Digital Plus",
                "Format_Settings_Endianness": "Big",
                "CodecID": "A_EAC3",
                "Duration": "5766.112000000",
                "BitRate_Mode": "CBR",
                "BitRate": "256000",
                "Language": "pt-BR",
                "Default": "No",
                "Forced": "No",
            },
            {
                "@type": "Audio",
                "@typeorder": "2",
                "StreamOrder": "2",
                "ID": "3",
                "UniqueID": "1232921585164326950923",
                "Format": "E-AC-3",
                "Format_Commercial_IfAny": "Dolby Digital Plus",
                "Format_Settings_Endianness": "Big",
                "CodecID": "A_EAC3",
                "Duration": "5766.112000000",
                "BitRate_Mode": "CBR",
                "BitRate": "256000",
                "Language": "pt",
                "Default": "No",
                "Forced": "No",
            },
            {
                "@type": "Text",
                "@typeorder": "7",
                "StreamOrder": "9",
                "ID": "10",
                "UniqueID": "2233390560797234737",
                "Format": "UTF-8",
                "CodecID": "S_TEXT/UTF8",
                "Duration": "5480.360000000",
                "BitRate": "45",
                "FrameRate": "0.206",
                "FrameCount": "1129",
                "ElementCount": "1129",
                "StreamSize": "31194",
                "Language": "es-419",
                "Default": "No",
                "Forced": "No",
            },
            {
                "@type": "Text",
                "@typeorder": "9",
                "StreamOrder": "11",
                "ID": "12",
                "UniqueID": "1345374948683222936",
                "Format": "UTF-8",
                "CodecID": "S_TEXT/UTF8",
                "Duration": "5561.600000000",
                "BitRate": "46",
                "FrameRate": "0.164",
                "FrameCount": "914",
                "ElementCount": "914",
                "StreamSize": "32145",
                "Language": "es-ES",
                "Default": "No",
                "Forced": "No",
            },
            {
                "@type": "Text",
                "@typeorder": "11",
                "StreamOrder": "13",
                "ID": "14",
                "UniqueID": "17039172451186467602",
                "Format": "UTF-8",
                "CodecID": "S_TEXT/UTF8",
                "Duration": "4966.120000000",
                "BitRate": "1",
                "FrameRate": "0.007",
                "FrameCount": "35",
                "ElementCount": "35",
                "StreamSize": "1011",
                "Language": "fr-CA",
                "Default": "No",
                "Forced": "No",
            },
            {
                "@type": "Text",
                "@typeorder": "24",
                "StreamOrder": "26",
                "ID": "27",
                "UniqueID": "16221047442617815320",
                "Format": "UTF-8",
                "CodecID": "S_TEXT/UTF8",
                "Duration": "4961.520000000",
                "BitRate": "0",
                "FrameRate": "0.002",
                "FrameCount": "11",
                "ElementCount": "11",
                "StreamSize": "379",
                "Language": "pt-BR",
                "Default": "No",
                "Forced": "No",
            },
            {
                "@type": "Text",
                "@typeorder": "30",
                "StreamOrder": "32",
                "ID": "33",
                "UniqueID": "4259582444071016270",
                "Format": "UTF-8",
                "CodecID": "S_TEXT/UTF8",
                "Duration": "5507.508000000",
                "BitRate": "50",
                "FrameRate": "0.253",
                "FrameCount": "1392",
                "ElementCount": "1392",
                "StreamSize": "34539",
                "Language": "zh-Hans",
                "Default": "No",
                "Forced": "No",
            },
            {
                "@type": "Text",
                "@typeorder": "31",
                "StreamOrder": "33",
                "ID": "34",
                "UniqueID": "4890027048965677919",
                "Format": "UTF-8",
                "CodecID": "S_TEXT/UTF8",
                "Duration": "5730.725000000",
                "BitRate": "43",
                "FrameRate": "0.207",
                "FrameCount": "1186",
                "ElementCount": "1186",
                "StreamSize": "31154",
                "Language": "zh-Hant",
                "Default": "No",
                "Forced": "No",
            },
        ],
    },
}


from subzero.language import Language  # noqa: E402


@pytest.fixture
def video_file():
    return "tests/subliminal_patch/data/file_1.mkv"


@pytest.fixture
def mediainfo_data():
    """Pre-parsed mediainfo data in the format that embedded_subs_reader expects.

    Mirrors the M_INFO tracks: es-419 (spa MX), es-ES, fr-CA, pt-BR, zh-Hans, zh-Hant.
    Uses real Language objects so CustomLanguage detection works correctly.
    """
    return {
        "subtitle": [
            {"language": Language("spa", "MX"), "format": "SubRip", "forced": False, "hearing_impaired": False},
            {"language": Language("spa", "ES"), "format": "SubRip", "forced": False, "hearing_impaired": False},
            {"language": Language("fra", "CA"), "format": "SubRip", "forced": False, "hearing_impaired": False},
            {"language": Language("por", "BR"), "format": "SubRip", "forced": False, "hearing_impaired": False},
            {"language": Language.fromietf("zh-Hans"), "format": "SubRip", "forced": False, "hearing_impaired": False},
            {"language": Language.fromietf("zh-Hant"), "format": "SubRip", "forced": False, "hearing_impaired": False},
        ],
        "audio": [
            {"language": Language("por", "BR"), "format": "E-AC-3"},
            {"language": Language("por"), "format": "E-AC-3"},
        ],
    }


def test_embedded_subs_reader(mediainfo_data, video_file):
    from unittest.mock import patch
    with patch(
        "utilities.video_analyzer.parse_video_metadata",
        return_value={"mediainfo": mediainfo_data},
    ), patch(
        "utilities.video_analyzer.alpha3_from_alpha2", return_value=None
    ):
        result = video_analyzer.embedded_subs_reader(video_file, 1e6)
        assert ["spl", False, False, "SubRip"] in result
        assert ["pob", False, False, "SubRip"] in result
        assert ["zht", False, False, "SubRip"] in result


def test_embedded_audio_reader(mediainfo_data, video_file):
    from unittest.mock import patch
    with patch(
        "utilities.video_analyzer.parse_video_metadata",
        return_value={"mediainfo": mediainfo_data},
    ), patch(
        "utilities.video_analyzer.language_from_alpha3", side_effect=lambda alpha3: alpha3
    ):
        result = video_analyzer.embedded_audio_reader(video_file, 1e6)
        assert {"pob", "por"} == set(result)
