import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import BackgroundTasks

from src.config.constants import EmailServiceType
from src.core import email_service_catalog
from src.core.register import RegistrationResult
from src.database.models import Base, EmailService, RegistrationTask
from src.database.session import DatabaseSessionManager
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


class DummySettings:
    custom_domain_base_url = ""
    custom_domain_api_key = None
    external_api_enabled = True


def _make_db(name: str):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


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


def test_create_email_service_accepts_new_distinct_provider_types(monkeypatch):
    manager = _make_db("email-route-new-provider-create.db")
    monkeypatch.setattr(email_routes, "get_db", lambda: _session_context(manager))

    requests = [
        email_routes.EmailServiceCreate(
            service_type="yyds_mail",
            name="YYDS ???",
            config={
                "base_url": "https://mail.yyds.test",
                "api_key": "key-1",
                "default_domain": "yyds.test",
            },
        ),
        email_routes.EmailServiceCreate(
            service_type="cloudmail",
            name="CloudMail ???",
            config={
                "base_url": "https://cloudmail.test",
                "admin_password": "secret",
                "domain": "cloudmail.test",
            },
        ),
        email_routes.EmailServiceCreate(
            service_type="luckmail",
            name="LuckMail ???",
            config={
                "base_url": "https://mails.luckyous.com/",
                "api_key": "key-2",
                "project_code": "openai",
            },
        ),
    ]

    responses = [asyncio.run(email_routes.create_email_service(req)) for req in requests]

    assert [item.service_type for item in responses] == ["yyds_mail", "cloudmail", "luckmail"]


def test_start_and_batch_registration_accept_new_provider_types(monkeypatch):
    manager = _make_db("registration-new-provider-validation.db")
    monkeypatch.setattr(registration_routes, "get_db", lambda: _session_context(manager))

    single_request = registration_routes.RegistrationTaskCreate(
        email_service_type="yyds_mail",
        email_service_id=1,
    )
    single_response = asyncio.run(
        registration_routes.start_registration(single_request, BackgroundTasks())
    )
    assert single_response.status == "pending"

    batch_request = registration_routes.BatchRegistrationRequest(
        count=2,
        email_service_type="cloudmail",
        interval_min=0,
        interval_max=0,
        concurrency=1,
        mode="pipeline",
    )
    batch_response = asyncio.run(
        registration_routes.start_batch_registration(batch_request, BackgroundTasks())
    )
    assert batch_response.count == 2
    assert len(batch_response.tasks) == 2


