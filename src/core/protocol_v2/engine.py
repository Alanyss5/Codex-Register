from __future__ import annotations

import inspect
import json
import logging
import secrets
import string
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from ...config.constants import (
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    generate_random_user_info,
)
from ..http_client import OpenAIHTTPClient
from ...database import crud
from ...database.session import get_db
from .client import ChatGPTProtocolClient
from .oauth_client import OAuthProtocolClient

if TYPE_CHECKING:
    from ..register import RegistrationResult


logger = logging.getLogger(__name__)


class EmailServiceAdapter:
    def __init__(self, email_service, email: str, email_id: Optional[str], log_fn):
        self.email_service = email_service
        self.email = email
        self.email_id = email_id
        self.log_fn = log_fn
        self._used_codes: set[str] = set()

    def wait_for_verification_code(self, email: str, timeout: int = 60, otp_sent_at=None, exclude_codes=None):
        exclude_codes = set(exclude_codes or ()) | self._used_codes
        method = getattr(self.email_service, "get_verification_code")
        signature = inspect.signature(method)
        kwargs = {
            "email": email,
            "email_id": self.email_id,
            "timeout": timeout,
            "otp_sent_at": otp_sent_at,
        }
        if "exclude_codes" in signature.parameters:
            kwargs["exclude_codes"] = exclude_codes
        code = method(**{key: value for key, value in kwargs.items() if key in signature.parameters})
        if code:
            self._used_codes.add(str(code))
        return code


