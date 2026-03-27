from contextlib import contextmanager
from pathlib import Path

from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services.temp_mail import TempMailService
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


def test_registration_available_services_exposes_temp_mail_domain_pool(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "registration_available_temp_mail.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="temp_mail",
                name="Worker temp mail",
                config={"domains": ["alpha.example.com", "beta.example.com"]},
                enabled=True,
                priority=0,
            )
        )

    monkeypatch.setattr(registration_routes, "get_db", lambda: _session_context(manager))
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())

    payload = _run_coroutine_without_awaits(registration_routes.get_available_email_services())

    service = payload["temp_mail"]["services"][0]
    assert payload["temp_mail"]["available"] is True
    assert payload["temp_mail"]["count"] == 1
    assert service["name"] == "Worker temp mail"
    assert service["domain_count"] == 2
    assert service["domains_preview"] == ["alpha.example.com", "beta.example.com"]
    assert service["domain_source"] == "config_domains"


def test_registration_available_services_uses_worker_domain_pool_when_config_pool_missing(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "registration_available_temp_mail_worker.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="temp_mail",
                name="Worker-backed temp mail",
                config={
                    "base_url": "https://apmail.889110.xyz",
                    "admin_password": "admin888",
                    "domain": "fallback.example.com",
                },
                enabled=True,
                priority=0,
            )
        )

    monkeypatch.setattr(registration_routes, "get_db", lambda: _session_context(manager))
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(TempMailService, "_fetch_domains_from_worker", lambda self: {"domains": ["worker-a.example.com", "worker-b.example.com"]})

    payload = _run_coroutine_without_awaits(registration_routes.get_available_email_services())

    service = payload["temp_mail"]["services"][0]
    assert service["domain_source"] == "worker_api"
    assert service["domains_preview"] == ["worker-a.example.com", "worker-b.example.com"]
