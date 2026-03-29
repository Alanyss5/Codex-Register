import asyncio
from contextlib import contextmanager
from pathlib import Path

from fastapi import BackgroundTasks

from src.database.models import Base
from src.database.session import DatabaseSessionManager
from src.web.routes import registration as registration_routes


def _make_session_manager(name: str):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_start_batch_registration_allows_count_above_100(monkeypatch):
    manager = _make_session_manager("batch-registration-over-100.db")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    request = registration_routes.BatchRegistrationRequest(
        count=101,
        email_service_type="temp_mail",
        interval_min=0,
        interval_max=0,
        concurrency=1,
        mode="pipeline",
    )

    response = asyncio.run(
        registration_routes.start_batch_registration(request, BackgroundTasks())
    )

    assert response.count == 101
    assert len(response.tasks) == 101
