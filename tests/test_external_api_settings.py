from src.config.settings import SETTING_DEFINITIONS, get_settings, update_settings


def test_external_api_settings_definitions_exist():
    assert "external_api_enabled" in SETTING_DEFINITIONS
    assert SETTING_DEFINITIONS["external_api_enabled"].db_key == "external_api.enabled"

    assert "external_api_key" in SETTING_DEFINITIONS
    assert SETTING_DEFINITIONS["external_api_key"].db_key == "external_api.key"
    assert SETTING_DEFINITIONS["external_api_key"].is_secret is True


def test_update_settings_supports_external_api_fields():
    current = get_settings()
    old_enabled = getattr(current, "external_api_enabled", False)
    old_key = getattr(current, "external_api_key", None)
    old_key_value = old_key.get_secret_value() if old_key else ""

    try:
        settings = update_settings(external_api_enabled=True, external_api_key="demo-key")

        assert settings.external_api_enabled is True
        assert settings.external_api_key.get_secret_value() == "demo-key"

        latest = get_settings()
        assert latest.external_api_enabled is True
    finally:
        update_settings(external_api_enabled=old_enabled, external_api_key=old_key_value)
