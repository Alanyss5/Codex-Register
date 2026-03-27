from fastapi.testclient import TestClient

from src.web.app import create_app


def test_login_page_renders_successfully():
    client = TestClient(create_app())

    response = client.get("/login")

    assert response.status_code == 200
    assert "login" in response.text.lower()
