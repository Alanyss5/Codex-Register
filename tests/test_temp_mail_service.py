import pytest

from src.services.temp_mail import TempMailService


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError(f"missing fake response for {method} {url}")
        return self.responses.pop(0)


RAW_OPENAI_MAIL = """From: OpenAI <noreply@openai.com>
Subject: Your verification code
Content-Type: text/plain; charset=utf-8

Your OpenAI verification code is 654321
"""

RAW_OPENAI_MAIL_WITH_DOMAIN_DIGITS = """To: tester@889110.xyz
From: OpenAI <noreply@openai.com>
Subject: Verify your email
Content-Type: text/plain; charset=utf-8

Your OpenAI verification code is 456789
"""


def _install_fake_clock(monkeypatch, start=0.0):
    clock = {"value": float(start)}

    def fake_time():
        return clock["value"]

    def fake_sleep(seconds):
        clock["value"] += float(seconds)

    monkeypatch.setattr("src.services.temp_mail.time.time", fake_time)
    monkeypatch.setattr("src.services.temp_mail.time.sleep", fake_sleep)
    return clock


def test_get_verification_code_falls_back_to_admin_when_user_api_token_is_rejected():
    service = TempMailService(
        {
            "base_url": "https://apmail.889110.xyz",
            "admin_password": "admin888",
            "domain": "fallback.example.com",
        }
    )
    email = "tmpabc123@fallback.example.com"
    service._email_cache[email] = {"email": email, "jwt": "expired-jwt"}
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(status_code=401, payload={"error": "Your token has expired, please login again"}),
            FakeResponse(
                payload={
                    "results": [
                        {
                            "id": "mail-1",
                            "source": "OpenAI <noreply@tm.openai.com>",
                            "subject": "OpenAI verification code",
                            "text": "Your OpenAI verification code is 654321",
                        }
                    ],
                    "total": 1,
                }
            ),
        ]
    )

    code = service.get_verification_code(email, timeout=3)

    assert code == "654321"
    assert service._email_cache[email]["disable_user_api"] is True
    assert service.http_client.calls[0]["url"] == "https://apmail.889110.xyz/api/mails"
    assert service.http_client.calls[1]["url"] == "https://apmail.889110.xyz/admin/mails"
    assert service.http_client.calls[1]["kwargs"]["params"]["address"] == email


def test_get_verification_code_skips_user_api_after_token_marked_unavailable():
    service = TempMailService(
        {
            "base_url": "https://apmail.889110.xyz",
            "admin_password": "admin888",
            "domain": "fallback.example.com",
        }
    )
    email = "tmpabc123@fallback.example.com"
    service._email_cache[email] = {
        "email": email,
        "jwt": "expired-jwt",
        "disable_user_api": True,
    }
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(
                payload={
                    "results": [
                        {
                            "id": "mail-2",
                            "source": "OpenAI <noreply@tm.openai.com>",
                            "subject": "OpenAI verification code",
                            "text": "Use code 123456 to continue",
                        }
                    ],
                    "total": 1,
                }
            ),
        ]
    )

    code = service.get_verification_code(email, timeout=3)

    assert code == "123456"
    assert len(service.http_client.calls) == 1
    assert service.http_client.calls[0]["url"] == "https://apmail.889110.xyz/admin/mails"


def test_get_verification_code_prefers_api_mails_with_address_jwt(monkeypatch):
    _install_fake_clock(monkeypatch)

    service = TempMailService(
        {
            "base_url": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["example.test"],
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"address": "tester@example.test", "jwt": "address-jwt-123"}),
            FakeResponse(
                payload={
                    "results": [
                        {
                            "id": "mail-1",
                            "raw": RAW_OPENAI_MAIL,
                            "address": "tester@example.test",
                        }
                    ],
                    "total": 1,
                }
            ),
        ]
    )

    email_info = service.create_email()
    code = service.get_verification_code(email_info["email"], timeout=10)

    assert code == "654321"
    assert service.http_client.calls[1]["url"] == "https://mail.example.test/api/mails"
    assert service.http_client.calls[1]["kwargs"]["headers"]["Authorization"] == "Bearer address-jwt-123"


