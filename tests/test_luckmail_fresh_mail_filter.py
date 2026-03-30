from types import SimpleNamespace

import pytest

from src.services.luckmail_mail import LuckMailService


def _mail(message_id: str, code: str) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        subject=f"Your OpenAI verification code is {code}",
        body="",
        html_body="",
    )


class _FakeLuckMailUser:
    def __init__(self):
        self.purchase_calls = 0
        self.get_purchases_calls = 0
        self._token_code_result = SimpleNamespace(has_new_mail=False, verification_code=None)
        self._token_mails = []

    def purchase_emails(self, **kwargs):
        self.purchase_calls += 1
        return {
            "purchases": [
                {
                    "id": 101,
                    "email_address": "fresh@example.com",
                    "token": "tok_fresh",
                }
            ]
        }

    def get_purchases(self, **kwargs):
        self.get_purchases_calls += 1
        return SimpleNamespace(
            list=[
                SimpleNamespace(
                    id=88,
                    email_address="reused@example.com",
                    token="tok_reused",
                    project_name="openai",
                )
            ]
        )

    def get_token_code(self, token):
        return self._token_code_result

    def get_token_mails(self, token):
        return SimpleNamespace(mails=list(self._token_mails))


class _FakeLuckMailClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self.user = _FakeLuckMailUser()


@pytest.fixture
def luckmail_service(monkeypatch):
    import src.services.luckmail_mail as luckmail_module

    monkeypatch.setattr(luckmail_module, "_load_luckmail_client_class", lambda: _FakeLuckMailClient)
    service = LuckMailService(
        {
            "base_url": "https://mails.example.com/",
            "api_key": "luck-key",
            "project_code": "openai",
        }
    )
    return service


def test_openai_purchase_mode_still_allows_explicit_reuse_opt_in(monkeypatch):
    import src.services.luckmail_mail as luckmail_module

    monkeypatch.setattr(luckmail_module, "_load_luckmail_client_class", lambda: _FakeLuckMailClient)
    service = LuckMailService(
        {
            "base_url": "https://mails.example.com/",
            "api_key": "luck-key",
            "project_code": "openai",
            "reuse_existing_purchases": True,
        }
    )

    email_info = service.create_email()

    assert service.client.user.get_purchases_calls == 1
    assert service.client.user.purchase_calls == 0
    assert email_info["email"] == "reused@example.com"
    assert email_info["source"] == "reuse_purchase"


def test_openai_purchase_mode_buys_fresh_mailbox_and_snapshots_existing_mail_ids(luckmail_service):
    user = luckmail_service.client.user
    user._token_mails = [_mail("existing-1", "111111")]

    email_info = luckmail_service.create_email()

    assert user.get_purchases_calls == 0
    assert user.purchase_calls == 1
    assert email_info["source"] == "new_purchase"
    assert email_info["message_ids_snapshot"] == {"existing-1"}


def test_get_verification_code_ignores_stale_purchase_code_without_new_mail(luckmail_service, monkeypatch):
    import src.services.luckmail_mail as luckmail_module

    user = luckmail_service.client.user
    user._token_code_result = SimpleNamespace(has_new_mail=False, verification_code="111111")
    user._token_mails = [_mail("existing-1", "111111")]

    luckmail_service._cache_order(
        {
            "id": "tok_fresh",
            "service_id": "tok_fresh",
            "order_no": "",
            "email": "fresh@example.com",
            "token": "tok_fresh",
            "purchase_id": 101,
            "inbox_mode": "purchase",
            "message_ids_snapshot": {"existing-1"},
        }
    )

    clock = {"value": 0.0}

    def fake_time():
        clock["value"] += 0.6
        return clock["value"]

    monkeypatch.setattr(luckmail_module.time, "time", fake_time)
    monkeypatch.setattr(luckmail_module.time, "sleep", lambda _: None)

    assert luckmail_service.get_verification_code("fresh@example.com", email_id="tok_fresh", timeout=1) is None


def test_get_verification_code_prefers_code_from_new_mail_ids_over_stale_token_code(luckmail_service):
    user = luckmail_service.client.user
    user._token_code_result = SimpleNamespace(has_new_mail=True, verification_code="111111")
    user._token_mails = [
        _mail("existing-1", "111111"),
        _mail("new-2", "222222"),
    ]

    luckmail_service._cache_order(
        {
            "id": "tok_fresh",
            "service_id": "tok_fresh",
            "order_no": "",
            "email": "fresh@example.com",
            "token": "tok_fresh",
            "purchase_id": 101,
            "inbox_mode": "purchase",
            "message_ids_snapshot": {"existing-1"},
        }
    )

    assert luckmail_service.get_verification_code("fresh@example.com", email_id="tok_fresh", timeout=1) == "222222"


def test_get_verification_code_falls_back_to_fresh_token_code_when_mail_list_lags(luckmail_service):
    user = luckmail_service.client.user
    user._token_code_result = SimpleNamespace(has_new_mail=True, verification_code="333333")
    user._token_mails = [_mail("existing-1", "111111")]

    luckmail_service._cache_order(
        {
            "id": "tok_fresh",
            "service_id": "tok_fresh",
            "order_no": "",
            "email": "fresh@example.com",
            "token": "tok_fresh",
            "purchase_id": 101,
            "inbox_mode": "purchase",
            "message_ids_snapshot": {"existing-1"},
        }
    )

    assert luckmail_service.get_verification_code("fresh@example.com", email_id="tok_fresh", timeout=1) == "333333"


