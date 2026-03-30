import asyncio
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

from fastapi import BackgroundTasks

from src.core.external_batches import service as external_batch_service
from src.core.register import RegistrationResult
from src.core.registration_engines.factory import create_registration_runner
from src.web.routes import registration as registration_routes


def _fake_task(task_uuid: str, email_service_id=None, proxy=None):
    return SimpleNamespace(
        id=1,
        task_uuid=task_uuid,
        status="pending",
        email_service_id=email_service_id,
        proxy=proxy,
        logs=None,
        result=None,
        error_message=None,
        created_at=datetime.utcnow(),
        started_at=None,
        completed_at=None,
    )


@contextmanager
def _fake_get_db():
    yield object()


def test_create_registration_runner_defaults_to_protocol():
    runner = create_registration_runner(
        engine_name=None,
        email_service=SimpleNamespace(service_type=SimpleNamespace(value="tempmail")),
        proxy_url=None,
        proxy_source="direct",
        callback_logger=None,
        task_uuid=None,
    )

    assert runner.__class__.__module__ == "src.core.register"
    assert runner.__class__.__name__ == "RegistrationEngine"


def test_create_registration_runner_returns_browser_engine():
    runner = create_registration_runner(
        engine_name="browser",
        email_service=SimpleNamespace(service_type=SimpleNamespace(value="tempmail")),
        proxy_url=None,
        proxy_source="direct",
        callback_logger=None,
        task_uuid=None,
    )

    assert runner.__class__.__module__ == "src.core.registration_engines.browser"
    assert runner.__class__.__name__ == "BrowserRegistrationEngine"


def test_start_registration_passes_browser_engine_to_background_task(monkeypatch):
    monkeypatch.setattr(registration_routes, "get_db", _fake_get_db)
    monkeypatch.setattr(
        registration_routes.crud,
        "create_registration_task",
        lambda db, task_uuid, email_service_id=None, proxy=None: _fake_task(task_uuid, email_service_id, proxy),
    )

    background_tasks = BackgroundTasks()
    request = registration_routes.RegistrationTaskCreate(
        email_service_type="tempmail",
        engine="browser",
    )

    response = asyncio.run(registration_routes.start_registration(request, background_tasks))

    assert response.status == "pending"
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].kwargs["registration_engine_name"] == "browser"


def test_start_registration_defaults_to_protocol_engine(monkeypatch):
    monkeypatch.setattr(registration_routes, "get_db", _fake_get_db)
    monkeypatch.setattr(
        registration_routes.crud,
        "create_registration_task",
        lambda db, task_uuid, email_service_id=None, proxy=None: _fake_task(task_uuid, email_service_id, proxy),
    )

    background_tasks = BackgroundTasks()
    request = registration_routes.RegistrationTaskCreate(
        email_service_type="tempmail",
    )

    asyncio.run(registration_routes.start_registration(request, background_tasks))

    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].kwargs["registration_engine_name"] == "protocol"


def test_start_batch_registration_passes_engine_to_background_task(monkeypatch):
    monkeypatch.setattr(registration_routes, "get_db", _fake_get_db)
    monkeypatch.setattr(
        registration_routes.crud,
        "create_registration_task",
        lambda db, task_uuid, email_service_id=None, proxy=None: _fake_task(task_uuid, email_service_id, proxy),
    )
    monkeypatch.setattr(
        registration_routes.crud,
        "get_registration_task",
        lambda db, task_uuid: _fake_task(task_uuid),
    )

    background_tasks = BackgroundTasks()
    request = registration_routes.BatchRegistrationRequest(
        count=2,
        email_service_type="tempmail",
        engine="browser",
    )

    response = asyncio.run(registration_routes.start_batch_registration(request, background_tasks))

    assert response.count == 2
    assert len(background_tasks.tasks) == 1
    assert background_tasks.tasks[0].kwargs["registration_engine_name"] == "browser"


def test_external_batch_payload_parses_browser_engine():
    request = external_batch_service._request_from_payload(
        {
            "count": 1,
            "email": {"type": "tempmail"},
            "execution": {
                "mode": "pipeline",
                "concurrency": 1,
                "interval_min": 5,
                "interval_max": 30,
                "engine": "browser",
            },
        }
    )

    assert request.engine == "browser"


def test_get_proxy_for_registration_returns_proxy_resolution_summary(monkeypatch):
    monkeypatch.setattr(registration_routes.crud, "get_random_proxy", lambda db: None)

    class _FakeResolution:
        proxy_url = "http://152.32.236.215:7000"

        def summary(self):
            return {
                "source": "dynamic",
                "proxy_url": self.proxy_url,
                "exit_country": "US",
                "attempts": 2,
            }

    monkeypatch.setattr(
        "src.core.dynamic_proxy.get_proxy_resolution_for_task",
        lambda previous_proxy_url=None: _FakeResolution(),
    )

    proxy_url, proxy_id, proxy_resolution = registration_routes.get_proxy_for_registration(object())

    assert proxy_url == "http://152.32.236.215:7000"
    assert proxy_id is None
    assert proxy_resolution["source"] == "dynamic"
    assert proxy_resolution["exit_country"] == "US"


def test_run_sync_registration_task_passes_proxy_resolution_to_browser_runner(monkeypatch):
    monkeypatch.setattr(registration_routes, "get_db", _fake_get_db)
    monkeypatch.setattr(registration_routes, "get_settings", lambda: SimpleNamespace(
        tempmail_base_url="https://api.test",
        tempmail_timeout=30,
        tempmail_max_retries=2,
    ))
    monkeypatch.setattr(registration_routes.task_manager, "is_cancelled", lambda task_uuid: False)
    monkeypatch.setattr(registration_routes.task_manager, "update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        registration_routes.task_manager,
        "create_log_callback",
        lambda task_uuid, prefix="", batch_id="": (lambda message: None),
    )
    monkeypatch.setattr(registration_routes.crud, "append_task_log", lambda *args, **kwargs: None)

    task = _fake_task("task-1")

    def _update_registration_task(db, task_uuid, **kwargs):
        for key, value in kwargs.items():
            setattr(task, key, value)
        return task

    monkeypatch.setattr(registration_routes.crud, "update_registration_task", _update_registration_task)
    monkeypatch.setattr(registration_routes.EmailServiceFactory, "create", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        registration_routes,
        "_resolve_proxy_for_service",
        lambda **kwargs: (
            "http://152.32.236.215:7000",
            None,
            "auto",
            {
                "source": "dynamic",
                "proxy_url": "http://152.32.236.215:7000",
                "exit_country": "US",
                "attempts": 2,
            },
        ),
    )

    captured = {}

    class _FakeRunner:
        def run(self):
            return RegistrationResult(success=False, error_message="expected test failure")

    def _fake_create_registration_runner(**kwargs):
        captured.update(kwargs)
        return _FakeRunner()

    monkeypatch.setattr(registration_routes, "create_registration_runner", _fake_create_registration_runner)

    registration_routes._run_sync_registration_task(
        task_uuid="task-1",
        email_service_type="tempmail",
        proxy=None,
        email_service_config=None,
        registration_engine_name="browser",
    )

    assert captured["proxy_url"] == "http://152.32.236.215:7000"
    assert captured["proxy_source"] == "auto"
    assert captured["proxy_resolution"]["exit_country"] == "US"
