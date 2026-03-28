import asyncio
from types import SimpleNamespace

import src.web.routes.settings as settings_routes


class _Secret:
    def __init__(self, value: str):
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


def test_get_all_settings_includes_external_api_block(monkeypatch):
    monkeypatch.setattr(
        settings_routes,
        "get_settings",
        lambda: SimpleNamespace(
            proxy_enabled=False,
            proxy_type="http",
            proxy_host="127.0.0.1",
            proxy_port=7890,
            proxy_username=None,
            proxy_password=None,
            proxy_dynamic_enabled=False,
            proxy_dynamic_api_url="",
            proxy_dynamic_api_key_header="X-API-Key",
            proxy_dynamic_result_field="",
            proxy_dynamic_api_key=None,
            registration_max_retries=3,
            registration_timeout=120,
            registration_default_password_length=12,
            registration_sleep_min=5,
            registration_sleep_max=30,
            webui_host="0.0.0.0",
            webui_port=8000,
            debug=False,
            webui_access_password=None,
            tempmail_base_url="https://api.tempmail.lol/v2",
            tempmail_timeout=30,
            tempmail_max_retries=3,
            email_code_timeout=120,
            email_code_poll_interval=3,
            external_api_enabled=True,
            external_api_key=_Secret("demo-key"),
        ),
    )

    payload = asyncio.run(settings_routes.get_all_settings())

    assert payload["external_api"] == {
        "enabled": True,
        "has_api_key": True,
        "api_key_header": "X-API-Key",
    }


def test_update_external_api_settings_preserves_existing_key_when_blank(monkeypatch):
    captured = {}

    def _update_settings(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(settings_routes, "update_settings", _update_settings)

    response = asyncio.run(
        settings_routes.update_external_api_settings(
            settings_routes.ExternalApiSettings(enabled=True, api_key="   ")
        )
    )

    assert captured == {"external_api_enabled": True}
    assert response["success"] is True
    assert response["has_api_key"] is None
