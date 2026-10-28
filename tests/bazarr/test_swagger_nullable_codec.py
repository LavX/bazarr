# coding=utf-8
"""The subtitle model has to declare the codec it can send as null.

``entry_codec()`` answers None for every external subtitle and for an embedded
entry indexed before codecs were stored, and the serialised row carries
``"codec": null``. A schema that calls the field a plain string tells a
generated client that null is invalid.
"""


def test_the_subtitle_codec_is_declared_nullable():
    from api.swaggerui import subtitles_model

    assert subtitles_model['codec'].schema().get('x-nullable') is True


def test_the_subtitle_codec_is_still_a_string():
    from api.swaggerui import subtitles_model

    assert subtitles_model['codec'].schema()['type'] == 'string'
