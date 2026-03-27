from contextlib import contextmanager
from pathlib import Path
import threading
import asyncio
from types import SimpleNamespace

from src.config.constants import EmailServiceType
from src.core.register import RegistrationResult
from src.database.models import Account, Base, EmailService, RegistrationTask
from src.database.session import DatabaseSessionManager
from src.web.routes import registration as registration_routes


def _make_session_manager(name):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


@contextmanager
def _patched_get_db(manager):
    session = manager.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _seed_outlook_services(session):
    first = EmailService(
        service_type="outlook",
        name="Outlook A",
        config={
            "email": "reserved-a@example.com",
            "client_id": "client-a",
            "refresh_token": "refresh-a",
        },
        enabled=True,
        priority=0,
    )
    second = EmailService(
        service_type="outlook",
        name="Outlook B",
        config={
            "email": "available-b@example.com",
            "client_id": "client-b",
            "refresh_token": "refresh-b",
        },
        enabled=True,
        priority=1,
    )
    session.add_all([first, second])
    session.commit()
    session.refresh(first)
    session.refresh(second)
    return first, second


def _seed_failed_outlook_placeholder(session, email, service_name="placeholder"):
    account = Account(
        email=email,
        password="",
        email_service="outlook",
        email_service_id=str(service_name),
        status="failed",
        source="register",
        extra_data={
            "register_failed_reason": "username_rejected_or_existing_account",
        },
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _seed_recoverable_outlook_account(session, email, service_name="placeholder"):
    account = Account(
        email=email,
        password="saved-password",
        email_service="outlook",
        email_service_id=str(service_name),
        status="failed",
        source="register",
        extra_data={
            "register_failed_reason": "token_recovery_pending",
            "recovery_ready": True,
            "account_created": True,
            "token_acquired": False,
        },
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def _patch_registration_selection(monkeypatch, selected):
    def fake_create(service_type, config, name=None):
        selected["service_type"] = service_type
        selected["config"] = dict(config)
        selected["name"] = name
        return SimpleNamespace(service_type=service_type, config=config, name=name)

    def fake_run(self):
        return RegistrationResult(success=False, error_message="stop after selection")

    monkeypatch.setattr(registration_routes.EmailServiceFactory, "create", fake_create)
    monkeypatch.setattr(registration_routes.RegistrationEngine, "run", fake_run)


def test_outlook_execution_uses_task_bound_email_service_id_when_runtime_arg_is_none(monkeypatch):
    manager = _make_session_manager("outlook-selection-bound.db")
    selected = {}

    with manager.session_scope() as session:
        first, second = _seed_outlook_services(session)
        session.add(
            RegistrationTask(
                task_uuid="task-bound-email-service",
                status="pending",
                email_service_id=second.id,
            )
        )
        session.commit()

    @contextmanager
    def fake_get_db():
        with _patched_get_db(manager) as session:
            yield session

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    _patch_registration_selection(monkeypatch, selected)

    registration_routes._run_sync_registration_task(
        task_uuid="task-bound-email-service",
        email_service_type="outlook",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    assert selected["service_type"] == EmailServiceType.OUTLOOK
    assert selected["config"]["email"] == "available-b@example.com"
    assert selected["config"]["client_id"] == "client-b"


def test_outlook_auto_selection_skips_failed_placeholder_account(monkeypatch):
    manager = _make_session_manager("outlook-selection-failed-placeholder.db")
    selected = {}

    with manager.session_scope() as session:
        first, second = _seed_outlook_services(session)
        _seed_failed_outlook_placeholder(session, "reserved-a@example.com")
        session.add(
            RegistrationTask(
                task_uuid="task-skip-failed-placeholder",
                status="pending",
            )
        )
        session.commit()

    @contextmanager
    def fake_get_db():
        with _patched_get_db(manager) as session:
            yield session

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    _patch_registration_selection(monkeypatch, selected)

    registration_routes._run_sync_registration_task(
        task_uuid="task-skip-failed-placeholder",
        email_service_type="outlook",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    assert selected["service_type"] == EmailServiceType.OUTLOOK
    assert selected["config"]["email"] == "available-b@example.com"
    assert selected["config"]["client_id"] == "client-b"


def test_outlook_auto_selection_reports_consumed_accounts_with_clear_error(monkeypatch):
    manager = _make_session_manager("outlook-selection-all-consumed.db")

    with manager.session_scope() as session:
        first, second = _seed_outlook_services(session)
        _seed_failed_outlook_placeholder(session, "reserved-a@example.com")
        _seed_failed_outlook_placeholder(session, "available-b@example.com")
        session.add(
            RegistrationTask(
                task_uuid="task-all-consumed",
                status="pending",
            )
        )
        session.commit()

    @contextmanager
    def fake_get_db():
        with _patched_get_db(manager) as session:
            yield session

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    registration_routes._run_sync_registration_task(
        task_uuid="task-all-consumed",
        email_service_type="outlook",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    with _patched_get_db(manager) as session:
        task = session.query(RegistrationTask).filter_by(task_uuid="task-all-consumed").first()

    assert task.status == "failed"
    assert "已消耗" in (task.error_message or "")
    assert "Outlook" in (task.error_message or "")


def test_outlook_bound_consumed_service_is_rejected_before_engine_runs(monkeypatch):
    manager = _make_session_manager("outlook-selection-bound-consumed.db")
    selected = {}

    with manager.session_scope() as session:
        first, second = _seed_outlook_services(session)
        _seed_failed_outlook_placeholder(session, "available-b@example.com")
        session.add(
            RegistrationTask(
                task_uuid="task-bound-consumed",
                status="pending",
                email_service_id=second.id,
            )
        )
        session.commit()

    @contextmanager
    def fake_get_db():
        with _patched_get_db(manager) as session:
            yield session

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    def fake_create(service_type, config, name=None):
        selected["service_type"] = service_type
        selected["config"] = dict(config)
        selected["name"] = name
        return SimpleNamespace(service_type=service_type, config=config, name=name)

    def fake_run(self):
        selected["engine_ran"] = True
        return RegistrationResult(success=False, error_message="should not be reached")

    monkeypatch.setattr(registration_routes.EmailServiceFactory, "create", fake_create)
    monkeypatch.setattr(registration_routes.RegistrationEngine, "run", fake_run)

    registration_routes._run_sync_registration_task(
        task_uuid="task-bound-consumed",
        email_service_type="outlook",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    with _patched_get_db(manager) as session:
        task = session.query(RegistrationTask).filter_by(task_uuid="task-bound-consumed").first()

    assert selected == {}
    assert task.status == "failed"
    assert "已消耗" in (task.error_message or "")
    assert "Outlook" in (task.error_message or "")


def test_outlook_auto_selection_skips_reserved_pending_task_and_uses_next_available_service(monkeypatch):
    manager = _make_session_manager("outlook-selection-reserved.db")
    selected = {}

    with manager.session_scope() as session:
        first, second = _seed_outlook_services(session)
        session.add_all(
            [
                RegistrationTask(
                    task_uuid="reserved-task",
                    status="running",
                    email_service_id=first.id,
                ),
                RegistrationTask(
                    task_uuid="selected-task",
                    status="pending",
                ),
            ]
        )
        session.commit()

    @contextmanager
    def fake_get_db():
        with _patched_get_db(manager) as session:
            yield session

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    _patch_registration_selection(monkeypatch, selected)

    registration_routes._run_sync_registration_task(
        task_uuid="selected-task",
        email_service_type="outlook",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    assert selected["service_type"] == EmailServiceType.OUTLOOK
    assert selected["config"]["email"] == "available-b@example.com"
    assert selected["config"]["client_id"] == "client-b"


def test_outlook_auto_selection_reserves_account_atomically_across_concurrent_tasks(monkeypatch):
    manager = _make_session_manager("outlook-selection-concurrent.db")
    selections = {}

    with manager.session_scope() as session:
        first, second = _seed_outlook_services(session)
        session.add_all(
            [
                RegistrationTask(task_uuid="task-1", status="pending"),
                RegistrationTask(task_uuid="task-2", status="pending"),
            ]
        )
        session.commit()

    @contextmanager
    def fake_get_db():
        with _patched_get_db(manager) as session:
            yield session

    real_update = registration_routes.crud.update_registration_task
    task_1_reached_reservation = threading.Event()
    allow_task_1_reservation = threading.Event()

    def fake_update_registration_task(db, task_uuid, **kwargs):
        if "email_service_id" in kwargs and task_uuid == "task-1":
            task_1_reached_reservation.set()
            allow_task_1_reservation.wait(timeout=5)
        return real_update(db, task_uuid, **kwargs)

    def fake_create(service_type, config, name=None):
        selections[threading.current_thread().name] = dict(config)
        return SimpleNamespace(service_type=service_type, config=config, name=name)

    def fake_run(self):
        return RegistrationResult(success=False, error_message="stop after selection")

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes.crud, "update_registration_task", fake_update_registration_task)
    monkeypatch.setattr(registration_routes.EmailServiceFactory, "create", fake_create)
    monkeypatch.setattr(registration_routes.RegistrationEngine, "run", fake_run)

    errors = []

    def run_task(task_uuid, release_after_entry=False):
        try:
            if release_after_entry:
                task_1_reached_reservation.wait(timeout=5)
            registration_routes._run_sync_registration_task(
                task_uuid=task_uuid,
                email_service_type="outlook",
                proxy=None,
                email_service_config=None,
                email_service_id=None,
            )
        except Exception as exc:  # pragma: no cover - test diagnostics
            errors.append(exc)
        finally:
            if release_after_entry:
                allow_task_1_reservation.set()

    thread_1 = threading.Thread(target=run_task, args=("task-1", False), name="task-1-thread")
    thread_2 = threading.Thread(target=run_task, args=("task-2", True), name="task-2-thread")

    thread_1.start()
    thread_2.start()
    thread_1.join(timeout=10)
    thread_2.join(timeout=10)

    assert not errors
    assert selections["task-1-thread"]["email"] == "reserved-a@example.com"
    assert selections["task-2-thread"]["email"] == "available-b@example.com"


def test_outlook_auto_selection_keeps_recoverable_account_available(monkeypatch):
    manager = _make_session_manager("outlook-selection-recoverable.db")
    selected = {}

    with manager.session_scope() as session:
        _seed_outlook_services(session)
        _seed_recoverable_outlook_account(session, "reserved-a@example.com")
        session.add(
            RegistrationTask(
                task_uuid="task-recoverable-selection",
                status="pending",
            )
        )
        session.commit()

    @contextmanager
    def fake_get_db():
        with _patched_get_db(manager) as session:
            yield session

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    _patch_registration_selection(monkeypatch, selected)

    registration_routes._run_sync_registration_task(
        task_uuid="task-recoverable-selection",
        email_service_type="outlook",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    assert selected["service_type"] == EmailServiceType.OUTLOOK
    assert selected["config"]["email"] == "reserved-a@example.com"


def test_outlook_bound_recoverable_service_runs_instead_of_being_rejected(monkeypatch):
    manager = _make_session_manager("outlook-selection-bound-recoverable.db")
    selected = {}

    with manager.session_scope() as session:
        first, second = _seed_outlook_services(session)
        _seed_recoverable_outlook_account(session, "available-b@example.com")
        session.add(
            RegistrationTask(
                task_uuid="task-bound-recoverable",
                status="pending",
                email_service_id=second.id,
            )
        )
        session.commit()

    @contextmanager
    def fake_get_db():
        with _patched_get_db(manager) as session:
            yield session

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    def fake_create(service_type, config, name=None):
        selected["service_type"] = service_type
        selected["config"] = dict(config)
        selected["name"] = name
        return SimpleNamespace(service_type=service_type, config=config, name=name)

    def fake_run(self):
        selected["engine_ran"] = True
        return RegistrationResult(success=False, error_message="recovery still failed")

    monkeypatch.setattr(registration_routes.EmailServiceFactory, "create", fake_create)
    monkeypatch.setattr(registration_routes.RegistrationEngine, "run", fake_run)

    registration_routes._run_sync_registration_task(
        task_uuid="task-bound-recoverable",
        email_service_type="outlook",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    with _patched_get_db(manager) as session:
        task = session.query(RegistrationTask).filter_by(task_uuid="task-bound-recoverable").first()

    assert selected["engine_ran"] is True
    assert selected["config"]["email"] == "available-b@example.com"
    assert task.status == "failed"
    assert task.error_message == "recovery still failed"


def test_outlook_batch_skip_registered_keeps_recoverable_accounts(monkeypatch):
    manager = _make_session_manager("outlook-batch-recoverable.db")

    with manager.session_scope() as session:
        first, _second = _seed_outlook_services(session)
        first_id = first.id
        _seed_recoverable_outlook_account(session, "reserved-a@example.com")

    @contextmanager
    def fake_get_db():
        with _patched_get_db(manager) as session:
            yield session

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    async def fake_batch_runner(*args, **kwargs):
        return None

    monkeypatch.setattr(registration_routes, "run_outlook_batch_registration", fake_batch_runner)

    async def invoke():
        from fastapi import BackgroundTasks

        return await registration_routes.start_outlook_batch_registration(
            registration_routes.OutlookBatchRegistrationRequest(
                service_ids=[first_id],
                skip_registered=True,
            ),
            BackgroundTasks(),
        )

    response = asyncio.run(invoke())

    assert response.to_register == 1
    assert response.service_ids == [first_id]
