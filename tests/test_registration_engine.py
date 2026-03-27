import base64
import json
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from src.config.constants import EmailServiceType, OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES
from src.core.http_client import OpenAIHTTPClient
from src.core.openai.oauth import OAuthStart
from src.core.register import RegistrationEngine
from src.services.base import BaseEmailService
from src.services.outlook.base import EmailMessage
from src.services.outlook.service import OutlookService


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, on_return=None, url="", history=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.on_return = on_return
        self.url = url
        self.history = history or []

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class QueueSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.cookies = {}

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._request(method.upper(), url, **kwargs)

    def close(self):
        return None

    def _request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.steps:
            raise AssertionError(f"unexpected request: {method} {url}")
        expected_method, expected_url, response = self.steps.pop(0)
        assert method == expected_method
        assert url == expected_url
        if callable(response):
            response = response(self)
        if response.on_return:
            response.on_return(self)
        return response


class FakeEmailService(BaseEmailService):
    def __init__(self, codes):
        super().__init__(EmailServiceType.TEMPMAIL)
        self.codes = list(codes)
        self.otp_requests = []

    def create_email(self, config=None):
        return {
            "email": "tester@example.com",
            "service_id": "mailbox-1",
        }

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=r"(?<!\d)(\d{6})(?!\d)", otp_sent_at=None):
        self.otp_requests.append({
            "email": email,
            "email_id": email_id,
            "timeout": timeout,
            "otp_sent_at": otp_sent_at,
        })
        if not self.codes:
            raise AssertionError("no verification code queued")
        return self.codes.pop(0)

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id):
        return True

    def check_health(self):
        return True


class FakeOutlookEmailService(FakeEmailService):
    def __init__(self, codes):
        super().__init__(codes)
        self.service_type = EmailServiceType.OUTLOOK
        self.current_stage = None
        self.verification_stages = []

    def set_verification_stage(self, email, stage):
        self.current_stage = stage
        self.verification_stages.append({
            "email": email,
            "stage": stage,
        })

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=r"(?<!\d)(\d{6})(?!\d)", otp_sent_at=None):
        self.otp_requests.append({
            "email": email,
            "email_id": email_id,
            "timeout": timeout,
            "otp_sent_at": otp_sent_at,
            "stage": self.current_stage,
        })
        if not self.codes:
            raise AssertionError("no verification code queued")
        return self.codes.pop(0)


class FakeStageAwareTempMailService(FakeEmailService):
    def __init__(self, codes):
        super().__init__(codes)
        self.service_type = EmailServiceType.TEMP_MAIL
        self.verification_stages = []

    def set_verification_stage(self, email, stage):
        self.verification_stages.append({
            "email": email,
            "stage": stage,
        })


class FakeOAuthManager:
    def __init__(self):
        self.start_calls = 0
        self.callback_calls = []

    def start_oauth(self):
        self.start_calls += 1
        return OAuthStart(
            auth_url=f"https://auth.example.test/flow/{self.start_calls}",
            state=f"state-{self.start_calls}",
            code_verifier=f"verifier-{self.start_calls}",
            redirect_uri="http://localhost:1455/auth/callback",
        )

    def handle_callback(self, callback_url, expected_state, code_verifier):
        self.callback_calls.append({
            "callback_url": callback_url,
            "expected_state": expected_state,
            "code_verifier": code_verifier,
        })
        return {
            "account_id": "acct-1",
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "id_token": "id-1",
        }


class FakeOAuthManagerWithWorkspace(FakeOAuthManager):
    def __init__(self, workspace_id="ws-token"):
        super().__init__()
        self.workspace_id = workspace_id

    def handle_callback(self, callback_url, expected_state, code_verifier):
        result = super().handle_callback(callback_url, expected_state, code_verifier)
        result["id_token"] = _jwt_like_token({
            "sub": "acct-1",
            "workspaces": [{"id": self.workspace_id}],
        })
        return result


class FakeOpenAIClient:
    def __init__(self, sessions, sentinel_tokens):
        self._sessions = list(sessions)
        self._session_index = 0
        self._session = self._sessions[0]
        self._sentinel_tokens = list(sentinel_tokens)

    @property
    def session(self):
        return self._session

    def check_ip_location(self):
        return True, "US"

    def check_sentinel(self, did):
        if not self._sentinel_tokens:
            raise AssertionError("no sentinel token queued")
        return self._sentinel_tokens.pop(0)

    def close(self):
        if self._session_index + 1 < len(self._sessions):
            self._session_index += 1
            self._session = self._sessions[self._session_index]


def _workspace_cookie(workspace_id):
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"workspaces": [{"id": workspace_id}]}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{header}.{payload}.sig"


def _workspace_cookie_without_workspace():
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "session-only"}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{header}.{payload}.sig"


def _quoted_first_segment_workspace_cookie(workspace_id):
    first_segment = base64.urlsafe_b64encode(
        json.dumps({"workspaces": [{"id": workspace_id}]}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return urllib.parse.quote(f'"{first_segment}.sig"')


def _jwt_like_token(payload):
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    payload_segment = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{header}.{payload_segment}.sig"


def _next_auth_session_token(workspace_id):
    return _jwt_like_token({"workspaces": [{"id": workspace_id}]})


def _response_with_did(did):
    return DummyResponse(
        status_code=200,
        text="ok",
        on_return=lambda session: session.cookies.__setitem__("oai-did", did),
    )


def _response_with_login_cookies(workspace_id="ws-1", session_token="session-1"):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie(workspace_id)
        session.cookies["__Secure-next-auth.session-token"] = session_token

    return DummyResponse(status_code=200, payload={}, on_return=setter)


def _response_with_login_cookies_without_workspace(session_token="session-1"):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
        session.cookies["__Secure-next-auth.session-token"] = session_token

    return DummyResponse(status_code=200, payload={}, on_return=setter)


def _response_with_payload_workspace_only(workspace_id="ws-payload", session_token="session-1"):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
        session.cookies["__Secure-next-auth.session-token"] = session_token

    return DummyResponse(
        status_code=200,
        payload={"session": {"workspaces": [{"id": workspace_id}]}},
        on_return=setter,
    )


def _response_with_callback_before_workspace(
    callback_url="http://localhost:1455/auth/callback?code=code-2&state=state-2",
    session_token="session-1",
):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
        session.cookies["__Secure-next-auth.session-token"] = session_token

    return DummyResponse(
        status_code=200,
        payload={"callback_url": callback_url},
        on_return=setter,
    )


def _response_with_resume_history(
    resume_url="https://auth.example.test/login-challenge?login_challenge=challenge-1",
    session_token="session-1",
):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
        session.cookies["__Secure-next-auth.session-token"] = session_token

    history = [
        DummyResponse(
            status_code=302,
            headers={"Location": resume_url},
            url=OPENAI_API_ENDPOINTS["validate_otp"],
        )
    ]
    return DummyResponse(
        status_code=200,
        payload={},
        url=resume_url,
        history=history,
        on_return=setter,
    )


def _response_with_email_verification_resume_text(
    resume_url="https://auth.example.test/api/oauth/oauth2/auth?client_id=client-1&state=resume-1",
    session_token="session-1",
):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
        session.cookies["__Secure-next-auth.session-token"] = session_token

    text = f'<html><body><script>window.__STATE__={{"continue_url":"{resume_url}"}};</script></body></html>'
    return DummyResponse(
        status_code=200,
        payload={},
        text=text,
        url="https://auth.example.test/email-verification",
        on_return=setter,
    )


def _response_with_about_you_next_data_callback(
    callback_url="http://localhost:1455/auth/callback?code=code-next-data&state=state-1",
):
    encoded_callback = callback_url.replace(":", "\\u003A").replace("/", "\\u002F")
    text = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"callback_url":"'
        + encoded_callback
        + '"}}}</script></body></html>'
    )
    return DummyResponse(
        status_code=200,
        text=text,
        url="https://auth.example.test/about-you",
    )


def _response_with_about_you_next_data_resume(
    resume_url="https://auth.example.test/api/oauth/oauth2/auth?client_id=client-1&state=resume-next",
):
    encoded_resume = resume_url.replace(":", "\\u003A").replace("/", "\\u002F")
    text = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"continue_url":"'
        + encoded_resume
        + '"}}}</script></body></html>'
    )
    return DummyResponse(
        status_code=200,
        text=text,
        url="https://auth.example.test/about-you",
    )


