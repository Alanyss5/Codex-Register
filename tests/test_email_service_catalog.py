from contextlib import contextmanager
from pathlib import Path

import src.config.settings as settings_module
from src.core import email_service_catalog
from src.database.models import Base, CpaService, EmailService, Sub2ApiService, TeamManagerService
from src.database.session import DatabaseSessionManager


class DummySettings:
    external_api_enabled = True


@contextmanager
def _session_context(manager):
    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _make_db(name: str):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_build_external_capabilities_survives_temp_mail_configs_without_worker_credentials(monkeypatch):
    manager = _make_db("email_service_catalog_capabilities.db")
    with manager.session_scope() as session:
        session.add_all([
            EmailService(
                service_type="temp_mail",
                name="Temp pool",
                config={"domains": ["alpha.example.com", "beta.example.com"]},
                enabled=True,
                priority=0,
            ),
            CpaService(name="CPA", api_url="https://cpa.test", api_token="token", enabled=True, priority=0),
            Sub2ApiService(name="Sub2", api_url="https://sub2.test", api_key="key", enabled=True, priority=0),
            TeamManagerService(name="TM", api_url="https://tm.test", api_key="key", enabled=True, priority=0),
        ])

    monkeypatch.setattr(email_service_catalog, "get_db", lambda: _session_context(manager))
    monkeypatch.setattr(email_service_catalog, "get_settings", lambda: DummySettings())

    payload = email_service_catalog.build_external_capabilities()

    built_in = next(item for item in payload["email_types"] if item["type"] == "tempmail")
    assert built_in["available"] is True

    temp_mail = next(item for item in payload["email_types"] if item["type"] == "temp_mail")
    assert temp_mail["available"] is True
    assert temp_mail["count"] == 1
    assert temp_mail["services"][0]["domain_count"] == 2
    assert temp_mail["services"][0]["domains_preview"] == ["alpha.example.com", "beta.example.com"]
    assert temp_mail["services"][0]["domain_source"] == "config_domains"

    settings = payload["settings"]
    assert settings["external_api_enabled"] is True
    assert {provider["provider"] for provider in payload["upload_providers"]} == {"cpa", "sub2api", "tm"}


def test_build_external_capabilities_uses_worker_domain_summary_when_credentials_exist(monkeypatch):
    manager = _make_db("email_service_catalog_worker_domains.db")
    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="temp_mail",
                name="Temp worker",
                config={
                    "base_url": "https://apmail.889110.xyz",
                    "admin_password": "admin888",
                    "domain": "fallback.example.com",
                },
                enabled=True,
                priority=0,
            )
        )

    monkeypatch.setattr(email_service_catalog, "get_db", lambda: _session_context(manager))
    monkeypatch.setattr(email_service_catalog, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(email_service_catalog.TempMailService, "_fetch_domains_from_worker", lambda self: {"domains": ["w1.example.com", "w2.example.com"]})

    payload = email_service_catalog.build_external_capabilities()

    temp_mail = next(item for item in payload["email_types"] if item["type"] == "temp_mail")
    assert temp_mail["services"][0]["domain_source"] == "worker_api"
    assert temp_mail["services"][0]["domains_preview"] == ["w1.example.com", "w2.example.com"]


def test_build_external_capabilities_temp_mail_gracefully_handles_empty_summary(monkeypatch):
    manager = _make_db("email_service_catalog_empty_domains.db")
    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="temp_mail",
                name="Temp empty",
                config={},
                enabled=True,
                priority=0,
            )
        )

    monkeypatch.setattr(email_service_catalog, "get_db", lambda: _session_context(manager))
    monkeypatch.setattr(email_service_catalog, "get_settings", lambda: DummySettings())

    payload = email_service_catalog.build_external_capabilities()

    temp_mail = next(item for item in payload["email_types"] if item["type"] == "temp_mail")
    assert temp_mail["services"][0]["domain_source"] == "none"
    assert temp_mail["services"][0]["domain_count"] == 0
    assert temp_mail["services"][0]["domains_preview"] == []
