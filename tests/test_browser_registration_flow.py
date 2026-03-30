from types import SimpleNamespace

from src.core.registration_engines.browser import BrowserRegistrationEngine


class _FakeElement:
    def __init__(self, on_click=None, fail_click=False):
        self.clicked = 0
        self.wait = SimpleNamespace(displayed=lambda timeout=1: True)
        self.on_click = on_click
        self.fail_click = fail_click

    def click(self):
        if self.fail_click:
            raise RuntimeError("click failed")
        self.clicked += 1
        if self.on_click:
            self.on_click()


class _FakePage:
    def __init__(self, selector_map=None, url="about:blank", title="ChatGPT"):
        self.selector_map = selector_map or {}
        self.url = url
        self.title = title
        self.visited = []
        self.cookie_rows = []
        self.js_calls = []

    def get(self, url):
        self.visited.append(url)
        self.url = url

    def ele(self, selector, timeout=0):
        return self.selector_map.get(selector)

    def cookies(self):
        return list(self.cookie_rows)

    def run_js(self, script, *args):
        self.js_calls.append((script, args))


def _build_engine():
    email_service = SimpleNamespace(service_type=SimpleNamespace(value="tempmail"))
    return BrowserRegistrationEngine(email_service=email_service, callback_logger=lambda *_: None)


def test_open_signup_entry_prefers_auth_login_and_signup_testid(monkeypatch):
    engine = _build_engine()
    signup_button = _FakeElement()
    engine.page = _FakePage(
        selector_map={
            'css:button[data-testid="signup-button"]': signup_button,
        }
    )
    monkeypatch.setattr("src.core.registration_engines.browser.time.sleep", lambda *_: None)

    assert engine._open_signup_entry() is True
    assert engine.page.visited == ["https://chatgpt.com/auth/login"]
    assert signup_button.clicked == 1


def test_locate_email_input_accepts_generic_type_email_selector():
    engine = _build_engine()
    email_input = _FakeElement()
    engine.page = _FakePage(
        selector_map={
            'css:input[type="email"]': email_input,
        }
    )

    assert engine._locate_email_input() is email_input


def test_wait_for_post_auth_ready_accepts_chatgpt_surface_without_auth_openai_transition(monkeypatch):
    engine = _build_engine()
    prompt = _FakeElement()
    engine.page = _FakePage(
        selector_map={
            'xpath=//textarea[@id="prompt-textarea"]': prompt,
        },
        url="https://chatgpt.com/",
    )
    monkeypatch.setattr("src.core.registration_engines.browser.time.sleep", lambda *_: None)

    assert engine._wait_for_post_auth_ready(max_checks=1, sleep_seconds=0) is True


class _FakeCookieJar:
    def __init__(self):
        self.values = {}
        self.set_calls = []

    def set(self, name, value, domain=None, path=None):
        self.values[name] = value
        self.set_calls.append((name, value, domain, path))

    def get(self, name, default=None):
        return self.values.get(name, default)

    def __iter__(self):
        for name, value in self.values.items():
            yield SimpleNamespace(name=name, value=value)


class _FakeHeaders:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def get(self, key, default=None):
        return self.mapping.get(key, default)

    def get_all(self, key):
        value = self.mapping.get(key, "")
        if not value:
            return []
        if isinstance(value, list):
            return value
        return [value]