def _response_with_consent_next_data_workspace(
    workspace_id="ws-consent-next-data",
    session_token="session-consent-next-data",
):
    payload = {
        "props": {
            "pageProps": {
                "session": {
                    "workspaces": [{"id": workspace_id}],
                },
                "page": {"type": "sign_in_with_chatgpt_codex_consent"},
            }
        }
    }

    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
        session.cookies["__Secure-next-auth.session-token"] = session_token

    text = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></body></html>"
    )
    return DummyResponse(
        status_code=200,
        text=text,
        url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        on_return=setter,
    )


def _response_with_consent_app_router_workspace(
    workspace_id="ws-consent-app-router",
    session_token="session-consent-app-router",
):
    payload_fragment = json.dumps(
        {
            "session": {
                "workspaces": [{"id": workspace_id}],
            },
            "page": {"type": "sign_in_with_chatgpt_codex_consent"},
        }
    )

    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
        session.cookies["__Secure-next-auth.session-token"] = session_token

    text = (
        "<html><body><script>"
        "(self.__next_f=self.__next_f||[]).push("
        + json.dumps([1, payload_fragment])
        + ");</script></body></html>"
    )
    return DummyResponse(
        status_code=200,
        text=text,
        url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        on_return=setter,
    )


def _manifest_cookie(payload):
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return urllib.parse.quote(encoded)


def _response_with_username_rejected():
    payload = {
        "error": {
            "message": "Failed to register username. Please try again.",
            "code": "bad_request",
        }
    }
    return DummyResponse(status_code=400, payload=payload, text=json.dumps(payload))


@contextmanager
def _fake_get_db_context():
    yield object()


def _make_engine_session_manager(name):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        try:
            db_path.unlink()
        except PermissionError:
            db_path = runtime_dir / f"{db_path.stem}-{datetime.now().timestamp():.0f}{db_path.suffix}"
    from src.database.models import Base
    from src.database.session import DatabaseSessionManager

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def _build_outlook_username_rejection_engine(monkeypatch):
    created_accounts = []

    monkeypatch.setattr("src.core.register.get_db", _fake_get_db_context)
    monkeypatch.setattr("src.core.register.crud.get_account_by_email", lambda db, email: None)
    monkeypatch.setattr(
        "src.core.register.crud.create_account",
        lambda db, **kwargs: created_accounts.append(kwargs),
    )

    email_service = FakeOutlookEmailService([])
    email_service.create_email = lambda config=None: {
        "email": "StephanieChavez3037@outlook.com",
        "service_id": "mailbox-1",
    }

    engine = RegistrationEngine(email_service)
    engine.oauth_manager = FakeOAuthManager()
    return engine, created_accounts


def test_check_sentinel_sends_non_empty_pow(monkeypatch):
    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["sentinel"], DummyResponse(payload={"token": "sentinel-token"})),
    ])
    client = OpenAIHTTPClient()
    client._session = session

    monkeypatch.setattr(
        "src.core.http_client.build_sentinel_pow_token",
        lambda user_agent: "gAAAAACpow-token",
    )

    token = client.check_sentinel("device-1")

    assert token == "sentinel-token"
    body = json.loads(session.calls[0]["kwargs"]["data"])
    assert body["id"] == "device-1"
    assert body["flow"] == "authorize_continue"
    assert body["p"] == "gAAAAACpow-token"


def test_run_registers_then_relogs_to_fetch_token():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "register"
    assert result.workspace_id == "ws-1"
    assert result.session_token == "session-1"
    assert fake_oauth.start_calls == 2
    assert len(email_service.otp_requests) == 2
    assert all(item["otp_sent_at"] is not None for item in email_service.otp_requests)
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 1
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 0
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 1
    relogin_start_body = json.loads(session_two.calls[1]["kwargs"]["data"])
    assert relogin_start_body["screen_hint"] == "login"
    assert relogin_start_body["username"]["value"] == "tester@example.com"
    password_verify_body = json.loads(session_two.calls[2]["kwargs"]["data"])
    assert password_verify_body == {"password": result.password}
    assert result.metadata["token_acquired_via_relogin"] is True


def test_run_prefers_current_session_after_create_account_add_phone_consent_before_relogin():
    def create_account_add_phone(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
        session.cookies["__Secure-next-auth.session-token"] = "session-add-phone"
        return DummyResponse(
            payload={
                "page": {"type": "add_phone"},
                "continue_url": "https://auth.example.test/add-phone",
            },
            url=OPENAI_API_ENDPOINTS["create_account"],
        )

    def consent_page_with_workspace(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie("ws-add-phone")
        session.cookies["__Secure-next-auth.session-token"] = "session-add-phone"
        return DummyResponse(
            status_code=200,
            url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            text="<html><body>consent</body></html>",
        )

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], create_account_add_phone),
        (
            "GET",
            "https://auth.example.test/add-phone",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/add-phone",
                text=(
                    "<html><body><script>"
                    'window.__STATE__={"continue_url":"https://auth.openai.com/sign-in-with-chatgpt/codex/consent"};'
                    "</script></body></html>"
                ),
            ),
        ),
        ("GET", "https://auth.openai.com/sign-in-with-chatgpt/codex/consent", consent_page_with_workspace),
        ("GET", "https://auth.openai.com/sign-in-with-chatgpt/codex/consent", consent_page_with_workspace),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/consent-continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/consent-continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-add-phone&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManagerWithWorkspace("ws-add-phone")
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-add-phone"
    assert result.session_token == "session-add-phone"
    assert fake_oauth.start_calls == 1
    assert len(email_service.otp_requests) == 1
    assert any(call["url"] == "https://auth.example.test/add-phone" for call in session.calls)
    assert any(call["url"] == "https://auth.openai.com/sign-in-with-chatgpt/codex/consent" for call in session.calls)
    assert sum(1 for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 1
    assert result.metadata["token_acquired_via_relogin"] is False


def test_get_workspace_id_reads_workspace_from_cookie_payload_segment():
    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = QueueSession([])
    engine.session.cookies["oai-client-auth-session"] = _workspace_cookie("ws-cookie")

    workspace_id = engine._get_workspace_id()

    assert workspace_id == "ws-cookie"


def test_get_workspace_id_returns_none_when_cookie_has_no_workspace():
    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = QueueSession([])
    engine.session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()

    workspace_id = engine._get_workspace_id()

    assert workspace_id is None
    assert any("workspace" in log.lower() for log in engine.logs)


def test_get_workspace_id_reads_workspace_from_quoted_first_segment_cookie():
    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = QueueSession([])
    engine.session.cookies["oai-client-auth-session"] = _quoted_first_segment_workspace_cookie("ws-quoted")

    workspace_id = engine._get_workspace_id()

    assert workspace_id == "ws-quoted"


def test_get_workspace_id_reads_workspace_from_next_auth_session_token():
    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = QueueSession([])
    engine.session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
    engine.session.cookies["__Secure-next-auth.session-token"] = _next_auth_session_token("ws-next-auth")

    workspace_id = engine._get_workspace_id()

    assert workspace_id == "ws-next-auth"


def test_get_workspace_id_reads_workspace_from_unified_session_manifest_cookie():
    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = QueueSession([])
    engine.session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
    engine.session.cookies["unified_session_manifest"] = _manifest_cookie(
        {"workspaces": [{"id": "ws-manifest"}]}
    )

    workspace_id = engine._get_workspace_id()

    assert workspace_id == "ws-manifest"


def test_select_workspace_includes_oauth_context_headers():
    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = QueueSession([
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/next"}),
        ),
    ])
    engine.session.headers = {"User-Agent": "UA-test/1.0"}
    engine.session.cookies["oai-did"] = "did-test-1"

    continue_url = engine._select_workspace("ws-header-check")

    assert continue_url == "https://auth.example.test/next"
    headers = engine.session.calls[0]["kwargs"]["headers"]
    assert headers["accept"] == "application/json"
    assert headers["content-type"] == "application/json"
    assert headers["origin"] == "https://auth.openai.com"
    assert headers["referer"] == "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
    assert headers["user-agent"] == "UA-test/1.0"
    assert headers["oai-device-id"] == "did-test-1"
    assert "traceparent" in headers
    assert "x-datadog-trace-id" in headers


