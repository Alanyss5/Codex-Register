from types import SimpleNamespace

from src.config.constants import EmailServiceType
from src.core.register import RegistrationEngine


class FakeEmailService:
    def __init__(self, service_type=EmailServiceType.TEMPMAIL):
        self.service_type = service_type
        self.created = 0

    def create_email(self, config=None):
        self.created += 1
        return {"email": "tester@example.com", "service_id": "mailbox-1"}


class FakeProtocolEngine:
    instances = []
    chatgpt_result = (True, "registered")
    session_result = (True, {})
    oauth_result = (False, "oauth-not-used")

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.logs = []
        self.blacklisted_domains = []
        FakeProtocolEngine.instances.append(self)

    def run(self):
        from src.core.register import RegistrationResult

        email_service = self.kwargs["email_service"]
        email_info = email_service.create_email()
        email = email_info["email"]

        register_ok, register_payload = self.chatgpt_result
        if register_ok:
            session_ok, session_payload = self.session_result
            if session_ok:
                return RegistrationResult(
                    success=True,
                    email=email,
                    password="Passw0rd!123",
                    account_id=session_payload.get("account_id", "acct-1"),
                    workspace_id=session_payload.get("workspace_id", "ws-1"),
                    access_token=session_payload.get("access_token", "access-1"),
                    session_token=session_payload.get("session_token", "session-1"),
                    metadata={
                        "registration_engine": "protocol",
                        "auth_provider": session_payload.get("auth_provider", ""),
                    },
                    logs=self.logs,
                )
            return RegistrationResult(success=False, email=email, error_message=str(session_payload), logs=self.logs)

        if self._looks_like_existing_account(str(register_payload)):
            oauth_ok, oauth_payload = self.oauth_result
            if oauth_ok:
                return RegistrationResult(
                    success=True,
                    email=email,
                    password="Passw0rd!123",
                    account_id=oauth_payload.get("account_id", "acct-login"),
                    workspace_id=oauth_payload.get("workspace_id", "ws-login"),
                    access_token=oauth_payload.get("access_token", "access-login"),
                    refresh_token=oauth_payload.get("refresh_token", "refresh-login"),
                    id_token=oauth_payload.get("id_token", "id-login"),
                    metadata={
                        "registration_engine": "protocol",
                        "auth_provider": "oauth",
                    },
                    source="login",
                    logs=self.logs,
                )

        if self._should_blacklist_domain(email, str(register_payload)):
            self.blacklisted_domains.append(email.split("@", 1)[1].lower())

        return RegistrationResult(
            success=False,
            email=email,
            error_message=str(register_payload),
            metadata={"registration_engine": "protocol"},
            logs=self.logs,
        )

    @staticmethod
    def _looks_like_existing_account(message: str) -> bool:
        lowered = message.lower()
        return "already exists" in lowered or "already registered" in lowered or "user_exists" in lowered

    @staticmethod
    def _should_blacklist_domain(email: str, message: str) -> bool:
        lowered = message.lower()
        return (
            email.endswith("@example.com")
            and (
                "already exists" in lowered
                or "already registered" in lowered
                or "user_exists" in lowered
            )
        )


def _patch_protocol_engine(monkeypatch):
    FakeProtocolEngine.instances.clear()
    monkeypatch.setattr("src.core.register.ProtocolRegistrationEngineV2", FakeProtocolEngine)


def test_registration_engine_run_delegates_to_v2_protocol_engine(monkeypatch):
    _patch_protocol_engine(monkeypatch)
    FakeProtocolEngine.chatgpt_result = (True, "registered")
    FakeProtocolEngine.session_result = (
        True,
        {
            "access_token": "access-v2",
            "session_token": "session-v2",
            "account_id": "acct-v2",
            "workspace_id": "ws-v2",
            "auth_provider": "nextauth",
        },
    )

    engine = RegistrationEngine(FakeEmailService(), proxy_url="http://proxy:8080", proxy_source="auto")
    result = engine.run()

    assert result.success is True
    assert result.email == "tester@example.com"
    assert result.account_id == "acct-v2"
    assert result.workspace_id == "ws-v2"
    assert result.access_token == "access-v2"
    assert result.session_token == "session-v2"
    assert result.metadata["registration_engine"] == "protocol"
    assert FakeProtocolEngine.instances[0].kwargs["proxy_url"] == "http://proxy:8080"
    assert FakeProtocolEngine.instances[0].kwargs["proxy_source"] == "auto"


def test_registration_engine_run_uses_oauth_fallback_for_existing_account(monkeypatch):
    _patch_protocol_engine(monkeypatch)
    FakeProtocolEngine.chatgpt_result = (False, "already exists for this email address")
    FakeProtocolEngine.oauth_result = (
        True,
        {
            "access_token": "oauth-access",
            "refresh_token": "oauth-refresh",
            "id_token": "oauth-id",
            "account_id": "acct-oauth",
            "workspace_id": "ws-oauth",
        },
    )

    engine = RegistrationEngine(FakeEmailService(), proxy_url=None, proxy_source="direct")
    result = engine.run()

    assert result.success is True
    assert result.source == "login"
    assert result.account_id == "acct-oauth"
    assert result.workspace_id == "ws-oauth"
    assert result.access_token == "oauth-access"
    assert result.refresh_token == "oauth-refresh"
    assert result.id_token == "oauth-id"
    assert result.metadata["auth_provider"] == "oauth"


def test_registration_engine_run_blacklists_domain_on_disposable_existing_account(monkeypatch):
    _patch_protocol_engine(monkeypatch)
    FakeProtocolEngine.chatgpt_result = (False, "user_exists")
    FakeProtocolEngine.oauth_result = (False, "oauth-skipped")

    engine = RegistrationEngine(FakeEmailService(), proxy_url=None, proxy_source="direct")
    result = engine.run()

    assert result.success is False
    assert result.error_message == "user_exists"
    assert FakeProtocolEngine.instances[0].blacklisted_domains == ["example.com"]


def test_protocol_v2_blacklist_persists_domain_setting(monkeypatch):
    from contextlib import contextmanager

    from src.core.protocol_v2.engine import ProtocolRegistrationEngineV2

    writes = {}

    @contextmanager
    def fake_get_db():
        yield object()

    monkeypatch.setattr("src.core.protocol_v2.engine.get_db", fake_get_db)
    monkeypatch.setattr("src.core.protocol_v2.engine.crud.get_setting", lambda db, key: None)

    def fake_set_setting(db, key, value, description, category):
        writes["key"] = key
        writes["value"] = value
        writes["description"] = description
        writes["category"] = category

    monkeypatch.setattr("src.core.protocol_v2.engine.crud.set_setting", fake_set_setting)

    engine = ProtocolRegistrationEngineV2(email_service=FakeEmailService(EmailServiceType.TEMPMAIL))

    changed = engine._blacklist_domain_if_needed("tester@example.com", "user_exists")

    assert changed is True
    assert writes["key"] == "email.domain_blacklist"
    assert "example.com" in writes["value"]
