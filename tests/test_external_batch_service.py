
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.database.models import Base, Account, EmailService, RegistrationTask, Sub2ApiService
from src.database.session import DatabaseSessionManager
from src.database import external_batch_crud
import src.core.external_batches.service as external_batch_service_module
import src.web.routes.registration as registration_routes
from src.core.external_batches.service import ExternalBatchCreateRequest, ExternalBatchService
from src.core.external_batches.recovery import recover_interrupted_external_batches


def _make_db(name: str):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def _seed_temp_mail(session):
    session.add(EmailService(service_type="temp_mail", name="tm-1", config={"domain": "a.test"}, enabled=True, priority=0))


@contextmanager
def _session_context(manager):
    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_create_batch_persists_batch_and_items_and_is_idempotent():
    manager = _make_db("external_batch_create.db")
    service = ExternalBatchService()
    with manager.session_scope() as session:
        _seed_temp_mail(session)

    request = ExternalBatchCreateRequest(
        count=3,
        idempotency_key="batch-001",
        email_type="temp_mail",
        email_service_id=None,
        upload_enabled=False,
        upload_provider=None,
        upload_service_id=None,
        mode="pipeline",
        concurrency=1,
        interval_min=1,
        interval_max=2,
    )

    with manager.session_scope() as session:
        first = service.create_batch(session, request)
        second = service.create_batch(session, request)
        items = external_batch_crud.list_batch_items(session, first.batch_uuid)

    assert first.id == second.id
    assert first.batch_uuid == second.batch_uuid
    assert len(items) == 3
    assert [item.item_index for item in items] == [0, 1, 2]
    assert all(item.registration_task_uuid for item in items)


def test_recompute_summary_marks_completed_partial_and_counts_uploads():
    manager = _make_db("external_batch_summary.db")
    service = ExternalBatchService()
    with manager.session_scope() as session:
        _seed_temp_mail(session)
        session.add(Sub2ApiService(name="sub2-1", api_url="https://sub2.test", api_key="k1", enabled=True, priority=0))

    request = ExternalBatchCreateRequest(
        count=3,
        idempotency_key=None,
        email_type="temp_mail",
        email_service_id=None,
        upload_enabled=True,
        upload_provider="sub2api",
        upload_service_id=1,
        mode="pipeline",
        concurrency=1,
        interval_min=1,
        interval_max=1,
    )

    with manager.session_scope() as session:
        batch = service.create_batch(session, request)
        items = external_batch_crud.list_batch_items(session, batch.batch_uuid)
        external_batch_crud.update_batch_item(session, items[0].id, status="completed", upload_status="success")
        external_batch_crud.update_batch_item(session, items[1].id, status="failed", failure_reason="no_available_email_service", upload_status="skipped")
        external_batch_crud.update_batch_item(session, items[2].id, status="completed", upload_status="failed", upload_error="upload_failed")
        refreshed = service.recompute_summary(session, batch.batch_uuid)

    assert refreshed.status == "completed_partial"
    assert refreshed.completed_count == 3
    assert refreshed.success_count == 2
    assert refreshed.failed_count == 1
    assert refreshed.upload_success_count == 1
    assert refreshed.upload_failed_count == 1


def test_recovery_marks_interrupted_batches_and_registration_tasks_failed():
    manager = _make_db("external_batch_recovery.db")
    service = ExternalBatchService()
    with manager.session_scope() as session:
        _seed_temp_mail(session)

    request = ExternalBatchCreateRequest(
        count=2,
        idempotency_key=None,
        email_type="temp_mail",
        email_service_id=None,
        upload_enabled=False,
        upload_provider=None,
        upload_service_id=None,
        mode="parallel",
        concurrency=2,
        interval_min=0,
        interval_max=0,
    )

    with manager.session_scope() as session:
        batch = service.create_batch(session, request)
        items = external_batch_crud.list_batch_items(session, batch.batch_uuid)
        external_batch_crud.update_batch(session, batch.batch_uuid, status="running")
        external_batch_crud.update_batch_item(session, items[0].id, status="running")
        external_batch_crud.update_batch_item(session, items[1].id, status="pending")
        session.query(RegistrationTask).filter(RegistrationTask.task_uuid == items[0].registration_task_uuid).update({"status": "running"})
        session.query(RegistrationTask).filter(RegistrationTask.task_uuid == items[1].registration_task_uuid).update({"status": "pending"})

    with manager.session_scope() as session:
        recover_interrupted_external_batches(session)
        refreshed = external_batch_crud.get_batch_by_uuid(session, batch.batch_uuid)
        refreshed_items = external_batch_crud.list_batch_items(session, batch.batch_uuid)
        reg_tasks = {task.task_uuid: task for task in session.query(RegistrationTask).all()}

    assert refreshed.status == "failed"
    assert refreshed.failure_reason == "service_restarted"
    assert all(item.status == "failed" for item in refreshed_items)
    assert all(item.failure_reason == "service_restarted" for item in refreshed_items)
    assert all(reg_tasks[item.registration_task_uuid].status == "failed" for item in refreshed_items)