def test_select_organization_includes_oauth_context_headers():
    organization_select_url = "https://auth.openai.com/api/accounts/organization/select"
    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = QueueSession([
        (
            "POST",
            organization_select_url,
            DummyResponse(payload={"continue_url": "https://auth.example.test/org-next"}),
        ),
    ])
    engine.session.headers = {"User-Agent": "UA-org/2.0"}
    engine.session.cookies["oai-did"] = "did-org-1"

    continue_url = engine._select_organization(
        "org-1",
        "proj-1",
        referer="https://auth.example.test/organization-step",
    )

    assert continue_url == "https://auth.example.test/org-next"
    headers = engine.session.calls[0]["kwargs"]["headers"]
    assert headers["accept"] == "application/json"
    assert headers["content-type"] == "application/json"
    assert headers["origin"] == "https://auth.openai.com"
    assert headers["referer"] == "https://auth.example.test/organization-step"
    assert headers["user-agent"] == "UA-org/2.0"
    assert headers["oai-device-id"] == "did-org-1"
    assert "traceparent" in headers
    assert "x-datadog-parent-id" in headers


def test_remember_navigation_from_consent_logs_script_assets_and_bundle_hints():
    route_script_url = "https://auth.openai.com/assets/route-D83ftS1Y.js"
    main_script_url = "https://auth.openai.com/assets/main.js"
    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = QueueSession([
        (
            "GET",
            route_script_url,
            DummyResponse(
                status_code=200,
                url=route_script_url,
                text='const clientAction="/api/accounts/workspace/select"; const org="/api/accounts/organization/select";',
            ),
        ),
        (
            "GET",
            main_script_url,
            DummyResponse(
                status_code=200,
                url=main_script_url,
                text="console.log('noop')",
            ),
        ),
    ])
    engine.session.headers = {"User-Agent": "UA-assets/1.0"}

    response = DummyResponse(
        status_code=200,
        url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        text=(
            "<html><head>"
            '<script src="/assets/route-D83ftS1Y.js"></script>'
            '<script src="/assets/main.js"></script>'
            "</head><body>consent</body></html>"
        ),
    )

    engine._remember_navigation_from_response("redirect_1", response)

    assert any("consent_script_assets=2" in log for log in engine.logs)
    assert any("route-D83ftS1Y.js" in log for log in engine.logs)
    assert any("workspace/select" in log for log in engine.logs)
    assert any("organization/select" in log for log in engine.logs)


def test_resolve_oauth_callback_runs_organization_select_when_workspace_select_requires_it():
    organization_select_url = "https://auth.openai.com/api/accounts/organization/select"
    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = QueueSession([
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                payload={
                    "continue_url": "https://auth.example.test/organization",
                    "page": {"type": "organization_selection"},
                    "data": {
                        "orgs": [
                            {
                                "id": "org-1",
                                "projects": [{"id": "proj-1"}],
                            }
                        ]
                    },
                }
            ),
        ),
        (
            "POST",
            organization_select_url,
            DummyResponse(payload={"continue_url": "https://auth.example.test/org-continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/org-continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-org&state=state-1"},
            ),
        ),
    ])
    engine.session.cookies["oai-client-auth-session"] = _workspace_cookie("ws-org")

    callback_url, workspace_id, resolution_error = engine._resolve_oauth_callback_url()

    assert callback_url == "http://localhost:1455/auth/callback?code=code-org&state=state-1"
    assert workspace_id == "ws-org"
    assert resolution_error is None
    assert any(call["url"] == organization_select_url for call in engine.session.calls)


def test_register_password_marks_username_rejection_as_consumed(monkeypatch):
    engine, created_accounts = _build_outlook_username_rejection_engine(monkeypatch)
    engine.email = "StephanieChavez3037@outlook.com"
    engine.email_info = {"service_id": "mailbox-1"}
    engine.session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["register"], _response_with_username_rejected()),
    ])

    ok, password = engine._register_password()

    assert ok is False
    assert password is None
    assert len(created_accounts) == 1
    assert created_accounts[0]["email"] == "StephanieChavez3037@outlook.com"
    assert created_accounts[0]["status"] == "failed"


def test_run_reports_specific_error_for_username_rejected_outlook(monkeypatch):
    engine, _created_accounts = _build_outlook_username_rejection_engine(monkeypatch)
    engine.http_client = FakeOpenAIClient([
        QueueSession([
            ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
            (
                "POST",
                OPENAI_API_ENDPOINTS["signup"],
                DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
            ),
            ("POST", OPENAI_API_ENDPOINTS["register"], _response_with_username_rejected()),
        ])
    ], ["sentinel-1"])

    result = engine.run()

    assert result.success is False
    assert "注册密码失败" not in result.error_message
    assert any(token in result.error_message for token in ("已被占用", "疑似已注册", "已注册"))


