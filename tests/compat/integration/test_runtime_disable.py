"""Codex P2: when compat_endpoint.enabled is toggled off at runtime, the
already-mounted compat blueprint must refuse traffic. The blueprint is
registered at boot based on the startup value, so without the
before_request guard the endpoint keeps serving with the old token until
restart."""
from flask import Flask


def test_runtime_disable_returns_503(monkeypatch):
    from compat.routes import compat_bp

    # Pretend boot happened with enabled=true (real install would have
    # mounted the blueprint at this point). Now operator flips it off:
    monkeypatch.setattr("app.config.settings.compat_endpoint.enabled", False)
    monkeypatch.setattr("compat.auth.settings.compat_endpoint.token", "a" * 32)

    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")

    response = app.test_client().get(
        "/api/v1/subtitles", headers={"Api-Key": "a" * 32}
    )
    assert response.status_code == 503
    assert response.json == {"error": "compat endpoint disabled"}


def test_runtime_disable_blocks_unauthenticated_too(monkeypatch):
    """The disable guard must run BEFORE the auth check so a disabled
    endpoint does not leak its existence by responding 403 to an
    unauthenticated probe."""
    from compat.routes import compat_bp

    monkeypatch.setattr("app.config.settings.compat_endpoint.enabled", False)
    monkeypatch.setattr("compat.auth.settings.compat_endpoint.token", "a" * 32)

    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")

    # No Api-Key header. The disable guard must short-circuit BEFORE the
    # auth wrapper has a chance to return 403.
    response = app.test_client().get("/api/v1/subtitles")
    assert response.status_code == 503


def test_runtime_enable_lets_traffic_through(monkeypatch):
    """Sanity: when enabled stays true, the disable guard does not interfere
    with the normal request lifecycle."""
    from compat.routes import compat_bp

    monkeypatch.setattr("app.config.settings.compat_endpoint.enabled", True)
    monkeypatch.setattr("compat.auth.settings.compat_endpoint.token", "a" * 32)

    app = Flask(__name__)
    app.register_blueprint(compat_bp, url_prefix="/api/v1")

    response = app.test_client().get(
        "/api/v1/subtitles", headers={"Api-Key": "a" * 32}
    )
    # Whatever the endpoint returns, it MUST NOT be the runtime-disable
    # 503 from this guard. (400 missing-languages, 200 with results, etc.
    # are all fine outcomes for "guard did not block".)
    assert response.status_code != 503
