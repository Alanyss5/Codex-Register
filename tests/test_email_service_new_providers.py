import builtins
import sys
import types
from pathlib import Path

import pytest

import src.services  # noqa: F401 - ensure service registrations run
from src.config.constants import EmailServiceType
from src.services.base import EmailServiceFactory
from src.services.cloudmail import CloudMailService
from src.services.luckmail_mail import LuckMailService, _load_luckmail_client_class
from src.services.yyds_mail import YYDSMailService


def test_email_service_type_includes_new_provider_values():
    assert EmailServiceType("yyds_mail") is EmailServiceType.YYDS_MAIL
    assert EmailServiceType("cloudmail") is EmailServiceType.CLOUDMAIL
    assert EmailServiceType("luckmail") is EmailServiceType.LUCKMAIL


def test_email_service_factory_registers_new_provider_classes():
    assert EmailServiceFactory.get_service_class(EmailServiceType.YYDS_MAIL) is YYDSMailService
    assert EmailServiceFactory.get_service_class(EmailServiceType.CLOUDMAIL) is CloudMailService
    assert EmailServiceFactory.get_service_class(EmailServiceType.LUCKMAIL) is LuckMailService


def test_cloudmail_defaults_enable_prefix_true():
    service = CloudMailService(
        {
            "base_url": "https://mail.example.test",
            "admin_password": "secret",
            "domain": "example.test",
        }
    )

    assert service.config["enable_prefix"] is True
    assert service.service_type is EmailServiceType.CLOUDMAIL


def test_cloudmail_allows_explicit_enable_prefix_override():
    service = CloudMailService(
        {
            "base_url": "https://mail.example.test",
            "admin_password": "secret",
            "domain": "example.test",
            "enable_prefix": False,
        }
    )

    assert service.config["enable_prefix"] is False


def test_yyds_mail_uses_distinct_service_type():
    service = YYDSMailService(
        {
            "base_url": "https://mail.example.test",
            "api_key": "key-1",
            "default_domain": "example.test",
        }
    )

    assert service.service_type is EmailServiceType.YYDS_MAIL


def test_luckmail_loader_uses_installed_module(monkeypatch):
    class DummyLuckMailClient:
        pass

    module = types.ModuleType("luckmail")
    module.LuckMailClient = DummyLuckMailClient
    monkeypatch.setitem(sys.modules, "luckmail", module)

    loaded = _load_luckmail_client_class()

    assert loaded is DummyLuckMailClient


def test_luckmail_loader_supports_local_vendored_directory(monkeypatch):
    runtime_root = Path("tests_runtime") / "luckmail_vendor_loader"
    if runtime_root.exists():
        import shutil

        shutil.rmtree(runtime_root)

    module_path = runtime_root / "repo" / "src" / "services" / "luckmail_mail.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("# fake module path", encoding="utf-8")

    vendored_repo = runtime_root / "repo" / "luckmail"
    package_dir = vendored_repo / "luckmail"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(
        "class LuckMailClient:\n    pass\n",
        encoding="utf-8",
    )

    import src.services.luckmail_mail as luckmail_module

    monkeypatch.setattr(luckmail_module, "__file__", str(module_path))

    sys.modules.pop("luckmail", None)

    real_import = builtins.__import__
    vendored_repo_resolved = str(vendored_repo.resolve())

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "luckmail":
            normalized_sys_paths = {str(Path(p).resolve()) for p in sys.path if p}
            if vendored_repo_resolved in normalized_sys_paths:
                return real_import(name, globals, locals, fromlist, level)
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    loaded = _load_luckmail_client_class()

    assert loaded is not None
    assert loaded.__name__ == "LuckMailClient"


def test_luckmail_service_raises_clear_error_when_backend_missing(monkeypatch):
    monkeypatch.setattr("src.services.luckmail_mail._load_luckmail_client_class", lambda: None)

    with pytest.raises(ValueError, match="LuckMail SDK"):
        LuckMailService(
            {
                "base_url": "https://mails.luckyous.com/",
                "api_key": "key-1",
                "project_code": "openai",
            }
        )
