from types import SimpleNamespace

from src.config.constants import OPENAI_API_ENDPOINTS


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, url="", on_return=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.on_return = on_return

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class CookieJar(dict):
    jar = ()


class QueueSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.cookies = CookieJar()

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def _request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.steps:
            raise AssertionError(f"unexpected request: {method} {url}")
        expected_method, expected_url, response = self.steps.pop(0)
        assert method == expected_method
        assert url == expected_url
        if callable(response):
            response = response(self)
        if response.on_return:
            response.on_return(self)
        if not response.url:
            response.url = url
        return response


class FakeProfile:
    user_agent = "UA-test/1.0"
    impersonate = "chrome136"

    def _ch_ua_headers(self):
        return {"sec-ch-ua": '"Chromium";v="136"', "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"'}

    def sentinel_headers(self):
        return {"content-type": "application/json"}


class FakeHttpClient:
    def __init__(self, session, sentinel_token="sentinel-token"):
        self._session = session
        self.profile = FakeProfile()
        self.sentinel_token = sentinel_token

    @property
    def session(self):
        return self._session

    def check_sentinel(self, did, proxies=None):
        return self.sentinel_token


class FakeMailbox:
    def __init__(self, code="123456"):
        self.code = code
        self.requests = []

    def wait_for_verification_code(self, email, timeout=60, otp_sent_at=None, exclude_codes=None):
        self.requests.append(
            {
                "email": email,
                "timeout": timeout,
                "otp_sent_at": otp_sent_at,
                "exclude_codes": set(exclude_codes or ()),
            }
        )
        return self.code


def test_chatgpt_client_completes_source_style_registration_and_session_reuse():
    from src.core.protocol_v2.client import ChatGPTProtocolClient

    def mark_nextauth_cookie(session):
        session.cookies["__Secure-next-auth.session-token"] = "nextauth-session"
        return DummyResponse(
            payload={
                "continue_url": "https://chatgpt.com/",
            },
            url=OPENAI_API_ENDPOINTS["create_account"],
        )

    session = QueueSession(
        [
            ("GET", "https://chatgpt.com/", DummyResponse(status_code=200, text="<html />", url="https://chatgpt.com/")),
            ("GET", "https://chatgpt.com/api/auth/csrf", DummyResponse(payload={"csrfToken": "csrf-1"})),
            (
                "POST",
                "https://chatgpt.com/api/auth/signin/openai",
                DummyResponse(payload={"url": "https://auth.example.test/authorize"}),
            ),
            (
                "GET",
                "https://auth.example.test/authorize",
                DummyResponse(status_code=200, url="https://auth.openai.com/u/signup/password"),
            ),
            ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
            ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
            (
                "POST",
                OPENAI_API_ENDPOINTS["validate_otp"],
                DummyResponse(payload={"continue_url": "https://auth.openai.com/about-you"}),
            ),
            ("POST", OPENAI_API_ENDPOINTS["create_account"], mark_nextauth_cookie),
            ("GET", "https://chatgpt.com/api/auth/session", DummyResponse(payload={
                "accessToken": "access-1",
                "sessionToken": "session-1",
                "user": {"id": "user-1"},
                "account": {"id": "acct-1"},
                "authProvider": "nextauth",
            })),
        ]
    )

    client = ChatGPTProtocolClient(http_client=FakeHttpClient(session), callback_logger=None)
    mailbox = FakeMailbox()

    ok, message = client.register_complete_flow(
        email="tester@example.com",
        password="Passw0rd!123",
        first_name="Test",
        last_name="User",
        birthdate="1990-01-02",
        mailbox_client=mailbox,
    )

    assert ok is True
    assert message == "注册成功"
    session_ok, tokens = client.reuse_session_and_get_tokens()
    assert session_ok is True
    assert tokens["access_token"] == "access-1"
    assert tokens["session_token"] == "session-1"
    assert tokens["account_id"] == "acct-1"
    assert tokens["workspace_id"] == "acct-1"
    assert mailbox.requests[0]["email"] == "tester@example.com"


def test_chatgpt_client_falls_back_to_password_flow_for_unknown_start_state():
    from src.core.protocol_v2.client import ChatGPTProtocolClient

    def mark_nextauth_cookie(session):
        session.cookies["__Secure-next-auth.session-token"] = "nextauth-session"
        return DummyResponse(payload={"continue_url": "https://chatgpt.com/"}, url=OPENAI_API_ENDPOINTS["create_account"])

    session = QueueSession(
        [
            ("GET", "https://chatgpt.com/", DummyResponse(status_code=200, text="<html />", url="https://chatgpt.com/")),
            ("GET", "https://chatgpt.com/api/auth/csrf", DummyResponse(payload={"csrfToken": "csrf-1"})),
            (
                "POST",
                "https://chatgpt.com/api/auth/signin/openai",
                DummyResponse(payload={"url": "https://auth.example.test/authorize"}),
            ),
            (
                "GET",
                "https://auth.example.test/authorize",
                DummyResponse(status_code=200, url="https://auth.openai.com/u/flow/unknown"),
            ),
            ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
            ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
            (
                "POST",
                OPENAI_API_ENDPOINTS["validate_otp"],
                DummyResponse(payload={"continue_url": "https://auth.openai.com/about-you"}),
            ),
            ("POST", OPENAI_API_ENDPOINTS["create_account"], mark_nextauth_cookie),
        ]
    )

    client = ChatGPTProtocolClient(http_client=FakeHttpClient(session), callback_logger=None)
    mailbox = FakeMailbox()

    ok, message = client.register_complete_flow(
        email="tester@example.com",
        password="Passw0rd!123",
        first_name="Test",
        last_name="User",
        birthdate="1990-01-02",
        mailbox_client=mailbox,
    )

    assert ok is True
    assert message == "注册成功"
    assert any(call["url"] == OPENAI_API_ENDPOINTS["register"] for call in session.calls)


def test_chatgpt_client_requires_nextauth_cookie_before_session_reuse():
    from src.core.protocol_v2.client import ChatGPTProtocolClient
    from src.core.protocol_v2.flow import FlowState

    session = QueueSession([])
    client = ChatGPTProtocolClient(http_client=FakeHttpClient(session), callback_logger=None)
    client.last_registration_state = FlowState(page_type="chatgpt_home", current_url="https://chatgpt.com/")

    ok, error = client.reuse_session_and_get_tokens()

    assert ok is False
    assert "next-auth.session-token" in error


def test_chatgpt_client_authorize_retries_after_transient_failure():
    from src.core.protocol_v2.client import ChatGPTProtocolClient

    attempts = {"count": 0}

    def flaky_authorize(_session):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary tls failure")
        return DummyResponse(status_code=200, url="https://auth.openai.com/u/signup/password")

    session = QueueSession(
        [
            ("GET", "https://auth.example.test/authorize", flaky_authorize),
            ("GET", "https://auth.example.test/authorize", flaky_authorize),
        ]
    )
    client = ChatGPTProtocolClient(http_client=FakeHttpClient(session), callback_logger=None)

    final_url = client.authorize("https://auth.example.test/authorize", max_retries=2)

    assert final_url == "https://auth.openai.com/u/signup/password"
    assert attempts["count"] == 2
