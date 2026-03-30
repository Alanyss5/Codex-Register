import pytest

from src.services.cloudmail import CloudMailService
from src.services.luckmail_mail import LuckMailService


def test_cloudmail_defaults_enable_prefix_and_uses_cloudmail_type():
    service = CloudMailService(
        {
            "base_url": "https://cloudmail.example.com",
            "admin_password": "secret",
            "domain": "cloudmail.example.com",
        },
        name="CloudMail",
    )

    assert service.service_type.value == "cloudmail"
    assert service.config["enable_prefix"] is True


def test_luckmail_requires_sdk(monkeypatch):
    monkeypatch.setattr("src.services.luckmail_mail._load_luckmail_client_class", lambda: None)

    with pytest.raises(ValueError, match="LuckMail SDK"):
        LuckMailService(
            {
                "base_url": "https://mails.luckyous.com/",
                "api_key": "luck-key",
                "project_code": "openai",
            }
        )