class ProtocolRegistrationEngineV2:
    http_client_cls = OpenAIHTTPClient
    chatgpt_client_cls = ChatGPTProtocolClient
    oauth_client_cls = OAuthProtocolClient

    def __init__(
        self,
        email_service,
        proxy_url: Optional[str] = None,
        proxy_source: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        browser_mode: str = "protocol",
        max_retries: int = 3,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.proxy_source = proxy_source or "direct"
        self.callback_logger = callback_logger or (lambda message: logger.info(message))
        self.task_uuid = task_uuid
        self.browser_mode = browser_mode or "protocol"
        self.max_retries = max(1, int(max_retries or 1))
        self.email: Optional[str] = None
        self.password: Optional[str] = None
        self.email_info: Optional[Dict[str, Any]] = None
        self.logs: list[str] = []

    def _log(self, message: str, level: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        self.logs.append(line)
        self.callback_logger(line)
        if level == "error":
            logger.error(line)
        elif level == "warning":
            logger.warning(line)
        else:
            logger.info(line)

    @staticmethod
    def _generate_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        size = max(PASSWORD_MIN_LENGTH, min(PASSWORD_MAX_LENGTH, length or DEFAULT_PASSWORD_LENGTH))
        chars = [secrets.choice(string.ascii_lowercase), secrets.choice(string.ascii_uppercase), secrets.choice(string.digits), "!"]
        chars.extend(secrets.choice(PASSWORD_CHARSET) for _ in range(size - 4))
        secrets.SystemRandom().shuffle(chars)
        return "".join(chars)

    @staticmethod
    def _should_retry(message: str) -> bool:
        lowered = str(message or "").lower()
        return any(marker in lowered for marker in ("tls", "ssl", "authorize", "session", "otp", "workspace"))

    @staticmethod
    def _looks_like_existing_account(message: str) -> bool:
        lowered = str(message or "").lower()
        return any(marker in lowered for marker in ("user_exists", "already exists", "already registered", "email already"))

    def _get_email_provider(self) -> str:
        service_type = getattr(self.email_service, "service_type", None)
        return str(getattr(service_type, "value", "") or "").strip().lower()

    def _should_blacklist_domain(self, message: str) -> bool:
        lowered = str(message or "").lower()
        return any(marker in lowered for marker in ("user_exists", "already exists", "already registered", "invalid email"))

    def _blacklist_domain_if_needed(self, email_addr: str, reason: str) -> bool:
        provider = self._get_email_provider()
        if provider not in {"tempmail", "temp_mail", "freemail"}:
            return False
        if not email_addr or not self._should_blacklist_domain(reason):
            return False
        domain = email_addr.split("@", 1)[-1].lower()
        try:
            with get_db() as db:
                setting = crud.get_setting(db, "email.domain_blacklist")
                if setting and setting.value:
                    try:
                        blacklist = json.loads(setting.value)
                    except (json.JSONDecodeError, TypeError):
                        blacklist = []
                else:
                    blacklist = []
                if domain not in blacklist:
                    blacklist.append(domain)
                    crud.set_setting(
                        db,
                        key="email.domain_blacklist",
                        value=json.dumps(blacklist),
                        description="被 OpenAI 拒绝注册的邮箱域名黑名单",
                        category="email",
                    )
                    self._log(f"已将域名 {domain} 加入黑名单 (共 {len(blacklist)} 个)", "warning")
                else:
                    self._log(f"域名 {domain} 已在黑名单中，跳过", "warning")
            return True
        except Exception as exc:
            self._log(f"写入邮箱域名黑名单失败: {exc}", "warning")
            return False

    def _check_ip_location(self, http_client) -> tuple[bool, Optional[str]]:
        try:
            return http_client.check_ip_location()
        except Exception as exc:
            self._log(f"检查 IP 地理位置失败: {exc}", "warning")
            return True, None

    def _create_email(self) -> bool:
        try:
            self.email_info = self.email_service.create_email()
        except Exception as exc:
            self._log(f"创建邮箱失败: {exc}", "error")
            return False
        self.email = str((self.email_info or {}).get("email") or "").strip()
        return bool(self.email)

    def _fill_result_from_session(self, result: "RegistrationResult", session_result: Dict[str, Any], source: str) -> "RegistrationResult":
        result.success = True
        result.source = source
        result.access_token = session_result.get("access_token", "") or ""
        result.refresh_token = session_result.get("refresh_token", "") or ""
        result.id_token = session_result.get("id_token", "") or ""
        result.session_token = session_result.get("session_token", "") or ""
        result.account_id = session_result.get("account_id", "") or ""
        result.workspace_id = session_result.get("workspace_id", "") or ""
        result.metadata = {
            "registration_engine": "protocol",
            "auth_provider": session_result.get("auth_provider", ""),
            "user_id": session_result.get("user_id", ""),
            "user": session_result.get("user") or {},
            "account": session_result.get("account") or {},
        }
        return result

    def run(self) -> "RegistrationResult":
        from ..register import RegistrationResult

        result = RegistrationResult(success=False, logs=self.logs, metadata={"registration_engine": "protocol"})
        last_error = ""

        for attempt in range(self.max_retries):
            http_client = self.http_client_cls(proxy_url=self.proxy_url)
            ip_ok, location = self._check_ip_location(http_client)
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                return result

            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            result.email = self.email or ""
            self.password = self.password or self._generate_password()
            result.password = self.password

            user_info = generate_random_user_info()
            first_name, last_name = str(user_info["name"]).split(" ", 1)
            birthdate = user_info["birthdate"]
            mailbox = EmailServiceAdapter(
                self.email_service,
                email=self.email or "",
                email_id=(self.email_info or {}).get("service_id"),
                log_fn=self._log,
            )
            chatgpt_client = self.chatgpt_client_cls(
                http_client=http_client,
                callback_logger=self._log,
                browser_mode=self.browser_mode,
            )

            ok, payload = chatgpt_client.register_complete_flow(
                email=self.email or "",
                password=self.password,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
                mailbox_client=mailbox,
            )
            if ok:
                session_ok, session_payload = chatgpt_client.reuse_session_and_get_tokens()
                if session_ok:
                    return self._fill_result_from_session(result, session_payload, source="register")
                last_error = str(session_payload)
            else:
                last_error = str(payload)
                if self._looks_like_existing_account(last_error):
                    oauth_client = self.oauth_client_cls(http_client=http_client, callback_logger=self._log)
                    oauth_ok, oauth_payload = oauth_client.login_and_get_tokens(
                        email=self.email or "",
                        password=self.password,
                        mailbox_client=mailbox,
                    )
                    if oauth_ok:
                        return self._fill_result_from_session(result, oauth_payload, source="login")

            self._blacklist_domain_if_needed(self.email or "", last_error)
            if attempt < self.max_retries - 1 and self._should_retry(last_error):
                self._log(f"注册失败，准备重试: {last_error}", "warning")
                continue
            break

        result.error_message = last_error or "注册失败"
        result.metadata = result.metadata or {"registration_engine": "protocol"}
        return result
