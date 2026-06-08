"""Provider Hub registry must not drop a whole provider over one bad language code.

A manifest can declare a language babelfish doesn't know. In
registry._languages_from_manifest the fallback `Language(code)` sat unguarded inside the
`except`, so an unresolvable code raised and propagated, making
register_active_provider_classes skip the entire provider. The unsupported code should be
skipped while the valid ones still resolve. (Montenegrin "cnr" specifically is now
registered at startup - see test_extra_languages - so a clearly-invalid code is used here.)
"""
from types import SimpleNamespace

import provider_hub.registry as registry
from subzero.language import Language


def test_unsupported_language_code_is_skipped_not_fatal():
    manifest = SimpleNamespace(
        languages=["eng", "zzz", "srp", "hrv"],
        provider_id="someprovider",
    )

    # Sanity: the offending code really is unsupported by babelfish.
    try:
        Language("zzz")
        raise AssertionError("expected Language('zzz') to be unsupported")
    except ValueError:
        pass

    languages = registry._languages_from_manifest(manifest)  # must not raise

    alpha3 = {lang.alpha3 for lang in languages}
    assert "zzz" not in alpha3, "unsupported code must be dropped"
    assert {"eng", "srp", "hrv"}.issubset(alpha3), "valid codes must still resolve"
