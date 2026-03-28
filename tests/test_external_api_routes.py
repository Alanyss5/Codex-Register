from types import SimpleNamespace
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.core.external_batches.service import ExternalBatchCreateRequest, ExternalBatchService
from src.database import external_batch_crud
from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
import src.web.routes.external as external_router_package
import src.web.routes.external.capabilities as capabilities_routes
import src.web.routes.external.registration as registration_routes
import src.web.deps.external_auth as external_auth


def _settings(enabled: bool, api_key: str = "test-key"):
    return SimpleNamespace(
        external_api_enabled=enabled,
        external_api_key=SimpleNamespace(get_secret_value=lambda: api_key),
    )


def _client(monkeypatch):
    monkeypatch.setattr(external_auth, "get_settings", lambda: _settings(True, "abc"))
    app = FastAPI()
    app.include_router(external_router_package.router, prefix="/external")
    return TestClient(app)


def _make_db(name: str):
    from pathlib import Path

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


def test_external_auth_requires_api_key(monkeypatch):
    monkeypatch.setattr(external_auth, "get_settings", lambda: _settings(True, "abc"))

    with pytest.raises(HTTPException) as exc:
        external_auth.require_external_api_key(x_api_key=None)

    assert exc.value.status_code == 401


def test_external_auth_rejects_when_disabled(monkeypatch):
    monkeypatch.setattr(external_auth, "get_settings", lambda: _settings(False, "abc"))

    with pytest.raises(HTTPException) as exc:
        external_auth.require_external_api_key(x_api_key="abc")

    assert exc.value.status_code == 403


def test_external_auth_accepts_valid_api_key(monkeypatch):
    monkeypatch.setattr(external_auth, "get_settings", lambda: _settings(True, "abc"))

    assert external_auth.require_external_api_key(x_api_key="abc") is None


def test_external_capabilities_route_uses_adapter(monkeypatch):
    monkeypatch.setattr(
        capabilities_routes,
        "_get_external_capabilities",
        lambda: {"email_types": [{"type": "temp_mail", "available": True}], "upload_providers": []},
    )

    response = capabilities_routes.get_external_capabilities()

    assert response["email_types"][0]["type"] == "temp_mail"


def test_external_registration_routes_use_service_adapter(monkeypatch):
    created = {"batch_uuid": "b-1", "status": "pending", "requested_count": 2, "idempotent_replay": False}

    monkeypatch.setattr(registration_routes, "_create_external_batch", lambda payload, background_tasks=None: created)
    monkeypatch.setattr(registration_routes, "_get_external_batch", lambda batch_uuid: {"batch_uuid": batch_uuid, "status": "running"})
    monkeypatch.setattr(registration_routes, "_cancel_external_batch", lambda batch_uuid: {"batch_uuid": batch_uuid, "status": "cancelled"})

    request = registration_routes.ExternalBatchCreateRequest(
        count=2,
        email={"type": "temp_mail"},
        upload={"enabled": False},
        execution={"mode": "pipeline", "concurrency": 1, "interval_min": 0, "interval_max": 1},
    )

    create_resp = registration_routes.create_external_registration_batch(request)
    status_resp = registration_routes.get_external_registration_batch("b-1")
    cancel_resp = registration_routes.cancel_external_registration_batch("b-1")

    assert create_resp["batch_uuid"] == "b-1"
    assert status_resp["status"] == "running"
    assert cancel_resp["status"] == "cancelled"


def test_external_registration_create_rejects_invalid_interval_bounds(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/external/registration/batches",
        headers={"X-API-Key": "abc"},
        json={
            "count": 2,
            "email": {"type": "temp_mail"},
            "upload": {"enabled": False},
            "execution": {"mode": "pipeline", "concurrency": 1, "interval_min": 5, "interval_max": 2},
        },
    )

    assert response.status_code == 422
    assert "interval_max" in response.text


def test_external_registration_create_requires_upload_provider_when_enabled(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/external/registration/batches",
        headers={"X-API-Key": "abc"},
        json={
            "count": 1,
            "email": {"type": "temp_mail"},
            "upload": {"enabled": True},
            "execution": {"mode": "pipeline", "concurrency": 1, "interval_min": 0, "interval_max": 0},
        },
    )

    assert response.status_code == 422
    assert "upload.provider" in response.text


def test_external_registration_create_rejects_extra_fields(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/external/registration/batches",
        headers={"X-API-Key": "abc"},
        json={
            "count": 1,
            "email": {"type": "temp_mail", "unexpected": True},
            "upload": {"enabled": False},
            "execution": {"mode": "pipeline", "concurrency": 1, "interval_min": 0, "interval_max": 0},
        },
    )

    assert response.status_code == 422
    assert "unexpected" in response.text


def test_external_registration_create_returns_200_for_idempotent_replay(monkeypatch):
    monkeypatch.setattr(
        registration_routes,
        "_create_external_batch",
        lambda payload, background_tasks=None: {
            "batch_uuid": "b-1",
            "status": "pending",
            "requested_count": 1,
            "idempotent_replay": True,
        },
    )
    client = _client(monkeypatch)

    response = client.post(
        "/external/registration/batches",
        headers={"X-API-Key": "abc"},
        json={
            "count": 1,
            "idempotency_key": "same-batch",
            "email": {"type": "temp_mail"},
            "upload": {"enabled": False},
            "execution": {"mode": "pipeline", "concurrency": 1, "interval_min": 0, "interval_max": 0},
        },
    )

    assert response.status_code == 200
    assert response.json()["idempotent_replay"] is True


