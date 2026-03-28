from fastapi.testclient import TestClient

from src.web.app import create_app


def test_login_page_renders_successfully():
    client = TestClient(create_app())

    response = client.get("/login")

    assert response.status_code == 200
    assert "login" in response.text.lower()


def test_settings_template_contains_external_api_controls():
    template = open("templates/settings.html", "r", encoding="utf-8").read()

    assert 'id="external-api-settings-form"' in template
    assert 'id="external-api-enabled"' in template
    assert 'id="external-api-key"' in template
