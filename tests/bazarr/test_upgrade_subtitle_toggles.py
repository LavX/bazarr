from types import SimpleNamespace

import pytest


class _Settings:
    def __init__(self, data=None):
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __setitem__(self, key, value):
        self._data[key] = value


def test_existing_config_without_keys_keeps_manual_on_and_translated_off():
    from app.config import migrate_upgrade_subtitle_toggles

    settings = _Settings()
    assert migrate_upgrade_subtitle_toggles(settings, existing_config=True) is True
    assert settings.get('general.upgrade_manual') is True
    assert settings.get('general.upgrade_translated') is False


def test_existing_config_keeps_stored_manual_value_and_turns_translated_off():
    from app.config import migrate_upgrade_subtitle_toggles

    settings = _Settings({'general.upgrade_manual': False})
    assert migrate_upgrade_subtitle_toggles(settings, existing_config=True) is True
    assert settings.get('general.upgrade_manual') is False
    assert settings.get('general.upgrade_translated') is False


def test_existing_config_does_not_copy_combined_toggle_onto_translated():
    from app.config import migrate_upgrade_subtitle_toggles

    settings = _Settings({'general.upgrade_manual': True})
    assert migrate_upgrade_subtitle_toggles(settings, existing_config=True) is True
    assert settings.get('general.upgrade_manual') is True
    assert settings.get('general.upgrade_translated') is False


def test_new_install_does_not_grandfather_manual_on():
    from app.config import migrate_upgrade_subtitle_toggles

    settings = _Settings()
    assert migrate_upgrade_subtitle_toggles(settings, existing_config=False) is True
    assert settings.get('general.upgrade_manual') is None
    assert settings.get('general.upgrade_translated') is False


def test_already_split_config_is_left_alone():
    from app.config import migrate_upgrade_subtitle_toggles

    settings = _Settings({
        'general.upgrade_manual': True,
        'general.upgrade_translated': True,
    })
    assert migrate_upgrade_subtitle_toggles(settings, existing_config=True) is False
    assert settings.get('general.upgrade_translated') is True


@pytest.mark.parametrize(
    'manual, translated, expected',
    [
        (False, False, [1, 3]),
        (True, False, [1, 2, 3, 4]),
        (False, True, [1, 3, 6]),
        (True, True, [1, 2, 3, 4, 6]),
    ],
)
def test_upgrade_query_actions_split_manual_and_translated(monkeypatch, manual, translated, expected):
    from subtitles import upgrade

    monkeypatch.setattr(
        upgrade,
        'settings',
        SimpleNamespace(
            general=SimpleNamespace(
                days_to_upgrade_subs=7,
                upgrade_manual=manual,
                upgrade_translated=translated,
            )
        ),
    )
    _, actions = upgrade.get_queries_condition_parameters()
    assert actions == expected