def test_get_verification_code_falls_back_to_admin_when_api_mails_fails(monkeypatch):
    _install_fake_clock(monkeypatch)

    service = TempMailService(
        {
            "base_url": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["example.test"],
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"address": "tester@example.test", "jwt": "address-jwt-123"}),
            FakeResponse(status_code=401, payload={"error": "unauthorized"}),
            FakeResponse(
                payload={
                    "results": [
                        {
                            "id": "mail-2",
                            "source": "noreply@openai.com",
                            "subject": "Your verification code",
                            "text": "Your OpenAI verification code is 112233",
                            "address": "tester@example.test",
                        }
                    ],
                    "total": 1,
                }
            ),
        ]
    )

    email_info = service.create_email()
    code = service.get_verification_code(email_info["email"], timeout=10)

    assert code == "112233"
    assert service.http_client.calls[1]["url"] == "https://mail.example.test/api/mails"
    assert service.http_client.calls[2]["url"] == "https://mail.example.test/admin/mails"


def test_get_verification_code_ignores_domain_digits_in_raw_headers(monkeypatch):
    _install_fake_clock(monkeypatch)

    service = TempMailService(
        {
            "base_url": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["889110.xyz"],
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"address": "tester@889110.xyz", "jwt": "address-jwt-123"}),
            FakeResponse(
                payload={
                    "results": [
                        {
                            "id": "mail-3",
                            "source": "noreply@openai.com",
                            "subject": "Verify your email",
                            "text": "Thanks for signing up.",
                            "raw": RAW_OPENAI_MAIL_WITH_DOMAIN_DIGITS,
                            "address": "tester@889110.xyz",
                        }
                    ],
                    "total": 1,
                }
            ),
        ]
    )

    email_info = service.create_email()
    code = service.get_verification_code(email_info["email"], timeout=10)

    assert code == "456789"


def test_get_verification_code_prefers_mail_received_after_otp_sent(monkeypatch):
    _install_fake_clock(monkeypatch)

    service = TempMailService(
        {
            "base_url": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["example.test"],
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"address": "tester@example.test", "jwt": "address-jwt-123"}),
            FakeResponse(
                payload={
                    "results": [
                        {
                            "id": "mail-old",
                            "source": "noreply@openai.com",
                            "subject": "Your verification code",
                            "text": "Your OpenAI verification code is 111111",
                            "createdAt": 1700000000,
                            "address": "tester@example.test",
                        },
                        {
                            "id": "mail-new",
                            "source": "noreply@openai.com",
                            "subject": "Your verification code",
                            "text": "Your OpenAI verification code is 222222",
                            "createdAt": 1700000020,
                            "address": "tester@example.test",
                        },
                    ],
                    "total": 2,
                }
            ),
        ]
    )

    email_info = service.create_email()
    code = service.get_verification_code(email_info["email"], timeout=10, otp_sent_at=1700000010)

    assert code == "222222"


def test_relogin_stage_does_not_reuse_consumed_signup_mail(monkeypatch):
    _install_fake_clock(monkeypatch)

    service = TempMailService(
        {
            "base_url": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["example.test"],
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"address": "tester@example.test", "jwt": "address-jwt-123"}),
            FakeResponse(
                payload={
                    "results": [
                        {
                            "id": "mail-signup",
                            "source": "noreply@openai.com",
                            "subject": "Your verification code",
                            "text": "Your OpenAI verification code is 111111",
                            "address": "tester@example.test",
                        }
                    ],
                    "total": 1,
                }
            ),
            FakeResponse(
                payload={
                    "results": [
                        {
                            "id": "mail-signup",
                            "source": "noreply@openai.com",
                            "subject": "Your verification code",
                            "text": "Your OpenAI verification code is 111111",
                            "address": "tester@example.test",
                        }
                    ],
                    "total": 1,
                }
            ),
        ]
    )

    email_info = service.create_email()
    first_code = service.get_verification_code(email_info["email"], timeout=1)
    service.set_verification_stage(email_info["email"], "relogin_otp")
    second_code = service.get_verification_code(
        email_info["email"],
        timeout=1,
        otp_sent_at=1700000010,
    )

    assert first_code == "111111"
    assert second_code is None


