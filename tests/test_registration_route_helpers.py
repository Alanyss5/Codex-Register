from src.config.constants import EmailServiceType
from src.web.routes.registration import _normalize_email_service_config, _resolve_proxy_for_service


def test_temp_mail_without_explicit_proxy_stays_direct(monkeypatch):
    def unexpected_proxy_lookup(_db):
        raise AssertionError("auto proxy lookup should not run for temp_mail")

    monkeypatch.setattr(
        "src.web.routes.registration.get_proxy_for_registration",
        unexpected_proxy_lookup,
    )

    proxy_url, proxy_id, proxy_source = _resolve_proxy_for_service(
        db=None,
        service_type=EmailServiceType.TEMP_MAIL,
        explicit_proxy=None,
    )

    assert proxy_url is None
    assert proxy_id is None
    assert proxy_source == "direct"


def test_cloudmail_without_explicit_proxy_stays_direct(monkeypatch):
    def unexpected_proxy_lookup(_db):
        raise AssertionError("auto proxy lookup should not run for cloudmail")

    monkeypatch.setattr(
        "src.web.routes.registration.get_proxy_for_registration",
        unexpected_proxy_lookup,
    )

    proxy_url, proxy_id, proxy_source = _resolve_proxy_for_service(
        db=None,
        service_type=EmailServiceType.CLOUDMAIL,
        explicit_proxy=None,
    )

    assert proxy_url is None
    assert proxy_id is None
    assert proxy_source == "direct"


def test_non_temp_mail_without_explicit_proxy_uses_auto_proxy(monkeypatch):
    monkeypatch.setattr(
        "src.web.routes.registration.get_proxy_for_registration",
        lambda _db: ("http://auto-proxy:8080", 7),
    )

    proxy_url, proxy_id, proxy_source = _resolve_proxy_for_service(
        db=None,
        service_type=EmailServiceType.TEMPMAIL,
        explicit_proxy=None,
    )

    assert proxy_url == "http://auto-proxy:8080"
    assert proxy_id == 7
    assert proxy_source == "auto"


def test_normalize_yyds_mail_maps_domain_to_default_domain():
    config = _normalize_email_service_config(
        EmailServiceType.YYDS_MAIL,
        {"domain": "mail.example.com"},
    )

    assert config["default_domain"] == "mail.example.com"
    assert "domain" not in config


def test_normalize_cloudmail_maps_default_domain_to_domain():
    config = _normalize_email_service_config(
        EmailServiceType.CLOUDMAIL,
        {"default_domain": "cloud.example.com"},
    )

    assert config["domain"] == "cloud.example.com"
    assert "default_domain" not in config


def test_normalize_luckmail_maps_domain_to_preferred_domain():
    config = _normalize_email_service_config(
        EmailServiceType.LUCKMAIL,
        {"domain": "outlook.com"},
    )

    assert config["preferred_domain"] == "outlook.com"
    assert "domain" not in config
