"""
Temp-Mail 邮箱服务实现
基于自部署 Cloudflare Worker 临时邮箱服务
接口文档参见 plan/temp-mail.md
"""

import re
import time
import json
import logging
from datetime import datetime
from email import message_from_string
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default as email_policy
from html import unescape
from typing import Optional, Dict, Any, List

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN
from .temp_mail_addressing import generate_local_part, choose_domain
from .temp_mail_domain_provider import resolve_temp_mail_domains, summarize_temp_mail_domains


logger = logging.getLogger(__name__)

SEMANTIC_OTP_PATTERNS = (
    re.compile(r"(?:openai\s+verification\s+code\s+is|verification\s+code\s+is|code\s+is)\s*[:：]?\s*(\d{6})", re.I),
    re.compile(r"(?:openai\s+verification\s+code|verification\s+code|验证码)\s*[:：]?\s*(\d{6})", re.I),
)


class TempMailService(BaseEmailService):
    """
    Temp-Mail 邮箱服务
    基于自部署 Cloudflare Worker 的临时邮箱，admin 模式管理邮箱
    不走代理，不使用 requests 库
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 TempMail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: Worker 域名地址，如 https://mail.example.com (必需)
                - admin_password: Admin 密码，对应 x-admin-auth header (必需)
                - domain: 邮箱域名，如 example.com（可选，作为 domains/worker 回退）
                - domains: 可选域名池，数组或逗号分隔字符串（优先级最高）
                - enable_prefix: 是否启用前缀，默认 False
                - timeout: 请求超时时间，默认 30
                - max_retries: 最大重试次数，默认 3
            name: 服务名称
        """
        super().__init__(EmailServiceType.TEMP_MAIL, name)

        required_keys = ["base_url", "admin_password"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "enable_prefix": False,
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **(config or {})}

        # 不走代理，proxy_url=None
        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)

        # 邮箱缓存：email -> {jwt, address}
        self._email_cache: Dict[str, Dict[str, Any]] = {}
        self._verification_state: Dict[str, Dict[str, Any]] = {}
        self._last_verification_debug: Dict[str, Dict[str, Any]] = {}

    def _decode_mime_header(self, value: str) -> str:
        """解码 MIME 头，兼容 RFC 2047 编码主题。"""
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    def _extract_body_from_message(self, message: Message) -> str:
        """从 MIME 邮件对象中提取可读正文。"""
        parts: List[str] = []

        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue

                content_type = (part.get_content_type() or "").lower()
                if content_type not in ("text/plain", "text/html"):
                    continue

                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace") if payload else ""
                except Exception:
                    try:
                        text = part.get_content()
                    except Exception:
                        text = ""

                if content_type == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                parts.append(text)
        else:
            try:
                payload = message.get_payload(decode=True)
                charset = message.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace") if payload else ""
            except Exception:
                try:
                    body = message.get_content()
                except Exception:
                    body = str(message.get_payload() or "")

            if "html" in (message.get_content_type() or "").lower():
                body = re.sub(r"<[^>]+>", " ", body)
            parts.append(body)

        return unescape("\n".join(part for part in parts if part).strip())

    def _extract_mail_fields(self, mail: Dict[str, Any]) -> Dict[str, str]:
        """统一提取邮件字段，兼容 raw MIME 和不同 Worker 返回格式。"""
        sender = str(
            mail.get("source")
            or mail.get("from")
            or mail.get("from_address")
            or mail.get("fromAddress")
            or ""
        ).strip()
        subject = str(mail.get("subject") or mail.get("title") or "").strip()
        body_text = str(
            mail.get("text")
            or mail.get("body")
            or mail.get("content")
            or mail.get("html")
            or ""
        ).strip()
        raw = str(mail.get("raw") or "").strip()

        if raw:
            try:
                message = message_from_string(raw, policy=email_policy)
                sender = sender or self._decode_mime_header(message.get("From", ""))
                subject = subject or self._decode_mime_header(message.get("Subject", ""))
                parsed_body = self._extract_body_from_message(message)
                if parsed_body:
                    body_text = f"{body_text}\n{parsed_body}".strip() if body_text else parsed_body
            except Exception as e:
                logger.debug(f"解析 TempMail raw 邮件失败: {e}")
                body_text = f"{body_text}\n{raw}".strip() if body_text else raw

        body_text = unescape(re.sub(r"<[^>]+>", " ", body_text))
        return {
            "sender": sender,
            "subject": subject,
            "body": body_text,
            "raw": raw,
        }

    def _extract_body_from_raw_mail(self, raw: str) -> str:
        """Extract a readable body from raw MIME content without searching header noise."""
        if not raw:
            return ""

        try:
            message = message_from_string(raw, policy=email_policy)
            return self._extract_body_from_message(message)
        except Exception as e:
            logger.debug(f"解析 TempMail raw 正文失败: {e}")
            return ""

    @staticmethod
    def _find_code_in_text(text: str, *, semantic_first: bool = True, pattern: str = OTP_CODE_PATTERN) -> Optional[str]:
        normalized = unescape(re.sub(r"<[^>]+>", " ", str(text or ""))).strip()
        if not normalized:
            return None

        if semantic_first:
            for candidate_pattern in SEMANTIC_OTP_PATTERNS:
                match = candidate_pattern.search(normalized)
                if match:
                    return match.group(1)

        match = re.search(pattern, normalized)
        return match.group(1) if match else None

    def _extract_verification_code_from_mail(
        self,
        parsed_mail: Dict[str, str],
        pattern: str = OTP_CODE_PATTERN,
    ) -> Optional[str]:
        """Prefer body-derived OTPs and avoid matching digits from headers/domain names."""
        body_text = str(parsed_mail.get("body") or "").strip()
        subject = str(parsed_mail.get("subject") or "").strip()
        raw_text = str(parsed_mail.get("raw") or "").strip()
        raw_body_text = self._extract_body_from_raw_mail(raw_text)

        for text in (body_text, raw_body_text, subject):
            code = self._find_code_in_text(text, semantic_first=True, pattern=pattern)
            if code:
                return code

        for text in (body_text, raw_body_text):
            code = self._find_code_in_text(text, semantic_first=False, pattern=pattern)
            if code:
                return code

        return self._find_code_in_text(subject, semantic_first=False, pattern=pattern)

    def _get_verification_state(self, email: str) -> Dict[str, Any]:
        state = self._verification_state.setdefault(
            email,
            {
                "stage": "signup_otp",
                "consumed_mail_ids": set(),
                "last_code": None,
                "last_mail_id": None,
                "last_mail_timestamp": None,
                "last_stage": None,
            },
        )
        consumed_mail_ids = state.get("consumed_mail_ids")
        if not isinstance(consumed_mail_ids, set):
            state["consumed_mail_ids"] = set(consumed_mail_ids or [])
        return state

    def set_verification_stage(self, email: str, stage: str) -> None:
        state = self._get_verification_state(email)
        state["stage"] = stage or "signup_otp"

    @staticmethod
    def _extract_mail_timestamp(mail: Dict[str, Any]) -> Optional[float]:
        for key in ("createdAt", "created_at", "receivedAt", "received_at", "timestamp", "date"):
            value = mail.get(key)
            if value in (None, ""):
                continue

            if isinstance(value, (int, float)):
                numeric = float(value)
                return numeric / 1000 if numeric > 1_000_000_000_000 else numeric

            if isinstance(value, str):
                stripped = value.strip()
                if not stripped:
                    continue
                if stripped.isdigit():
                    numeric = float(stripped)
                    return numeric / 1000 if numeric > 1_000_000_000_000 else numeric
                try:
                    return datetime.fromisoformat(stripped.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue

        return None

    def _admin_headers(self) -> Dict[str, str]:
        """构造 admin 请求头"""
        return {
            "x-admin-auth": self.config["admin_password"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _address_jwt_headers(jwt: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _fetch_domains_from_worker(self) -> Dict[str, Any]:
        """Fetch temp-mail domains from worker admin endpoint."""
        return self._make_request("GET", "/admin/domains")

    def get_domain_summary(self, preview_limit: int = 3) -> Dict[str, Any]:
        """Return safe domain summary for API/capabilities output."""
        summary = summarize_temp_mail_domains(
            self.config,
            fetch_domains=self._fetch_domains_from_worker,
            preview_limit=preview_limit,
        )
        summary.pop("domains", None)
        return summary

    def _make_request(self, method: str, path: str, **kwargs) -> Any:
        """
        发送请求并返回 JSON 数据
        """
        base_url = self.config["base_url"].rstrip("/")
        url = f"{base_url}{path}"

        kwargs.setdefault("headers", {})
        for k, v in self._admin_headers().items():
            kwargs["headers"].setdefault(k, v)

        # 强制限制超时时间和重试次数，防止用户的烂 Worker 把全站拖垮（包括查询域名、刷新邮箱、获取验证码等）
        old_timeout = self.http_client.config.timeout
        old_retries = self.http_client.config.max_retries
        self.http_client.config.timeout = 3
        self.http_client.config.max_retries = 1
        
        try:
            response = self.http_client.request(method, url, **kwargs)

            if response.status_code >= 400:
                error_msg = f"请求失败: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"{error_msg} - {error_data}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"
                self.update_status(False, EmailServiceError(error_msg))
                raise EmailServiceError(error_msg)

            try:
                return response.json()
            except json.JSONDecodeError:
                return {"raw_response": response.text}

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")
        finally:
            self.http_client.config.timeout = old_timeout
            self.http_client.config.max_retries = old_retries

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        通过 admin API 创建临时邮箱

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - jwt: 用户级 JWT token
            - service_id: 同 email（用作标识）
        """
        enable_prefix = bool((config or {}).get("enable_prefix", self.config.get("enable_prefix", False)))
        name = generate_local_part(enable_prefix=enable_prefix)
        domains = resolve_temp_mail_domains(
            self.config,
            fetch_domains=self._fetch_domains_from_worker,
        )
        if not domains:
            raise EmailServiceError("TempMail 未找到可用域名，请检查 domains/domain 或 /admin/domains")
        domain = choose_domain(domains)

        body = {
            "enablePrefix": enable_prefix,
            "name": name,
            "domain": domain,
        }

        try:
            response = self._make_request("POST", "/admin/new_address", json=body)

            address = response.get("address", "").strip()
            jwt = response.get("jwt", "").strip()

            if not address:
                raise EmailServiceError(f"API 返回数据不完整: {response}")

            email_info = {
                "email": address,
                "jwt": jwt,
                "service_id": address,
                "id": address,
                "created_at": time.time(),
            }

            # 缓存 jwt，供获取验证码时使用
            self._email_cache[address] = email_info

            logger.info(f"成功创建 TempMail 邮箱: {address}")
            self.update_status(True)
            return email_info

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    def _fetch_api_mails(self, jwt: str) -> Dict[str, Any]:
        """Fetch mailbox contents through the worker address-scoped API."""
        return self._make_request(
            "GET",
            "/api/mails",
            params={"limit": 20, "offset": 0},
            headers=self._address_jwt_headers(jwt),
        )

    def _fetch_admin_mails(self, email: str) -> Dict[str, Any]:
        """Fetch mailbox contents through the worker admin API."""
        return self._make_request(
            "GET",
            "/admin/mails",
            params={"limit": 20, "offset": 0, "address": email},
        )

    def _fetch_mail_batch(
        self,
        email: str,
        jwt: Optional[str],
    ) -> tuple[List[Dict[str, Any]], str, Optional[str]]:
        """Prefer address-scoped API mails, then fall back to admin mails."""
        last_error: Optional[str] = None

        if jwt:
            try:
                response = self._fetch_api_mails(jwt)
                mails = response.get("results", response if isinstance(response, list) else [])
                if isinstance(mails, list):
                    return mails, "/api/mails", None
                last_error = f"/api/mails returned unexpected payload: {response}"
            except Exception as e:
                last_error = str(e)
                cached = {**self._email_cache.get(email, {}), "disable_user_api": True, "disable_api_mails": True}
                self._email_cache[email] = cached
                logger.warning(f"TempMail /api/mails 失败，切换到 admin 拉取邮件: {email} - {e}")

        try:
            response = self._fetch_admin_mails(email)
            mails = response.get("results", response if isinstance(response, list) else [])
            if isinstance(mails, list):
                return mails, "/admin/mails", last_error
            if last_error:
                last_error = f"{last_error}; /admin/mails returned unexpected payload: {response}"
            else:
                last_error = f"/admin/mails returned unexpected payload: {response}"
        except Exception as e:
            admin_error = str(e)
            if last_error:
                last_error = f"{last_error}; {admin_error}"
            else:
                last_error = admin_error

        return [], "unavailable", last_error

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """Fetch a verification code from TempMail."""
        logger.info(f"??? TempMail ?? {email} ?????...")

        start_time = time.time()
        seen_mail_ids: set = set()
        poll_count = 0
        last_error: Optional[str] = None
        verification_state = self._get_verification_state(email)
        stage = str(verification_state.get("stage") or "signup_otp")
        consumed_mail_ids = verification_state.setdefault("consumed_mail_ids", set())
        last_code = verification_state.get("last_code")
        last_stage = verification_state.get("last_stage")
        min_timestamp = float(otp_sent_at or 0)
        debug_state = {
            "stage": stage,
            "poll_count": 0,
            "last_status": "waiting",
            "otp_sent_at": otp_sent_at or 0,
            "min_timestamp": min_timestamp,
            "fresh_verification_count": 0,
            "fresh_preferred_sender_count": 0,
            "stale_preferred_sender_count": 0,
            "available_fresh_verification_count": 0,
            "available_fresh_preferred_sender_count": 0,
            "used_fresh_preferred_sender_count": 0,
            "selected_sender": None,
            "selected_code": None,
            "selected_received_timestamp": None,
            "deferred_generic_only_polls": 0,
            "candidate_summaries": [],
        }
        self._last_verification_debug[email.lower()] = debug_state

        cached = self._email_cache.get(email, {})
        jwt = None if cached.get("disable_user_api") or cached.get("disable_api_mails") else cached.get("jwt")

        while time.time() - start_time < timeout:
            poll_count += 1
            debug_state["poll_count"] = poll_count
            try:
                mails, source_path, request_error = self._fetch_mail_batch(email, jwt)
                if request_error:
                    last_error = request_error
                if not isinstance(mails, list):
                    time.sleep(3)
                    continue

                matched_candidates: List[Dict[str, Any]] = []
                debug_candidates: List[Dict[str, Any]] = []

                for index, mail in enumerate(mails):
                    mail_id = mail.get("id")
                    if not mail_id or mail_id in seen_mail_ids:
                        continue

                    seen_mail_ids.add(mail_id)
                    if mail_id in consumed_mail_ids:
                        continue

                    parsed = self._extract_mail_fields(mail)
                    sender = parsed["sender"].lower()
                    subject = parsed["subject"]
                    body_text = parsed["body"]
                    content = f"{sender}\n{subject}\n{body_text}".strip()

                    if "openai" not in sender and "openai" not in content.lower():
                        continue

                    code = self._extract_verification_code_from_mail(parsed, pattern=pattern)
                    if not code:
                        continue

                    mail_ts = self._extract_mail_timestamp(mail)
                    is_fresh = otp_sent_at is None or (mail_ts is not None and mail_ts >= otp_sent_at)
                    freshness_rank = 1 if otp_sent_at is None else 0
                    if otp_sent_at is not None:
                        if mail_ts is not None:
                            if mail_ts < otp_sent_at:
                                debug_state["stale_preferred_sender_count"] += 1
                                continue
                            freshness_rank = 2
                            debug_state["available_fresh_verification_count"] += 1
                            debug_state["available_fresh_preferred_sender_count"] += 1
                            debug_state["fresh_preferred_sender_count"] += 1
                        elif stage == "relogin_otp":
                            debug_state["deferred_generic_only_polls"] += 1
                            continue

                    if stage == "relogin_otp" and code == last_code and last_stage == "signup_otp":
                        continue

                    debug_state["fresh_verification_count"] += int(is_fresh)
                    debug_candidates.append(
                        {
                            "sender": parsed["sender"] or "-",
                            "received_timestamp": mail_ts,
                            "delta_from_otp_sent": None if otp_sent_at is None or mail_ts is None else round(mail_ts - otp_sent_at, 3),
                            "code": code,
                            "preferred": bool(is_fresh),
                        }
                    )
                    matched_candidates.append(
                        {
                            "code": code,
                            "mail_id": mail_id,
                            "sender": parsed["sender"] or "-",
                            "timestamp": mail_ts if mail_ts is not None else float("-inf"),
                            "freshness_rank": freshness_rank,
                            "index": index,
                        }
                    )

                if debug_candidates:
                    debug_state["candidate_summaries"] = debug_candidates[:5]

                if matched_candidates:
                    matched_candidates.sort(
                        key=lambda item: (item["freshness_rank"], item["timestamp"], item["index"]),
                        reverse=True,
                    )
                    selected = matched_candidates[0]
                    code = selected["code"]
                    selected_mail_id = selected["mail_id"]
                    consumed_mail_ids.add(selected_mail_id)
                    verification_state["last_code"] = code
                    verification_state["last_mail_id"] = selected_mail_id
                    verification_state["last_mail_timestamp"] = (
                        None if selected["timestamp"] == float("-inf") else selected["timestamp"]
                    )
                    verification_state["last_stage"] = stage
                    debug_state["last_status"] = "matched"
                    debug_state["selected_sender"] = selected["sender"]
                    debug_state["selected_code"] = code
                    debug_state["selected_received_timestamp"] = (
                        None if selected["timestamp"] == float("-inf") else selected["timestamp"]
                    )
                    if selected["freshness_rank"] >= 2:
                        debug_state["used_fresh_preferred_sender_count"] = 1
                    wait_duration = max(time.time() - start_time, 0.0)
                    logger.info(
                        f"TempMail verification code found for {email}: {code} "
                        f"(source={source_path}, stage={stage}, polls={poll_count}, wait={wait_duration:.2f}s)"
                    )
                    self.update_status(True)
                    return code

            except Exception as e:
                last_error = str(e)
                logger.debug(f"?? TempMail ?????: {e}")

            time.sleep(3)

        wait_duration = max(time.time() - start_time, 0.0)
        debug_state["last_status"] = "timeout"
        logger.warning(
            f"?? TempMail ?????: {email}; polls={poll_count}; wait={wait_duration:.2f}s; "
            f"last_error={last_error or '-'}"
        )
        return None

    def get_last_verification_debug(self, email: str) -> Dict[str, Any]:
        return dict(self._last_verification_debug.get(email.lower(), {}))

    def list_emails(self, limit: int = 100, offset: int = 0, **kwargs) -> List[Dict[str, Any]]:
        """
        列出邮箱

        Args:
            limit: 返回数量上限
            offset: 分页偏移
            **kwargs: 额外查询参数，透传给 admin API

        Returns:
            邮箱列表
        """
        params = {
            "limit": limit,
            "offset": offset,
        }
        params.update({k: v for k, v in kwargs.items() if v is not None})

        try:
            response = self._make_request("GET", "/admin/mails", params=params)
            mails = response.get("results", [])
            if not isinstance(mails, list):
                raise EmailServiceError(f"API 返回数据格式错误: {response}")

            emails: List[Dict[str, Any]] = []
            for mail in mails:
                address = (mail.get("address") or "").strip()
                mail_id = mail.get("id") or address
                email_info = {
                    "id": mail_id,
                    "service_id": mail_id,
                    "email": address,
                    "subject": mail.get("subject"),
                    "from": mail.get("source"),
                    "created_at": mail.get("createdAt") or mail.get("created_at"),
                    "raw_data": mail,
                }
                emails.append(email_info)

                if address:
                    cached = self._email_cache.get(address, {})
                    self._email_cache[address] = {**cached, **email_info}

            self.update_status(True)
            return emails
        except Exception as e:
            logger.warning(f"列出 TempMail 邮箱失败: {e}")
            self.update_status(False, e)
            return list(self._email_cache.values())

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱

        Note:
            当前 TempMail admin API 文档未见删除地址接口，这里先从本地缓存移除，
            以满足统一接口并避免服务实例化失败。
        """
        removed = False
        emails_to_delete = []

        for address, info in self._email_cache.items():
            candidate_ids = {
                address,
                info.get("id"),
                info.get("service_id"),
            }
            if email_id in candidate_ids:
                emails_to_delete.append(address)

        for address in emails_to_delete:
            self._email_cache.pop(address, None)
            removed = True

        if removed:
            logger.info(f"已从 TempMail 缓存移除邮箱: {email_id}")
            self.update_status(True)
        else:
            logger.info(f"TempMail 缓存中未找到邮箱: {email_id}")

        return removed

    def check_health(self) -> bool:
        """检查服务健康状态"""
        # 临时缩短超时时间并关闭重试，避免失效的Worker域名导致同步请求阻塞前端UI长达数十秒
        old_timeout = self.http_client.config.timeout
        old_retries = self.http_client.config.max_retries
        self.http_client.config.timeout = 5
        self.http_client.config.max_retries = 1
        
        try:
            self._make_request(
                "GET",
                "/admin/mails",
                params={"limit": 1, "offset": 0},
                timeout=5,
            )
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"TempMail 健康检查失败: {e}")
            self.update_status(False, e)
            return False
        finally:
            self.http_client.config.timeout = old_timeout
            self.http_client.config.max_retries = old_retries