def test_create_email_uses_worker_domains_and_default_no_prefix():
    service = TempMailService(
        {
            "base_url": "https://apmail.889110.xyz",
            "admin_password": "admin888",
            "domain": "fallback.example.com",
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"domains": ["d1.example.com", "d2.example.com"]}),
            FakeResponse(payload={"address": "abc123@d2.example.com", "jwt": "jwt-1"}),
        ]
    )

    created = service.create_email()

    assert created["email"].endswith("@d2.example.com") or created["email"].endswith("@d1.example.com")

    domains_call = service.http_client.calls[0]
    assert domains_call["method"] == "GET"
    assert domains_call["url"] == "https://apmail.889110.xyz/admin/domains"

    create_call = service.http_client.calls[1]
    assert create_call["method"] == "POST"
    payload = create_call["kwargs"]["json"]
    assert payload["enablePrefix"] is False
    assert not payload["name"].startswith("tmp")
    assert payload["domain"] in {"d1.example.com", "d2.example.com"}


def test_create_email_falls_back_to_config_domain_when_domains_api_fails():
    service = TempMailService(
        {
            "base_url": "https://apmail.889110.xyz",
            "admin_password": "admin888",
            "domain": "fallback.example.com",
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(status_code=500, payload={"error": "boom"}),
            FakeResponse(payload={"address": "abc123@fallback.example.com", "jwt": "jwt-1"}),
        ]
    )

    created = service.create_email()

    assert created["email"].endswith("@fallback.example.com")
    create_call = service.http_client.calls[1]
    assert create_call["kwargs"]["json"]["domain"] == "fallback.example.com"


def test_create_email_supports_configured_domain_pool_without_required_domain():
    service = TempMailService(
        {
            "base_url": "https://apmail.889110.xyz",
            "admin_password": "admin888",
            "domains": ["pool.example.com"],
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"address": "abc123@pool.example.com", "jwt": "jwt-1"}),
        ]
    )

    created = service.create_email()

    assert created["email"].endswith("@pool.example.com")
    assert len(service.http_client.calls) == 1
    assert service.http_client.calls[0]["kwargs"]["json"]["domain"] == "pool.example.com"


def test_create_email_fails_when_no_domains_available_anywhere():
    service = TempMailService(
        {
            "base_url": "https://apmail.889110.xyz",
            "admin_password": "admin888",
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"domains": []}),
        ]
    )

    with pytest.raises(Exception, match="未找到可用域名|可用域名"):
        service.create_email()


def test_create_email_falls_back_when_worker_returns_empty_domain_list():
    service = TempMailService(
        {
            "base_url": "https://apmail.889110.xyz",
            "admin_password": "admin888",
            "domain": "fallback.example.com",
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"domains": []}),
            FakeResponse(payload={"address": "abc123@fallback.example.com", "jwt": "jwt-1"}),
        ]
    )

    created = service.create_email()

    assert created["email"].endswith("@fallback.example.com")


def test_create_email_raises_when_api_response_missing_address():
    service = TempMailService(
        {
            "base_url": "https://apmail.889110.xyz",
            "admin_password": "admin888",
            "domain": "fallback.example.com",
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"domains": ["fallback.example.com"]}),
            FakeResponse(payload={"jwt": "jwt-1"}),
        ]
    )

    with pytest.raises(Exception, match="返回数据不完整|数据不完整"):
        service.create_email()


def test_create_email_admin_requests_include_admin_auth_header():
    service = TempMailService(
        {
            "base_url": "https://apmail.889110.xyz",
            "admin_password": "admin888",
            "domain": "fallback.example.com",
            "enable_prefix": True,
        }
    )
    service.http_client = FakeHTTPClient(
        [
            FakeResponse(payload={"domains": ["fallback.example.com"]}),
            FakeResponse(payload={"address": "tmpabc123@fallback.example.com", "jwt": "jwt-1"}),
        ]
    )

    created = service.create_email()

    assert created["email"].endswith("@fallback.example.com")
    for call in service.http_client.calls:
        assert call["kwargs"]["headers"]["x-admin-auth"] == "admin888"
    assert service.http_client.calls[1]["kwargs"]["json"]["enablePrefix"] is True
