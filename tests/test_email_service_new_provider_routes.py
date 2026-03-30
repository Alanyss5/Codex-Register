import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.config.constants import EmailServiceType
from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services.base import EmailServiceFactory
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


class DummySettings:
    custom_domain_base_url = ""
    custom_domain_api_key = None


@contextmanager
def _session_context(manager):
    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _run_coroutine_without_awaits(coro):
    try:
        coro.send(None)
    except StopIteration as exc:
        return exc.value
    raise AssertionError("coroutine unexpectedly awaited")


def test_new_provider_services_registered():
    assert EmailServiceFactory.get_service_class(EmailServiceType("yyds_mail")).__name__ == "YYDSMailService"
    assert EmailServiceFactory.get_service_class(EmailServiceType("cloudmail")).__name__ == "CloudMailService"
    assert EmailServiceFactory.get_service_class(EmailServiceType("luckmail")).__name__ == "LuckMailService"


def test_email_service_types_include_new_providers():
    result = asyncio.run(email_routes.get_service_types())

    yyds_mail = next(item for item in result["types"] if item["value"] == "yyds_mail")
    cloudmail = next(item for item in result["types"] if item["value"] == "cloudmail")
    luckmail = next(item for item in result["types"] if item["value"] == "luckmail")

    assert yyds_mail["label"] == "YYDS Mail"
    assert [field["name"] for field in yyds_mail["config_fields"]] == [
        "base_url",
        "api_key",
        "default_domain",
        "timeout",
    ]

    assert cloudmail["label"] == "CloudMail"
    assert [field["name"] for field in cloudmail["config_fields"]] == [
        "base_url",
        "admin_password",
        "domain",
        "enable_prefix",
    ]

    assert luckmail["label"] == "LuckMail"
    assert [field["name"] for field in luckmail["config_fields"]] == [
        "base_url",
        "api_key",
        "project_code",
        "email_type",
        "preferred_domain",
        "poll_interval",
    ]


def test_filter_sensitive_config_marks_new_provider_secrets():
    filtered = email_routes.filter_sensitive_config(
        {
            "base_url": "https://mail.example.test",
            "admin_password": "admin-secret",
            "api_key": "luck-secret",
            "preferred_domain": "outlook.com",
        }
    )

    assert filtered["base_url"] == "https://mail.example.test"
    assert filtered["preferred_domain"] == "outlook.com"
    assert filtered["has_admin_password"] is True
    assert filtered["has_api_key"] is True
    assert "admin_password" not in filtered
    assert "api_key" not in filtered


def test_registration_available_services_include_new_providers(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "new_provider_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add_all(
            [
                EmailService(
                    service_type="yyds_mail",
                    name="YYDS 主服务",
                    config={
                        "base_url": "https://maliapi.215.im/v1",
                        "api_key": "yyds-key",
                        "default_domain": "mail.example.com",
                    },
                    enabled=True,
                    priority=0,
                ),
                EmailService(
                    service_type="cloudmail",
                    name="CloudMail 主服务",
                    config={
                        "base_url": "https://cloudmail.example.com",
                        "admin_password": "cloudmail-admin",
                        "domain": "cloudmail.example.com",
                    },
                    enabled=True,
                    priority=1,
                ),
                EmailService(
                    service_type="luckmail",
                    name="LuckMail 主服务",
                    config={
                        "base_url": "https://mails.luckyous.com/",
                        "api_key": "luck-key",
                        "project_code": "openai",
                        "email_type": "ms_graph",
                        "preferred_domain": "outlook.com",
                    },
                    enabled=True,
                    priority=2,
                ),
            ]
        )

    monkeypatch.setattr(registration_routes, "get_db", lambda: _session_context(manager))
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())

    payload = _run_coroutine_without_awaits(registration_routes.get_available_email_services())

    assert payload["yyds_mail"]["available"] is True
    assert payload["yyds_mail"]["services"][0]["default_domain"] == "mail.example.com"

    assert payload["cloudmail"]["available"] is True
    assert payload["cloudmail"]["services"][0]["type"] == "cloudmail"
    assert payload["cloudmail"]["services"][0]["domain_count"] == 1
    assert payload["cloudmail"]["services"][0]["domains_preview"] == ["cloudmail.example.com"]

    assert payload["luckmail"]["available"] is True
    assert payload["luckmail"]["services"][0]["preferred_domain"] == "outlook.com"
    assert payload["luckmail"]["services"][0]["project_code"] == "openai"


def test_update_cloudmail_can_persist_enable_prefix_false(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "cloudmail_update_false.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        service = EmailService(
            service_type="cloudmail",
            name="CloudMail 编辑测试",
            config={
                "base_url": "https://cloudmail.example.com",
                "admin_password": "cloudmail-admin",
                "domain": "cloudmail.example.com",
                "enable_prefix": True,
            },
            enabled=True,
            priority=0,
        )
        session.add(service)
        session.commit()
        session.refresh(service)
        service_id = service.id

    monkeypatch.setattr(email_routes, "get_db", lambda: _session_context(manager))

    response = asyncio.run(
        email_routes.update_email_service(
            service_id,
            email_routes.EmailServiceUpdate(
                config={"enable_prefix": False},
            ),
        )
    )

    assert response.config["enable_prefix"] is False

    with manager.session_scope() as session:
        persisted = session.query(EmailService).filter_by(id=service_id).first()
        assert persisted.config["enable_prefix"] is False
