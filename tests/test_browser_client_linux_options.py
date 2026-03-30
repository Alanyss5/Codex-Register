import types

from src.core.registration_engines import browser_client
from src.core.registration_engines.persona import build_persona


class _FakeChromiumOptions:
    def __init__(self):
        self.arguments = []
        self.browser_path = None
        self.user_data_path = None
        self.proxy = None
        self.headless_value = None
        self.local_port = None

    def set_local_port(self, port):
        self.local_port = port
        return self

    def set_argument(self, argument):
        self.arguments.append(argument)
        return self

    def headless(self, on_off=True):
        self.headless_value = on_off
        return self

    def set_browser_path(self, path):
        self.browser_path = path
        return self

    def set_user_data_path(self, path):
        self.user_data_path = path
        return self

    def set_proxy(self, proxy):
        self.proxy = proxy
        return self


def test_build_options_enables_headless_and_auto_port_on_linux(monkeypatch):
    fake_module = types.SimpleNamespace(ChromiumOptions=_FakeChromiumOptions)

    monkeypatch.setattr(browser_client.sys, "platform", "linux")
    monkeypatch.setattr(browser_client, "tempfile", types.SimpleNamespace(mkdtemp=lambda prefix: "/tmp/fake-browser"))
    monkeypatch.setattr(browser_client.os.path, "exists", lambda path: path == "/usr/bin/chromium")
    monkeypatch.setitem(__import__("sys").modules, "DrissionPage", fake_module)
    monkeypatch.setattr(browser_client.BrowserClient, "detect_runtime_locale", lambda self: ("en-US", "US"))
    monkeypatch.setattr(browser_client.BrowserClient, "_find_free_local_port", staticmethod(lambda: 45678))

    client = browser_client.BrowserClient(proxy_url="http://127.0.0.1:8080")
    client.persona = build_persona(runtime_country="US", runtime_language="en-US")
    client.persona = client.persona.__class__(**{**client.persona.__dict__, "profile": types.SimpleNamespace(**{**client.persona.profile.__dict__, "user_agent": "UA/1", "full_version": "131.0.0.1"})})
    options = client._build_options()

    assert options.headless_value is True
    assert options.local_port == 45678
    assert options.browser_path == "/usr/bin/chromium"
    assert "--lang=en-US" in options.arguments
    assert "--user-agent=UA/1" in options.arguments
    assert "--proxy-server=127.0.0.1:8080" in options.arguments


def test_apply_page_stealth_uses_persona_overrides(monkeypatch):
    class FakePage:
        def __init__(self):
            self.init_scripts = []
            self.cdp_calls = []

        def add_init_js(self, script):
            self.init_scripts.append(script)

        def run_cdp(self, method, **kwargs):
            self.cdp_calls.append((method, kwargs))

    fake_page = FakePage()
    client = browser_client.BrowserClient(proxy_url=None)
    client.persona = build_persona(runtime_country="US", runtime_language="en-US")
    client.page = fake_page

    client._apply_page_stealth()

    assert fake_page.init_scripts
    assert any(call[0] == "Network.setUserAgentOverride" for call in fake_page.cdp_calls)
    assert any(call[0] == "Emulation.setTimezoneOverride" for call in fake_page.cdp_calls)
    assert any(call[0] == "Emulation.setLocaleOverride" for call in fake_page.cdp_calls)