def test_get_verification_code_does_not_use_raw_token_code_for_custom_context_pattern_when_mail_list_lags(
    luckmail_service, monkeypatch
):
    import src.services.luckmail_mail as luckmail_module

    user = luckmail_service.client.user
    user._token_code_result = SimpleNamespace(has_new_mail=True, verification_code="333333")
    user._token_mails = [_mail("existing-1", "111111")]

    luckmail_service._cache_order(
        {
            "id": "tok_fresh",
            "service_id": "tok_fresh",
            "order_no": "",
            "email": "fresh@example.com",
            "token": "tok_fresh",
            "purchase_id": 101,
            "inbox_mode": "purchase",
            "message_ids_snapshot": {"existing-1"},
            "message_ids_snapshot_trusted": True,
        }
    )

    clock = {"value": 0.0}

    def fake_time():
        clock["value"] += 0.6
        return clock["value"]

    monkeypatch.setattr(luckmail_module.time, "time", fake_time)
    monkeypatch.setattr(luckmail_module.time, "sleep", lambda _: None)

    assert (
        luckmail_service.get_verification_code(
            "fresh@example.com",
            email_id="tok_fresh",
            timeout=1,
            pattern=r"OpenAI verification code is (\d{6})",
        )
        is None
    )


def test_get_verification_code_skips_unrelated_new_mail_until_pattern_matches(luckmail_service):
    user = luckmail_service.client.user
    user._token_code_result = SimpleNamespace(has_new_mail=True, verification_code="654321")
    user._token_mails = [
        SimpleNamespace(
            message_id="new-1",
            subject="Your order number is 123456",
            body="",
            html_body="",
        ),
        SimpleNamespace(
            message_id="new-2",
            subject="OpenAI verification code is 654321",
            body="",
            html_body="",
        ),
    ]

    luckmail_service._cache_order(
        {
            "id": "tok_fresh",
            "service_id": "tok_fresh",
            "order_no": "",
            "email": "fresh@example.com",
            "token": "tok_fresh",
            "purchase_id": 101,
            "inbox_mode": "purchase",
            "message_ids_snapshot": {"existing-1"},
        }
    )

    assert (
        luckmail_service.get_verification_code(
            "fresh@example.com",
            email_id="tok_fresh",
            timeout=1,
            pattern=r"OpenAI verification code is (\d{6})",
        )
        == "654321"
    )


def test_get_verification_code_ignores_mail_without_message_id_when_freshness_cannot_be_proved(
    luckmail_service, monkeypatch
):
    import src.services.luckmail_mail as luckmail_module

    user = luckmail_service.client.user
    user._token_code_result = SimpleNamespace(has_new_mail=False, verification_code="111111")
    user._token_mails = [
        SimpleNamespace(
            message_id="",
            subject="OpenAI verification code is 111111",
            body="",
            html_body="",
        )
    ]

    luckmail_service._cache_order(
        {
            "id": "tok_fresh",
            "service_id": "tok_fresh",
            "order_no": "",
            "email": "fresh@example.com",
            "token": "tok_fresh",
            "purchase_id": 101,
            "inbox_mode": "purchase",
            "message_ids_snapshot": set(),
            "message_ids_snapshot_trusted": True,
        }
    )

    clock = {"value": 0.0}

    def fake_time():
        clock["value"] += 0.6
        return clock["value"]

    monkeypatch.setattr(luckmail_module.time, "time", fake_time)
    monkeypatch.setattr(luckmail_module.time, "sleep", lambda _: None)

    assert luckmail_service.get_verification_code("fresh@example.com", email_id="tok_fresh", timeout=1) is None


def test_get_verification_code_rebuilds_baseline_when_initial_snapshot_was_untrusted(
    luckmail_service, monkeypatch
):
    import src.services.luckmail_mail as luckmail_module

    user = luckmail_service.client.user
    user._token_code_result = SimpleNamespace(has_new_mail=False, verification_code="111111")
    user._token_mails = [_mail("old-1", "111111")]

    luckmail_service._cache_order(
        {
            "id": "tok_fresh",
            "service_id": "tok_fresh",
            "order_no": "",
            "email": "fresh@example.com",
            "token": "tok_fresh",
            "purchase_id": 101,
            "inbox_mode": "purchase",
            "message_ids_snapshot": set(),
            "message_ids_snapshot_trusted": False,
        }
    )

    clock = {"value": 0.0}

    def fake_time():
        clock["value"] += 0.6
        return clock["value"]

    monkeypatch.setattr(luckmail_module.time, "time", fake_time)
    monkeypatch.setattr(luckmail_module.time, "sleep", lambda _: None)

    assert luckmail_service.get_verification_code("fresh@example.com", email_id="tok_fresh", timeout=1) is None
    order_info = luckmail_service._find_order("fresh@example.com", "tok_fresh")
    assert order_info["message_ids_snapshot"] == {"old-1"}
    assert order_info["message_ids_snapshot_trusted"] is True


def test_get_verification_code_does_not_treat_direct_token_polling_without_snapshot_as_fresh_mail(
    luckmail_service, monkeypatch
):
    import src.services.luckmail_mail as luckmail_module

    user = luckmail_service.client.user
    user._token_code_result = SimpleNamespace(has_new_mail=False, verification_code="111111")
    user._token_mails = [_mail("old-1", "111111")]

    clock = {"value": 0.0}

    def fake_time():
        clock["value"] += 0.6
        return clock["value"]

    monkeypatch.setattr(luckmail_module.time, "time", fake_time)
    monkeypatch.setattr(luckmail_module.time, "sleep", lambda _: None)

    assert luckmail_service.get_verification_code("fresh@example.com", email_id="tok_fresh", timeout=1) is None