def test_existing_account_login_uses_auto_sent_otp_without_manual_send():
    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-existing", "session-existing")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-existing"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-existing",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["246810"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "login"
    assert fake_oauth.start_calls == 1
    assert sum(1 for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert len(email_service.otp_requests) == 1
    assert email_service.otp_requests[0]["otp_sent_at"] is not None
    assert result.metadata["token_acquired_via_relogin"] is False


def test_run_registers_then_relogs_outlook_with_manual_resend_and_longer_timeout():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "123456"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert email_service.verification_stages[0]["stage"] == "signup_otp"
    assert email_service.verification_stages[-1]["stage"] == "relogin_otp"
    assert {item["stage"] for item in email_service.verification_stages} == {"signup_otp", "relogin_otp"}
    assert [item["stage"] for item in email_service.otp_requests] == ["signup_otp", "relogin_otp"]
    assert email_service.otp_requests[0]["timeout"] == 120
    assert email_service.otp_requests[1]["timeout"] == 180
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 1
    assert session_two.calls[3]["kwargs"]["headers"]["referer"] == "https://auth.openai.com/log-in/password"


def test_run_registers_then_relogs_outlook_keeps_polling_when_manual_resend_fails():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(status_code=500, payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-fallback", "session-fallback")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-fallback"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-fallback",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "123456"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-fallback"
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 1
    assert email_service.otp_requests[1]["timeout"] == 180


def test_run_persists_recoverable_outlook_account_when_workspace_lookup_fails(monkeypatch):
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-workspace-fail.db")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies_without_workspace()),
        (
            "GET",
            "https://auth.example.test/flow/2",
            DummyResponse(status_code=200, payload={}, url="https://auth.example.test/flow/2"),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321", "777888"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is False
    assert result.error_message == "OAuth 续跑失败"

    with manager.session_scope() as session:
        account = session.query(Account).filter(Account.email == "tester@example.com").first()
        account_snapshot = {
            "password": account.password,
            "status": account.status,
            "email_service": account.email_service,
            "extra_data": dict(account.extra_data or {}),
        } if account else None

    assert account_snapshot is not None
    assert account_snapshot["password"]
    assert account_snapshot["status"] == "failed"
    assert account_snapshot["email_service"] == "outlook"
    assert account_snapshot["extra_data"]["recovery_ready"] is True
    assert account_snapshot["extra_data"]["account_created"] is True
    assert account_snapshot["extra_data"]["token_acquired"] is False
    assert account_snapshot["extra_data"]["register_failed_reason"] == "token_recovery_pending"
    assert account_snapshot["extra_data"]["last_recovery_error"] == "OAuth 续跑失败"
    assert account_snapshot["extra_data"]["last_oauth_resume_source"] == "authorize_replay_failed"


def test_run_recovers_outlook_account_with_saved_password_and_updates_existing_record(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-success.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-recovered", "session-recovered")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/recover"}),
        ),
        (
            "GET",
            "https://auth.example.test/recover",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["789012"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.metadata["database_saved"] is True
    assert not any(call["url"] == OPENAI_API_ENDPOINTS["register"] for call in session.calls)
    assert not any(call["url"] == OPENAI_API_ENDPOINTS["create_account"] for call in session.calls)
    assert [item["stage"] for item in email_service.otp_requests] == ["relogin_otp"]
    assert email_service.otp_requests[0]["timeout"] == 180

    with manager.session_scope() as session_db:
        accounts = session_db.query(Account).filter(Account.email == "tester@example.com").all()
        assert len(accounts) == 1
        account = accounts[0]
        assert account.status == "active"
        assert account.password == "saved-password"
        assert account.workspace_id == "ws-recovered"
        assert account.access_token == "access-1"
        assert account.extra_data["recovery_ready"] is False
        assert account.extra_data["token_acquired"] is True


def test_run_recovers_outlook_account_uses_relogin_otp_and_manual_resend(monkeypatch):
    from src.database import crud

    manager = _make_engine_session_manager("recoverable-relogin-otp.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-recovered", "session-recovered")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/recover"}),
        ),
        (
            "GET",
            "https://auth.example.test/recover",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["789012"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.metadata["database_saved"] is True
    assert email_service.verification_stages[-1]["stage"] == "relogin_otp"
    assert [item["stage"] for item in email_service.otp_requests] == ["relogin_otp"]
    assert email_service.otp_requests[0]["timeout"] == 180
    send_otp_calls = [call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]]
    assert len(send_otp_calls) == 1
    assert send_otp_calls[0]["kwargs"]["headers"]["referer"] == "https://auth.openai.com/log-in/password"


def test_run_persists_recoverable_outlook_account_on_relogin_otp_timeout(monkeypatch):
    from src.database.models import Account
    from src.database import crud

    manager = _make_engine_session_manager("recoverable-relogin-timeout.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
    ])

    email_service = FakeOutlookEmailService([None])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is False
    assert result.error_message == "获取验证码失败"
    assert email_service.verification_stages[-1]["stage"] == "relogin_otp"
    assert [item["stage"] for item in email_service.otp_requests] == ["relogin_otp"]
    assert email_service.otp_requests[0]["timeout"] == 180

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        extra_data = dict(account.extra_data or {})

    assert extra_data["recovery_ready"] is True
    assert extra_data["token_acquired"] is False
    assert extra_data["last_recovery_error"] == "获取验证码失败"
    assert extra_data["last_otp_stage"] == "relogin_otp"


def test_run_uses_workspace_from_validate_otp_response_payload():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_payload_workspace_only("ws-payload")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-payload"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-payload",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient(
        [session_one, session_two],
        ["sentinel-1", "sentinel-2", "sentinel-3"],
    )
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-payload"


def test_set_verification_stage_syncs_to_stage_aware_temp_mail_service():
    email_service = FakeStageAwareTempMailService([])
    engine = RegistrationEngine(email_service)
    engine.email = "tester@example.com"

    engine._set_verification_stage("signup_otp")
    engine._set_verification_stage("relogin_otp")

    assert [item["stage"] for item in email_service.verification_stages] == ["signup_otp", "relogin_otp"]


def test_run_can_complete_oauth_callback_before_workspace_is_resolved():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_callback_before_workspace()),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient(
        [session_one, session_two],
        ["sentinel-1", "sentinel-2", "sentinel-3"],
    )
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-token-fallback")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-token-fallback"
    assert not any(call["url"] == OPENAI_API_ENDPOINTS["select_workspace"] for call in session_two.calls)


def test_run_recovers_outlook_account_by_resuming_login_challenge_after_validate_otp(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-login-challenge-resume.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_resume_history()),
        (
            "GET",
            "https://auth.example.test/login-challenge?login_challenge=challenge-1",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["789012"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-replayed")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-replayed"
    assert result.metadata["database_saved"] is True
    assert not any(call["url"] == OPENAI_API_ENDPOINTS["select_workspace"] for call in session.calls)
    assert session.calls[-1]["url"] == "https://auth.example.test/login-challenge?login_challenge=challenge-1"
    assert result.metadata["last_oauth_resume_source"] == "login_challenge_resume"

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-replayed"


def test_run_recovers_outlook_account_from_email_verification_page_text_resume(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-email-verification-resume.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    resume_url = "https://auth.example.test/api/oauth/oauth2/auth?client_id=client-1&state=resume-1"
    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_email_verification_resume_text(resume_url)),
        (
            "GET",
            resume_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["789012"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-email-verification")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-email-verification"
    assert not any(call["url"] == OPENAI_API_ENDPOINTS["select_workspace"] for call in session.calls)
    assert any(call["url"] == resume_url for call in session.calls)
    assert result.metadata["last_oauth_resume_source"] in {
        "resume_url_found_after_validate_otp",
        "continue_url_resume",
        "resume_url_found_from_navigation",
    }

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-email-verification"


def test_run_recovers_outlook_account_by_completing_about_you_before_relogin(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-success.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/log-in/password",
                }
            ),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/email-verification",
                }
            ),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/about-you",
                }
            ),
        ),
        (
            "GET",
            "https://auth.example.test/about-you",
            DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you</html>"),
        ),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            _response_with_callback_before_workspace(
                callback_url="http://localhost:1455/auth/callback?code=code-about-you&state=state-2",
                session_token="session-about-you",
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you"
    assert result.session_token == "session-about-you"
    assert any(call["url"] == OPENAI_API_ENDPOINTS["create_account"] for call in session.calls)
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 2

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you"


def test_run_recovers_outlook_account_when_about_you_create_account_reports_user_exists(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-user-exists.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "鑾峰彇 Workspace ID 澶辫触",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/log-in/password",
                }
            ),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/email-verification",
                }
            ),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/about-you",
                }
            ),
        ),
        (
            "GET",
            "https://auth.example.test/about-you",
            DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you</html>"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["create_account"],
            DummyResponse(
                status_code=400,
                payload={
                    "error": {
                        "message": "An account already exists for this email address.",
                        "code": "user_already_exists",
                    }
                },
                text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
            ),
        ),
        (
            "GET",
            "https://auth.example.test/flow/1",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(status_code=200, payload={}, url="https://auth.example.test/log-in"),
        ),
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            _response_with_callback_before_workspace(
                callback_url="http://localhost:1455/auth/callback?code=code-about-you-exists&state=state-2",
                session_token="session-about-you-exists",
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you-exists")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you-exists"
    assert result.session_token == "session-about-you-exists"
    assert any(call["url"] == OPENAI_API_ENDPOINTS["create_account"] for call in session.calls)
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 2

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you-exists"


def test_run_recovers_outlook_account_from_same_session_after_about_you_user_exists(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-same-session.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "鑾峰彇 Workspace ID 澶辫触",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    def create_account_user_exists_with_callback():
        def setter(session):
            session.cookies["oai-client-auth-session"] = _workspace_cookie_without_workspace()
            session.cookies["__Secure-next-auth.session-token"] = "session-about-you-same-session"

        return DummyResponse(
            status_code=400,
            payload={
                "error": {
                    "message": "An account already exists for this email address.",
                    "code": "user_already_exists",
                },
                "callback_url": "http://localhost:1455/auth/callback?code=code-about-you-same-session&state=state-1",
            },
            text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
            on_return=setter,
        )

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/log-in/password",
                }
            ),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/email-verification",
                }
            ),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/about-you",
                }
            ),
        ),
        (
            "GET",
            "https://auth.example.test/about-you",
            DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you</html>"),
        ),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], create_account_user_exists_with_callback()),
    ])

    email_service = FakeOutlookEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you-same-session")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you-same-session"
    assert result.session_token == "session-about-you-same-session"
    assert len([call for call in session.calls if call["url"].startswith("https://auth.example.test/flow/")]) == 1

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you-same-session"