def test_run_batch_pipeline_completes_items_and_uploads(monkeypatch):
    manager = _make_db("external_batch_run_pipeline.db")
    service = ExternalBatchService()
    with manager.session_scope() as session:
        _seed_temp_mail(session)
        session.add(Sub2ApiService(name="sub2-1", api_url="https://sub2.test", api_key="k1", enabled=True, priority=0))

    request = ExternalBatchCreateRequest(
        count=2,
        idempotency_key=None,
        email_type="temp_mail",
        email_service_id=None,
        upload_enabled=True,
        upload_provider="sub2api",
        upload_service_id=1,
        mode="pipeline",
        concurrency=1,
        interval_min=0,
        interval_max=0,
    )

    with manager.session_scope() as session:
        batch = service.create_batch(session, request)

    monkeypatch.setattr("src.database.session.get_db", lambda: _session_context(manager))

    async def _fake_run_registration_task(task_uuid, *_args, **_kwargs):
        with manager.session_scope() as session:
            task = session.query(RegistrationTask).filter(RegistrationTask.task_uuid == task_uuid).first()
            task.status = "completed"
            task.result = {"email": f"{task_uuid}@example.com", "account_id": f"acc-{task_uuid}"}
            session.add(
                Account(
                    email=f"{task_uuid}@example.com",
                    password="pw",
                    account_id=f"acc-{task_uuid}",
                    email_service="temp_mail",
                    email_service_id="svc-1",
                    status="active",
                    source="register",
                )
            )

    monkeypatch.setattr(registration_routes, "run_registration_task", _fake_run_registration_task)
    monkeypatch.setattr(
        external_batch_service_module,
        "upload_registered_account",
        lambda account, target: SimpleNamespace(success=True, message="ok", account=account, target=target),
    )

    import asyncio

    asyncio.run(service.run_batch(batch.batch_uuid))

    with manager.session_scope() as session:
        refreshed = external_batch_crud.get_batch_by_uuid(session, batch.batch_uuid)
        items = external_batch_crud.list_batch_items(session, batch.batch_uuid)

    assert refreshed.status == "completed"
    assert refreshed.success_count == 2
    assert refreshed.upload_success_count == 2
    assert all(item.status == "completed" for item in items)
    assert all(item.upload_status == "success" for item in items)


def test_run_batch_marks_prefailed_item_without_calling_registration(monkeypatch):
    manager = _make_db("external_batch_prefailed_item.db")
    service = ExternalBatchService()
    with manager.session_scope() as session:
        _seed_temp_mail(session)

    request = ExternalBatchCreateRequest(
        count=1,
        idempotency_key=None,
        email_type="temp_mail",
        email_service_id=None,
        upload_enabled=False,
        upload_provider=None,
        upload_service_id=None,
        mode="pipeline",
        concurrency=1,
        interval_min=0,
        interval_max=0,
    )

    with manager.session_scope() as session:
        batch = service.create_batch(session, request)
        item = external_batch_crud.list_batch_items(session, batch.batch_uuid)[0]
        external_batch_crud.update_batch_item(session, item.id, failure_reason="no_available_email_service")
        task_uuid = item.registration_task_uuid

    monkeypatch.setattr("src.database.session.get_db", lambda: _session_context(manager))

    calls = {"count": 0}

    async def _fake_run_registration_task(*_args, **_kwargs):
        calls["count"] += 1

    monkeypatch.setattr(registration_routes, "run_registration_task", _fake_run_registration_task)

    import asyncio

    asyncio.run(service.run_batch(batch.batch_uuid))

    with manager.session_scope() as session:
        refreshed = external_batch_crud.get_batch_by_uuid(session, batch.batch_uuid)
        item = external_batch_crud.list_batch_items(session, batch.batch_uuid)[0]
        task = session.query(RegistrationTask).filter(RegistrationTask.task_uuid == task_uuid).first()

    assert calls["count"] == 0
    assert refreshed.status == "failed"
    assert item.status == "failed"
    assert item.failure_reason == "no_available_email_service"
    assert task.status == "failed"
    assert task.error_message == "no_available_email_service"


