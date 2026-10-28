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


def test_the_queued_answers_of_the_ocr_routes_document_their_session():
    from api.subtitles.batch import BatchOperation
    from api.subtitles.subtitles import Subtitles

    for model in (Subtitles.patch_job_model, BatchOperation.post_response_model):
        assert 'job_id' in model and 'session' in model
    assert 'path' in Subtitles.patch_job_model
