# coding=utf-8
"""The swagger UI must serve its own assets from wherever flask_restx lives.

The handler used to point at the vendored ``libs/flask_restx/static`` tree,
which the library unvendoring removed, so every asset request returned 500 and
the API documentation page rendered blank.
"""
import os


def test_the_swagger_static_dir_exists_and_holds_the_ui():
    from app.ui import swaggerui_static_dir

    basepath = swaggerui_static_dir()
    assert os.path.isdir(basepath), f'{basepath} does not exist'
    for asset in ('swagger-ui.css', 'swagger-ui-bundle.js',
                  'swagger-ui-standalone-preset.js'):
        assert os.path.isfile(os.path.join(basepath, asset)), (
            f'{asset} missing from {basepath}; the swagger page loads these '
            'and renders blank without them')
