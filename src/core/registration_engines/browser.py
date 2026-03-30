"""Browser-driven registration engine adapted from the user's second version."""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from ...config.constants import DEFAULT_PASSWORD_LENGTH, PASSWORD_CHARSET, generate_random_user_info
from ...config.settings import get_settings
from ...database import crud
from ...database.session import get_db
from ...services import BaseEmailService
from ..register import RegistrationResult
from .browser_client import BrowserClient


logger = logging.getLogger(__name__)


class BrowserRegistrationEngine:
    """Separate browser registration engine using DrissionPage automation."""

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        proxy_source: Optional[str] = None,
        proxy_resolution: Optional[dict] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.proxy_source = proxy_source or "direct"
        self.proxy_resolution = proxy_resolution or {}
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        self.browser_client = BrowserClient(
            proxy_url=proxy_url,
            runtime_country=self.proxy_resolution.get("exit_country"),
            runtime_language="en-US",
        )
        self.page = None
        self.email_info: Optional[Dict[str, Any]] = None
        self.email: Optional[str] = None
        self.password: Optional[str] = None
        self.logs: list[str] = []

    def _log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"
        self.logs.append(log_message)

        if self.callback_logger:
            self.callback_logger(log_message)

        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception:
                pass

        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _smart_fill(self, selector: str, value: str, click_first: bool = False) -> bool:
        try:
            elements = self.page.eles(selector, timeout=8)
            target_ele = next((ele for ele in elements if ele.wait.displayed(timeout=2)), None)
            if not target_ele:
                return False

            if click_first:
                target_ele.click()
                time.sleep(0.3)
            else:
                try:
                    self.page.run_js("arguments[0].focus();", target_ele)
                except Exception:
                    pass
                time.sleep(0.2)
                target_ele.click()

            self.page.actions.key_down("CONTROL").type("a").key_up("CONTROL").type("\ue003")
            time.sleep(0.2)

            for char in str(value):
                self.page.actions.type(char)
                time.sleep(0.05)

            if not getattr(target_ele, "value", None) or target_ele.value != str(value):
                self.page.run_js(f'arguments[0].value = "{value}";', target_ele)
                self.page.run_js('arguments[0].dispatchEvent(new Event("input", { bubbles: true }));', target_ele)
                self.page.run_js('arguments[0].dispatchEvent(new Event("change", { bubbles: true }));', target_ele)

            return True
        except Exception as exc:
            self._log(f"浏览器输入失败: {exc}", "error")
            return False

    def _first_visible(self, selectors: list[str], timeout: float = 3):
        for selector in selectors:
            try:
                element = self.page.ele(selector, timeout=timeout)
                if element and element.wait.displayed(timeout=1):
                    self._log(f"命中选择器: {selector}")
                    return element
            except Exception:
                continue
        return None

    def _log_page_snapshot(self, stage: str):
        try:
            current_url = self.page.url
        except Exception:
            current_url = "<unknown>"
        try:
            title = self.page.title
        except Exception:
            title = "<unknown>"
        self._log(f"[{stage}] 页面状态 url={current_url} title={title}")

    def _open_signup_entry(self) -> bool:
        self.page.get("https://chatgpt.com/auth/login")
        time.sleep(5)
        self._log_page_snapshot("signup_entry")
        if self._is_cloudflare_challenge_page():
            self._log("Cloudflare challenge detected on signup entry, waiting for resolution", "warning")
            self._wait_for_challenge_resolution()
            self._log_page_snapshot("signup_entry_after_challenge")

        if self._locate_email_input():
            return True

        signup_btn = self._first_visible(
            [
                'css:button[data-testid="signup-button"]',
                "text=Sign up for free",
                "text=Sign up",
                'xpath=//a[contains(@href,"signup")]',
                'xpath=//a[contains(@href,"register")]',
                'xpath=//a[contains(@href,"create-account")]',
                'xpath=//button[contains(., "Sign up")]',
            ],
            timeout=2,
        )
        if signup_btn:
            self._log("Detected signup entry button, clicking through to registration flow")
            signup_btn.click()
            time.sleep(4)
            self._log_page_snapshot("signup_clicked")
            if self._is_cloudflare_challenge_page():
                self._log("Cloudflare challenge detected after signup click, waiting for resolution", "warning")
                if not self._wait_for_challenge_resolution():
                    self._log("Cloudflare challenge did not resolve after signup click", "warning")
                self._log_page_snapshot("signup_clicked_after_challenge")
            return True

        self._log("Signup button not found on /auth/login, falling back to auth.create-account", "warning")
        self.page.get("https://auth.openai.com/create-account")
        time.sleep(4)
        self._log_page_snapshot("signup_fallback_auth_openai")
        if self._is_cloudflare_challenge_page():
            self._log("Cloudflare challenge detected on auth fallback page, waiting for resolution", "warning")
            self._wait_for_challenge_resolution()
            self._log_page_snapshot("signup_fallback_after_challenge")
        return True

    def _locate_email_input(self):
        return self._first_visible(
            [
                'css:input[type="email"]',
                'css:input[name="email"]',
                "css:#email",
                "css:#email-address",
                'xpath=//input[@type="email" or @name="email" or @id="email-address" or @id="email"]',
            ],
            timeout=3,
        )

    def _wait_for_post_auth_ready(self, max_checks: int = 10, sleep_seconds: float = 2.0) -> bool:
        ready_selectors = [
            'xpath=//textarea[@id="prompt-textarea"]',
            "text=Okay, let’s go",
            "text=Continue",
            "text=Skip",
            "text=Done",
        ]
        for _ in range(max_checks):
            self._log_page_snapshot("post_auth_wait")
            if self._first_visible(ready_selectors, timeout=0.5):
                return True
            try:
                current_url = str(self.page.url or "")
            except Exception:
                current_url = ""
            if current_url.startswith("https://chatgpt.com/") and "auth.openai.com" not in current_url:
                return True
            time.sleep(sleep_seconds)
        return False

    def _click_element(self, element, label: str = "") -> bool:
        if not element:
            return False
        try:
            element.click()
            return True
        except Exception:
            try:
                self.page.run_js("arguments[0].click();", element)
                return True
            except Exception as exc:
                self._log(f"Failed to click target {label or '<unknown>'}: {exc}", "warning")
                return False

    def _is_cloudflare_challenge_page(self) -> bool:
        try:
            title = str(self.page.title or "").strip().lower()
        except Exception:
            title = ""
        if "just a moment" in title:
            return True
        challenge_indicators = [
            "text=Just a moment...",
            "text=Checking your browser",
            "text=Verifying you are human",
            "css:#challenge-running",
            "css:[data-translate='checking_browser']",
        ]
        return bool(self._first_visible(challenge_indicators, timeout=0.3))

    def _wait_for_challenge_resolution(self, max_checks: int = 12, sleep_seconds: float = 5.0) -> bool:
        for _ in range(max_checks):
            if not self._is_cloudflare_challenge_page():
                return True
            self._log_page_snapshot("cloudflare_challenge")
            time.sleep(sleep_seconds)
        return not self._is_cloudflare_challenge_page()

    def _resume_email_stage_if_needed(self) -> bool:
        email_input = self._locate_email_input()
        if not email_input or not self.email:
            return False
        try:
            email_input.input(self.email)
        except Exception:
            if not self._smart_fill('css:input[type="email"]', self.email, click_first=True):
                return False
        continue_btn = self._first_visible(
            [
                'xpath=//button[@type="submit" and .//text()="Continue"]',
                "text=Continue",
                "text=继续",
            ],
            timeout=1,
        )
        if continue_btn:
            self._click_element(continue_btn, "resume-email-continue")
        return True

    def _dismiss_post_auth_prompts(self, max_checks: int = 12, sleep_seconds: float = 1.5) -> bool:
        kill_list = [
            "text=Continue",
            "text=Skip Tour",
            "text=Skip",
            "text=Next",
            "text=Done",
        ]
        for _ in range(max_checks):
            if self.page.ele('xpath=//textarea[@id="prompt-textarea"]', timeout=0.5):
                return True

            lets_go_btn = self._first_visible(
                ["text=Okay, let’s go", "text=Okay, let's go"],
                timeout=0.5,
            )
            if lets_go_btn:
                self._click_element(lets_go_btn, "okay-lets-go")
                time.sleep(3)
                return True

            clicked = False
            for target in kill_list:
                btn = self.page.ele(target, timeout=0.5)
                if btn and btn.wait.displayed(timeout=0.5):
                    self._click_element(btn, target)
                    clicked = True
                    break

            if not clicked:
                return bool(self.page.ele('xpath=//textarea[@id="prompt-textarea"]', timeout=0.5))

            time.sleep(sleep_seconds)
        return bool(self.page.ele('xpath=//textarea[@id="prompt-textarea"]', timeout=0.5))

    @staticmethod
    def _extract_session_token_from_cookie_text(cookie_text: str) -> str:
        text = str(cookie_text or "")
        if not text:
            return ""

        direct = re.search(r"(?:^|[;,]\s*)(?:__|_)Secure-next-auth\.session-token=([^;,]*)", text)
        if direct:
            direct_value = str(direct.group(1) or "").strip().strip('"').strip("'")
            if direct_value:
                return direct_value

        parts = re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=([^;,]*)", text)
        if not parts:
            return ""

        chunk_map = {}
        for index, value in parts:
            clean_value = str(value or "").strip().strip('"').strip("'")
            if clean_value:
                chunk_map[int(index)] = clean_value
        return "".join(chunk_map[i] for i in sorted(chunk_map)) if chunk_map else ""

    @staticmethod
    def _flatten_set_cookie_headers(response) -> str:
        if response is None:
            return ""
        headers = getattr(response, "headers", None)
        if not headers:
            return ""
        try:
            raw = headers.get_all("Set-Cookie") if hasattr(headers, "get_all") else []
        except Exception:
            raw = []
        if raw:
            return "; ".join(str(value) for value in raw if value)
        return str(headers.get("Set-Cookie") or "").strip()

    @staticmethod
    def _extract_request_cookie_header(response) -> str:
        request = getattr(response, "request", None)
        headers = getattr(request, "headers", {}) or {}
        return str(headers.get("Cookie") or "").strip()

    def _sync_page_cookies_to_http_session(self, session) -> None:
        if not self.page or not session or not hasattr(session, "cookies"):
            return
        try:
            cookie_rows = self.page.cookies()
        except Exception:
            cookie_rows = []
        for cookie in cookie_rows or []:
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            if not name:
                continue
            domain = cookie.get("domain") or "chatgpt.com"
            path = cookie.get("path") or "/"
            try:
                session.cookies.set(name, value, domain=domain, path=path)
            except Exception:
                try:
                    session.cookies.set(name, value)
                except Exception:
                    continue

    def _dump_http_session_cookies(self, session) -> str:
        if not session or not hasattr(session, "cookies"):
            return ""
        parts = []
        try:
            for cookie in session.cookies:
                parts.append(f"{cookie.name}={cookie.value}")
        except Exception:
            pass
        return "; ".join(parts)

    def _capture_auth_session_via_http(self):
        session = getattr(self.browser_client, "session", None)
        if not session:
            return "", "", {}

        self._sync_page_cookies_to_http_session(session)
        access_token = ""
        metadata: Dict[str, Any] = {"method": "http_session_capture"}
        cookie_sources: list[str] = []

        def _request(extra_headers: Optional[dict] = None):
            headers = {
                "accept": "application/json",
                "referer": "https://chatgpt.com/",
                "origin": "https://chatgpt.com",
                "user-agent": self.browser_client.persona.user_agent,
                "cache-control": "no-cache",
                "pragma": "no-cache",
            }
            if extra_headers:
                headers.update(extra_headers)
            response = session.get(
                "https://chatgpt.com/api/auth/session",
                headers=headers,
                timeout=20,
            )
            return response

        response = None
        try:
            response = _request()
            cookie_sources.extend(
                [
                    self._dump_http_session_cookies(session),
                    self._flatten_set_cookie_headers(response),
                    self._extract_request_cookie_header(response),
                ]
            )
            if getattr(response, "status_code", None) == 200:
                payload = response.json() or {}
                access_token = str(payload.get("accessToken") or "").strip()
                if payload.get("sessionToken"):
                    return str(payload["sessionToken"]).strip(), access_token, {
                        **metadata,
                        "user_id": (payload.get("user") or {}).get("id", ""),
                        "account_id": (payload.get("account") or {}).get("id", ""),
                        "workspace_id": (payload.get("account") or {}).get("id", ""),
                        "email_verified": (payload.get("user") or {}).get("email_verified", False),
                        "plan_type": (payload.get("account") or {}).get("planType", "free"),
                        "expires": payload.get("expires", ""),
                    }
                metadata.update(
                    {
                        "user_id": (payload.get("user") or {}).get("id", ""),
                        "account_id": (payload.get("account") or {}).get("id", ""),
                        "workspace_id": (payload.get("account") or {}).get("id", ""),
                        "email_verified": (payload.get("user") or {}).get("email_verified", False),
                        "plan_type": (payload.get("account") or {}).get("planType", "free"),
                        "expires": payload.get("expires", ""),
                    }
                )
        except Exception as exc:
            self._log(f"HTTP auth/session capture failed: {exc}", "warning")

        session_token = ""
        for source in cookie_sources:
            session_token = self._extract_session_token_from_cookie_text(source)
            if session_token:
                break

        if (not session_token) and access_token:
            try:
                retry_response = _request({"authorization": f"Bearer {access_token}"})
                for source in (
                    self._dump_http_session_cookies(session),
                    self._flatten_set_cookie_headers(retry_response),
                    self._extract_request_cookie_header(retry_response),
                ):
                    session_token = self._extract_session_token_from_cookie_text(source)
                    if session_token:
                        break
            except Exception as exc:
                self._log(f"HTTP auth/session bearer retry failed: {exc}", "warning")

        return session_token, access_token, metadata

    def run(self) -> RegistrationResult:
        result = RegistrationResult(success=False, logs=self.logs)
        try:
            self._log("=" * 60)
            self._log("启动浏览器注册引擎（DrissionPage）")
            self._log(f"代理来源={self.proxy_source} proxy={self.proxy_url or 'direct'}")
            if self.proxy_resolution:
                self._log(
                    "代理审计: "
                    f"exit_ip={self.proxy_resolution.get('exit_ip') or '-'} "
                    f"exit_country={self.proxy_resolution.get('exit_country') or '-'} "
                    f"expected_country={self.proxy_resolution.get('expected_country') or '-'} "
                    f"attempts={self.proxy_resolution.get('attempts') or 0} "
                    f"reused_proxy={self.proxy_resolution.get('reused_proxy')}"
                )

            self._log("阶段1：创建邮箱")
            self.email_info = self.email_service.create_email()
            self.email = str(self.email_info["email"]).strip().lower()
            self.password = "".join(secrets.choice(PASSWORD_CHARSET) for _ in range(DEFAULT_PASSWORD_LENGTH))
            self._log(f"邮箱创建成功: {self.email}")

            self._log("阶段2：初始化浏览器")
            self.page = self.browser_client.init_browser()
            self._log(
                f"浏览器环境 country={self.browser_client.runtime_country or 'Unknown'} "
                f"lang={self.browser_client.runtime_language}"
            )
            self._log(f"人格摘要: {json.dumps(self.browser_client.persona.summary(), ensure_ascii=False)}")
            self._log("Stage 3: opening signup entry")
            self._open_signup_entry()

            self._log("Stage 5: locating email input")
            email_input = None
            for _ in range(3):
                if self._is_cloudflare_challenge_page():
                    self._log("Cloudflare challenge is blocking email page, waiting before retry", "warning")
                    if not self._wait_for_challenge_resolution():
                        self._log_page_snapshot("cloudflare_challenge_unresolved")
                        result.error_message = "cloudflare challenge unresolved"
                        return result
                email_input = self._locate_email_input()
                if email_input:
                    break
                self._log("Email input not visible yet, retrying signup surface detection", "warning")
                self._open_signup_entry()
            if not email_input:
                self._log_page_snapshot("email_input_missing")
                result.error_message = "email input page load timeout"
                return result

            email_input.input(self.email)
            self._log("已填写邮箱，提交继续")
            time.sleep(0.5)
            continue_btn = self._first_visible(
                [
                    'xpath=//button[@type="submit" and .//text()="Continue"]',
                    'xpath=//button[@type="submit" and contains(., "继续")]',
                    'text=Continue',
                    'text=继续',
                ],
                timeout=2,
            )
            if continue_btn:
                continue_btn.click()
            else:
                self._log("未找到继续按钮，尝试回车提交", "warning")
                self.page.actions.key_down("ENTER").key_up("ENTER")

            for _ in range(15):
                time.sleep(4)
                self._log_page_snapshot("轮询阶段")

                if self._resume_email_stage_if_needed():
                    self._log("Registration context returned to email stage, resubmitted email")
                    continue

                if self.page.ele("text=Your session has ended", timeout=2) or self.page.ele("text=Don't have an account?", timeout=2):
                    self._log("检测到会话跳回，重新拉回注册页")
                    signup_link = self.page.ele('xpath=//a[text()="Sign up"]', timeout=3)
                    if signup_link:
                        signup_link.click()
                    else:
                        self._open_signup_entry()
                    continue

                pwd_input = self.page.ele('xpath=//input[@type="password" or @name="password"]', timeout=2)
                if pwd_input and pwd_input.wait.displayed(timeout=2):
                    self._log("进入密码填写阶段")
                    self._smart_fill('xpath=//input[@type="password" or @name="password"]', self.password, click_first=True)
                    time.sleep(1.5)
                    btn = self.page.ele('xpath=//button[@type="submit" and .//text()="Continue"]', timeout=4)
                    if btn:
                        btn.click()
                    else:
                        self.page.actions.key_down("ENTER").key_up("ENTER")
                    continue

                otp_input = self.page.ele('xpath=//input[@autocomplete="one-time-code" or contains(@class, "code")]', timeout=2)
                if self.page.ele("text=Check your inbox", timeout=2) or self.page.ele("text=检查收件箱", timeout=2) or otp_input:
                    pwd_bypass_btn = self.page.ele("text=Continue with password", timeout=1)
                    if pwd_bypass_btn and pwd_bypass_btn.wait.displayed(timeout=1):
                        self._log("检测到继续用密码入口，优先走密码分支")
                        try:
                            pwd_bypass_btn.click()
                        except Exception:
                            self.page.run_js("arguments[0].click();", pwd_bypass_btn)
                        continue

                    self._log("等待邮箱验证码")
                    otp = self.email_service.get_verification_code(email=self.email, timeout=120)
                    if otp:
                        self._smart_fill('xpath=//input[@autocomplete="one-time-code" or contains(@class, "code")]', otp, click_first=True)
                        time.sleep(0.5)
                        self.page.actions.key_down("ENTER").key_up("ENTER")
                    continue

                if self.page.ele("text=confirm your age", timeout=2) or self.page.ele("text=确认你的年龄", timeout=2) or self.page.ele('xpath=//input[@name="name"]', timeout=2):
                    self._log("进入资料填写阶段")
                    info = generate_random_user_info()

                    safe_year = str(secrets.choice(range(1990, 2000)))
                    safe_month = str(secrets.choice(range(1, 13))).zfill(2)
                    safe_day = str(secrets.choice(range(1, 29))).zfill(2)
                    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                    month_abbr = month_names[int(safe_month) - 1]

                    self._smart_fill('xpath=//input[@name="name" or @placeholder="Full name"]', info["name"], click_first=True)
                    time.sleep(0.5)

                    comboboxes = self.page.eles('xpath=//select | //button[@aria-haspopup="listbox"] | //button[@role="combobox"]')
                    if len(comboboxes) >= 3:
                        self.page.actions.key_down("TAB").key_up("TAB")
                        time.sleep(0.3)
                        for char in month_abbr:
                            self.page.actions.type(char)
                            time.sleep(0.05)

                        self.page.actions.key_down("TAB").key_up("TAB")
                        time.sleep(0.3)
                        for char in str(int(safe_day)):
                            self.page.actions.type(char)
                            time.sleep(0.05)

                        self.page.actions.key_down("TAB").key_up("TAB")
                        time.sleep(0.3)
                        for char in safe_year:
                            self.page.actions.type(char)
                            time.sleep(0.05)
                    else:
                        self.page.actions.key_down("TAB").key_up("TAB")
                        time.sleep(0.3)
                        age_input = self.page.ele('xpath=//input[@name="age" or @placeholder="Age"]', timeout=1)
                        fill_value = "25" if age_input else f"{safe_month}{safe_day}{safe_year}"
                        for char in fill_value:
                            self.page.actions.type(char)
                            time.sleep(0.15)

                    time.sleep(1.5)
                    finish_btn = self._first_visible(
                        [
                            "text=Finish creating account",
                            "text=完成创建账号",
                            "text=完成创建帐户",
                        ],
                        timeout=2,
                    )
                    if finish_btn:
                        finish_btn.click()
                    else:
                        self.page.actions.key_down("ENTER").key_up("ENTER")
                    break

            self._log("Profile submitted, waiting for post-auth page to stabilize before token capture")
            self._wait_for_post_auth_ready(max_checks=20, sleep_seconds=2)
            self._dismiss_post_auth_prompts(max_checks=12, sleep_seconds=1.5)
            self._log_page_snapshot("post_auth_ready")

            full_session_token = ""
            access_token = ""
            extracted_metadata: Dict[str, Any] = {"registration_engine": "browser"}
            api_tab = None

            try:
                api_tab = self.page.new_tab("https://chatgpt.com/api/auth/session")
                time.sleep(3)
                body_ele = api_tab.ele("tag:body")
                page_text = body_ele.text if body_ele else api_tab.html
                start_idx = page_text.find("{")
                end_idx = page_text.rfind("}") + 1

                if start_idx != -1 and end_idx > start_idx:
                    auth_data = json.loads(page_text[start_idx:end_idx])
                    full_session_token = auth_data.get("sessionToken", "")
                    access_token = auth_data.get("accessToken", "")

                    if full_session_token:
                        user_info = auth_data.get("user", {})
                        account_info = auth_data.get("account", {})
                        extracted_metadata.update(
                            {
                                "user_id": user_info.get("id", ""),
                                "account_id": account_info.get("id", ""),
                                "workspace_id": account_info.get("id", ""),
                                "email_verified": user_info.get("email_verified", False),
                                "plan_type": account_info.get("planType", "free"),
                                "expires": auth_data.get("expires", ""),
                                "method": "api_json_parse",
                            }
                        )
                        self._log("已通过 API 会话页提取凭证")
            except Exception as exc:
                self._log(f"会话页提取失败，尝试 Cookie 回退: {exc}", "warning")
            finally:
                if api_tab:
                    try:
                        api_tab.close()
                    except Exception:
                        pass

            if not full_session_token:
                http_session_token, http_access_token, http_metadata = self._capture_auth_session_via_http()
                if http_session_token:
                    full_session_token = http_session_token
                    access_token = access_token or http_access_token
                    extracted_metadata.update(http_metadata)
                    self._log("HTTP session capture succeeded after browser-tab extraction path")

            if not full_session_token:
                raw_cookies = self.page.cookies()
                cookies_dict = {cookie["name"]: cookie["value"] for cookie in raw_cookies}
                token_parts = []
                if "__Secure-next-auth.session-token" in cookies_dict:
                    token_parts.append(cookies_dict["__Secure-next-auth.session-token"])

                chunks = [key for key in cookies_dict.keys() if "__Secure-next-auth.session-token." in key]
                if chunks:
                    chunks.sort(key=lambda key: int(key.split(".")[-1]))
                    for key in chunks:
                        token_parts.append(cookies_dict[key])

                full_session_token = "".join(token_parts)
                extracted_metadata["method"] = "cookie_assembly_fallback"

            if full_session_token:
                result.success = True
                result.email = self.email or ""
                result.password = self.password or ""
                result.session_token = full_session_token
                result.access_token = access_token
                result.account_id = extracted_metadata.get("account_id", "") or extracted_metadata.get("user_id", "")
                result.workspace_id = extracted_metadata.get("workspace_id", "") or result.account_id
                result.metadata = extracted_metadata
                self._log("浏览器注册完成，凭证提取成功")
            else:
                result.error_message = "未能提取有效 Session Token"
                self._log(result.error_message, "error")

            return result
        except Exception as exc:
            self._log(f"浏览器注册异常: {exc}", "error")
            result.error_message = str(exc)
            return result
        finally:
            self.browser_client.close()

    def save_to_database(self, result: RegistrationResult) -> bool:
        if not result.success:
            return False

        try:
            settings = get_settings()
            with get_db() as db:
                account = crud.create_account(
                    db,
                    email=result.email,
                    password=result.password,
                    client_id=settings.openai_client_id,
                    session_token=result.session_token,
                    email_service=self.email_service.service_type.value,
                    email_service_id=self.email_info.get("service_id") if self.email_info else None,
                    account_id=result.account_id or ((result.metadata or {}).get("user_id") if result.metadata else None),
                    workspace_id=result.workspace_id,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    id_token=result.id_token,
                    proxy_used=self.proxy_url,
                    extra_data=result.metadata,
                    source=result.source,
                )
                self._log(f"浏览器注册账户已保存到数据库，ID: {account.id}")
                return True
        except Exception as exc:
            self._log(f"保存浏览器注册结果失败: {exc}", "error")
            return False