class _FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None, request_headers=None):
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = _FakeHeaders(headers)
        self.request = SimpleNamespace(headers=request_headers or {})

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cookies = _FakeCookieJar()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_capture_auth_session_via_http_uses_set_cookie_and_json_access_token():
    engine = _build_engine()
    engine.page = _FakePage()
    engine.page.cookie_rows = [
        {"name": "__Host-next-auth.csrf-token", "value": "csrf-1", "domain": "chatgpt.com", "path": "/"},
    ]
    session = _FakeSession(
        [
            _FakeResponse(
                payload={
                    "accessToken": "access-http",
                    "user": {"id": "user-1", "email_verified": True},
                    "account": {"id": "acct-1", "planType": "free"},
                    "expires": "2099-01-01T00:00:00.000Z",
                },
                headers={
                    "Set-Cookie": "__Secure-next-auth.session-token=http-session-token; Path=/; Secure; HttpOnly"
                },
            )
        ]
    )
    engine.browser_client = SimpleNamespace(
        session=session,
        persona=SimpleNamespace(user_agent="UA-test/1.0"),
    )

    token, access_token, metadata = engine._capture_auth_session_via_http()

    assert token == "http-session-token"
    assert access_token == "access-http"
    assert metadata["user_id"] == "user-1"
    assert metadata["account_id"] == "acct-1"
    assert metadata["workspace_id"] == "acct-1"
    assert metadata["method"] == "http_session_capture"
    assert session.cookies.set_calls[0][0] == "__Host-next-auth.csrf-token"


def test_capture_auth_session_via_http_assembles_chunked_session_cookie_from_response_headers():
    engine = _build_engine()
    engine.page = _FakePage()
    session = _FakeSession(
        [
            _FakeResponse(
                payload={},
                headers={
                    "Set-Cookie": [
                        "__Secure-next-auth.session-token.0=chunk-A; Path=/; Secure; HttpOnly",
                        "__Secure-next-auth.session-token.1=chunk-B; Path=/; Secure; HttpOnly",
                    ]
                },
            )
        ]
    )
    engine.browser_client = SimpleNamespace(
        session=session,
        persona=SimpleNamespace(user_agent="UA-test/1.0"),
    )

    token, access_token, metadata = engine._capture_auth_session_via_http()

    assert token == "chunk-Achunk-B"
    assert access_token == ""
    assert metadata["method"] == "http_session_capture"


def test_dismiss_post_auth_prompts_reuses_reference_kill_list_and_stops_at_prompt():
    engine = _build_engine()
    page = _FakePage(url="https://chatgpt.com/")

    def _after_continue():
        page.selector_map.pop("text=Continue", None)
        page.selector_map['xpath=//textarea[@id="prompt-textarea"]'] = _FakeElement()

    continue_btn = _FakeElement(on_click=_after_continue)
    page.selector_map["text=Continue"] = continue_btn
    engine.page = page

    assert engine._dismiss_post_auth_prompts(max_checks=2, sleep_seconds=0) is True
    assert continue_btn.clicked == 1


def test_resume_email_stage_refills_email_when_context_is_lost():
    engine = _build_engine()
    typed = []
    email_input = _FakeElement()
    email_input.input = lambda value: typed.append(value)
    continue_btn = _FakeElement()
    engine.email = "debug@example.com"
    engine.page = _FakePage(
        selector_map={
            'css:input[type="email"]': email_input,
            "text=Continue": continue_btn,
        },
        url="https://chatgpt.com/auth/login",
    )

    assert engine._resume_email_stage_if_needed() is True
    assert typed == ["debug@example.com"]
    assert continue_btn.clicked == 1


def test_wait_for_challenge_resolution_detects_cloudflare_title_and_succeeds_after_clear(monkeypatch):
    engine = _build_engine()
    page = _FakePage(url="https://chatgpt.com/auth/login", title="Just a moment...")
    states = ["Just a moment...", "Just a moment...", "ChatGPT"]

    def _sleep(_):
        if states:
            page.title = states.pop(0)

    engine.page = page
    monkeypatch.setattr("src.core.registration_engines.browser.time.sleep", _sleep)

    assert engine._wait_for_challenge_resolution(max_checks=3, sleep_seconds=0) is True


def test_wait_for_challenge_resolution_returns_false_when_challenge_persists(monkeypatch):
    engine = _build_engine()
    engine.page = _FakePage(url="https://chatgpt.com/auth/login", title="Just a moment...")
    monkeypatch.setattr("src.core.registration_engines.browser.time.sleep", lambda *_: None)

    assert engine._wait_for_challenge_resolution(max_checks=2, sleep_seconds=0) is False
