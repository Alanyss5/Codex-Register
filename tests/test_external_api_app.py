import builtins

from fastapi.testclient import TestClient

from src.web.app import create_app
from src.web.routes import api_router


def test_api_router_mounts_external_routes():
    paths = {route.path for route in api_router.routes}
    assert "/external/capabilities" in paths
    assert "/external/registration/batches" in paths


def test_app_startup_tolerates_missing_external_recovery(monkeypatch):
    import src.database.init_db as init_db
    import src.database.session as db_session

    monkeypatch.setattr(init_db, "initialize_database", lambda: None)

    class _DummyDbContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(db_session, "get_db", lambda: _DummyDbContext())

    original_import = builtins.__import__

    def _patched_import(name, globals=None, locals=None, fromlist=(), level=0):
        if "external_batches.recovery" in name:
            raise ModuleNotFoundError(name)
        if name.endswith("external_batches") and fromlist and "recovery" in fromlist:
            raise ModuleNotFoundError(name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _patched_import)

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/login")
        assert response.status_code == 200