def test_run_recovers_outlook_account_from_authorize_replay_after_about_you_user_exists(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-authorize-replay.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    user_exists_response = DummyResponse(
        status_code=400,
        payload={
            "error": {
                "message": "An account already exists for this email address.",
                "code": "user_already_exists",
            }
        },
        text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
    )

    oauth_replay_url = (
        "https://auth.example.test/api/oauth/oauth2/auth"
        "?client_id=client-1&state=resume-1"
    )
    login_challenge_url = (
        "https://auth.example.test/api/accounts/login?login_challenge=challenge-after-about-you"
    )

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": "about_you"},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                },
                on_return=_response_with_login_cookies_without_workspace().on_return,
            ),
        ),
        (
            "GET",
            "https://auth.example.test/about-you",
            DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you</html>"),
        ),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], user_exists_response),
        (
            "GET",
            "https://auth.example.test/flow/1",
            DummyResponse(
                status_code=302,
                headers={"Location": oauth_replay_url},
            ),
        ),
        (
            "GET",
            oauth_replay_url,
            DummyResponse(
                status_code=302,
                headers={"Location": login_challenge_url},
            ),
        ),
        (
            "GET",
            login_challenge_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-about-you-replay&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you-authorize-replay")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you-authorize-replay"
    assert result.metadata["last_oauth_resume_source"] == "callback_found_from_authorize_replay"
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 1
    assert len([call for call in session.calls if call["url"] == "https://auth.example.test/flow/1"]) == 2
    assert [item["stage"] for item in email_service.otp_requests] == ["relogin_otp"]

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you-authorize-replay"


def test_run_recovers_outlook_account_from_direct_consent_after_about_you_user_exists(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-direct-consent.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    user_exists_response = DummyResponse(
        status_code=400,
        payload={
            "error": {
                "message": "An account already exists for this email address.",
                "code": "user_already_exists",
            }
        },
        text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
    )

    oauth_replay_url = (
        "https://auth.example.test/api/oauth/oauth2/auth"
        "?client_id=client-1&state=resume-1"
    )
    login_challenge_url = (
        "https://auth.example.test/api/accounts/login?login_challenge=challenge-after-about-you"
    )
    consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

    def consent_response():
        def setter(queue_session):
            queue_session.cookies["oai-client-auth-session"] = _workspace_cookie("ws-about-you-direct-consent")
            queue_session.cookies["__Secure-next-auth.session-token"] = "session-about-you-direct-consent"

        return DummyResponse(
            status_code=200,
            url=consent_url,
            text="<html>consent</html>",
            on_return=setter,
        )

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": "about_you"},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                },
                on_return=_response_with_login_cookies_without_workspace("session-about-you-initial").on_return,
            ),
        ),
        (
            "GET",
            "https://auth.example.test/about-you",
            DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you</html>"),
        ),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], user_exists_response),
        (
            "GET",
            "https://auth.example.test/flow/1",
            DummyResponse(
                status_code=302,
                headers={"Location": oauth_replay_url},
            ),
        ),
        (
            "GET",
            oauth_replay_url,
            DummyResponse(
                status_code=302,
                headers={"Location": login_challenge_url},
            ),
        ),
        (
            "GET",
            login_challenge_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login</html>",
            ),
        ),
        (
            "GET",
            login_challenge_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login again</html>",
            ),
        ),
        ("GET", consent_url, consent_response()),
        ("GET", consent_url, consent_response()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/consent-continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/consent-continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-about-you-consent&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you-direct-consent")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you-direct-consent"
    assert result.session_token == "session-about-you-direct-consent"
    assert any(call["url"] == consent_url for call in session.calls)
    assert any(call["url"] == OPENAI_API_ENDPOINTS["select_workspace"] for call in session.calls)

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you-direct-consent"


def test_run_recovers_outlook_account_from_direct_consent_next_data_workspace(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-direct-consent-next-data.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    user_exists_response = DummyResponse(
        status_code=400,
        payload={
            "error": {
                "message": "An account already exists for this email address.",
                "code": "user_already_exists",
            }
        },
        text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
    )

    oauth_replay_url = (
        "https://auth.example.test/api/oauth/oauth2/auth"
        "?client_id=client-1&state=resume-1"
    )
    login_challenge_url = (
        "https://auth.example.test/api/accounts/login?login_challenge=challenge-after-about-you"
    )
    consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
    organization_select_url = "https://auth.openai.com/api/accounts/organization/select"

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": "about_you"},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                },
                on_return=_response_with_login_cookies_without_workspace("session-about-you-initial").on_return,
            ),
        ),
        (
            "GET",
            "https://auth.example.test/about-you",
            DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you</html>"),
        ),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], user_exists_response),
        (
            "GET",
            "https://auth.example.test/flow/1",
            DummyResponse(
                status_code=302,
                headers={"Location": oauth_replay_url},
            ),
        ),
        (
            "GET",
            oauth_replay_url,
            DummyResponse(
                status_code=302,
                headers={"Location": login_challenge_url},
            ),
        ),
        (
            "GET",
            login_challenge_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login</html>",
            ),
        ),
        (
            "GET",
            login_challenge_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login again</html>",
            ),
        ),
        ("GET", consent_url, _response_with_consent_next_data_workspace("ws-about-you-consent-html")),
        ("GET", consent_url, _response_with_consent_next_data_workspace("ws-about-you-consent-html")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                payload={
                    "continue_url": "https://auth.example.test/organization",
                    "page": {"type": "organization_selection"},
                    "data": {
                        "orgs": [
                            {
                                "id": "org-consent",
                                "projects": [{"id": "proj-consent"}],
                            }
                        ]
                    },
                }
            ),
        ),
        (
            "POST",
            organization_select_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-about-you-consent-html&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you-consent-html")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you-consent-html"
    assert result.session_token == "session-consent-next-data"
    assert any(call["url"] == consent_url for call in session.calls)
    assert any(call["url"] == OPENAI_API_ENDPOINTS["select_workspace"] for call in session.calls)
    assert any(call["url"] == organization_select_url for call in session.calls)

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you-consent-html"


def test_run_recovers_outlook_account_from_direct_consent_app_router_workspace(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-direct-consent-app-router.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "鑾峰彇 Workspace ID 澶辫触",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    user_exists_response = DummyResponse(
        status_code=400,
        payload={
            "error": {
                "message": "An account already exists for this email address.",
                "code": "user_already_exists",
            }
        },
        text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
    )

    oauth_replay_url = (
        "https://auth.example.test/api/oauth/oauth2/auth"
        "?client_id=client-1&state=resume-1"
    )
    login_challenge_url = (
        "https://auth.example.test/api/accounts/login?login_challenge=challenge-after-about-you"
    )
    consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": "about_you"},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                },
                on_return=_response_with_login_cookies_without_workspace("session-about-you-initial").on_return,
            ),
        ),
        (
            "GET",
            "https://auth.example.test/about-you",
            DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you</html>"),
        ),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], user_exists_response),
        (
            "GET",
            "https://auth.example.test/flow/1",
            DummyResponse(
                status_code=302,
                headers={"Location": oauth_replay_url},
            ),
        ),
        (
            "GET",
            oauth_replay_url,
            DummyResponse(
                status_code=302,
                headers={"Location": login_challenge_url},
            ),
        ),
        (
            "GET",
            login_challenge_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login</html>",
            ),
        ),
        (
            "GET",
            login_challenge_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login again</html>",
            ),
        ),
        ("GET", consent_url, _response_with_consent_app_router_workspace("ws-about-you-consent-rsc")),
        ("GET", consent_url, _response_with_consent_app_router_workspace("ws-about-you-consent-rsc")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/consent-continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/consent-continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-about-you-consent-rsc&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you-consent-rsc")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you-consent-rsc"
    assert result.session_token == "session-consent-app-router"
    assert any(call["url"] == consent_url for call in session.calls)
    assert any(call["url"] == OPENAI_API_ENDPOINTS["select_workspace"] for call in session.calls)

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you-consent-rsc"


def test_run_retries_about_you_submission_after_relogin_when_second_session_still_requires_it(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-repeat-after-relogin.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/log-in/password",
                }
            ),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "POST",
                    "continue_url": "https://auth.example.test/email-verification",
                }
            ),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                }
            ),
        ),
        (
            "GET",
            "https://auth.example.test/about-you",
            DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you</html>"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["create_account"],
            DummyResponse(
                status_code=400,
                payload={
                    "error": {
                        "message": "An account already exists for this email address.",
                        "code": "user_already_exists",
                    }
                },
                text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
            ),
        ),
        (
            "GET",
            "https://auth.example.test/flow/1",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login</html>",
            ),
        ),
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                },
                on_return=lambda queue_session: queue_session.cookies.__setitem__(
                    "__Secure-next-auth.session-token",
                    "session-about-you-repeat",
                ),
            ),
        ),
        (
            "GET",
            "https://auth.example.test/about-you",
            DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you again</html>"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["create_account"],
            DummyResponse(
                status_code=400,
                payload={
                    "error": {
                        "message": "An account already exists for this email address.",
                        "code": "user_already_exists",
                    },
                    "callback_url": "http://localhost:1455/auth/callback?code=code-about-you-repeat&state=state-2",
                },
                text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you-repeat")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you-repeat"
    assert result.session_token == "session-about-you-repeat"
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["create_account"]]) == 2

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you-repeat"