def test_run_batch_uses_account_id_fallback_for_upload_lookup(monkeypatch):
    manager = _make_db("external_batch_account_id_fallback.db")
    service = ExternalBatchService()
    with manager.session_scope() as session:
        _seed_temp_mail(session)
        session.add(Sub2ApiService(name="sub2-1", api_url="https://sub2.test", api_key="k1", enabled=True, priority=0))

    request = ExternalBatchCreateRequest(
        count=1,
        idempotency_key=None,
        email_type="temp_mail",
        email_service_id=None,
        upload_enabled=True,
        upload_provider="sub2api",
        upload_service_id=1,
        mode="pipeline",
        concurrency=1,
        interval_min=0,
        interval_max=0,
    )

    with manager.session_scope() as session:
        batch = service.create_batch(session, request)

    monkeypatch.setattr("src.database.session.get_db", lambda: _session_context(manager))

    async def _fake_run_registration_task(task_uuid, *_args, **_kwargs):
        with manager.session_scope() as session:
            task = session.query(RegistrationTask).filter(RegistrationTask.task_uuid == task_uuid).first()
            task.status = "completed"
            task.result = {"account_id": f"acc-{task_uuid}"}
            session.add(
                Account(
                    email=f"stored-{task_uuid}@example.com",
                    password="pw",
                    account_id=f"acc-{task_uuid}",
                    email_service="temp_mail",
                    email_service_id="svc-1",
                    status="active",
                    source="register",
                )
            )

    seen = {}

    def _fake_upload(account, target):
        seen["email"] = account.email if account else None
        seen["provider"] = target.provider if target else None
        return SimpleNamespace(success=False, message="upload_failed")

    monkeypatch.setattr(registration_routes, "run_registration_task", _fake_run_registration_task)
    monkeypatch.setattr(external_batch_service_module, "upload_registered_account", _fake_upload)

    import asyncio

    asyncio.run(service.run_batch(batch.batch_uuid))

    with manager.session_scope() as session:
        refreshed = external_batch_crud.get_batch_by_uuid(session, batch.batch_uuid)
        item = external_batch_crud.list_batch_items(session, batch.batch_uuid)[0]

    assert seen["provider"] == "sub2api"
    assert seen["email"].startswith("stored-")
    assert refreshed.status == "completed"
    assert refreshed.upload_failed_count == 1
    assert item.upload_status == "failed"
    assert item.upload_error == "upload_failed"


def test_request_cancel_rejects_finished_batch():
    manager = _make_db("external_batch_cancel_finished.db")
    service = ExternalBatchService()
    with manager.session_scope() as session:
        _seed_temp_mail(session)

    request = ExternalBatchCreateRequest(
        count=1,
        idempotency_key=None,
        email_type="temp_mail",
        email_service_id=None,
        upload_enabled=False,
        upload_provider=None,
        upload_service_id=None,
        mode="pipeline",
        concurrency=1,
        interval_min=0,
        interval_max=0,
    )

    with manager.session_scope() as session:
        batch = service.create_batch(session, request)
        external_batch_crud.update_batch(session, batch.batch_uuid, status="completed")

    with manager.session_scope() as session:
        with pytest.raises(ValueError, match="already finished"):
            service.request_cancel(session, batch.batch_uuid)
