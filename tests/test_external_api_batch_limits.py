from src.web.routes.external import registration as registration_routes

from tests.test_external_api_routes import _client


def test_external_registration_create_allows_count_above_100(monkeypatch):
    monkeypatch.setattr(
        registration_routes,
        "_create_external_batch",
        lambda payload, background_tasks=None: {
            "batch_uuid": "b-101",
            "status": "pending",
            "requested_count": payload["count"],
            "idempotent_replay": False,
        },
    )
    client = _client(monkeypatch)

    response = client.post(
        "/external/registration/batches",
        headers={"X-API-Key": "abc"},
        json={
            "count": 101,
            "email": {"type": "temp_mail"},
            "upload": {"enabled": False},
            "execution": {"mode": "pipeline", "concurrency": 1, "interval_min": 0, "interval_max": 0},
        },
    )

    assert response.status_code == 202
    assert response.json()["requested_count"] == 101
