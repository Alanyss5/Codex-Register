from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from ..http_client import OpenAIHTTPClient
from ...config.constants import OPENAI_API_ENDPOINTS
from .flow import FlowState, describe_flow_state, extract_flow_state


class ChatGPTProtocolClient:
    BASE = "https://chatgpt.com"
    AUTH = "https://auth.openai.com"

    def __init__(self, http_client: OpenAIHTTPClient, callback_logger=None, browser_mode: str = "protocol"):
        self.http_client = http_client
        self.callback_logger = callback_logger or (lambda message: None)
        self.browser_mode = browser_mode or "protocol"
        self.logs: list[str] = []
        self.last_registration_state = FlowState()
        self._refresh_runtime()

    def _refresh_runtime(self) -> None:
        self.session = self.http_client.session
        profile = getattr(self.http_client, "profile", None)
        self.user_agent = getattr(profile, "user_agent", "Mozilla/5.0")
        self.sec_ch_ua = ""
        if profile and hasattr(profile, "_ch_ua_headers"):
            self.sec_ch_ua = profile._ch_ua_headers().get("sec-ch-ua", "")
        self.device_id = str(uuid.uuid4())
        self._seed_device_cookie()

    def _seed_device_cookie(self) -> None:
        cookies = getattr(self.session, "cookies", None)
        if cookies is None:
            return
        try:
            if hasattr(cookies, "set"):
                cookies.set("oai-did", self.device_id)
            else:
                cookies["oai-did"] = self.device_id
        except Exception:
            pass

    def _log(self, message: str) -> None:
        self.logs.append(message)
        self.callback_logger(message)

    def _headers(
        self,
        url: str,
        *,
        accept: str,
        referer: Optional[str] = None,
        origin: Optional[str] = None,
        content_type: Optional[str] = None,
        navigation: bool = False,
        fetch_site: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        base = dict(getattr(self.http_client, "default_headers", {}) or {})
        base.setdefault("User-Agent", self.user_agent)
        base["Accept"] = accept
        if referer:
            base["Referer"] = referer
        if origin:
            base["Origin"] = origin
        if content_type:
            base["Content-Type"] = content_type
        base["Sec-Fetch-Mode"] = "navigate" if navigation else "cors"
        base["Sec-Fetch-Dest"] = "document" if navigation else "empty"
        base["Sec-Fetch-Site"] = fetch_site or ("none" if navigation else "same-origin")
        if extra_headers:
            base.update(extra_headers)
        return base

    def _reset_session(self) -> None:
        proxy_url = getattr(self.http_client, "proxy_url", None)
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)
        self._refresh_runtime()

    def _state_from_url(self, url: str, method: str = "GET") -> FlowState:
        return extract_flow_state(current_url=url, auth_base=self.AUTH, default_method=method)

    def _state_from_payload(self, data: Dict[str, Any], current_url: str = "") -> FlowState:
        return extract_flow_state(data=data, current_url=current_url, auth_base=self.AUTH)

    @staticmethod
    def _state_signature(state: FlowState) -> tuple[str, str, str, str]:
        return (
            state.page_type or "",
            state.method or "",
            state.continue_url or "",
            state.current_url or "",
        )

    @staticmethod
    def _is_registration_complete_state(state: FlowState) -> bool:
        page_type = state.page_type or ""
        current_url = (state.current_url or "").lower()
        continue_url = (state.continue_url or "").lower()
        return (
            page_type in {"callback", "chatgpt_home", "oauth_callback"}
            or ("chatgpt.com" in current_url and "/api/auth/session" not in current_url)
            or ("chatgpt.com" in continue_url and "/api/auth/session" not in continue_url)
        )

    @staticmethod
    def _state_is_password_registration(state: FlowState) -> bool:
        return state.page_type in {"create_account_password", "password"}

    @staticmethod
    def _state_is_email_otp(state: FlowState) -> bool:
        target = (state.continue_url or state.current_url or "").lower()
        return state.page_type == "email_otp_verification" or "email-verification" in target or "email-otp" in target

    @staticmethod
    def _state_is_about_you(state: FlowState) -> bool:
        target = (state.continue_url or state.current_url or "").lower()
        return state.page_type == "about_you" or "about-you" in target

    @staticmethod
    def _state_requires_navigation(state: FlowState) -> bool:
        if state.page_type in {"callback", "chatgpt_home", "oauth_callback"}:
            return False
        if (state.method or "GET").upper() != "GET":
            return False
        if state.external_url and state.external_url != state.current_url:
            return True
        if state.continue_url and state.continue_url != state.current_url:
            return True
        return False

    def _follow_flow_state(self, state: FlowState, referer: Optional[str] = None) -> Tuple[bool, FlowState | str]:
        target_url = state.external_url or state.continue_url or state.current_url
        if not target_url:
            return False, "缺少可跟随的 continue_url"

        try:
            response = self.session.get(
                target_url,
                headers=self._headers(
                    target_url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=referer,
                    navigation=True,
                    fetch_site="none",
                ),
                allow_redirects=True,
                timeout=30,
            )
            content_type = (response.headers.get("content-type", "") or "").lower()
            if "application/json" in content_type:
                next_state = self._state_from_payload(response.json(), current_url=str(response.url))
            else:
                next_state = self._state_from_url(str(response.url))
            self._log(f"follow state -> {describe_flow_state(next_state)}")
            return True, next_state
        except Exception as exc:
            return False, str(exc)

    def get_next_auth_session_token(self) -> str:
        cookies = getattr(self.session, "cookies", None)
        if cookies is None:
            return ""
        try:
            if isinstance(cookies, dict):
                return str(cookies.get("__Secure-next-auth.session-token") or "")
            for cookie in getattr(cookies, "jar", cookies):
                if getattr(cookie, "name", "") == "__Secure-next-auth.session-token":
                    return str(getattr(cookie, "value", "") or "")
        except Exception:
            return ""
        return ""

    def fetch_chatgpt_session(self) -> Tuple[bool, Dict[str, Any] | str]:
        url = f"{self.BASE}/api/auth/session"
        response = self.session.get(
            url,
            headers=self._headers(url, accept="application/json", referer=f"{self.BASE}/", fetch_site="same-origin"),
            timeout=30,
        )
        if response.status_code != 200:
            return False, f"/api/auth/session -> HTTP {response.status_code}"
        data = response.json()
        if not str(data.get("accessToken") or "").strip():
            return False, "/api/auth/session 未返回 accessToken"
        return True, data

    @staticmethod
    def _decode_jwt_payload(token: str) -> Dict[str, Any]:
        if not token or "." not in token:
            return {}
        try:
            payload = token.split(".", 2)[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        except Exception:
            return {}

    def reuse_session_and_get_tokens(self) -> Tuple[bool, Dict[str, Any] | str]:
        state = self.last_registration_state or FlowState()
        if self._state_requires_navigation(state):
            ok, followed = self._follow_flow_state(state, referer=state.current_url or f"{self.AUTH}/about-you")
            if not ok:
                return False, f"注册回调落地失败: {followed}"
            self.last_registration_state = followed

        session_cookie = self.get_next_auth_session_token()
        if not session_cookie:
            return False, "缺少 __Secure-next-auth.session-token，注册回调可能未落地"

        ok, session_or_error = self.fetch_chatgpt_session()
        if not ok:
            return False, session_or_error

        session_data = session_or_error
        access_token = str(session_data.get("accessToken") or "").strip()
        session_token = str(session_data.get("sessionToken") or session_cookie or "").strip()
        user = session_data.get("user") or {}
        account = session_data.get("account") or {}
        auth_payload = self._decode_jwt_payload(access_token).get("https://api.openai.com/auth") or {}
        account_id = str(account.get("id") or auth_payload.get("chatgpt_account_id") or "").strip()
        user_id = str(user.get("id") or auth_payload.get("chatgpt_user_id") or auth_payload.get("user_id") or "").strip()
        return True, {
            "access_token": access_token,
            "session_token": session_token,
            "account_id": account_id,
            "user_id": user_id,
            "workspace_id": account_id,
            "auth_provider": session_data.get("authProvider") or "",
            "user": user,
            "account": account,
            "expires": session_data.get("expires"),
            "raw_session": session_data,
        }

    def visit_homepage(self) -> bool:
        response = self.session.get(
            f"{self.BASE}/",
            headers=self._headers(
                f"{self.BASE}/",
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                navigation=True,
                fetch_site="none",
            ),
            allow_redirects=True,
            timeout=30,
        )
        return response.status_code == 200

    def get_csrf_token(self) -> Optional[str]:
        response = self.session.get(
            f"{self.BASE}/api/auth/csrf",
            headers=self._headers(
                f"{self.BASE}/api/auth/csrf",
                accept="application/json",
                referer=f"{self.BASE}/",
                fetch_site="same-origin",
            ),
            timeout=30,
        )
        if response.status_code != 200:
            return None
        return str((response.json() or {}).get("csrfToken") or "").strip() or None

    def signin(self, email: str, csrf_token: str) -> Optional[str]:
        response = self.session.post(
            f"{self.BASE}/api/auth/signin/openai",
            params={
                "prompt": "login",
                "ext-oai-did": self.device_id,
                "auth_session_logging_id": str(uuid.uuid4()),
                "screen_hint": "login_or_signup",
                "login_hint": email,
            },
            data={
                "callbackUrl": f"{self.BASE}/",
                "csrfToken": csrf_token,
                "json": "true",
            },
            headers=self._headers(
                f"{self.BASE}/api/auth/signin/openai",
                accept="application/json",
                referer=f"{self.BASE}/",
                origin=self.BASE,
                content_type="application/x-www-form-urlencoded",
                fetch_site="same-origin",
            ),
            timeout=30,
        )
        if response.status_code != 200:
            return None
        return str((response.json() or {}).get("url") or "").strip() or None

    def authorize(self, url: str, max_retries: int = 3) -> Optional[str]:
        last_error = None
        for attempt in range(max_retries):
            try:
                response = self.session.get(
                    url,
                    headers=self._headers(
                        url,
                        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        referer=f"{self.BASE}/",
                        navigation=True,
                        fetch_site="none",
                    ),
                    allow_redirects=True,
                    timeout=30,
                )
                return str(response.url)
            except Exception as exc:
                last_error = exc
                if attempt < max_retries - 1:
                    continue
        if last_error:
            raise last_error
        return None

    def register_user(self, email: str, password: str) -> Tuple[bool, str]:
        response = self.session.post(
            OPENAI_API_ENDPOINTS["register"],
            json={"username": email, "password": password},
            headers=self._headers(
                OPENAI_API_ENDPOINTS["register"],
                accept="application/json",
                referer=f"{self.AUTH}/create-account/password",
                origin=self.AUTH,
                content_type="application/json",
                fetch_site="same-origin",
            ),
            timeout=30,
        )
        if response.status_code == 200:
            return True, "注册成功"
        try:
            error_data = response.json()
            error = error_data.get("error", {})
            return False, str(error.get("message") or error.get("code") or response.text[:200])
        except Exception:
            return False, response.text[:200]

    def send_email_otp(self) -> bool:
        response = self.session.get(
            OPENAI_API_ENDPOINTS["send_otp"],
            headers=self._headers(
                OPENAI_API_ENDPOINTS["send_otp"],
                accept="application/json, text/plain, */*",
                referer=f"{self.AUTH}/create-account/password",
                fetch_site="same-origin",
            ),
            allow_redirects=True,
            timeout=30,
        )
        return response.status_code == 200

    def verify_email_otp(self, otp_code: str, return_state: bool = False) -> Tuple[bool, FlowState | str]:
        response = self.session.post(
            OPENAI_API_ENDPOINTS["validate_otp"],
            json={"code": otp_code},
            headers=self._headers(
                OPENAI_API_ENDPOINTS["validate_otp"],
                accept="application/json",
                referer=f"{self.AUTH}/email-verification",
                origin=self.AUTH,
                content_type="application/json",
                fetch_site="same-origin",
            ),
            timeout=30,
        )
        if response.status_code != 200:
            return False, response.text[:200]
        next_state = self._state_from_payload(response.json() or {}, current_url=str(response.url) or f"{self.AUTH}/about-you")
        return (True, next_state) if return_state else (True, "验证成功")

    def create_account(
        self,
        first_name: str,
        last_name: str,
        birthdate: str,
        return_state: bool = False,
    ) -> Tuple[bool, FlowState | str]:
        headers = self._headers(
            OPENAI_API_ENDPOINTS["create_account"],
            accept="application/json",
            referer=f"{self.AUTH}/about-you",
            origin=self.AUTH,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": self.device_id},
        )
        sentinel_token = None
        if hasattr(self.http_client, "check_sentinel"):
            sentinel_token = self.http_client.check_sentinel(self.device_id)
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        response = self.session.post(
            OPENAI_API_ENDPOINTS["create_account"],
            json={"name": f"{first_name} {last_name}", "birthdate": birthdate},
            headers=headers,
            timeout=30,
        )
        if response.status_code != 200:
            return False, response.text[:200]
        next_state = self._state_from_payload(response.json() or {}, current_url=str(response.url) or self.BASE)
        return (True, next_state) if return_state else (True, "账号创建成功")

    def register_complete_flow(
        self,
        email: str,
        password: str,
        first_name: str,
        last_name: str,
        birthdate: str,
        mailbox_client,
    ) -> Tuple[bool, str]:
        final_url = ""
        for auth_attempt in range(3):
            if auth_attempt > 0:
                self._reset_session()
            if not self.visit_homepage():
                continue
            csrf_token = self.get_csrf_token()
            if not csrf_token:
                continue
            auth_url = self.signin(email, csrf_token)
            if not auth_url:
                continue
            final_url = self.authorize(auth_url)
            if not final_url:
                continue
            final_path = urlparse(final_url).path
            if "api/accounts/authorize" in final_path or final_path == "/error":
                continue
            break
        if not final_url:
            return False, "Authorize 失败"

        state = self._state_from_url(final_url)
        register_submitted = False
        otp_verified = False
        account_created = False
        seen_states: Dict[tuple[str, str, str, str], int] = {}

        for _ in range(12):
            signature = self._state_signature(state)
            seen_states[signature] = seen_states.get(signature, 0) + 1
            if seen_states[signature] > 2:
                return False, f"注册状态卡住: {describe_flow_state(state)}"

            if self._is_registration_complete_state(state):
                self.last_registration_state = state
                return True, "注册成功"

            if self._state_is_password_registration(state):
                if register_submitted:
                    return False, "注册密码阶段重复进入"
                ok, message = self.register_user(email, password)
                if not ok:
                    return False, message
                register_submitted = True
                self.send_email_otp()
                state = self._state_from_url(f"{self.AUTH}/email-verification")
                continue

            if self._state_is_email_otp(state):
                otp_code = mailbox_client.wait_for_verification_code(email, timeout=30)
                if not otp_code:
                    return False, "未收到验证码"
                ok, next_state = self.verify_email_otp(otp_code, return_state=True)
                if not ok:
                    return False, str(next_state)
                otp_verified = True
                state = next_state
                self.last_registration_state = state
                continue

            if self._state_is_about_you(state):
                if account_created:
                    return False, "填写信息阶段重复进入"
                ok, next_state = self.create_account(first_name, last_name, birthdate, return_state=True)
                if not ok:
                    return False, str(next_state)
                account_created = True
                state = next_state
                self.last_registration_state = state
                continue

            if self._state_requires_navigation(state):
                ok, next_state = self._follow_flow_state(state, referer=state.current_url or f"{self.AUTH}/about-you")
                if not ok:
                    return False, str(next_state)
                state = next_state
                self.last_registration_state = state
                continue

            if (not register_submitted) and (not otp_verified) and (not account_created):
                state = self._state_from_url(f"{self.AUTH}/create-account/password")
                continue

            return False, f"未支持的注册状态: {describe_flow_state(state)}"

        return False, "注册状态机超出最大步数"