def test_run_recovers_outlook_account_from_about_you_next_data_callback(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-next-data-callback.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        ("POST", OPENAI_API_ENDPOINTS["signup"], DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}})),
        ("POST", OPENAI_API_ENDPOINTS["password_verify"], DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                }
            ),
        ),
        ("GET", "https://auth.example.test/about-you", _response_with_about_you_next_data_callback()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["create_account"],
            DummyResponse(
                status_code=400,
                payload={
                    "error": {
                        "message": "An account already exists for this email address.",
                        "code": "user_already_exists",
                    }
                },
                text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you-next-data")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you-next-data"
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 1
    assert result.metadata["last_oauth_resume_source"] in {
        "callback_found_from_about_you_page_text_1",
        "callback_found_from_cached_navigation",
    }

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you-next-data"


def test_run_recovers_outlook_account_from_about_you_next_data_resume(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-next-data-resume.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    resume_url = "https://auth.example.test/api/oauth/oauth2/auth?client_id=client-1&state=resume-about-you"
    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        ("POST", OPENAI_API_ENDPOINTS["signup"], DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}})),
        ("POST", OPENAI_API_ENDPOINTS["password_verify"], DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                }
            ),
        ),
        ("GET", "https://auth.example.test/about-you", _response_with_about_you_next_data_resume(resume_url)),
        (
            "POST",
            OPENAI_API_ENDPOINTS["create_account"],
            DummyResponse(
                status_code=400,
                payload={
                    "error": {
                        "message": "An account already exists for this email address.",
                        "code": "user_already_exists",
                    }
                },
                text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
            ),
        ),
        (
            "GET",
            resume_url,
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-about-you-resume&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-about-you-next-resume")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-about-you-next-resume"
    assert any(call["url"] == resume_url for call in session.calls)
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 1

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-about-you-next-resume"


def test_run_stops_relogin_after_repeated_about_you_user_exists_without_resume_evidence(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-about-you-user-exists-exhausted.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    user_exists_response = DummyResponse(
        status_code=400,
        payload={
            "error": {
                "message": "An account already exists for this email address.",
                "code": "user_already_exists",
            }
        },
        text='{"error":{"message":"An account already exists for this email address.","code":"user_already_exists"}}',
    )

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        ("POST", OPENAI_API_ENDPOINTS["signup"], DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}})),
        ("POST", OPENAI_API_ENDPOINTS["password_verify"], DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                }
            ),
        ),
        ("GET", "https://auth.example.test/about-you", DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you</html>")),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], user_exists_response),
        (
            "GET",
            "https://auth.example.test/flow/1",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login</html>",
            ),
        ),
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        ("POST", OPENAI_API_ENDPOINTS["signup"], DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}})),
        ("POST", OPENAI_API_ENDPOINTS["password_verify"], DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]},
                    "method": "GET",
                    "continue_url": "https://auth.example.test/about-you",
                }
            ),
        ),
        ("GET", "https://auth.example.test/about-you", DummyResponse(status_code=200, url="https://auth.example.test/about-you", text="<html>about you again</html>")),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], user_exists_response),
        (
            "GET",
            "https://auth.example.test/flow/2",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login again</html>",
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is False
    assert result.error_message == "about-you 返回 user_already_exists，但连续两次未暴露 callback/workspace"
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 2
    assert any("停止重复登录" in log for log in engine.logs)

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "failed"
        assert account.extra_data["last_recovery_error"] == result.error_message
        assert account.extra_data["last_oauth_resume_source"] == "about_you_user_exists_without_resume_exhausted"
        assert account.extra_data["last_workspace_resolution_source"] == "about_you_user_exists_without_resume"


def test_run_recovers_outlook_account_from_authorize_replay_login_challenge(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-authorize-login-challenge-success.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies_without_workspace()),
        (
            "GET",
            "https://auth.example.test/flow/1",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/login-challenge?login_challenge=replay-success"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/login-challenge?login_challenge=replay-success",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["789012"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-authorize-replay")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-authorize-replay"
    assert result.metadata["last_oauth_resume_source"] == "callback_found_from_authorize_replay"
    assert "callback_found_in_redirect_chain" in (result.metadata["last_recovery_debug_summary"] or "")
    assert "%3Credacted%3E" in (result.metadata["last_recovery_debug_summary"] or "")

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-authorize-replay"


def test_submit_login_start_omits_screen_hint_like_login_web():
    session = QueueSession([
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
    ])

    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)
    engine.session = session
    engine.email = "tester@example.com"

    result = engine._submit_login_start("did-1", "sentinel-1")

    assert result.success is True
    payload = json.loads(session.calls[0]["kwargs"]["data"])
    assert payload == {
        "username": {
            "value": "tester@example.com",
            "kind": "email",
        },
    }


def test_start_saved_password_recovery_uses_login_screen_hint():
    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
    ])

    email_service = FakeOutlookEmailService([])
    engine = RegistrationEngine(email_service)
    engine.email = "tester@example.com"
    engine.password = "saved-password"
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManager()

    ok, error = engine._start_saved_password_recovery()

    assert ok is True
    assert error == ""
    payload = json.loads(session.calls[1]["kwargs"]["data"])
    assert payload["screen_hint"] == "login"