def test_external_registration_status_maps_batch_not_found_to_404(monkeypatch):
    monkeypatch.setattr(registration_routes, "_get_external_batch", lambda batch_uuid: (_ for _ in ()).throw(ValueError("batch_not_found")))
    client = _client(monkeypatch)

    response = client.get("/external/registration/batches/missing", headers={"X-API-Key": "abc"})

    assert response.status_code == 404
    assert response.json()["detail"] == "batch_not_found"


def test_external_registration_create_maps_service_unavailable_to_503(monkeypatch):
    monkeypatch.setattr(registration_routes, "_create_external_batch", lambda payload, background_tasks=None: (_ for _ in ()).throw(RuntimeError("external_batch_service_unavailable")))
    client = _client(monkeypatch)

    response = client.post(
        "/external/registration/batches",
        headers={"X-API-Key": "abc"},
        json={
            "count": 1,
            "email": {"type": "temp_mail"},
            "upload": {"enabled": False},
            "execution": {"mode": "pipeline", "concurrency": 1, "interval_min": 0, "interval_max": 0},
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "external_batch_service_unavailable"


def test_external_registration_cancel_maps_finished_batch_to_400(monkeypatch):
    monkeypatch.setattr(registration_routes, "_cancel_external_batch", lambda batch_uuid: (_ for _ in ()).throw(ValueError("batch is already finished")))
    client = _client(monkeypatch)

    response = client.post("/external/registration/batches/b-1/cancel", headers={"X-API-Key": "abc"})

    assert response.status_code == 400
    assert response.json()["detail"] == "batch is already finished"


def test_external_registration_create_returns_failure_category(monkeypatch):
    monkeypatch.setattr(
        registration_routes,
        "_create_external_batch",
        lambda payload, background_tasks=None: {
            "batch_uuid": "b-create",
            "status": "pending",
            "requested_count": 1,
            "failure_category": None,
            "idempotent_replay": False,
        },
    )
    client = _client(monkeypatch)

    response = client.post(
        "/external/registration/batches",
        headers={"X-API-Key": "abc"},
        json={
            "count": 1,
            "email": {"type": "temp_mail"},
            "upload": {"enabled": False},
            "execution": {"mode": "pipeline", "concurrency": 1, "interval_min": 0, "interval_max": 0},
        },
    )

    assert response.status_code == 202
    assert "failure_category" in response.json()
    assert response.json()["failure_category"] is None


def test_external_registration_get_returns_failure_category(monkeypatch):
    monkeypatch.setattr(
        registration_routes,
        "_get_external_batch",
        lambda batch_uuid: {
            "batch_uuid": batch_uuid,
            "status": "failed",
            "failure_category": "transient",
        },
    )
    client = _client(monkeypatch)
    batch_uuid = "b-get"

    response = client.get(f"/external/registration/batches/{batch_uuid}", headers={"X-API-Key": "abc"})

    assert response.status_code == 200
    assert response.json()["batch_uuid"] == batch_uuid
    assert "failure_category" in response.json()
    assert response.json()["failure_category"] == "transient"


def test_external_registration_cancel_returns_failure_category(monkeypatch):
    monkeypatch.setattr(
        registration_routes,
        "_cancel_external_batch",
        lambda batch_uuid: {
            "batch_uuid": batch_uuid,
            "status": "cancelled",
            "failure_category": None,
        },
    )
    client = _client(monkeypatch)
    batch_uuid = "b-cancel"

    response = client.post(f"/external/registration/batches/{batch_uuid}/cancel", headers={"X-API-Key": "abc"})

    assert response.status_code == 200
    assert "failure_category" in response.json()
    assert response.json()["failure_category"] is None


def test_external_registration_idempotent_replay_returns_failure_category(monkeypatch):
    monkeypatch.setattr(
        registration_routes,
        "_create_external_batch",
        lambda payload, background_tasks=None: {
            "batch_uuid": "b-idempotent",
            "status": "failed",
            "requested_count": 1,
            "failure_category": "transient",
            "idempotent_replay": True,
        },
    )
    client = _client(monkeypatch)
    payload = {
        "count": 1,
        "idempotency_key": "unique-key",
        "email": {"type": "temp_mail"},
        "upload": {"enabled": False},
        "execution": {"mode": "pipeline", "concurrency": 1, "interval_min": 0, "interval_max": 0},
    }

    response = client.post("/external/registration/batches", headers={"X-API-Key": "abc"}, json=payload)

    assert response.status_code == 200
    assert response.json()["idempotent_replay"] is True
    assert response.json()["failure_category"] == "transient"


def test_external_registration_get_backfills_legacy_failed_batch_failure_category(monkeypatch):
    manager = _make_db("external_api_routes_backfill_get.db")
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
        external_batch_crud.update_batch(
            session,
            batch.batch_uuid,
            status="failed",
            failure_reason=None,
            failure_category=None,
        )
        external_batch_crud.update_batch_item(
            session,
            item.id,
            status="failed",
            failure_reason="no_available_email_service",
        )

    @contextmanager
    def _db_context():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.database.session.get_db", _db_context)
    client = _client(monkeypatch)

    response = client.get(f"/external/registration/batches/{batch.batch_uuid}", headers={"X-API-Key": "abc"})

    with manager.session_scope() as session:
        refreshed = external_batch_crud.get_batch_by_uuid(session, batch.batch_uuid)

    assert response.status_code == 200
    assert response.json()["failure_category"] == "transient"
    assert refreshed.failure_category == "transient"
