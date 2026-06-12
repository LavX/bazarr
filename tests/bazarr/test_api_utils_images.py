# coding=utf-8


def test_postprocess_image_urls_replace_existing_arr_instance_query(monkeypatch):
    from api import utils

    monkeypatch.setattr(utils, "base_url", "")

    item = utils.postprocess({
        "radarrId": 5,
        "arr_instance_id": 7,
        "title": "Arrival",
        "poster": "/MediaCover/5/poster-500.jpg?arr_instance_id=1&lastWrite=222",
        "fanart": "/MediaCover/5/fanart.jpg?lastWrite=333&arr_instance_id=1",
    })

    assert item["poster"] == (
        "/images/movies/MediaCover/5/poster-500.jpg?lastWrite=222&arr_instance_id=7"
    )
    assert item["fanart"] == (
        "/images/movies/MediaCover/5/fanart.jpg?lastWrite=333&arr_instance_id=7"
    )
