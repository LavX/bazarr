from subzero.language import Language


def test_compat_pool_uses_provider_language_hook(monkeypatch):
    from compat import service

    captured = {}

    class CapturingPool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(service, "_compat_pool", None)
    monkeypatch.setattr(service, "get_providers_sorted", lambda: ["opensubtitlescom"])
    monkeypatch.setattr(service, "get_providers_auth", lambda: {})
    monkeypatch.setattr(
        service,
        "get_provider_language_hook",
        lambda: lambda provider: {Language("eng")},
        raising=False,
    )
    monkeypatch.setattr("subliminal_patch.core.SZAsyncProviderPool", CapturingPool)

    service._get_compat_pool()

    assert callable(captured["language_hook"])
    assert captured["language_hook"]("opensubtitlescom") == {Language("eng")}
