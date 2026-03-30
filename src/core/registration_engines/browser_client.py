"""DrissionPage-based browser client for browser registration engine."""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests

from .persona import SessionPersona, build_persona


logger = logging.getLogger(__name__)


@dataclass
class BrowserRequestConfig:
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    impersonate: str = "chrome"
    verify_ssl: bool = True
    follow_redirects: bool = True


class BrowserClient:
    """Hybrid browser + HTTP client used by the browser registration engine."""

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        config: Optional[BrowserRequestConfig] = None,
        runtime_country: Optional[str] = None,
        runtime_language: str = "en-US",
    ):
        self.proxy_url = proxy_url.strip() if proxy_url else None
        self.config = config or BrowserRequestConfig()
        self.page = None
        self._user_data_path: Optional[str] = None
        self.runtime_country: Optional[str] = runtime_country
        self.runtime_language: str = runtime_language
        self.persona: SessionPersona = build_persona(runtime_country=runtime_country or "US", runtime_language=runtime_language)
        self.api_session = self._build_api_session()

    @property
    def session(self):
        return self.api_session

    def _build_api_session(self):
        proxies = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
        impersonate = getattr(self.persona.profile, "impersonate", None) or self.config.impersonate
        return cffi_requests.Session(
            proxies=proxies,
            impersonate=impersonate,
            verify=self.config.verify_ssl,
            timeout=self.config.timeout,
        )

    def _rebuild_api_session(self):
        try:
            self.api_session.close()
        except Exception:
            pass
        self.api_session = self._build_api_session()

    def get(self, url: str, **kwargs):
        return self.api_session.get(url, **kwargs)

    def post(self, url: str, data=None, json=None, **kwargs):
        return self.api_session.post(url, data=data, json=json, **kwargs)

    def check_ip_location(self) -> Tuple[bool, Optional[str]]:
        try:
            response = self.api_session.get("https://cloudflare.com/cdn-cgi/trace", timeout=10)
            loc_match = re.search(r"loc=([A-Z]+)", response.text)
            loc = loc_match.group(1) if loc_match else "Unknown"
            return (False if loc in ["CN", "HK", "MO", "TW"] else True, loc)
        except Exception:
            return True, "Unknown"

    def detect_runtime_locale(self) -> Tuple[str, Optional[str]]:
        """Infer browser language from proxy exit location."""
        try:
            _, loc = self.check_ip_location()
            self.runtime_country = loc
            if loc in {"CN", "HK", "MO", "TW"}:
                self.runtime_language = "zh-CN"
            elif loc == "JP":
                self.runtime_language = "ja-JP"
            elif loc == "KR":
                self.runtime_language = "ko-KR"
            else:
                self.runtime_language = "en-US"
        except Exception:
            self.runtime_country = "Unknown"
            self.runtime_language = "en-US"
        self.persona = build_persona(self.runtime_country, self.runtime_language)
        self._rebuild_api_session()
        return self.runtime_language, self.runtime_country

    @staticmethod
    def _find_free_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            return int(sock.getsockname()[1])

    def _build_options(self):
        try:
            from DrissionPage import ChromiumOptions
        except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency
            raise RuntimeError("browser engine requires DrissionPage. Please install it first.") from exc

        co = ChromiumOptions()
        local_port = self._find_free_local_port()
        co.set_local_port(local_port)
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-setuid-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument("--proxy-bypass-list=<-loopback>")
        co.set_argument("--password-store=basic")
        language, country = self.detect_runtime_locale()
        co.set_argument(f"--window-size={self.persona.screen.width},{self.persona.screen.height}")
        co.set_argument(f"--user-agent={self.persona.user_agent}")
        co.set_argument(f"--lang={language}")
        is_linux = sys.platform.startswith("linux")
        co.headless(is_linux)
        logger.info(
            "Browser locale selected: lang=%s country=%s headless=%s local_port=%s ua=%s",
            language,
            country,
            is_linux,
            local_port,
            self.persona.profile.full_version,
        )

        if not self.proxy_url:
            co.set_argument("--no-proxy-server")

        if is_linux:
            for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
                if os.path.exists(candidate):
                    co.set_browser_path(candidate)
                    logger.info("Browser binary selected: %s", candidate)
                    break

        self._user_data_path = tempfile.mkdtemp(prefix="codex_browser_")
        co.set_user_data_path(self._user_data_path)

        if self.proxy_url:
            parsed = urlparse(self.proxy_url if "://" in self.proxy_url else f"http://{self.proxy_url}")
            server = f"{parsed.hostname}:{parsed.port}" if parsed.hostname and parsed.port else self.proxy_url
            co.set_argument(f"--proxy-server={server}")

            if parsed.username or parsed.password:
                co.set_proxy(
                    {
                        "server": server,
                        "username": parsed.username,
                        "password": parsed.password,
                    }
                )
            else:
                co.set_proxy(self.proxy_url)
            logger.info("Browser proxy configured: %s", server)

        return co

    def init_browser(self):
        try:
            from DrissionPage import ChromiumPage
        except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency
            raise RuntimeError("browser engine requires DrissionPage. Please install it first.") from exc

        options = self._build_options()
        self.page = ChromiumPage(addr_or_opts=options)
        self._apply_page_stealth()
        return self.page

    def _apply_page_stealth(self):
        if not self.page:
            return

        try:
            self.page.add_init_js(self.persona.init_script())
        except Exception as exc:
            logger.warning("Failed to inject browser init stealth script: %s", exc)

        try:
            self.page.run_cdp("Network.enable")
            self.page.run_cdp(
                "Network.setUserAgentOverride",
                userAgent=self.persona.user_agent,
                acceptLanguage=self.persona.accept_language,
                platform="Windows",
                userAgentMetadata=self.persona.user_agent_metadata(),
            )
        except Exception as exc:
            logger.warning("Failed to apply CDP user-agent override: %s", exc)

        try:
            self.page.run_cdp("Emulation.setTimezoneOverride", timezoneId=self.persona.timezone_id)
        except Exception as exc:
            logger.warning("Failed to apply timezone override: %s", exc)

        try:
            self.page.run_cdp("Emulation.setLocaleOverride", locale=self.persona.locale)
        except Exception as exc:
            logger.warning("Failed to apply locale override: %s", exc)

    def audit_snapshot(self) -> dict:
        return {
            "runtime_country": self.runtime_country,
            "runtime_language": self.runtime_language,
            "persona": self.persona.summary(),
        }

    def close(self):
        if self.page:
            try:
                self.page.quit()
            except Exception:
                pass

        if self._user_data_path and os.path.exists(self._user_data_path):
            try:
                shutil.rmtree(self._user_data_path, ignore_errors=True)
            except Exception:
                pass

        try:
            self.api_session.close()
        except Exception:
            pass

        self.page = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