def test_runtime_keeps_luckmail_as_distinct_service_type(monkeypatch):
    manager = _make_db("registration-runtime-luckmail.db")
    selected = {}

    with manager.session_scope() as session:
        provider = EmailService(
            service_type="luckmail",
            name="Luckmail 1",
            config={
                "base_url": "https://mails.luckyous.com/",
                "api_key": "luck-key",
                "project_code": "openai",
                "email_type": "ms_graph",
                "preferred_domain": "outlook.com",
            },
            enabled=True,
            priority=0,
        )
        session.add(provider)
        session.commit()
        session.refresh(provider)

        session.add(
            RegistrationTask(
                task_uuid="task-luckmail-runtime",
                status="pending",
                email_service_id=provider.id,
            )
        )
        session.commit()

    monkeypatch.setattr(registration_routes, "get_db", lambda: _session_context(manager))
    monkeypatch.setattr(
        registration_routes,
        "get_proxy_for_registration",
        lambda _db: ("http://auto-proxy:8080", 9),
    )

    def fake_create(service_type, config, name=None):
        selected["service_type"] = service_type
        selected["config"] = dict(config)
        selected["name"] = name
        return SimpleNamespace(service_type=service_type, config=config, name=name)

    def fake_run(_self):
        return RegistrationResult(success=False, error_message="stop after selection")

    monkeypatch.setattr(registration_routes.EmailServiceFactory, "create", fake_create)
    monkeypatch.setattr(registration_routes.RegistrationEngine, "run", fake_run)

    registration_routes._run_sync_registration_task(
        task_uuid="task-luckmail-runtime",
        email_service_type="luckmail",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    assert selected["service_type"] == EmailServiceType.LUCKMAIL
    assert selected["config"]["preferred_domain"] == "outlook.com"
    assert selected["config"]["proxy_url"] == "http://auto-proxy:8080"


def test_runtime_cloudmail_stays_direct_without_auto_proxy(monkeypatch):
    manager = _make_db("registration-runtime-cloudmail.db")
    selected = {}

    with manager.session_scope() as session:
        provider = EmailService(
            service_type="cloudmail",
            name="Cloudmail 1",
            config={
                "base_url": "https://cloudmail.test",
                "admin_password": "secret",
                "domain": "cloudmail.test",
                "enable_prefix": True,
            },
            enabled=True,
            priority=0,
        )
        session.add(provider)
        session.commit()
        session.refresh(provider)

        session.add(
            RegistrationTask(
                task_uuid="task-cloudmail-runtime",
                status="pending",
                email_service_id=provider.id,
            )
        )
        session.commit()

    monkeypatch.setattr(registration_routes, "get_db", lambda: _session_context(manager))
    monkeypatch.setattr(
        registration_routes,
        "get_proxy_for_registration",
        lambda _db: (_ for _ in ()).throw(AssertionError("cloudmail should stay direct")),
    )

    def fake_create(service_type, config, name=None):
        selected["service_type"] = service_type
        selected["config"] = dict(config)
        selected["name"] = name
        return SimpleNamespace(service_type=service_type, config=config, name=name)

    def fake_run(_self):
        return RegistrationResult(success=False, error_message="stop after selection")

    monkeypatch.setattr(registration_routes.EmailServiceFactory, "create", fake_create)
    monkeypatch.setattr(registration_routes.RegistrationEngine, "run", fake_run)

    registration_routes._run_sync_registration_task(
        task_uuid="task-cloudmail-runtime",
        email_service_type="cloudmail",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    assert selected["service_type"] == EmailServiceType.CLOUDMAIL
    assert selected["config"]["domain"] == "cloudmail.test"
    assert "proxy_url" not in selected["config"]


def test_external_capabilities_expose_cloudmail_yyds_and_luckmail_with_distinct_shapes(monkeypatch):
    manager = _make_db("email-catalog-new-provider-compat.db")
    with manager.session_scope() as session:
        session.add_all(
            [
                EmailService(
                    service_type="yyds_mail",
                    name="YYDS Pool",
                    config={"default_domain": "yyds.example.com"},
                    enabled=True,
                    priority=0,
                ),
                EmailService(
                    service_type="cloudmail",
                    name="Cloud Pool",
                    config={"domains": ["c1.example.com"]},
                    enabled=True,
                    priority=1,
                ),
                EmailService(
                    service_type="luckmail",
                    name="Luck Pool",
                    config={
                        "project_code": "openai",
                        "email_type": "ms_graph",
                        "preferred_domain": "outlook.com",
                    },
                    enabled=True,
                    priority=2,
                ),
            ]
        )

    monkeypatch.setattr(email_service_catalog, "get_db", lambda: _session_context(manager))
    monkeypatch.setattr(email_service_catalog, "get_settings", lambda: DummySettings())

    payload = email_service_catalog.build_external_capabilities()

    yyds = next(item for item in payload["email_types"] if item["type"] == "yyds_mail")
    cloudmail = next(item for item in payload["email_types"] if item["type"] == "cloudmail")
    luckmail = next(item for item in payload["email_types"] if item["type"] == "luckmail")

    assert yyds["available"] is True
    assert yyds["services"][0]["default_domain"] == "yyds.example.com"

    assert cloudmail["available"] is True
    assert cloudmail["services"][0]["type"] == "cloudmail"
    assert cloudmail["services"][0]["domain_count"] == 1

    assert luckmail["available"] is True
    assert luckmail["services"][0]["project_code"] == "openai"
    assert luckmail["services"][0]["email_type"] == "ms_graph"
    assert luckmail["services"][0]["preferred_domain"] == "outlook.com"
