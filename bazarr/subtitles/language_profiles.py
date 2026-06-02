# coding=utf-8


def profile_item_language_code(item):
    language = item.get('language')
    if not language:
        return None
    if item.get('forced') == 'True':
        return f'{language}:forced'
    if item.get('hi') == 'True':
        return f'{language}:hi'
    return language


def build_translate_from_map(profile):
    translate_from_map = {}
    if not profile:
        return translate_from_map

    for item in profile.get('items', []):
        source = item.get('translate_from')
        target = profile_item_language_code(item)
        if source and target:
            translate_from_map[target] = {
                'from': source,
                'hi': item.get('hi') == 'True',
                'forced': item.get('forced') == 'True',
            }

    return translate_from_map


class CombineRuleError(ValueError):
    pass


_VALID_FORMATS = ("srt", "ass")


def validate_combine_rule(rule, items):
    """Raise CombineRuleError if the rule is invalid against the profile items.
    Used by the profile-save API endpoint."""
    if not isinstance(rule, dict):
        raise CombineRuleError("combine rule must be an object")
    languages = rule.get("languages") or []
    fmt = rule.get("format")
    if not isinstance(languages, list) or not (2 <= len(languages) <= 3):
        raise CombineRuleError("combine.languages must have 2 to 3 entries")
    if len(set(languages)) != len(languages):
        raise CombineRuleError("combine.languages must be distinct")
    if fmt not in _VALID_FORMATS:
        raise CombineRuleError(f"combine.format must be one of {_VALID_FORMATS}")
    item_codes = {it.get("language") for it in (items or [])}
    for code in languages:
        if code not in item_codes:
            raise CombineRuleError(
                f"combine.languages entry {code!r} not in profile items"
            )


def get_combine_rule(profile):
    """Return the validated combine rule for the profile, or None.
    Silently returns None on invalid rules so an orphan rule does not
    crash the pipeline."""
    if not profile:
        return None
    rule = profile.get("combine")
    if not rule:
        return None
    try:
        validate_combine_rule(rule, profile.get("items") or [])
    except CombineRuleError:
        return None
    return {"languages": list(rule["languages"]), "format": rule["format"]}
