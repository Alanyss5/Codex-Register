from contextlib import contextmanager

from src.config.constants import OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES
from src.core.register import RegistrationEngine
from src.core.registration_post_create import (
    POST_CREATE_REENTERED_LOGIN_SOURCE,
    POST_CREATE_RESUME_SOURCE,
)
from src.database.models import Account
from tests.test_registration_engine import (
    DummyResponse,
    FakeEmailService,
    FakeOpenAIClient,
    FakeOAuthManagerWithWorkspace,
    FakeOutlookEmailService,
    QueueSession,
    _make_engine_session_manager,
    _response_with_did,
    _workspace_cookie,
)


def test_run_prefers_locked_post_create_continue_over_cached_about_you():
    def create_account_add_phone(session):
        return DummyResponse(
            payload={
                "page": {"type": "add_phone"},
                "continue_url": "https://auth.example.test/add-phone",
            },
            url=OPENAI_API_ENDPOINTS["create_account"],
        )

    def consent_page_with_workspace(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie("ws-post-create")
        session.cookies["__Secure-next-auth.session-token"] = "session-post-create"
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
            DummyResponse(payload={"continue_url": "https://auth.example.test/post-create-continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/post-create-continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-post-create&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-post-create")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-post-create"
    assert result.session_token == "session-post-create"
    assert result.metadata["token_acquired_via_relogin"] is False
    assert result.metadata["last_oauth_resume_source"] == POST_CREATE_RESUME_SOURCE
    assert any("post_create_continue_overrode_cached_resume" in log for log in engine.logs)
    assert any("post_create_continue_followed" in log for log in engine.logs)
    assert any(call["url"] == "https://auth.example.test/add-phone" for call in session.calls)
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 1


def test_run_can_complete_post_create_direct_consent_without_relogin():
    def create_account_direct_consent(session):
        return DummyResponse(
            payload={
                "page": {"type": "consent"},
                "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            },
            url=OPENAI_API_ENDPOINTS["create_account"],
        )

    def consent_page_with_workspace(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie("ws-post-create-consent")
        session.cookies["__Secure-next-auth.session-token"] = "session-post-create-consent"
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
        ("POST", OPENAI_API_ENDPOINTS["create_account"], create_account_direct_consent),
        ("GET", "https://auth.openai.com/sign-in-with-chatgpt/codex/consent", consent_page_with_workspace),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/post-create-consent-continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/post-create-consent-continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-post-create-consent&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-post-create-consent")

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-post-create-consent"
    assert result.metadata["token_acquired_via_relogin"] is False
    assert result.metadata["last_oauth_resume_source"] == POST_CREATE_RESUME_SOURCE
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 1


def test_run_fails_post_create_reentry_without_relogin_and_persists_recovery(monkeypatch):
    manager = _make_engine_session_manager("post-create-reentered-login.db")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)

    def create_account_add_phone(session):
        return DummyResponse(
            payload={
                "page": {"type": "add_phone"},
                "continue_url": "https://auth.example.test/add-phone",
            },
            url=OPENAI_API_ENDPOINTS["create_account"],
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
        ("POST", OPENAI_API_ENDPOINTS["create_account"], create_account_add_phone),
        (
            "GET",
            "https://auth.example.test/add-phone",
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
            "https://auth.example.test/log-in",
            DummyResponse(
                status_code=200,
                url="https://auth.example.test/log-in",
                text="<html>login diagnostic</html>",
            ),
        ),
    ])

    email_service = FakeOutlookEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManagerWithWorkspace("ws-unused")

    result = engine.run()

    assert result.success is False
    assert result.error_message == "账号已创建，但 post-create 续跑重新进入登录页"
    assert len([call for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["validate_otp"]]) == 1
    assert len([call for call in session.calls if call["url"] == "https://auth.example.test/flow/2"]) == 0
    assert any("post_create_continue_reentered_login" in log for log in engine.logs)

    with manager.session_scope() as session_db:
        account = session_db.query(Account).filter(Account.email == "tester@example.com").first()
        assert account is not None
        assert account.status == "failed"
        assert account.extra_data["last_recovery_error"] == result.error_message
        assert account.extra_data["last_oauth_resume_source"] == POST_CREATE_REENTERED_LOGIN_SOURCE
        assert account.extra_data["last_workspace_resolution_source"] == POST_CREATE_RESUME_SOURCE
