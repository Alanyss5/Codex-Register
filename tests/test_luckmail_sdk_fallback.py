import builtins
import sys
from pathlib import Path

import pytest

from src.services.luckmail_mail import _load_luckmail_client_class


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_luckmail_loader_falls_back_to_builtin_client(monkeypatch):
    import src.services.luckmail_mail as luckmail_module

    runtime_root = Path("tests_runtime") / "luckmail_builtin_loader"
    module_path = runtime_root / "repo" / "src" / "services" / "luckmail_mail.py"
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text("# fake module path", encoding="utf-8")

    monkeypatch.setattr(luckmail_module, "__file__", str(module_path))
    sys.modules.pop("luckmail", None)

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "luckmail":
            raise ModuleNotFoundError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    loaded = _load_luckmail_client_class()

    assert loaded is not None
    assert loaded.__name__ == "LuckMailClient"
    assert loaded.__module__ == "src.services.luckmail_sdk"


def test_builtin_luckmail_client_supports_auth_retry_and_token_noauth(monkeypatch):
    from src.services.luckmail_sdk import LuckMailClient

    client = LuckMailClient(base_url="https://mails.luckyous.com/", api_key="luck-key")
    calls = []
    responses = [
        _FakeResponse(401, {"code": 1002, "message": "unauthorized"}),
        _FakeResponse(200, {"code": 0, "message": "success", "data": {"balance": "12.5000"}}),
        _FakeResponse(
            200,
            {
                "code": 0,
                "message": "success",
                "data": {
                    "email_address": "user@example.com",
                    "has_new_mail": True,
                    "verification_code": "482910",
                },
            },
        ),
        _FakeResponse(
            200,
            {
                "code": 0,
                "message": "success",
                "data": {
                    "email_address": "user@example.com",
                    "project": "openai",
                    "mails": [
                        {
                            "message_id": "msg_1",
                            "subject": "Your code is 482910",
                            "body": "Your code is 482910",
                            "html_body": "<p>482910</p>",
                        }
                    ],
                },
            },
        ),
        _FakeResponse(200, {"code": 0, "message": "success", "data": None}),
    ]

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses[len(calls) - 1]

    monkeypatch.setattr(client.http_client, "request", fake_request)

    balance = client.user.get_balance()
    token_code = client.user.get_token_code("tok_demo")
    token_mails = client.user.get_token_mails("tok_demo")
    client.user.set_purchase_disabled(12, 1)

    assert balance.balance == "12.5000"
    assert token_code.has_new_mail is True
    assert token_code.verification_code == "482910"
    assert token_mails.email_address == "user@example.com"
    assert len(token_mails.mails) == 1
    assert token_mails.mails[0].message_id == "msg_1"

    assert calls[0][0] == "GET"
    assert calls[0][1].endswith("/api/v1/openapi/balance")
    assert calls[0][2]["headers"]["X-API-Key"] == "luck-key"
    assert "Authorization" not in calls[0][2]["headers"]

    assert calls[1][2]["headers"]["Authorization"] == "Bearer luck-key"
    assert "X-API-Key" not in calls[1][2]["headers"]

    assert calls[2][1].endswith("/api/v1/openapi/email/token/tok_demo/code")
    assert calls[2][2]["headers"] == {}

    assert calls[3][0] == "GET"
    assert calls[3][1].endswith("/api/v1/openapi/email/token/tok_demo/mails")
    assert calls[3][2]["headers"] == {}

    assert calls[4][0] == "PUT"
    assert calls[4][1].endswith("/api/v1/openapi/email/purchases/12/disabled")
    assert calls[4][2]["json"] == {"disabled": 1}


def test_builtin_luckmail_client_wraps_paginated_lists(monkeypatch):
    from src.services.luckmail_sdk import LuckMailClient

    client = LuckMailClient(base_url="https://mails.luckyous.com/", api_key="luck-key")

    def fake_request(method, url, **kwargs):
        return _FakeResponse(
            200,
            {
                "code": 0,
                "message": "success",
                "data": {
                    "list": [
                        {
                            "id": 1,
                            "email_address": "user1@example.com",
                            "token": "tok_1",
                            "user_disabled": 0,
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "page_size": 20,
                },
            },
        )

    monkeypatch.setattr(client.http_client, "request", fake_request)

    result = client.user.get_purchases(page=1, page_size=20, user_disabled=0)

    assert result.total == 1
    assert result.page == 1
    assert result.page_size == 20
    assert len(result.list) == 1
    assert result.list[0].email_address == "user1@example.com"
    assert result.list[0].token == "tok_1"