def test_run_recovers_outlook_account_with_session_bound_reauth_after_bare_login(monkeypatch):
    from src.database import crud
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-session-bound-reauth-success.db")

    with manager.session_scope() as session:
        crud.create_account(
            session,
            email="tester@example.com",
            password="saved-password",
            email_service="outlook",
            email_service_id="mailbox-1",
            status="failed",
            source="register",
            extra_data={
                "recovery_ready": True,
                "account_created": True,
                "token_acquired": False,
                "register_failed_reason": "token_recovery_pending",
                "last_recovery_error": "获取 Workspace ID 失败",
            },
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies_without_workspace()),
        (
            "GET",
            "https://auth.example.test/flow/1",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/login-challenge?login_challenge=reauth-1"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/login-challenge?login_challenge=reauth-1",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(status_code=200, payload={}, url="https://auth.example.test/log-in"),
        ),
        (
            "GET",
            "https://auth.example.test/login-challenge?login_challenge=reauth-1",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(status_code=200, payload={}, url="https://auth.example.test/log-in"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            _response_with_callback_before_workspace(
                callback_url="http://localhost:1455/auth/callback?code=code-reauth&state=state-reauth",
                session_token="session-reauth",
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-session-reauth")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-session-reauth"
    assert result.session_token == "session-reauth"
    assert result.metadata["last_oauth_resume_source"] == "session_bound_reauth"
    assert "session_bound_reauth_callback_resolved" in (result.metadata["last_recovery_debug_summary"] or "")
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 2
    assert [item["stage"] for item in email_service.otp_requests] == ["relogin_otp", "relogin_otp"]

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account.status == "active"
        assert account.workspace_id == "ws-session-reauth"


def test_run_reports_reentered_login_page_when_fresh_authorize_replay_loops_back_to_log_in(monkeypatch):
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-authorize-replay-fail.db")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies_without_workspace()),
        (
            "GET",
            "https://auth.example.test/flow/2",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/login-challenge?login_challenge=replay-1"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/login-challenge?login_challenge=replay-1",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(status_code=200, payload={}, url="https://auth.example.test/log-in"),
        ),
        (
            "GET",
            "https://auth.example.test/login-challenge?login_challenge=replay-1",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(status_code=200, payload={}, url="https://auth.example.test/log-in"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies_without_workspace()),
        (
            "GET",
            "https://auth.example.test/flow/2",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/login-challenge?login_challenge=replay-2"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/login-challenge?login_challenge=replay-2",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(status_code=200, payload={}, url="https://auth.example.test/log-in"),
        ),
        (
            "GET",
            "https://auth.example.test/login-challenge?login_challenge=replay-2",
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/log-in"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/log-in",
            DummyResponse(status_code=200, payload={}, url="https://auth.example.test/log-in"),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321", "777888"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient(
        [session_one, session_two],
        ["sentinel-1", "sentinel-2", "sentinel-3"],
    )
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is False
    assert result.error_message == "OAuth 恢复链路重新进入登录页"

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        extra_data = dict(account.extra_data or {})

    assert extra_data["recovery_ready"] is True
    assert extra_data["last_recovery_error"] == "OAuth 恢复链路重新进入登录页"
    assert extra_data["last_oauth_resume_source"] == "session_bound_reauth_reentered_login"
    assert "session_bound_reauth_reentered_login" in (extra_data.get("last_recovery_debug_summary") or "")


def test_run_records_workspace_resolution_metadata_when_all_sources_missing(monkeypatch):
    from src.database.models import Account

    manager = _make_engine_session_manager("recoverable-workspace-metadata.db")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies_without_workspace()),
    ])

    email_service = FakeOutlookEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is False

    with manager.session_scope() as session:
        account = session.query(Account).filter(Account.email == "tester@example.com").first()
        extra_data = dict(account.extra_data or {})

    assert extra_data["workspace_acquired"] is False
    assert extra_data["last_workspace_resolution_source"] == "auth_cookie_decoded_without_workspace"
    assert extra_data["last_workspace_resolution_error"] == "获取 Workspace ID 失败"


def _outlook_verification_email(code, received_timestamp):
    return EmailMessage(
        id=f"msg-{code}-{received_timestamp}",
        subject=f"Your ChatGPT code is {code}",
        sender="noreply@tm.openai.com",
        recipients=["tester@outlook.com"],
        body=f"Your ChatGPT code is {code}",
        received_at=datetime.fromtimestamp(received_timestamp),
        received_timestamp=received_timestamp,
        is_read=False,
    )


def _outlook_verification_email_from_sender(code, received_timestamp, sender):
    return EmailMessage(
        id=f"msg-{code}-{received_timestamp}-{sender}",
        subject=f"Your ChatGPT code is {code}",
        sender=sender,
        recipients=["tester@outlook.com"],
        body=f"Your ChatGPT code is {code}",
        received_at=datetime.fromtimestamp(received_timestamp),
        received_timestamp=received_timestamp,
        is_read=False,
    )


def test_outlook_verification_codes_are_isolated_by_stage(monkeypatch):
    service = OutlookService({
        "email": "tester@outlook.com",
        "password": "pw",
    })
    emails = [_outlook_verification_email("123456", 1_700_000_000)]

    monkeypatch.setattr(
        "src.services.outlook.service.get_email_code_settings",
        lambda: {"timeout": 1, "poll_interval": 0},
    )
    monkeypatch.setattr(service, "_try_providers_for_emails", lambda account, count=15, only_unseen=True: emails)

    service.set_verification_stage("tester@outlook.com", "signup_otp")
    first_code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=1_700_000_000,
    )

    service.set_verification_stage("tester@outlook.com", "relogin_otp")
    second_code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=1_700_000_000,
    )

    assert first_code == "123456"
    assert second_code == "123456"


def test_outlook_verification_rejects_codes_outside_current_window(monkeypatch):
    service = OutlookService({
        "email": "tester@outlook.com",
        "password": "pw",
    })
    old_email = [_outlook_verification_email("123456", 1_700_000_000)]

    monkeypatch.setattr(
        "src.services.outlook.service.get_email_code_settings",
        lambda: {"timeout": 1, "poll_interval": 0},
    )
    monkeypatch.setattr(service, "_try_providers_for_emails", lambda account, count=15, only_unseen=True: old_email)

    service.set_verification_stage("tester@outlook.com", "relogin_otp")
    code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=1_700_000_200,
    )

    assert code is None


