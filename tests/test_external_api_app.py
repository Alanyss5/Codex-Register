import builtins
import shutil
import sys
import tempfile
import types
from pathlib import Path

from fastapi.testclient import TestClient

import webui
import src.config.settings as settings_module
import src.database.session as db_session
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


def test_start_webui_uses_preloaded_app_instance(monkeypatch):
    fake_app = object()

    class _Settings:
        webui_host = "127.0.0.1"
        webui_port = 1455
        debug = False

    fake_module = types.ModuleType("src.web.app")
    fake_module.app = fake_app
    captured = {}

    monkeypatch.setattr(webui, "setup_application", lambda: _Settings())
    monkeypatch.setitem(sys.modules, "src.web.app", fake_module)
    monkeypatch.setattr(webui.uvicorn, "run", lambda **kwargs: captured.update(kwargs))

    webui.start_webui()

    assert captured["app"] is fake_app
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 1455


def test_setup_application_refreshes_stale_settings_cache(monkeypatch):
    from src.database.init_db import initialize_database

    temp_root = Path(tempfile.mkdtemp())
    try:
        data_dir = temp_root / "data"
        logs_dir = temp_root / "logs"
        data_dir.mkdir()
        logs_dir.mkdir()

        monkeypatch.setattr(webui, "project_root", temp_root)
        monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
        monkeypatch.setenv("APP_LOGS_DIR", str(logs_dir))
        monkeypatch.setattr(db_session, "_db_manager", None)
        monkeypatch.setattr(settings_module, "_settings", None)
        monkeypatch.setattr(webui, "setup_logging", lambda **kwargs: None)

        initialize_database()
        settings_module.update_settings(
            external_api_enabled=True,
            external_api_key="test-external-key",
        )

        monkeypatch.setattr(
            settings_module,
            "_settings",
            settings_module.Settings(external_api_enabled=False),
        )

        settings = webui.setup_application()

        assert settings.external_api_enabled is True
        assert settings.external_api_key.get_secret_value() == "test-external-key"
        db_session._db_manager.engine.dispose()
        monkeypatch.setattr(db_session, "_db_manager", None)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
