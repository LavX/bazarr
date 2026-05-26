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