def test_outlook_verification_allows_same_code_from_newer_email_in_same_stage(monkeypatch):
    service = OutlookService({
        "email": "tester@outlook.com",
        "password": "pw",
    })
    first_email = _outlook_verification_email("123456", 1_700_000_000)
    second_email = _outlook_verification_email("123456", 1_700_000_030)
    batches = [
        [first_email],
        [second_email, first_email],
    ]

    monkeypatch.setattr(
        "src.services.outlook.service.get_email_code_settings",
        lambda: {"timeout": 1, "poll_interval": 0},
    )
    monkeypatch.setattr(
        service,
        "_try_providers_for_emails",
        lambda account, count=15, only_unseen=True: batches.pop(0) if batches else [second_email, first_email],
    )

    service.set_verification_stage("tester@outlook.com", "relogin_otp")
    first_code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=1_700_000_000,
    )

    service.set_verification_stage("tester@outlook.com", "relogin_otp")
    second_code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=1_700_000_030,
    )

    assert first_code == "123456"
    assert second_code == "123456"


def test_outlook_relogin_prefers_otp_sender_before_generic_noreply(monkeypatch):
    service = OutlookService({
        "email": "tester@outlook.com",
        "password": "pw",
    })
    generic_email = _outlook_verification_email_from_sender(
        "111111",
        1_700_000_000,
        "noreply@tm.openai.com",
    )
    preferred_email = _outlook_verification_email_from_sender(
        "222222",
        1_700_000_010,
        "OpenAI <otp@tm1.openai.com>",
    )
    batches = [
        [generic_email],
        [preferred_email, generic_email],
    ]

    monkeypatch.setattr(
        "src.services.outlook.service.get_email_code_settings",
        lambda: {"timeout": 1, "poll_interval": 0},
    )
    monkeypatch.setattr(
        service,
        "_try_providers_for_emails",
        lambda account, count=15, only_unseen=True: batches.pop(0) if batches else [preferred_email, generic_email],
    )

    service.set_verification_stage("tester@outlook.com", "relogin_otp")
    code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=1_699_999_990,
    )

    assert code == "222222"


def test_outlook_verification_debug_tracks_poll_and_selected_sender(monkeypatch):
    service = OutlookService({
        "email": "tester@outlook.com",
        "password": "pw",
    })
    generic_email = _outlook_verification_email_from_sender(
        "111111",
        1_700_000_000,
        "noreply@tm.openai.com",
    )
    preferred_email = _outlook_verification_email_from_sender(
        "222222",
        1_700_000_010,
        "OpenAI <otp@tm1.openai.com>",
    )
    batches = [
        [generic_email],
        [preferred_email, generic_email],
    ]

    monkeypatch.setattr(
        "src.services.outlook.service.get_email_code_settings",
        lambda: {"timeout": 1, "poll_interval": 0},
    )
    monkeypatch.setattr(
        service,
        "_try_providers_for_emails",
        lambda account, count=15, only_unseen=True: batches.pop(0) if batches else [preferred_email, generic_email],
    )

    service.set_verification_stage("tester@outlook.com", "relogin_otp")
    code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=1_699_999_990,
    )
    debug = service.get_last_verification_debug("tester@outlook.com")

    assert code == "222222"
    assert debug["stage"] == "relogin_otp"
    assert debug["poll_count"] == 2
    assert debug["selected_sender"] == "OpenAI <otp@tm1.openai.com>"
    assert debug["selected_code"] == "222222"
    assert debug["deferred_generic_only_polls"] == 1


def test_outlook_verification_uses_fresh_generic_when_preferred_candidates_are_stale(monkeypatch):
    service = OutlookService({
        "email": "tester@outlook.com",
        "password": "pw",
    })
    otp_sent_at = 1_700_000_200
    fresh_generic_email = _outlook_verification_email_from_sender(
        "333333",
        otp_sent_at,
        "noreply@tm.openai.com",
    )
    stale_preferred_email = _outlook_verification_email_from_sender(
        "222222",
        otp_sent_at - 600,
        "OpenAI <otp@tm1.openai.com>",
    )

    monkeypatch.setattr(
        "src.services.outlook.service.get_email_code_settings",
        lambda: {"timeout": 1, "poll_interval": 0},
    )
    monkeypatch.setattr(
        service,
        "_try_providers_for_emails",
        lambda account, count=15, only_unseen=True: [fresh_generic_email, stale_preferred_email],
    )

    service.set_verification_stage("tester@outlook.com", "relogin_otp")
    code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=otp_sent_at,
    )
    debug = service.get_last_verification_debug("tester@outlook.com")

    assert code == "333333"
    assert debug["selected_sender"] == "noreply@tm.openai.com"
    assert debug["selected_code"] == "333333"
    assert debug["fresh_verification_count"] == 1
    assert debug["fresh_preferred_sender_count"] == 0
    assert debug["stale_preferred_sender_count"] == 1


def test_outlook_verification_uses_unused_generic_when_fresh_preferred_was_already_consumed(monkeypatch):
    service = OutlookService({
        "email": "tester@outlook.com",
        "password": "pw",
    })
    first_preferred_email = _outlook_verification_email_from_sender(
        "444444",
        1_700_000_000,
        "OpenAI <otp@tm1.openai.com>",
    )
    second_generic_email = _outlook_verification_email_from_sender(
        "444444",
        1_700_000_030,
        "noreply@tm.openai.com",
    )
    batches = [
        [first_preferred_email],
        [second_generic_email, first_preferred_email],
        [second_generic_email, first_preferred_email],
        [second_generic_email, first_preferred_email],
    ]

    monkeypatch.setattr(
        "src.services.outlook.service.get_email_code_settings",
        lambda: {"timeout": 1, "poll_interval": 0},
    )
    monkeypatch.setattr(
        service,
        "_try_providers_for_emails",
        lambda account, count=15, only_unseen=True: batches.pop(0) if batches else [second_generic_email, first_preferred_email],
    )

    service.set_verification_stage("tester@outlook.com", "relogin_otp")
    first_code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=1_700_000_000,
    )

    service.set_verification_stage("tester@outlook.com", "relogin_otp")
    second_code = service.get_verification_code(
        email="tester@outlook.com",
        timeout=1,
        otp_sent_at=1_700_000_030,
    )
    debug = service.get_last_verification_debug("tester@outlook.com")

    assert first_code == "444444"
    assert second_code == "444444"
    assert debug["selected_sender"] == "noreply@tm.openai.com"
    assert debug["available_fresh_preferred_sender_count"] == 0
    assert debug["used_fresh_preferred_sender_count"] == 1


def test_validate_otp_logs_error_context_for_non_200_response():
    session = QueueSession([
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                status_code=400,
                payload={
                    "error": {
                        "message": "Incorrect code",
                        "code": "invalid_request_error",
                    }
                },
                text='{"error":{"message":"Incorrect code","code":"invalid_request_error"}}',
                headers={"x-request-id": "req-validate-1"},
                url=OPENAI_API_ENDPOINTS["validate_otp"],
            ),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = session

    ok = engine._validate_verification_code("123456")

    assert ok is False
    assert any("req-validate-1" in log for log in engine.logs)
    assert any("invalid_request_error" in log for log in engine.logs)
    assert any("Incorrect code" in log for log in engine.logs)
