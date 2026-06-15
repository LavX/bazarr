"""
Test for Bazarr UI functionality including authentication decorators.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import Mock, patch  # noqa: F401
from flask import Flask

from app.ui import check_login


def test_check_login_decorator_preserves_function_signature():
    """
    Test that check_login decorator preserves the original function's signature and metadata.
    """
    def original_function(arg1, arg2, kwarg1=None):
        """Test function docstring."""
        return f"{arg1}:{arg2}:{kwarg1}"

    decorated_function = check_login(original_function)

    # Check that function metadata is preserved
    assert decorated_function.__name__ == original_function.__name__
    assert decorated_function.__doc__ == original_function.__doc__


def test_check_login_decorator_can_be_applied():
    """
    Test that check_login decorator can be successfully applied to functions.
    """
    def test_function():
        return "test_result"

    # Should not raise any exceptions when applying decorator
    decorated_function = check_login(test_function)
    assert callable(decorated_function)


def test_check_login_decorator_is_wrapper():
    """
    Test that check_login returns a wrapper function that can be called.
    """
    def original_function(value):
        return value * 2

    decorated_function = check_login(original_function)

    # Verify it's a different function (wrapped)
    assert decorated_function != original_function
    assert callable(decorated_function)
    assert decorated_function.__name__ == original_function.__name__


def test_instance_image_url_rejects_wrong_instance_kind(monkeypatch):
    """
    A movie image request must not be allowed to route through a Sonarr instance
    just because the caller supplied that instance id.
    """
    from app.ui import _instance_image_url
    import arr_instances.resolution as resolution

    class SonarrClient:
        kind = "sonarr"
        api_key = "key"
        verify_ssl = False
        _base_url_raw = "/"

        def base_url(self):
            return "http://sonarr.example:8989"

    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: SonarrClient(),
    )

    app = Flask(__name__)
    with app.test_request_context("/images/movies/MediaCover/1/poster.jpg?arr_instance_id=9"):
        assert _instance_image_url("radarr", "MediaCover/1/poster.jpg") == (None, None)


def test_instance_image_url_routes_matching_instance_kind(monkeypatch):
    from app.ui import _instance_image_url
    import arr_instances.resolution as resolution

    class RadarrClient:
        kind = "radarr"
        api_key = "key"
        verify_ssl = True
        _base_url_raw = "/radarr"

        def base_url(self):
            return "https://radarr.example:7878/radarr"

    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: RadarrClient(),
    )

    app = Flask(__name__)
    with app.test_request_context("/images/movies/radarr/MediaCover/1/poster.jpg?arr_instance_id=9"):
        assert _instance_image_url("radarr", "radarr/MediaCover/1/poster.jpg") == (
            "https://radarr.example:7878/radarr/api/v3/MediaCover/1/poster.jpg?apikey=key",
            True,
        )


def test_movie_image_route_returns_404_for_wrong_instance_kind(monkeypatch):
    from app import ui
    import arr_instances.resolution as resolution

    class SonarrClient:
        kind = "sonarr"
        api_key = "key"
        verify_ssl = False
        _base_url_raw = "/"

        def base_url(self):
            return "http://sonarr.example:8989"

    monkeypatch.setattr(ui, "settings", SimpleNamespace(auth=SimpleNamespace(type=None)))
    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: SonarrClient(),
    )

    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)

    response = app.test_client().get(
        "/images/movies/MediaCover/1/poster.jpg?arr_instance_id=9"
    )

    assert response.status_code == 404


def test_movie_image_route_fetches_matching_instance(monkeypatch):
    from app import ui
    import arr_instances.resolution as resolution

    captured = {}

    class RadarrClient:
        kind = "radarr"
        api_key = "key"
        verify_ssl = True
        _base_url_raw = "/radarr"

        def base_url(self):
            return "https://radarr.example:7878/radarr"

    class UpstreamResponse:
        headers = {"content-type": "image/jpeg"}

        def iter_content(self, chunk_size):
            captured["chunk_size"] = chunk_size
            yield b"image-bytes"

    def fake_get(url, stream, timeout, verify, headers):
        captured.update({
            "url": url,
            "stream": stream,
            "timeout": timeout,
            "verify": verify,
            "headers": headers,
        })
        return UpstreamResponse()

    monkeypatch.setattr(ui, "settings", SimpleNamespace(auth=SimpleNamespace(type=None)))
    monkeypatch.setattr(ui.requests, "get", fake_get)
    monkeypatch.setattr(
        resolution,
        "client_for_instance",
        lambda session, instance_id: RadarrClient(),
    )

    app = Flask(__name__)
    app.register_blueprint(ui.ui_bp)

    response = app.test_client().get(
        "/images/movies/radarr/MediaCover/1/poster.jpg?lastWrite=1&arr_instance_id=9"
    )

    assert response.status_code == 200
    assert response.data == b"image-bytes"
    assert response.content_type == "image/jpeg"
    assert captured["url"] == (
        "https://radarr.example:7878/radarr/api/v3/MediaCover/1/poster.jpg?apikey=key"
    )
    assert captured["stream"] is True
    assert captured["timeout"] == 15
    assert captured["verify"] is True
    assert captured["chunk_size"] == 2048


def test_check_login_no_authentication():
    """
    Test check_login decorator when no authentication is configured.
    """
    def test_function():
        return "success_response"

    # Mock settings for no authentication
    with patch('app.ui.settings') as mock_settings:
        mock_settings.auth.type = None

        decorated_function = check_login(test_function)
        result = decorated_function()

        assert result == "success_response"


def test_check_login_basic_auth_success():
    """
    Test check_login decorator with valid basic authentication.
    """
    def test_function():
        return "authenticated_response"

    # Mock Flask request context with basic auth
    app = Flask(__name__)
    with app.test_request_context(headers={'Authorization': 'Basic dGVzdDp0ZXN0'}):
        with patch('app.ui.settings') as mock_settings, \
             patch('app.ui.check_credentials', return_value=True):

            mock_settings.auth.type = 'basic'

            decorated_function = check_login(test_function)
            result = decorated_function()

            assert result == "authenticated_response"


def test_check_login_basic_auth_failure():
    """
    Test check_login decorator with invalid basic authentication.
    """
    def test_function():
        return "should_not_reach"

    # Mock Flask request context with invalid basic auth
    app = Flask(__name__)
    with app.test_request_context(headers={'Authorization': 'Basic aW52YWxpZA=='}):
        with patch('app.ui.settings') as mock_settings, \
             patch('app.ui.check_credentials', return_value=False):

            mock_settings.auth.type = 'basic'

            decorated_function = check_login(test_function)
            result = decorated_function()

            # Should return 401 tuple
            assert isinstance(result, tuple)
            assert result[1] == 401
            assert result[0] == 'Unauthorized'


def test_check_login_basic_auth_missing():
    """
    Test check_login decorator when basic auth is required but not provided.
    """
    def test_function():
        return "should_not_reach"

    # Mock Flask request context without authorization header
    app = Flask(__name__)
    with app.test_request_context():
        with patch('app.ui.settings') as mock_settings:
            mock_settings.auth.type = 'basic'

            decorated_function = check_login(test_function)
            result = decorated_function()

            # Should return 401 tuple
            assert isinstance(result, tuple)
            assert result[1] == 401
            assert result[0] == 'Unauthorized'


def test_check_login_form_auth_success():
    """
    Test check_login decorator with valid form authentication session.
    """
    def test_function():
        return "form_authenticated_response"

    # Mock Flask request context with valid session
    app = Flask(__name__)
    app.secret_key = 'test_secret'

    with app.test_request_context():
        with patch('app.ui.settings') as mock_settings, \
             patch('app.ui.session', {'logged_in': True}):

            mock_settings.auth.type = 'form'

            decorated_function = check_login(test_function)
            result = decorated_function()

            assert result == "form_authenticated_response"


def test_check_login_form_auth_failure():
    """
    Test check_login decorator when form auth session is invalid.
    """
    def test_function():
        return "should_not_reach"

    app = Flask(__name__)
    with app.test_request_context():
        with patch('app.ui.settings') as mock_settings, \
             patch('app.ui.session', {}) as mock_session, \
             patch('app.ui.abort') as mock_abort:  # noqa: F841

            mock_settings.auth.type = 'form'
            mock_abort.return_value = ('Unauthorized', 401)

            decorated_function = check_login(test_function)
            result = decorated_function()  # noqa: F841

            # Should call abort
            mock_abort.assert_called_once_with(401)


def test_check_login_preserves_function_arguments():
    """
    Test that check_login decorator properly passes through function arguments.
    """
    def test_function(arg1, arg2, kwarg1=None):
        return f"args:{arg1},{arg2} kwargs:{kwarg1}"

    with patch('app.ui.settings') as mock_settings:
        mock_settings.auth.type = None

        decorated_function = check_login(test_function)
        result = decorated_function("test1", "test2", kwarg1="test_kw")

        assert result == "args:test1,test2 kwargs:test_kw"


def test_check_login_preserves_function_exceptions():
    """
    Test that check_login decorator allows function exceptions to propagate.
    """
    def test_function():
        raise ValueError("Test exception")

    with patch('app.ui.settings') as mock_settings:
        mock_settings.auth.type = None

        decorated_function = check_login(test_function)

        with pytest.raises(ValueError, match="Test exception"):
            decorated_function()


def test_check_login_with_different_return_types():
    """
    Test that check_login decorator properly handles different return types.
    """
    test_cases = [
        ("string_result", str),
        (42, int),
        ([1, 2, 3], list),
        ({"key": "value"}, dict),
        (None, type(None)),
    ]

    with patch('app.ui.settings') as mock_settings:
        mock_settings.auth.type = None

        for expected_value, expected_type in test_cases:
            def test_function():
                return expected_value

            decorated_function = check_login(test_function)
            result = decorated_function()

            assert result == expected_value
            assert type(result) == expected_type  # noqa: E721
