"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import re
import json
import time
import random
import logging
import secrets
import string
import uuid
from typing import Optional, Dict, Any, Tuple, Callable, List
from dataclasses import dataclass
from datetime import datetime

from curl_cffi import requests as cffi_requests

from .openai.oauth import OAuthManager, OAuthStart, _decode_jwt_segment, _jwt_claims_no_verify, generate_oauth_url_no_prompt
from .http_client import OpenAIHTTPClient, HTTPClientError
from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType
from ..database import crud
from ..database.session import get_db
from ..config.constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    AccountStatus,
    TaskStatus,
)
from ..config.settings import get_settings


logger = logging.getLogger(__name__)


@dataclass
class RegistrationResult:
    """注册结果"""
    success: bool
    email: str = ""
    password: str = ""  # 注册密码
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 会话令牌
    error_message: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 或 'login'，区分账号来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """提交注册表单的结果"""
    success: bool
    page_type: str = ""  # 响应中的 page.type 字段
    is_existing_account: bool = False  # 是否为已注册账号
    response_data: Dict[str, Any] = None  # 完整的响应数据
    error_message: str = ""


class RegistrationEngine:
    """
    注册引擎
    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用
    """

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        proxy_source: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None
    ):
        """
        初始化注册引擎

        Args:
            email_service: 邮箱服务实例
            proxy_url: 代理 URL
            proxy_source: 代理来源（'explicit', 'auto', 'direct'）
            callback_logger: 日志回调函数
            task_uuid: 任务 UUID（用于数据库记录）
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.proxy_source = proxy_source or "direct"
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid

        # 创建 HTTP 客户端
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)

        # 创建 OAuth 管理器
        settings = get_settings()
        self.oauth_manager = OAuthManager(
            client_id=settings.openai_client_id,
            auth_url=settings.openai_auth_url,
            token_url=settings.openai_token_url,
            redirect_uri=settings.openai_redirect_uri,
            scope=settings.openai_scope,
            proxy_url=proxy_url  # 传递代理配置
        )

        # 状态变量
        self.email: Optional[str] = None
        self.password: Optional[str] = None  # 注册密码
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None  # 会话令牌
        self.logs: list = []
        self._otp_stage: str = "signup_otp"
        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳
        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self._token_acquisition_requires_login: bool = False  # 新注册账号需要二次登录拿 token
        self._last_registration_error: Optional[str] = None
        self._account_created: bool = False
        self._about_you_resume_attempts: int = 0
        self._recovery_account_id: Optional[int] = None
        self._recovery_mode: bool = False
        self._workspace_context: Dict[str, Any] = {}
        self._workspace_resolution_source: Optional[str] = None
        self._workspace_resolution_error: Optional[str] = None
        self._oauth_resume_source: Optional[str] = None
        self._session_bound_reauth_attempted: bool = False
        self._session_bound_reauth_otp_cycles: int = 0
        self._last_create_account_error_code: Optional[str] = None
        self._last_create_account_error_message: Optional[str] = None
        self._last_create_account_user_exists: bool = False
        self._about_you_user_exists_without_resume_attempts: int = 0

    def _log(self, message: str, level: str = "info"):
        """记录日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        # 添加到日志列表
        self.logs.append(log_message)

        # 调用回调函数
        if self.callback_logger:
            self.callback_logger(log_message)

        # 记录到数据库（如果有关联任务）
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")

        # 根据级别记录到日志系统
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成随机密码"""
        return ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱，先给新账号整个收件箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False

            self.email = self.email_info["email"]
            self._log(f"邮箱已就位，地址新鲜出炉: {self.email}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _start_oauth(self) -> bool:
        """开始 OAuth 流程"""
        try:
            self._log("开始 OAuth 授权流程，去门口刷个脸...")
            self.oauth_start = self.oauth_manager.start_oauth()
            self._log(f"OAuth URL 已备好，通道已经打开: {self.oauth_start.auth_url[:80]}...")
            return True
        except Exception as e:
            self._log(f"生成 OAuth URL 失败: {e}", "error")
            return False

    def _init_session(self) -> bool:
        """初始化会话"""
        try:
            self.session = self.http_client.session
            return True
        except Exception as e:
            self._log(f"初始化会话失败: {e}", "error")
            return False

    def _get_device_id(self) -> Optional[str]:
        """获取 Device ID"""
        if not self.oauth_start:
            return None

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if not self.session:
                    self.session = self.http_client.session

                response = self.session.get(
                    self.oauth_start.auth_url,
                    timeout=20
                )
                did = self.session.cookies.get("oai-did")

                if did:
                    self._log(f"Device ID: {did}")
                    return did

                self._log(
                    f"获取 Device ID 失败: 未返回 oai-did Cookie (HTTP {response.status_code}, 第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )
            except Exception as e:
                self._log(
                    f"获取 Device ID 失败: {e} (第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )

            if attempt < max_attempts:
                time.sleep(attempt)
                self.http_client.close()
                self.session = self.http_client.session

        return None

    def _get_session_user_agent(self) -> Optional[str]:
        session_headers = getattr(self.session, "headers", None) or {}
        return session_headers.get("User-Agent") or session_headers.get("user-agent")

    def _make_trace_headers(self) -> Dict[str, str]:
        trace_id = random.randint(10 ** 17, 10 ** 18 - 1)
        parent_id = random.randint(10 ** 17, 10 ** 18 - 1)
        return {
            "traceparent": f"00-{uuid.uuid4().hex}-{format(parent_id, '016x')}-01",
            "tracestate": "dd=s:1;o:rum",
            "x-datadog-origin": "rum",
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": str(trace_id),
            "x-datadog-parent-id": str(parent_id),
        }

    def _build_oauth_json_headers(self, referer: str) -> Dict[str, str]:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://auth.openai.com",
            "referer": referer,
        }
        user_agent = self._get_session_user_agent()
        if user_agent:
            headers["user-agent"] = user_agent

        session_cookies = getattr(self.session, "cookies", None) or {}
        device_id = None
        if hasattr(session_cookies, "get"):
            device_id = session_cookies.get("oai-did")
        elif isinstance(session_cookies, dict):
            device_id = session_cookies.get("oai-did")
        if device_id:
            headers["oai-device-id"] = device_id

        headers.update(self._make_trace_headers())
        return headers

    def _check_sentinel(self, did: str) -> Optional[str]:
        """检查 Sentinel 拦截"""
        try:
            sen_token = self.http_client.check_sentinel(did)
            if sen_token:
                self._log(f"Sentinel token 获取成功")
                return sen_token
            self._log("Sentinel 检查失败: 未获取到 token", "warning")
            return None

        except Exception as e:
            self._log(f"Sentinel 检查异常: {e}", "warning")
            return None

    def _submit_auth_start(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        screen_hint: Optional[str],
        referer: str,
        log_label: str,
        record_existing_account: bool = True,
    ) -> SignupFormResult:
        """
        提交授权入口表单

        Returns:
            SignupFormResult: 提交结果，包含账号状态判断
        """
        try:
            request_payload = {
                "username": {
                    "value": self.email,
                    "kind": "email",
                },
            }
            if screen_hint:
                request_payload["screen_hint"] = screen_hint

            request_body = json.dumps(request_payload)

            headers = {
                "referer": referer,
                "accept": "application/json",
                "content-type": "application/json",
            }

            if sen_token:
                sentinel = json.dumps({
                    "p": "",
                    "t": "",
                    "c": sen_token,
                    "id": did,
                    "flow": "authorize_continue",
                })
                headers["openai-sentinel-token"] = sentinel

            response = self.session.post(
                OPENAI_API_ENDPOINTS["signup"],
                headers=headers,
                data=request_body,
            )

            self._log(f"{log_label}状态: {response.status_code}")

            if response.status_code != 200:
                return SignupFormResult(
                    success=False,
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                )

            # 解析响应判断账号状态
            try:
                response_data = response.json()
                self._remember_workspace_payload(f"{screen_hint}_start", response_data)
                self._remember_navigation_from_response(f"{screen_hint}_start", response)
                page_type = response_data.get("page", {}).get("type", "")
                self._log(f"响应页面类型: {page_type}")

                is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]

                if is_existing:
                    self._otp_sent_at = time.time()
                    if record_existing_account:
                        self._log(f"检测到已注册账号，将自动切换到登录流程")
                        self._is_existing_account = True
                    else:
                        self._log("登录流程已触发，等待系统自动发送的验证码")

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=is_existing,
                    response_data=response_data
                )

            except Exception as parse_error:
                self._log(f"解析响应失败: {parse_error}", "warning")
                # 无法解析，默认成功
                return SignupFormResult(success=True)

        except Exception as e:
            self._log(f"{log_label}失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _submit_signup_form(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        record_existing_account: bool = True,
    ) -> SignupFormResult:
        """提交注册入口表单。"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            log_label="提交注册表单",
            record_existing_account=record_existing_account,
        )

    def _submit_login_start(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        screen_hint: Optional[str] = None,
    ) -> SignupFormResult:
        """提交登录入口表单。"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint=screen_hint,
            referer="https://auth.openai.com/log-in",
            log_label="提交登录入口",
            record_existing_account=False,
        )

    def _submit_login_password(self) -> SignupFormResult:
        """提交登录密码，进入邮箱验证码页面。"""
        try:
            response = self.session.post(
                OPENAI_API_ENDPOINTS["password_verify"],
                headers={
                    "referer": "https://auth.openai.com/log-in/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=json.dumps({"password": self.password}),
            )

            self._log(f"提交登录密码状态: {response.status_code}")

            if response.status_code != 200:
                return SignupFormResult(
                    success=False,
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                )

            response_data = response.json()
            self._remember_workspace_payload("login_password", response_data)
            self._remember_navigation_from_response("login_password", response)
            page_type = response_data.get("page", {}).get("type", "")
            self._log(f"登录密码响应页面类型: {page_type}")

            is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
            if is_existing:
                self._otp_sent_at = time.time()
                self._log("登录密码校验通过，等待系统自动发送的验证码")

            return SignupFormResult(
                success=True,
                page_type=page_type,
                is_existing_account=is_existing,
                response_data=response_data,
            )

        except Exception as e:
            self._log(f"提交登录密码失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _reset_auth_flow(self) -> None:
        """重置会话，准备重新发起 OAuth 流程。"""
        self.http_client.close()
        self.session = None
        self.oauth_start = None
        self.session_token = None
        self._otp_stage = "signup_otp"
        self._otp_sent_at = None
        self._session_bound_reauth_attempted = False
        self._session_bound_reauth_otp_cycles = 0
        self._reset_workspace_context()

    def _prepare_authorize_flow(self, label: str) -> Tuple[Optional[str], Optional[str]]:
        """初始化当前阶段的授权流程，返回 device id 和 sentinel token。"""
        self._log(f"{label}: 先把会话热热身...")
        if not self._init_session():
            return None, None

        self._log(f"{label}: OAuth 流程准备开跑，系好鞋带...")
        if not self._start_oauth():
            return None, None

        self._log(f"{label}: 领取 Device ID 通行证...")
        did = self._get_device_id()
        if not did:
            return None, None

        self._log(f"{label}: 解一道 Sentinel POW 小题，答对才给进...")
        sen_token = self._check_sentinel(did)
        if not sen_token:
            return did, None

        self._log(f"{label}: Sentinel 点头放行，继续前进")
        return did, sen_token

    def _resolve_oauth_callback_url(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        workspace_id = self._get_workspace_id()
        callback_url = str(self._workspace_context.get("callback_url") or "").strip()

        if workspace_id:
            self._workspace_context["resolved_workspace_id"] = workspace_id
            self._oauth_resume_source = self._oauth_resume_source or "workspace_select_required"
            callback_url = self._continue_from_workspace_selection(workspace_id)
            if not callback_url:
                return None, workspace_id, "选择 Workspace 失败"
            return callback_url, workspace_id, None

        if callback_url:
            self._mark_workspace_resolution("callback_available_before_workspace")
            self._oauth_resume_source = self._oauth_resume_source or "callback_found_from_validate_response"
            return callback_url, None, None

        callback_url = self._resume_oauth_callback()
        if callback_url:
            return callback_url, None, None

        if self._workspace_context.get("reentered_login"):
            return None, None, "OAuth 恢复链路重新进入登录页"
        return None, None, "OAuth 续跑失败"

    def _complete_token_exchange(self, result: RegistrationResult, skip_otp_validation: bool = False) -> bool:
        """在登录态已建立后，继续完成 workspace 和 OAuth token 获取。"""
        callback_url: Optional[str] = None
        workspace_id: Optional[str] = result.workspace_id or None
        resolution_error: Optional[str] = None

        if self._account_created and not self._token_acquisition_requires_login:
            self._log("建号后先尝试沿当前会话完成 OAuth，避免过早重新登录")
            callback_url, workspace_id, resolution_error = self._resolve_oauth_callback_url()
            if workspace_id:
                result.workspace_id = workspace_id
            if callback_url:
                self._log("建号后当前会话已拿到 callback，跳过重新登录")
            else:
                self._log(
                    "建号后当前会话未完成 OAuth，回退到重新登录拿 token: "
                    f"{resolution_error or 'current_session_unresolved'}",
                    "warning",
                )
                login_ready, login_error = self._restart_login_flow()
                if not login_ready:
                    result.error_message = login_error
                    return False

        if not callback_url:
            if not skip_otp_validation:
                otp_stage = "relogin_otp" if self._token_acquisition_requires_login else "signup_otp"
                code = self._get_verification_code(
                    stage=otp_stage,
                    timeout=self._get_verification_timeout(otp_stage),
                )
                if not code:
                    result.error_message = "获取验证码失败"
                    return False

                if not self._validate_verification_code(code):
                    result.error_message = "验证码校验失败"
                    return False

            about_you_url = self._find_about_you_candidate()
            if about_you_url and self._about_you_resume_attempts < 3:
                if getattr(self, "_last_create_account_user_exists", False):
                    self._log("检测到 about-you 且曾经触发过 user_already_exists 死锁，放弃尝试该账号以防封IP", level="error")
                    result.error_message = "账号状态异常(半成品死锁)"
                    return False
                    
                self._about_you_resume_attempts += 1
                self._log(
                    "恢复链路命中 about-you，先补完建号再重启登录: "
                    f"{self._sanitize_url_for_log(about_you_url)} "
                    f"(attempt {self._about_you_resume_attempts}/3)"
                )
                callback_url = None
                if self._account_created:
                    self._log(
                        f"恢复链路命中 about-you，但账号已建成 (_account_created=True)，"
                        f"跳过 create_account，直接尝试 authorize replay 拿 callback..."
                    )
                    callback_url = self._attempt_authorize_replay(allow_session_bound_reauth=False, reentry_log_level="warning")
                    if callback_url:
                        self._log("账号已建成 + authorize replay 成功拿到 callback，跳过建号循环")
                    else:
                        callback_url = self._attempt_direct_consent_recovery()
                        if callback_url:
                            self._log("账号已建成 + direct consent fallback 成功拿到 callback")
                        else:
                            if self._about_you_resume_attempts >= 2:
                                result.error_message = "账号已建成但无法获取 OAuth callback"
                                return False
                            login_ready, login_error = self._restart_login_flow()
                            if not login_ready:
                                result.error_message = login_error
                                return False
                            return self._complete_token_exchange(result)
                else:
                    self._oauth_resume_source = self._oauth_resume_source or "continue_url_resume"
                    self._set_recovery_debug_summary(
                        "about_you_requires_account_creation",
                        about_you_url=about_you_url,
                        attempt=self._about_you_resume_attempts,
                    )
                    if not self._create_user_account(allow_existing_account=True, about_you_url=about_you_url):
                        result.error_message = "创建用户账户失败"
                        return False

                unresolved_existing_account_without_resume = False
                if not callback_url:
                    self._account_created = True
                    callback_after_create = str(self._workspace_context.get("callback_url") or "").strip()
                    workspace_after_create = None
                    auth_cookie = self.session.cookies.get("oai-client-auth-session") if self.session else None
                    if auth_cookie:
                        workspace_after_create = self._get_workspace_id()
                    unresolved_existing_account_without_resume = False

                    self._log(
                        "about-you 建号后快照: "
                        f"callback_after_create={'yes' if callback_after_create else 'no'}, "
                        f"workspace_after_create={workspace_after_create or '-'}, "
                        f"auth_cookie_present={'yes' if auth_cookie else 'no'}, "
                        f"resume_url_after_create={self._sanitize_url_for_log(self._workspace_context.get('resume_url')) or '-'}, "
                        f"user_exists={self._last_create_account_user_exists}, "
                        f"user_exists_attempt={self._about_you_user_exists_without_resume_attempts}"
                    )

                    if callback_after_create or workspace_after_create:
                        self._about_you_user_exists_without_resume_attempts = 0
                        callback_url, workspace_id, resolution_error = self._resolve_oauth_callback_url()
                        if callback_url:
                            if workspace_id:
                                result.workspace_id = workspace_id
                            self._log("about-you 处理后当前会话已拿到 callback，直接继续换 token")
                        else:
                            self._log("about-you 处理后当前会话未完成 callback，回退到重新登录续跑", "warning")
                    else:
                        callback_url = None
                        workspace_id = None
                        resolution_error = None
                        resume_url_after_create = str(self._workspace_context.get("resume_url") or "").strip()
                        if resume_url_after_create and not self._is_about_you_url(resume_url_after_create):
                            self._log(
                                "about-you 处理后当前会话仍未暴露 callback/workspace，但已缓存恢复 URL，先沿当前会话续跑"
                            )
                            callback_url = self._resume_oauth_callback()
                            if callback_url:
                                self._about_you_user_exists_without_resume_attempts = 0
                                self._log("about-you 处理后通过当前会话恢复 URL 拿到 callback，直接继续换 token")
                            else:
                                resolution_error = (
                                    "OAuth 恢复链路重新进入登录页"
                                    if self._workspace_context.get("reentered_login")
                                    else "OAuth 续跑失败"
                                )
                                self._set_recovery_debug_summary(
                                    "about_you_user_exists_resume_attempt_failed",
                                    about_you_url=about_you_url,
                                    resume_url=resume_url_after_create,
                                    error=resolution_error,
                                )
                                self._log("about-you 处理后恢复 URL 未拿到 callback，回退到重新登录续跑", "warning")
                        unresolved_existing_account_without_resume = (
                            self._last_create_account_user_exists
                            and (
                                not resume_url_after_create
                                or self._is_about_you_url(resume_url_after_create)
                            )
                        )
                        if unresolved_existing_account_without_resume:
                            self._log(
                                "about-you 处理后当前会话仍未暴露 callback/workspace，先尝试同会话 authorize replay"
                            )
                            callback_url = self._attempt_authorize_replay(
                                allow_session_bound_reauth=False,
                                reentry_log_level="warning",
                            )
                            if callback_url:
                                self._about_you_user_exists_without_resume_attempts = 0
                                self._set_recovery_debug_summary(
                                    "about_you_user_exists_authorize_replay_callback_resolved",
                                    about_you_url=about_you_url,
                                    callback_url=callback_url,
                                )
                                self._log(
                                    "about-you/user_already_exists 后通过同会话 authorize replay 拿到 callback，直接继续换 token"
                                )
                            else:
                                login_challenge_url = self._find_cached_resume_candidate("login_challenge_resume")
                                if login_challenge_url:
                                    self._log(
                                        "about-you/user_already_exists 后 authorize replay 命中过 login_challenge，"
                                        "尝试 direct consent fallback",
                                        "warning",
                                    )
                                    callback_url = self._attempt_direct_consent_recovery()
                                if callback_url:
                                    self._about_you_user_exists_without_resume_attempts = 0
                                    self._set_recovery_debug_summary(
                                        "about_you_user_exists_direct_consent_callback_resolved",
                                        about_you_url=about_you_url,
                                        callback_url=callback_url,
                                    )
                                    self._log(
                                        "about-you/user_already_exists 后通过 direct consent fallback 拿到 callback，"
                                        "直接继续换 token"
                                    )
                                else:
                                    resolution_error = (
                                        "OAuth 恢复链路重新进入登录页"
                                        if self._workspace_context.get("reentered_login")
                                        else "OAuth 续跑失败"
                                    )
                                    self._about_you_user_exists_without_resume_attempts += 1
                                    self._oauth_resume_source = "about_you_user_exists_without_resume"
                                    self._set_recovery_debug_summary(
                                        "about_you_user_exists_without_resume",
                                        attempt=self._about_you_user_exists_without_resume_attempts,
                                        about_you_url=about_you_url,
                                        error_code=self._last_create_account_error_code,
                                        error=resolution_error,
                                    )
                                    self._mark_workspace_resolution(
                                        "about_you_user_exists_without_resume",
                                        "about-you 返回 user_already_exists，但当前会话未暴露 callback/workspace",
                                    )
                                    self._log(
                                        "about-you/user_already_exists 后同会话 authorize replay 未拿到 callback，回退到重新登录续跑",
                                        "warning",
                                    )

                    if not callback_url:
                        if (
                            unresolved_existing_account_without_resume
                            and self._about_you_user_exists_without_resume_attempts >= 2
                        ):
                            result.error_message = "about-you 返回 user_already_exists，但连续两次未暴露 callback/workspace"
                            self._oauth_resume_source = "about_you_user_exists_without_resume_exhausted"
                            self._set_recovery_debug_summary(
                                "about_you_user_exists_without_resume_exhausted",
                                attempt=self._about_you_user_exists_without_resume_attempts,
                                about_you_url=about_you_url,
                                error_code=self._last_create_account_error_code,
                            )
                            self._mark_workspace_resolution(
                                "about_you_user_exists_without_resume",
                                result.error_message,
                            )
                            self._log(
                                "about-you/user_already_exists 连续两次未拿到 callback/workspace，停止重复登录以避免触发 OTP 限制",
                                "error",
                            )
                            return False
                        login_ready, login_error = self._restart_login_flow()
                        if not login_ready:
                            result.error_message = login_error
                            return False
                        return self._complete_token_exchange(result)
            else:
                callback_url, workspace_id, resolution_error = self._resolve_oauth_callback_url()

        if workspace_id:
            result.workspace_id = workspace_id
        if not callback_url:
            result.error_message = resolution_error or "OAuth 续跑失败"
            return False

        token_info = self._handle_oauth_callback(callback_url)
        if not token_info:
            result.error_message = "处理 OAuth 回调失败"
            return False

        result.account_id = token_info.get("account_id", "")
        result.access_token = token_info.get("access_token", "")
        result.refresh_token = token_info.get("refresh_token", "")
        result.id_token = token_info.get("id_token", "")
        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"

        if not result.workspace_id:
            workspace_id = self._get_workspace_id()
            if not workspace_id:
                self._mark_workspace_resolution("token_acquired_without_workspace", "获取 Workspace ID 失败")
                result.error_message = "获取 Workspace ID 失败"
                return False
            result.workspace_id = workspace_id

        session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if session_cookie:
            self.session_token = session_cookie
            result.session_token = session_cookie

        return True

    def _restart_login_flow(self) -> Tuple[bool, str]:
        """新注册账号完成建号后，重新发起一次登录流程拿 token。"""
        self._token_acquisition_requires_login = True
        self._log(
            "重新登录前保留当前恢复快照: "
            f"callback={self._sanitize_url_for_log(self._workspace_context.get('callback_url')) or '-'}, "
            f"resume={self._sanitize_url_for_log(self._workspace_context.get('resume_url')) or '-'}, "
            f"terminal={self._sanitize_url_for_log(self._workspace_context.get('redirect_terminal_url')) or '-'}, "
            f"resume_source={self._oauth_resume_source or '-'}",
            "warning",
        )
        self._log("注册这边忙完了，再走一趟登录把 token 请出来，收个尾...")
        self._reset_auth_flow()

        did, sen_token = self._prepare_authorize_flow("重新登录")
        if not did:
            return False, "重新登录时获取 Device ID 失败"
        if not sen_token:
            return False, "重新登录时 Sentinel POW 验证失败"

        import time
        max_429_retries = 3
        backoff_delays = [15, 30, 45]
        login_start_result = None
        for i in range(max_429_retries + 1):
            login_start_result = self._submit_login_start(did, sen_token, screen_hint="login")
            if login_start_result.success:
                break
            if "429" in login_start_result.error_message and i < max_429_retries:
                delay = backoff_delays[i]
                self._log(f"重新登录遇到 HTTP 429 速率限制，等待 {delay}s 后重试 (第 {i+1} 次)...", "warning")
                time.sleep(delay)
                sen_token = self._check_sentinel(did) or sen_token
            else:
                break
        
        if not login_start_result.success:
            return False, f"重新登录提交邮箱失败: {login_start_result.error_message}"
        if login_start_result.page_type != OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            return False, f"重新登录未进入密码页面: {login_start_result.page_type or 'unknown'}"

        password_result = self._submit_login_password()
        if not password_result.success:
            return False, f"重新登录提交密码失败: {password_result.error_message}"
        if not password_result.is_existing_account:
            return False, f"重新登录未进入验证码页面: {password_result.page_type or 'unknown'}"
        self._prepare_relogin_otp_flow("重新登录")
        return True, ""

    # ======================================================================
    # chatgpt.com 会话桥接方法 (从 dou-jiang/codex-console 移植)
    # ======================================================================

    @staticmethod
    def _extract_session_token_from_cookie_text(cookie_text: str) -> str:
        """从 Cookie 文本中提取 next-auth session token（兼容分片）。"""
        text = str(cookie_text or "")
        if not text:
            return ""

        direct = re.search(r"(?:^|[;,]\s*)(?:__|_)Secure-next-auth\.session-token=([^;,]*)", text)
        if direct:
            direct_val = str(direct.group(1) or "").strip().strip('"').strip("'")
            if direct_val:
                return direct_val

        parts = re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=([^;,]*)", text)
        if not parts:
            return ""

        chunk_map = {}
        for idx, value in parts:
            try:
                clean_value = str(value or "").strip().strip('"').strip("'")
                if clean_value:
                    chunk_map[int(idx)] = clean_value
            except Exception:
                continue
        if not chunk_map:
            return ""
        return "".join(chunk_map[i] for i in sorted(chunk_map.keys()))

    @staticmethod
    def _extract_session_token_from_cookie_jar(cookie_jar) -> str:
        """从 requests CookieJar 中提取 session token。"""
        if not cookie_jar:
            return ""
        for name in ("__Secure-next-auth.session-token", "_Secure-next-auth.session-token"):
            val = str(cookie_jar.get(name) or "").strip()
            if val:
                return val
        return ""

    @staticmethod
    def _flatten_set_cookie_headers(response) -> str:
        """将 response 的所有 Set-Cookie 头拼成一行文本。"""
        if response is None:
            return ""
        headers = getattr(response, "headers", None)
        if not headers:
            return ""
        try:
            raw = headers.get_all("Set-Cookie") if hasattr(headers, "get_all") else []
        except Exception:
            raw = []
        if not raw:
            val = str(headers.get("Set-Cookie") or "").strip()
            return val
        return "; ".join(str(v) for v in raw if v)

    @staticmethod
    def _extract_request_cookie_header(response) -> str:
        """从 response.request 提取发送时的 Cookie header。"""
        if response is None:
            return ""
        req = getattr(response, "request", None)
        if not req:
            return ""
        headers = getattr(req, "headers", {}) or {}
        return str(headers.get("Cookie") or "").strip()

    def _dump_session_cookies(self) -> str:
        """将当前 session 的所有 cookie 导出为文本。"""
        if not self.session:
            return ""
        parts = []
        try:
            for cookie in self.session.cookies:
                parts.append(f"{cookie.name}={cookie.value}")
        except Exception:
            pass
        return "; ".join(parts)

    def _warmup_chatgpt_session(self) -> None:
        """仅预热 chatgpt 首页，避免提前消费一次性 continue_url。"""
        try:
            self.session.get(
                "https://chatgpt.com/",
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": "https://auth.openai.com/",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                timeout=20,
            )
        except Exception as e:
            self._log(f"chatgpt 首页预热异常: {e}", "warning")

    def _capture_auth_session_tokens(self, result, access_hint=None) -> bool:
        """
        直接通过 /api/auth/session 捕获 session_token + access_token。
        """
        access_token = str(access_hint or "").strip()
        set_cookie_text = ""
        request_cookie_text = ""
        try:
            headers = {
                "accept": "application/json",
                "referer": "https://chatgpt.com/",
                "origin": "https://chatgpt.com",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "cache-control": "no-cache",
                "pragma": "no-cache",
            }
            if access_token:
                headers["authorization"] = f"Bearer {access_token}"
            response = self.session.get(
                "https://chatgpt.com/api/auth/session",
                headers=headers,
                timeout=20,
            )
            set_cookie_text = self._flatten_set_cookie_headers(response)
            request_cookie_text = self._extract_request_cookie_header(response)
            if response.status_code == 200:
                try:
                    data = response.json() or {}
                    access_from_json = str(data.get("accessToken") or "").strip()
                    if access_from_json:
                        access_token = access_from_json
                except Exception:
                    pass
            else:
                self._log(f"/api/auth/session 返回异常状态: {response.status_code}", "warning")
        except Exception as e:
            self._log(f"获取 auth/session 失败: {e}", "warning")

        # 1) 直接从 cookie jar 拿
        session_token = self._extract_session_token_from_cookie_jar(self.session.cookies)

        # 2) 从完整 cookies 文本兜底（含分片）
        if not session_token:
            session_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())

        # 3) 从 set-cookie 兜底（含分片）
        if not session_token and set_cookie_text:
            session_token = self._extract_session_token_from_cookie_text(set_cookie_text)

        # 4) 从请求 Cookie 头兜底
        if not session_token and request_cookie_text:
            session_token = self._extract_session_token_from_cookie_text(request_cookie_text)

        # 兜底：已有 access_token 但无 session_token 时，带 Bearer 再请求一次
        if (not session_token) and access_token:
            try:
                retry_response = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers={
                        "accept": "application/json",
                        "referer": "https://chatgpt.com/",
                        "origin": "https://chatgpt.com",
                        "user-agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                        "authorization": f"Bearer {access_token}",
                        "cache-control": "no-cache",
                        "pragma": "no-cache",
                    },
                    timeout=20,
                )
                retry_set_cookie = self._flatten_set_cookie_headers(retry_response)
                retry_request_cookie = self._extract_request_cookie_header(retry_response)
                if not session_token:
                    session_token = self._extract_session_token_from_cookie_jar(self.session.cookies)
                if not session_token:
                    session_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())
                if not session_token and retry_set_cookie:
                    session_token = self._extract_session_token_from_cookie_text(retry_set_cookie)
                if not session_token and retry_request_cookie:
                    session_token = self._extract_session_token_from_cookie_text(retry_request_cookie)
            except Exception as e:
                self._log(f"Bearer 兜底换 session_token 失败: {e}", "warning")

        if not session_token:
            cookies_text = self._dump_session_cookies()
            raw_direct_match = re.search(
                r"(?:^|[;,]\s*)(?:__|_)Secure-next-auth\.session-token=([^;,]*)",
                cookies_text,
            )
            raw_direct_len = len(str(raw_direct_match.group(1) or "").strip()) if raw_direct_match else 0
            chunk_count = len(re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=", cookies_text))
            req_cookie_len = len(str(request_cookie_text or "").strip())
            self._log(
                f"auth/session 仍未命中 session_token（raw_direct_len={raw_direct_len}, chunks={chunk_count}, req_cookie_len={req_cookie_len}）",
                "warning",
            )

        # 设备 ID 同步
        did = ""
        try:
            did = str(self.session.cookies.get("oai-did") or "").strip()
        except Exception:
            did = ""
        if did:
            self.device_id = did

        if session_token:
            self.session_token = session_token
            result.session_token = session_token
        if access_token:
            result.access_token = access_token

        self._log(
            "Auth Session 捕获结果: session_token="
            + ("有" if bool(result.session_token) else "无")
            + ", access_token="
            + ("有" if bool(result.access_token) else "无")
        )
        return bool(result.session_token and result.access_token)

    def _follow_chatgpt_auth_redirects(self, start_url: str):
        """
        手动跟踪 chatgpt.com signin 后的 30x 重定向，识别 /api/auth/callback/openai。
        Returns: (callback_url, final_url)
        """
        import urllib.parse

        current_url = str(start_url or "").strip()
        callback_url = ""
        bridged_header_token = ""
        if not current_url:
            return "", ""

        max_redirects = 12
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        for i in range(max_redirects):
            self._log(f"会话桥接重定向 {i+1}/{max_redirects}: {current_url[:120]}...")
            if "/api/auth/callback/openai" in current_url and not callback_url:
                callback_url = current_url

            resp = self.session.get(
                current_url,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": "https://chatgpt.com/",
                    "user-agent": ua,
                },
                timeout=25,
                allow_redirects=False,
            )

            # 从每一跳响应头 Set-Cookie 抓 session_token
            set_cookie_text = self._flatten_set_cookie_headers(resp)
            token_from_header = self._extract_session_token_from_cookie_text(set_cookie_text)
            if token_from_header:
                bridged_header_token = token_from_header
                for name in ("__Secure-next-auth.session-token", "_Secure-next-auth.session-token"):
                    for domain in (".chatgpt.com", "chatgpt.com"):
                        try:
                            self.session.cookies.set(name, token_from_header, domain=domain, path="/")
                        except Exception:
                            continue
                self._log(
                    f"会话桥接命中 Set-Cookie session_token（len={len(token_from_header)}）"
                )

            if resp.status_code not in (301, 302, 303, 307, 308):
                break

            location = str(resp.headers.get("Location") or "").strip()
            if not location:
                break
            current_url = urllib.parse.urljoin(current_url, location)

        if callback_url and not str(current_url or "").startswith("https://chatgpt.com/"):
            try:
                self.session.get(
                    "https://chatgpt.com/",
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": current_url,
                        "user-agent": ua,
                    },
                    timeout=20,
                )
            except Exception:
                pass

        self._log(
            f"会话桥接重定向结束: callback={'有' if callback_url else '无'}, "
            f"set_cookie_token={'有' if bool(bridged_header_token) else '无'}, final={current_url[:120]}..."
        )
        return callback_url, current_url

    def _bootstrap_chatgpt_signin_for_session(self, result) -> bool:
        """
        chatgpt.com 会话桥接：csrf -> signin/openai -> 跟随跳转 -> auth/session，
        目标是在 auth.openai.com 登录态已建立后，通过 chatgpt.com 拿到 session_token。
        """
        self._log("Session Token 还没就位，尝试 chatgpt.com 会话桥接...")
        self._warmup_chatgpt_session()
        csrf_token = ""
        auth_url = ""
        try:
            csrf_resp = self.session.get(
                "https://chatgpt.com/api/auth/csrf",
                headers={
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/auth/login",
                    "origin": "https://chatgpt.com",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                timeout=20,
            )
            if csrf_resp.status_code == 200:
                csrf_token = str((csrf_resp.json() or {}).get("csrfToken") or "").strip()
            else:
                self._log(f"csrf 获取失败: HTTP {csrf_resp.status_code}", "warning")
        except Exception as e:
            self._log(f"csrf 获取异常: {e}", "warning")

        if not csrf_token:
            self._log("csrf token 为空，跳过会话桥接", "warning")
            return False

        try:
            signin_resp = self.session.post(
                "https://chatgpt.com/api/auth/signin/openai",
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": "https://chatgpt.com",
                    "referer": "https://chatgpt.com/auth/login",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                data={
                    "csrfToken": csrf_token,
                    "callbackUrl": "https://chatgpt.com/",
                    "json": "true",
                },
                timeout=20,
            )
            if signin_resp.status_code == 200:
                auth_url = str((signin_resp.json() or {}).get("url") or "").strip()
            else:
                self._log(f"signin/openai 失败: HTTP {signin_resp.status_code}", "warning")
        except Exception as e:
            self._log(f"signin/openai 异常: {e}", "warning")

        if not auth_url:
            self._log("signin/openai 未返回 auth_url，跳过会话桥接", "warning")
            return False

        callback_url = ""
        final_url = auth_url
        try:
            callback_url, final_url = self._follow_chatgpt_auth_redirects(auth_url)
        except Exception as e:
            self._log(f"会话桥接重定向跟踪异常: {e}", "warning")
            callback_url = ""
            final_url = auth_url

        # 若已拿到 callback，补打一跳确保 next-auth callback 被完整执行
        if callback_url and "error=" not in callback_url:
            try:
                self.session.get(
                    callback_url,
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": "https://chatgpt.com/auth/login",
                        "user-agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                    },
                    allow_redirects=True,
                    timeout=25,
                )
            except Exception as e:
                self._log(f"会话桥接 callback 补跳异常: {e}", "warning")
        elif callback_url and "error=" in callback_url:
            self._log(f"会话桥接回调返回错误参数: {callback_url[:140]}...", "warning")
        else:
            self._log(f"会话桥接未命中 callback，final_url={final_url[:120]}...", "warning")
            # 命中 auth.openai 登录页时，在 auth 侧重新走一遍完整登录来建立会话
            if "auth.openai.com/log-in" in str(final_url or "").lower():
                if not getattr(self, '_bridge_login_active', False):
                    self._log("会话桥接进入登录页，尝试自动登录后继续抓取 session_token...")
                    if self._bridge_login_for_session_token(result):
                        return True
                else:
                    self._log("会话桥接进入登录页，但已处于桥接登录中，跳过递归", "warning")

        self._warmup_chatgpt_session()
        cookie_text = self._dump_session_cookies()
        direct_token = self._extract_session_token_from_cookie_text(cookie_text)
        has_direct = bool(direct_token)
        chunk_count = len(re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=", cookie_text))
        if direct_token and not result.session_token:
            self.session_token = direct_token
            result.session_token = direct_token
            self._log(f"会话桥接已缓存 session_token（len={len(direct_token)}）")
        self._log(
            f"会话桥接后 cookie 概览: direct={'有' if has_direct else '无'}, chunks={chunk_count}"
        )
        return self._capture_auth_session_tokens(result, access_hint=result.access_token)

    def _bridge_login_for_session_token(self, result) -> bool:
        """
        当 chatgpt signin/openai 跳回 auth.openai 登录页时，自动补一次登录流程：
        login -> password -> email otp -> workspace -> auth/session。
        """
        try:
            if not self.email or not self.password:
                self._log("会话桥接自动登录缺少邮箱或密码，无法继续", "warning")
                return False

            did = ""
            try:
                did = str(self.session.cookies.get("oai-did") or "").strip()
            except Exception:
                did = ""
            if not did:
                did = str(uuid.uuid4())
                try:
                    self.session.cookies.set("oai-did", did, domain=".chatgpt.com", path="/")
                except Exception:
                    pass
            self.device_id = did

            sen_token = self._check_sentinel(did)
            login_start_result = self._submit_login_start(did, sen_token)
            if not login_start_result.success:
                self._log(
                    f"会话桥接自动登录入口失败: {login_start_result.error_message}",
                    "warning",
                )
                return False
            page_type = str(login_start_result.page_type or "").strip()

            if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
                self._log("会话桥接自动登录已直达邮箱验证码页，跳过密码提交")
            elif page_type == OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
                password_result = self._submit_login_password()
                if not password_result.success:
                    self._log(
                        f"会话桥接自动登录提交密码失败: {password_result.error_message}",
                        "warning",
                    )
                    return False
                if not password_result.is_existing_account:
                    self._log(
                        f"会话桥接自动登录未进入邮箱验证码页: {password_result.page_type or 'unknown'}",
                        "warning",
                    )
                    return False
            else:
                self._log(
                    f"会话桥接自动登录入口返回未知页面: {page_type or 'unknown'}",
                    "warning",
                )
                return False

            # 等待并校验桥接登录的 OTP
            bridge_otp_ok = False
            code = self._get_verification_code(stage="relogin_otp", timeout=90)
            if code:
                bridge_otp_ok = self._validate_verification_code(code)
            if not bridge_otp_ok:
                # 重试一次
                self._log("会话桥接自动登录首轮 OTP 未命中，重发重试...", "warning")
                self._retrigger_login_otp()
                code = self._get_verification_code(stage="relogin_otp", timeout=90)
                if code:
                    bridge_otp_ok = self._validate_verification_code(code)
            if not bridge_otp_ok:
                self._log("会话桥接自动登录验证码校验失败", "warning")
                return False

            # OTP 成功后，尝试多种方式获取 session_token
            self._log("会话桥接自动登录 OTP 通过，开始多路径抓取 token...")

            # 路径 0 (优先): 检查 validate_otp 是否已通过 _remember_navigation_from_response 记录了 chatgpt callback URL
            # 日志分析发现：第三次 OTP 成功后 validate_otp 返回 external_url，continue_url 就是
            # https://chatgpt.com/api/auth/callback/openai?code=... 形式的 OAuth 授权码回调。
            # 只需直接 GET 这个 URL，chatgpt.com 就会在 Set-Cookie 中返回 session_token。
            cached_callback = str(self._workspace_context.get("callback_url") or "").strip()
            if cached_callback and "/api/auth/callback/openai" in cached_callback:
                self._log(
                    f"路径0: validate_otp 已捕获 chatgpt callback URL，直接 GET 换 token: "
                    f"{self._sanitize_url_for_log(cached_callback)}"
                )
                try:
                    cb_resp = self.session.get(
                        cached_callback,
                        allow_redirects=True,
                        timeout=30,
                        headers={
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                            ),
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        },
                    )
                    # 从 Set-Cookie 中提取 session_token
                    st = ""
                    for cookie in self.session.cookies:
                        cookie_name = getattr(cookie, "name", "")
                        if "session-token" in cookie_name.lower() or cookie_name == "__Secure-next-auth.session-token":
                            st = getattr(cookie, "value", "")
                            break
                    if not st:
                        # 也检查响应头中的 Set-Cookie
                        set_cookie_headers = cb_resp.headers.get("set-cookie", "")
                        if "session-token=" in str(set_cookie_headers):
                            import re as _re
                            m = _re.search(r"session-token=([^;]+)", str(set_cookie_headers))
                            if m:
                                st = m.group(1)
                    if st and len(st) > 20:
                        result.session_token = st
                        self.session_token = st
                        self._log(f"路径0: 直接 GET chatgpt callback 成功拿到 session_token ✓ (len={len(st)})")
                        # 补取 access_token：通过 /api/auth/session 获取 accessToken
                        try:
                            self._warmup_chatgpt_session()
                            self._capture_auth_session_tokens(result, access_hint=result.access_token)
                            if result.access_token:
                                self._log(f"路径0: 补取 access_token 成功 ✓ (len={len(result.access_token)})")
                            else:
                                self._log("路径0: 补取 access_token 未命中（仅有 session_token）", "warning")
                        except Exception as e:
                            self._log(f"路径0: 补取 access_token 异常: {e}", "warning")
                        return True
                    else:
                        self._log(
                            f"路径0: GET chatgpt callback 完成但未找到 session_token "
                            f"(status={cb_resp.status_code}, cookies={len(list(self.session.cookies))}), "
                            f"继续尝试其他路径...",
                            "warning",
                        )
                except Exception as e:
                    self._log(f"路径0: GET chatgpt callback 异常: {e}，继续尝试其他路径...", "warning")
            elif cached_callback:
                self._log(
                    f"路径0: 已有 callback URL 但不是 chatgpt 类型，跳过直接 GET: "
                    f"{self._sanitize_url_for_log(cached_callback)}",
                    "warning",
                )

            # 路径 1: 直接 capture（有些场景 OTP 后 auth 会话足够）
            self._warmup_chatgpt_session()

            if self._capture_auth_session_tokens(result, access_hint=result.access_token):
                self._log("会话桥接自动登录在 OTP 后直接命中 session_token ✓")
                return True

            # 路径 2: 尝试 about_you 补交，然后 capture
            self._log("会话桥接自动登录: 路径1未命中，尝试补提交 about-you...")
            self._create_user_account(allow_existing_account=True)
            self._warmup_chatgpt_session()
            if self._capture_auth_session_tokens(result, access_hint=result.access_token):
                self._log("会话桥接自动登录在 about-you 后命中 session_token ✓")
                return True

            # 路径 3: 尝试走 workspace → select → follow_redirects → capture
            self._log("会话桥接自动登录: 路径2未命中，尝试 workspace 路径...")
            workspace_id = self._get_workspace_id()
            if not workspace_id:
                workspace_id = str(result.workspace_id or "").strip()
            if workspace_id:
                result.workspace_id = workspace_id
                continue_url = self._select_workspace(workspace_id)
                if continue_url:
                    callback_url, final_url = self._follow_redirects(continue_url)
                    self._log(f"会话桥接 workspace 路径: callback={'有' if callback_url else '无'}")
                    self._warmup_chatgpt_session()
                    if self._capture_auth_session_tokens(result, access_hint=result.access_token):
                        self._log("会话桥接自动登录 workspace 路径命中 session_token ✓")
                        return True

            # 路径 4: 重新走完整 chatgpt signin → authorize → follow_redirects
            self._log("会话桥接自动登录: 前3路径均未命中，重走 chatgpt signin...")
            self._bridge_login_active = True
            try:
                if self._bootstrap_chatgpt_signin_for_session(result):
                    self._log("会话桥接自动登录二次 bootstrap 命中 session_token ✓")
                    return True
            finally:
                self._bridge_login_active = False

            self._log("会话桥接自动登录所有路径均未命中 session_token", "warning")
            return False
        except Exception as e:
            self._log(f"会话桥接自动登录异常: {e}", "warning")
            return False

    def _retrigger_login_otp(self) -> bool:
        """在当前已登录会话中重新触发 OTP 发送。"""
        self._log("尝试在当前会话中原地重发登录验证码...")
        return self._send_verification_code(
            stage="relogin_otp",
            referer="https://auth.openai.com/email-verification",
            allow_failure=True,
        )

    def _complete_token_exchange_outlook(self, result: RegistrationResult) -> bool:
        """
        Outlook 入口链路专属收尾路径：
        走「登录 OTP -> Workspace -> OAuth callback」主干，
        含 3 级 OTP 降级重试，避免 Outlook 轮询卡死。
        """
        self._log("Outlook 专属链路: 等待登录验证码...")

        # ---- 第 1 级：直接等待 OTP（90s） ----
        attempted_codes: set = set()
        code = self._get_verification_code(
            stage="relogin_otp",
            timeout=90,
        )
        login_otp_ok = False
        if code:
            attempted_codes.add(code)
            login_otp_ok = self._validate_verification_code(code)

        # ---- 第 2 级：原地重发 OTP 后再等 ----
        if not login_otp_ok:
            self._log("登录验证码首轮未命中，尝试原地重发 OTP 后再校验...", "warning")
            resent = self._retrigger_login_otp()
            if resent:
                code = self._get_verification_code(
                    stage="relogin_otp",
                    timeout=90,
                )
                if code and code not in attempted_codes:
                    attempted_codes.add(code)
                    login_otp_ok = self._validate_verification_code(code)
                elif code and code in attempted_codes:
                    self._log(f"原地重发后取到重复验证码 {code}，跳过", "warning")

        # ---- 第 3 级：完整重登后再等 ----
        if not login_otp_ok:
            self._log("登录验证码仍未命中，尝试完整重登后再校验...", "warning")
            login_ready, login_error = self._restart_login_flow()
            if not login_ready:
                result.error_message = f"Outlook 重登失败: {login_error}"
                return False

            code = self._get_verification_code(
                stage="relogin_otp",
                timeout=120,
            )
            if code and code not in attempted_codes:
                attempted_codes.add(code)
                login_otp_ok = self._validate_verification_code(code)
            elif code and code in attempted_codes:
                self._log(f"完整重登后取到重复验证码 {code}，跳过", "warning")

        if not login_otp_ok:
            result.error_message = "Outlook 登录验证码 3 级重试全部失败"
            return False

        # ---- Outlook OTP 通过后：补 about-you + 多层降级 OAuth token 恢复 ----
        self._log("Outlook 专属链路: OTP 验证通过，补全 about-you 并启动 OAuth token recovery...")

        # 1. 补全 about-you 建号状态
        account_ok = self._create_user_account(allow_existing_account=True)
        if account_ok:
            self._account_created = True
            self._log("Outlook 链路: about-you 建号/确认完成 ✓")
        else:
            self._log("Outlook 链路: about-you 建号失败，仍尝试获取 token...", "warning")

        # 2. 多层降级 OAuth token 恢复
        fresh_callback_url = self._attempt_oauth_token_recovery()

        if fresh_callback_url:
            # 3. 用 callback 完成 token 交换
            token_info = self._handle_oauth_callback(fresh_callback_url)
            if token_info:
                result.account_id = token_info.get("account_id", "")
                result.access_token = token_info.get("access_token", "")
                result.refresh_token = token_info.get("refresh_token", "")
                result.id_token = token_info.get("id_token", "")
                result.password = self.password or ""
                result.source = "login" if self._is_existing_account else "register"
                self._log("Outlook 链路: OAuth token recovery 成功获取 access_token ✓")

                # 补全 workspace
                if not result.workspace_id:
                    result.workspace_id = self._get_workspace_id() or ""

                # session_token 从 cookie 中尝试获取
                session_cookie = self.session.cookies.get("__Secure-next-auth.session-token") if self.session else None
                if session_cookie:
                    self.session_token = session_cookie
                    result.session_token = session_cookie
            else:
                self._log("Outlook 链路: OAuth callback 处理失败", "warning")
        else:
            self._log("Outlook 链路: OAuth token recovery 未拿到 callback，回退到通用流程...", "warning")
            # 回退到通用流程（含 authorize replay 等老路径）
            self._complete_token_exchange(result, skip_otp_validation=True)

        # 4. 如果还没有 session_token，尝试 chatgpt.com 桥接补全
        if not result.session_token:
            self._log("Outlook 链路: 尝试 chatgpt 桥接辅助获取 session_token...", "warning")
            try:
                result.password = self.password or ""
                result.source = "login" if self._is_existing_account else "register"
                if self._bootstrap_chatgpt_signin_for_session(result):
                    self._log("Outlook 链路: chatgpt 会话桥接成功补充 session_token ✓")
                else:
                    self._warmup_chatgpt_session()
                    self._capture_auth_session_tokens(result)
            except Exception as e:
                self._log(f"辅助 chatgpt 桥接异常（忽略）: {e}", "warning")

        has_session = bool(result.session_token)
        has_access = bool(result.access_token)
        self._log(
            f"Outlook 链路 token 捕获结果: session_token={'有' if has_session else '无'}, "
            f"access_token={'有' if has_access else '无'}"
        )

        if not has_session and not has_access:
            result.error_message = "Outlook 链路: session_token 和 access_token 均未获取到"
            return False

        self._log("Outlook 专属链路完成: token 获取成功 ✓")
        return True

    def _attempt_oauth_token_recovery(self) -> Optional[str]:
        """Outlook OTP 通过后的多层降级 OAuth token 恢复。

        策略层级：
        Step A: strip prompt=login 重放原始 authorize URL（复用已满足的 login challenge）
        Step B: 全新 OAuth + prompt=none + login_hint（静默 SSO 尝试）

        返回 callback URL 或 None。
        """
        import urllib.parse

        # ── Step A: strip prompt=login 重放原始 authorize URL ──
        callback = self._step_a_replay_authorize_without_prompt()
        if callback:
            return callback

        # ── Step B: 全新 OAuth + prompt=none + login_hint ──
        callback = self._step_b_fresh_oauth_silent()
        if callback:
            return callback

        self._log("Outlook token recovery: Step A + Step B 均未获取 callback", "warning")
        return None

    # ────────────────────────────────────────────────────────────
    # Step A: 去掉 prompt=login，重放原始 authorize URL
    # ────────────────────────────────────────────────────────────
    def _step_a_replay_authorize_without_prompt(self) -> Optional[str]:
        """从原始 self.oauth_start.auth_url 中去掉 prompt=login 并手动跟踪重定向。"""
        import urllib.parse

        if not self.oauth_start:
            self._log("Step A: 没有可用的原始 authorize URL，跳过", "warning")
            return None

        try:
            original_url = self.oauth_start.auth_url
            parsed = urllib.parse.urlsplit(original_url)
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            params.pop("prompt", None)  # 去掉 prompt=login
            new_query = urllib.parse.urlencode(
                {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in params.items()},
                doseq=True,
            )
            replay_url = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, new_query, "")
            )
            self._log(
                f"Step A: strip prompt=login 重放: {self._sanitize_url_for_log(replay_url)}"
            )

            # 手动逐跳跟踪重定向（最多 12 跳）
            redirect_uri = self.oauth_start.redirect_uri
            current_url = replay_url
            max_hops = 12
            for hop in range(max_hops):
                resp = self.session.get(
                    current_url,
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": "https://auth.openai.com/",
                    },
                    allow_redirects=False,
                    timeout=20,
                )
                location = str(resp.headers.get("Location") or "").strip()
                self._log(
                    f"Step A hop {hop + 1}/{max_hops}: status={resp.status_code}, "
                    f"location={self._sanitize_url_for_log(location) or '-'}"
                )

                # 非重定向状态码 → 停止
                if resp.status_code not in (301, 302, 303, 307, 308):
                    final_url = str(resp.url or current_url)
                    resp_len = len(resp.text or "") if hasattr(resp, 'text') else 0
                    self._log(
                        f"Step A: 终止于 status={resp.status_code}, "
                        f"url={self._sanitize_url_for_log(final_url)}, body_len={resp_len}"
                    )
                    # 检查 consent 页面 → 记录但无法用 HTTP 处理（需 JS 渲染）
                    if self._is_consent_url(final_url):
                        self._log("Step A: 终止于 consent 页（JS 渲染，HTTP 无法处理）", "warning")
                    break

                if not location:
                    self._log("Step A: 重定向无 Location 头，终止", "warning")
                    break

                # 解析 Location（可能是相对路径）
                next_url = urllib.parse.urljoin(current_url, location)

                # ★ 核心检测：Location 含 code= 和 state= → 拿到 callback！
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"Step A: 在重定向中拿到 callback ✓")
                    return next_url

                # 如果 Location 指向 localhost（redirect_uri 就是 localhost），
                # 说明 callback 就在这里，但不应实际访问（本地没有服务）
                if next_url.startswith(redirect_uri):
                    self._log(f"Step A: 重定向目标是 redirect_uri (localhost)，直接提取 ✓")
                    return next_url

                # 如果被打回了 /log-in → login challenge 不匹配，Step A 失败
                if self._is_login_page_url(next_url):
                    self._log("Step A: 被重定向到 /log-in，login challenge 不匹配", "warning")
                    return None

                current_url = next_url

            self._log("Step A: 达到最大重定向次数或无 callback", "warning")
            return None

        except Exception as e:
            self._log(f"Step A 异常: {e}", "warning")
            return None

    # ────────────────────────────────────────────────────────────
    # Step B: 全新 OAuth + prompt=none + login_hint
    # ────────────────────────────────────────────────────────────
    def _step_b_fresh_oauth_silent(self) -> Optional[str]:
        """生成全新 OAuth URL（prompt=none + login_hint），尝试静默 SSO。"""
        import urllib.parse

        try:
            fresh_oauth = generate_oauth_url_no_prompt(login_hint=self.email)
            self.oauth_start = fresh_oauth  # 更新 oauth_start 用于后续 token 交换
            self._log(
                f"Step B: 发起静默 OAuth（prompt=none, login_hint={self.email}）: "
                f"{self._sanitize_url_for_log(fresh_oauth.auth_url)}"
            )

            redirect_uri = fresh_oauth.redirect_uri
            current_url = fresh_oauth.auth_url
            max_hops = 12
            for hop in range(max_hops):
                resp = self.session.get(
                    current_url,
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": "https://auth.openai.com/",
                    },
                    allow_redirects=False,
                    timeout=20,
                )
                location = str(resp.headers.get("Location") or "").strip()
                self._log(
                    f"Step B hop {hop + 1}/{max_hops}: status={resp.status_code}, "
                    f"location={self._sanitize_url_for_log(location) or '-'}"
                )

                if resp.status_code not in (301, 302, 303, 307, 308):
                    final_url = str(resp.url or current_url)
                    resp_len = len(resp.text or "") if hasattr(resp, 'text') else 0
                    self._log(
                        f"Step B: 终止于 status={resp.status_code}, "
                        f"url={self._sanitize_url_for_log(final_url)}, body_len={resp_len}"
                    )
                    if self._is_consent_url(final_url):
                        self._log("Step B: 终止于 consent 页（JS 渲染，HTTP 无法处理）", "warning")
                    break

                if not location:
                    break

                next_url = urllib.parse.urljoin(current_url, location)

                # callback 检测
                if "code=" in next_url and "state=" in next_url:
                    self._log("Step B: 在重定向中拿到 callback ✓")
                    return next_url
                if next_url.startswith(redirect_uri):
                    self._log("Step B: 重定向目标是 redirect_uri，直接提取 ✓")
                    return next_url

                # error=login_required → 静默授权不被支持
                if "error=login_required" in next_url or "error=consent_required" in next_url:
                    self._log(f"Step B: 静默授权返回错误: {self._sanitize_url_for_log(next_url)}", "warning")
                    return None

                # 被打回登录页
                if self._is_login_page_url(next_url):
                    self._log("Step B: 被重定向到 /log-in，静默授权失败", "warning")
                    return None

                current_url = next_url

            self._log("Step B: 达到最大重定向次数或无 callback", "warning")
            return None

        except Exception as e:
            self._log(f"Step B 异常: {e}", "warning")
            return None

    # ────────────────────────────────────────────────────────────
    # URL 分类辅助方法
    # ────────────────────────────────────────────────────────────
    @staticmethod
    def _is_login_page_url(url: str) -> bool:
        """判断 URL 是否指向 OpenAI 的登录页面。"""
        if not url:
            return False
        return (
            "/log-in" in url
            or "/login" in url
            or "/sign-in" in url
        ) and "auth.openai.com" in url

    @staticmethod
    def _is_consent_url(url: str) -> bool:
        """判断 URL 是否指向 consent 授权页面。"""
        if not url:
            return False
        return "consent" in url.lower() and "auth.openai.com" in url

    def _register_password(self, did: str, sen_token: Optional[str]) -> Tuple[bool, Optional[str]]:
        """注册密码"""
        try:
            self._last_registration_error = None
            # 生成密码
            password = self._generate_password()
            self.password = password  # 保存密码到实例变量
            self._log(f"生成密码: {password}")

            # 提交密码注册
            register_body = json.dumps({
                "password": password,
                "username": self.email
            })

            headers = {
                "referer": "https://auth.openai.com/create-account/password",
                "accept": "application/json",
                "content-type": "application/json",
            }

            if sen_token:
                sentinel = json.dumps({
                    "p": "",
                    "t": "",
                    "c": sen_token,
                    "id": did,
                    "flow": "authorize_continue",
                })
                headers["openai-sentinel-token"] = sentinel

            response = self.session.post(
                OPENAI_API_ENDPOINTS["register"],
                headers=headers,
                data=register_body,
            )

            self._log(f"提交密码状态: {response.status_code}")

            if response.status_code != 200:
                error_text = response.text[:500]
                self._log(f"密码注册失败: {error_text}", "warning")

                # 解析错误信息，判断是否是邮箱已注册
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", {}).get("message", "")
                    error_code = error_json.get("error", {}).get("code", "")
                    error_msg_lower = error_msg.lower()

                    if (
                        "already" in error_msg_lower
                        or "exists" in error_msg_lower
                        or error_code == "user_exists"
                        or "failed to register username" in error_msg_lower
                    ):
                        self._last_registration_error = "邮箱已被占用或疑似已注册，请更换新的 Outlook 邮箱"
                        self._mark_email_as_registered("username_rejected_or_existing_account")
                    elif error_msg:
                        self._last_registration_error = f"注册密码失败: {error_msg}"
                except Exception:
                    pass

                if not self._last_registration_error:
                    self._last_registration_error = "注册密码失败"
                return False, None

            return True, password

        except Exception as e:
            self._log(f"密码注册失败: {e}", "error")
            self._last_registration_error = "注册密码失败"
            return False, None

    def _mark_email_as_registered(
        self,
        register_failed_reason: str = "email_already_registered_on_openai",
    ):
        """标记邮箱为已注册状态（用于防止重复尝试）"""
        try:
            with get_db() as db:
                # 检查是否已存在该邮箱的记录
                existing = crud.get_account_by_email(db, self.email)
                if not existing:
                    # 创建一个失败记录，标记该邮箱已注册过
                    crud.create_account(
                        db,
                        email=self.email,
                        password="",  # 空密码表示未成功注册
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="failed",
                        extra_data={"register_failed_reason": register_failed_reason}
                    )
                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _set_verification_stage(self, stage: str) -> None:
        self._otp_stage = stage
        if (
            self.email
            and hasattr(self.email_service, "set_verification_stage")
        ):
            try:
                self.email_service.set_verification_stage(self.email, stage)
            except Exception as e:
                logger.warning(f"同步 OTP 阶段失败: {e}")

    def _get_verification_timeout(self, stage: Optional[str] = None) -> int:
        actual_stage = stage or self._otp_stage
        if (
            self.email_service.service_type == EmailServiceType.OUTLOOK
            and actual_stage == "relogin_otp"
        ):
            return 180
        return 120

    def _send_verification_code(
        self,
        *,
        stage: str = "signup_otp",
        referer: str = "https://auth.openai.com/create-account/password",
        allow_failure: bool = False,
    ) -> bool:
        """发送验证码"""
        try:
            self._set_verification_stage(stage)
            self._otp_sent_at = time.time()

            response = self.session.get(
                OPENAI_API_ENDPOINTS["send_otp"],
                headers={
                    "referer": referer,
                    "accept": "application/json",
                },
            )

            level = "warning" if allow_failure and response.status_code != 200 else "info"
            self._log(f"{stage} verification send status: {response.status_code}", level)
            return response.status_code == 200

        except Exception as e:
            self._log(
                f"{stage} verification send failed: {e}",
                "warning" if allow_failure else "error",
            )
            return False

    def _get_verification_code(
        self,
        *,
        stage: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """获取验证码"""
        try:
            actual_stage = stage or self._otp_stage
            actual_timeout = timeout or self._get_verification_timeout(actual_stage)
            self._set_verification_stage(actual_stage)
            self._log(f"Waiting for {actual_stage} verification code for {self.email}...")

            email_id = self.email_info.get("service_id") if self.email_info else None
            code = self.email_service.get_verification_code(
                email=self.email,
                email_id=email_id,
                timeout=actual_timeout,
                pattern=OTP_CODE_PATTERN,
                otp_sent_at=self._otp_sent_at,
            )

            if code:
                self._log(f"Got {actual_stage} verification code: {code}")
                self._log_email_service_verification_debug("OTP retrieval debug")
                return code
            self._log(f"Timed out waiting for {actual_stage} verification code", "error")
            self._log_email_service_verification_debug("OTP retrieval debug")
            return None

        except Exception as e:
            self._log(f"获取验证码失败: {e}", "error")
            self._log_email_service_verification_debug("OTP retrieval debug")
            return None

    def _log_email_service_verification_debug(self, label: str) -> None:
        if not self.email or not hasattr(self.email_service, "get_last_verification_debug"):
            return

        try:
            debug = self.email_service.get_last_verification_debug(self.email)
        except Exception as e:
            logger.warning(f"读取邮箱验证码调试信息失败: {e}")
            return

        if not isinstance(debug, dict) or not debug:
            return

        parts = [
            f"stage={debug.get('stage') or '-'}",
            f"polls={debug.get('poll_count') or 0}",
            f"status={debug.get('last_status') or '-'}",
            f"otp_sent_at={debug.get('otp_sent_at') or 0}",
            f"min_ts={debug.get('min_timestamp') or 0}",
            f"fresh_verifications={debug.get('fresh_verification_count') or 0}",
            f"fresh_preferred={debug.get('fresh_preferred_sender_count') or 0}",
            f"stale_preferred={debug.get('stale_preferred_sender_count') or 0}",
            f"available_fresh_verifications={debug.get('available_fresh_verification_count') or 0}",
            f"available_fresh_preferred={debug.get('available_fresh_preferred_sender_count') or 0}",
            f"used_fresh_preferred={debug.get('used_fresh_preferred_sender_count') or 0}",
            f"selected_sender={debug.get('selected_sender') or '-'}",
            f"selected_code={debug.get('selected_code') or '-'}",
            f"selected_ts={debug.get('selected_received_timestamp') or '-'}",
            f"deferred_generic_only_polls={debug.get('deferred_generic_only_polls') or 0}",
        ]

        candidates = debug.get("candidate_summaries") or []
        if candidates:
            formatted_candidates = []
            for item in candidates[:5]:
                formatted_candidates.append(
                    "sender={sender},ts={ts},delta={delta},code={code},preferred={preferred}".format(
                        sender=item.get("sender") or "-",
                        ts=item.get("received_timestamp") or "-",
                        delta=item.get("delta_from_otp_sent") if item.get("delta_from_otp_sent") is not None else "-",
                        code=item.get("code") or "-",
                        preferred=item.get("is_preferred_sender") is True,
                    )
                )
            parts.append("candidates=" + " | ".join(formatted_candidates))

        self._log(f"{label}: " + "; ".join(parts))

    def _log_response_debug(
        self,
        label: str,
        response: Any,
        payload: Optional[Dict[str, Any]] = None,
        *,
        level: str = "info",
    ) -> None:
        if response is None:
            return

        headers = getattr(response, "headers", {}) or {}
        request_id = ""
        if isinstance(headers, dict):
            request_id = (
                str(headers.get("x-request-id") or headers.get("request-id") or "").strip()
            )

        parts = [
            f"status={getattr(response, 'status_code', '-')}",
            f"url={self._sanitize_url_for_log(getattr(response, 'url', '') or '-')}",
        ]
        if request_id:
            parts.append(f"request_id={request_id}")

        if isinstance(payload, dict):
            page_type = str((payload.get("page") or {}).get("type") or "").strip()
            continue_url = str(payload.get("continue_url") or "").strip()
            callback_url = str(payload.get("callback_url") or "").strip()
            error_info = payload.get("error") or {}
            error_code = str(error_info.get("code") or "").strip()
            error_message = str(error_info.get("message") or "").strip()

            if page_type:
                parts.append(f"page_type={page_type}")
            if continue_url:
                parts.append(f"continue_url={self._sanitize_url_for_log(continue_url)}")
            if callback_url:
                parts.append(f"callback_url={self._sanitize_url_for_log(callback_url)}")
            if error_code:
                parts.append(f"error_code={error_code}")
            if error_message:
                parts.append(f"error_message={error_message[:240]}")

        text_excerpt = str(getattr(response, "text", "") or "").strip()
        if text_excerpt:
            parts.append(f"text={text_excerpt[:240]}")

        self._log(f"{label}: " + "; ".join(parts), level)

    def _log_session_cookie_debug(self, label: str) -> None:
        if not self.session:
            return

        parts = []

        auth_cookie = self.session.cookies.get("oai-client-auth-session")
        if auth_cookie:
            decoded_segments, failed_segments = self._decode_auth_cookie_json_segments(auth_cookie)
            auth_parts = [
                f"decoded={len(decoded_segments)}",
                f"failed={failed_segments}",
            ]
            if decoded_segments:
                primary = decoded_segments[0]
                for key in [
                    "email",
                    "email_verified",
                    "email_verification_mode",
                    "original_screen_hint",
                    "session_id",
                ]:
                    value = primary.get(key)
                    if value in (None, "", []):
                        continue
                    value_text = str(value)
                    if key == "session_id" and len(value_text) > 12:
                        value_text = value_text[:12] + "..."
                    auth_parts.append(f"{key}={value_text}")
            parts.append("auth_cookie(" + ", ".join(auth_parts) + ")")
        else:
            parts.append("auth_cookie=missing")

        next_auth_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if next_auth_cookie:
            claims = _jwt_claims_no_verify(next_auth_cookie)
            workspace_id = self._extract_workspace_id_from_data(claims)
            claim_keys = ",".join(sorted(claims.keys())[:6]) if isinstance(claims, dict) else "-"
            parts.append(f"next_auth(workspace={workspace_id or '-'}, keys={claim_keys or '-'})")
        else:
            parts.append("next_auth=missing")

        for name in ["login_session", "auth_provider"]:
            if self.session.cookies.get(name):
                parts.append(f"{name}=present")

        for name in ["auth-session-minimized", "unified_session_manifest"]:
            cookie_value = self.session.cookies.get(name)
            if not cookie_value:
                continue
            payloads = self._decode_cookie_json_payloads(cookie_value)
            workspace_id = None
            sample_keys = "-"
            for payload in payloads:
                workspace_id = self._extract_workspace_id_from_data(payload)
                if workspace_id:
                    break
            if payloads:
                sample_keys = ",".join(sorted(payloads[0].keys())[:6]) or "-"
            parts.append(
                f"{name}(decoded={len(payloads)}, workspace={workspace_id or '-'}, keys={sample_keys})"
            )

        self._log(f"{label}: " + "; ".join(parts))

    def _validate_verification_code(self, code: str) -> bool:
        """验证验证码"""
        try:
            code_body = f'{{"code":"{code}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers={
                    "referer": "https://auth.openai.com/email-verification",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=code_body,
            )

            self._log(f"验证码校验状态: {response.status_code}")
            try:
                response_data = response.json()
            except Exception:
                response_data = None

            self._log_response_debug(
                "validate_otp response",
                response,
                response_data,
                level="warning" if response.status_code != 200 else "info",
            )
            self._log_session_cookie_debug("validate_otp cookies")
            if response.status_code == 200:
                self._remember_workspace_payload("validate_otp", response_data)
                self._remember_navigation_from_response("validate_otp", response)
                self._log_navigation_snapshot("验证码校验后导航快照")
            return response.status_code == 200

        except Exception as e:
            self._log(f"验证验证码失败: {e}", "error")
            return False

    def _prime_about_you_page(self, about_you_url: str) -> bool:
        """预取 about-you 页面，尽量复用浏览器的会话建立顺序。"""
        try:
            candidate = str(about_you_url or "").strip()
            if not candidate:
                return True

            self._log(
                "预取 about-you 页面，补齐建号前会话状态: "
                f"{self._sanitize_url_for_log(candidate)}"
            )
            response = self.session.get(
                candidate,
                headers={
                    "referer": "https://auth.openai.com/email-verification",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=20,
            )
            self._log(f"about-you 页面状态: {response.status_code}")
            self._log_response_debug("about_you_page response", response, level="info")
            self._log_session_cookie_debug("about_you_page cookies")
            self._remember_navigation_from_response("about_you_page", response)
            self._log_navigation_snapshot("about-you 页面预取后导航快照")

            if response.status_code >= 400:
                self._log(
                    f"about-you 页面预取失败: HTTP {response.status_code}",
                    "warning",
                )
                return False

            return True

        except Exception as e:
            self._log(f"预取 about-you 页面失败: {e}", "error")
            return False

    def _create_user_account(
        self,
        allow_existing_account: bool = False,
        about_you_url: Optional[str] = None,
    ) -> bool:
        """创建用户账户"""
        try:
            self._last_create_account_error_code = None
            self._last_create_account_error_message = None
            self._last_create_account_user_exists = False
            if about_you_url and not self._prime_about_you_page(about_you_url):
                return False

            user_info = generate_random_user_info()
            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")
            create_account_body = json.dumps(user_info)

            response = self.session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                headers={
                    "referer": "https://auth.openai.com/about-you",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=create_account_body,
            )

            self._log(f"账户创建状态: {response.status_code}")
            try:
                response_data = response.json()
            except Exception:
                response_data = None
            self._log_response_debug(
                "create_account response",
                response,
                response_data,
                level="warning" if response.status_code != 200 else "info",
            )
            self._log_session_cookie_debug("create_account cookies")
            self._remember_workspace_payload("create_account", response_data)
            self._remember_navigation_from_response("create_account", response)
            self._log_navigation_snapshot("建号后导航快照")
            page_type = ""
            continue_url = ""
            callback_hint = ""
            if isinstance(response_data, dict):
                page_type = str((response_data.get("page") or {}).get("type") or "").strip()
                continue_url = str(response_data.get("continue_url") or "").strip()
                callback_hint = str(response_data.get("callback_url") or "").strip()
            response_keys = ",".join(sorted(response_data.keys())) if isinstance(response_data, dict) else "-"
            self._log(
                "create_account 快照: "
                f"status={response.status_code}, "
                f"page_type={page_type or '-'}, "
                f"continue_url={self._sanitize_url_for_log(continue_url) or '-'}, "
                f"callback_url={self._sanitize_url_for_log(callback_hint) or '-'}, "
                f"response_keys={response_keys}"
            )

            if response.status_code != 200:
                error_payload = response_data if isinstance(response_data, dict) else None
                error_info = error_payload.get("error", {}) if isinstance(error_payload, dict) else {}
                error_code = str(error_info.get("code") or "").strip().lower()
                error_message = str(error_info.get("message") or "").strip()
                self._last_create_account_error_code = error_code or None
                self._last_create_account_error_message = error_message or None

                if error_code == "registration_disallowed" and self.email and "@" in self.email:
                    domain = self.email.rsplit("@", 1)[-1]
                    if domain:
                        self._blacklist_email_domain(domain)

                error_message_lower = error_message.lower()
                if allow_existing_account and (
                    error_code == "user_already_exists"
                    or "already exists for this email address" in error_message_lower
                ):
                    self._last_create_account_user_exists = True
                    self._log("about-you 建号返回 user_already_exists，按已建号继续恢复登录")
                    return True

                error_text = response.text[:200]
                if not error_text and error_payload is not None:
                    error_text = json.dumps(error_payload, ensure_ascii=False)[:200]
                self._log(f"账户创建失败: {error_text}", "warning")
                return False

            return True

        except Exception as e:
            self._log(f"创建账户失败: {e}", "error")
            return False

    def _blacklist_email_domain(self, domain: str) -> None:
        """将被 OpenAI 拒绝的邮箱域名加入黑名单"""
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

                domain_lower = domain.lower().strip()
                if domain_lower not in blacklist:
                    blacklist.append(domain_lower)
                    crud.set_setting(
                        db,
                        key="email.domain_blacklist",
                        value=json.dumps(blacklist),
                        description="被 OpenAI 拒绝注册的邮箱域名黑名单",
                        category="email",
                    )
                    self._log(f"已将域名 {domain_lower} 加入黑名单 (共 {len(blacklist)} 个)")
                else:
                    self._log(f"域名 {domain_lower} 已在黑名单中，跳过")
        except Exception as e:
            logger.warning(f"写入邮箱域名黑名单失败: {e}")

    def _decode_auth_cookie_json_segments(self, auth_cookie: str) -> Tuple[list, int]:
        import base64
        from urllib.parse import unquote

        decoded_segments = []
        failed_segments = 0
        seen_payloads = set()
        candidates = [str(auth_cookie or "").strip()]
        decoded_cookie = unquote(candidates[0]) if candidates and candidates[0] else ""
        if decoded_cookie and decoded_cookie != candidates[0]:
            candidates.append(decoded_cookie)

        for candidate in candidates:
            if not candidate:
                continue

            normalized = candidate
            if (
                (normalized.startswith('"') and normalized.endswith('"'))
                or (normalized.startswith("'") and normalized.endswith("'"))
            ):
                normalized = normalized[1:-1]

            segments = [segment for segment in normalized.split(".") if segment]
            if normalized and "." in normalized:
                first_segment = normalized.split(".", 1)[0]
                if first_segment:
                    segments.insert(0, first_segment)

            for segment in segments:
                try:
                    pad = "=" * ((4 - (len(segment) % 4)) % 4)
                    decoded = base64.urlsafe_b64decode((segment + pad).encode("ascii"))
                    decoded_json = json.loads(decoded.decode("utf-8"))
                    if isinstance(decoded_json, dict):
                        payload_key = json.dumps(decoded_json, sort_keys=True, ensure_ascii=True)
                        if payload_key not in seen_payloads:
                            seen_payloads.add(payload_key)
                            decoded_segments.append(decoded_json)
                except Exception:
                    failed_segments += 1
        return decoded_segments, failed_segments

    def _decode_cookie_json_payloads(self, cookie_value: Any) -> List[Dict[str, Any]]:
        import base64
        from urllib.parse import unquote

        queue = [str(cookie_value or "").strip()]
        seen_candidates = set()
        seen_payloads = set()
        payloads: List[Dict[str, Any]] = []

        def remember_payload(payload: Any) -> None:
            if isinstance(payload, dict):
                payload_key = json.dumps(payload, sort_keys=True, ensure_ascii=True)
                if payload_key in seen_payloads:
                    return
                seen_payloads.add(payload_key)
                payloads.append(payload)
                return
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, (dict, list)):
                        remember_payload(item)
                    elif isinstance(item, str) and item.strip():
                        queue.append(item.strip())

        while queue:
            candidate = str(queue.pop(0) or "").strip()
            if not candidate or candidate in seen_candidates:
                continue
            seen_candidates.add(candidate)

            if (
                (candidate.startswith('"') and candidate.endswith('"'))
                or (candidate.startswith("'") and candidate.endswith("'"))
            ):
                queue.append(candidate[1:-1].strip())

            decoded_candidate = unquote(candidate)
            if decoded_candidate and decoded_candidate != candidate:
                queue.append(decoded_candidate)

            try:
                decoded_json = json.loads(candidate)
            except Exception:
                decoded_json = None
            if decoded_json is not None:
                if isinstance(decoded_json, str):
                    queue.append(decoded_json.strip())
                else:
                    remember_payload(decoded_json)

            base64_candidates = [candidate]
            if "." in candidate:
                first_segment = candidate.split(".", 1)[0].strip()
                if first_segment:
                    base64_candidates.append(first_segment)
                base64_candidates.extend(
                    segment.strip() for segment in candidate.split(".") if segment.strip()
                )

            for segment in base64_candidates:
                try:
                    pad = "=" * ((4 - (len(segment) % 4)) % 4)
                    decoded_text = base64.urlsafe_b64decode((segment + pad).encode("ascii")).decode("utf-8")
                except Exception:
                    continue
                decoded_text = decoded_text.strip()
                if decoded_text:
                    queue.append(decoded_text)

        return payloads

    def _collect_interesting_text_fragments_from_data(
        self,
        data: Any,
        fragments: List[str],
        seen: set,
    ) -> None:
        if isinstance(data, str):
            fragment = self._normalize_response_text(data)
            if not fragment:
                return
            fragment_lower = fragment.lower()
            interesting_tokens = (
                "http://",
                "https://",
                "localhost",
                "callback",
                "continue_url",
                "workspace",
                "workspaces",
                "organization",
                "orgs",
                "consent",
                "/api/",
            )
            if any(token in fragment_lower for token in interesting_tokens):
                if fragment not in seen:
                    seen.add(fragment)
                    fragments.append(fragment)
            return

        if isinstance(data, dict):
            for value in data.values():
                self._collect_interesting_text_fragments_from_data(value, fragments, seen)
            return

        if isinstance(data, list):
            for item in data:
                self._collect_interesting_text_fragments_from_data(item, fragments, seen)

    def _extract_app_router_push_payloads_from_text(self, text: Any) -> List[str]:
        normalized = self._normalize_response_text(text)
        if not normalized:
            return []

        import re

        payloads: List[str] = []
        seen = set()
        for match in re.finditer(r'push\(\s*', normalized):
            prefix = normalized[max(0, match.start() - 96):match.start()].lower()
            if "__next_f" not in prefix:
                continue
            payload_text = self._extract_json_block_from_text(normalized, match.end())
            if payload_text and payload_text not in seen:
                seen.add(payload_text)
                payloads.append(payload_text)

        return payloads

    def _extract_app_router_text_fragments_from_text(self, text: Any) -> List[str]:
        fragments: List[str] = []
        seen = set()
        for payload_text in self._extract_app_router_push_payloads_from_text(text):
            try:
                decoded = json.loads(payload_text)
            except Exception:
                continue
            self._collect_interesting_text_fragments_from_data(decoded, fragments, seen)

        return fragments

    def _extract_json_payload_candidates_from_fragment(self, fragment: str) -> List[str]:
        candidate = self._normalize_response_text(fragment)
        if not candidate:
            return []

        import re

        payloads: List[str] = []
        seen = set()
        hints = [
            "workspaces",
            "workspace",
            "callback_url",
            "continue_url",
            "redirect_url",
            "location",
            "orgs",
            "organization",
        ]

        for hint in hints:
            for match in re.finditer(re.escape(hint), candidate, re.IGNORECASE):
                lower_bound = max(0, match.start() - 4096)
                start_index = candidate.rfind("{", lower_bound, match.start() + 1)
                while start_index != -1:
                    payload_text = self._extract_json_block_from_text(candidate, start_index)
                    if payload_text and match.start() < start_index + len(payload_text):
                        if payload_text not in seen:
                            seen.add(payload_text)
                            payloads.append(payload_text)
                        break
                    start_index = candidate.rfind("{", lower_bound, start_index)

        return payloads

    def _reset_workspace_context(self) -> None:
        self._workspace_context = {
            "payloads": {},
            "continue_url": None,
            "callback_url": None,
            "resume_url": None,
            "resume_source": None,
            "redirect_locations": [],
            "token_info": None,
            "navigation_urls": [],
            "redirect_terminal_url": None,
            "redirect_terminal_status": None,
            "reentered_login": False,
            "last_login_challenge_url": None,
            "last_recovery_debug_summary": None,
            "resolved_workspace_id": None,
        }
        self._workspace_resolution_source = None
        self._workspace_resolution_error = None
        self._oauth_resume_source = None

    def _sanitize_url_for_log(self, url: Optional[str]) -> str:
        candidate = str(url or "").strip()
        if not candidate:
            return ""
        try:
            import urllib.parse

            parsed = urllib.parse.urlsplit(candidate)
            query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            sanitized_query = []
            for key, value in query_pairs:
                if key.lower() in {
                    "code",
                    "state",
                    "id_token",
                    "access_token",
                    "refresh_token",
                    "login_challenge",
                    "consent_challenge",
                }:
                    value = "<redacted>"
                elif len(value) > 24:
                    value = f"{value[:8]}...{value[-4:]}"
                sanitized_query.append((key, value))
            return urllib.parse.urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    urllib.parse.urlencode(sanitized_query),
                    parsed.fragment,
                )
            )
        except Exception:
            return candidate

    def _set_recovery_debug_summary(self, reason: str, **details: Any) -> None:
        parts = [reason]
        for key, value in details.items():
            text = str(value or "").strip()
            if not text:
                continue
            if "url" in key.lower():
                text = self._sanitize_url_for_log(text)
            parts.append(f"{key}={text}")
        self._workspace_context["last_recovery_debug_summary"] = "; ".join(parts)

    def _find_cached_resume_candidate(self, resume_source: str) -> Optional[str]:
        navigation_urls = self._workspace_context.get("navigation_urls", []) or []
        for item in reversed(navigation_urls):
            candidate = str(item.get("url") or "").strip()
            source = str(item.get("source") or "").strip()
            if candidate and self._classify_resume_source(source, candidate) == resume_source:
                return candidate
        return None

    def _is_about_you_url(self, url: Optional[str]) -> bool:
        candidate = str(url or "").strip().lower()
        if not candidate:
            return False
        return "/about-you" in candidate or candidate.endswith("about_you")

    def _extract_about_you_candidate_from_data(self, data: Any) -> Optional[str]:
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str):
                    value_text = value.strip()
                    if self._is_about_you_url(value_text):
                        return value_text
                    if str(key).strip().lower() == "type" and value_text.lower() in {"about_you", "about-you"}:
                        return "https://auth.openai.com/about-you"
                candidate = self._extract_about_you_candidate_from_data(value)
                if candidate:
                    return candidate
        elif isinstance(data, list):
            for item in data:
                candidate = self._extract_about_you_candidate_from_data(item)
                if candidate:
                    return candidate
        return None

    def _find_about_you_candidate(self) -> Optional[str]:
        resume_url = str(self._workspace_context.get("resume_url") or "").strip()
        if self._is_about_you_url(resume_url):
            return resume_url

        payloads = self._workspace_context.get("payloads", {}) or {}
        for payload in reversed(list(payloads.values())):
            candidate = self._extract_about_you_candidate_from_data(payload)
            if candidate:
                return candidate

        navigation_urls = self._workspace_context.get("navigation_urls", []) or []
        for item in reversed(navigation_urls):
            candidate = str(item.get("url") or "").strip()
            if self._is_about_you_url(candidate):
                return candidate

        return None

    def _resume_candidate_priority(self, source_name: str, candidate: str) -> int:
        candidate_lower = str(candidate or "").strip().lower()
        source_lower = str(source_name or "").strip().lower()
        if not candidate_lower:
            return -1

        priority = 0
        if self._is_login_page_url(candidate):
            priority = max(priority, 10)
        if "/email-verification" in candidate_lower:
            priority = max(priority, 20)
        if "login_challenge=" in candidate_lower:
            priority = max(priority, 30)
        if "consent_challenge=" in candidate_lower:
            priority = max(priority, 35)
        if "/add-phone" in candidate_lower:
            priority = max(priority, 36)
        if "/sign-in-with-chatgpt/" in candidate_lower or "/consent" in candidate_lower:
            priority = max(priority, 38)
        if "/workspace/select" in candidate_lower:
            priority = max(priority, 42)
        if "/organization/select" in candidate_lower:
            priority = max(priority, 45)
        if self._is_about_you_url(candidate):
            priority = max(priority, 40)
        if "/api/oauth/oauth2/auth" in candidate_lower:
            priority = max(priority, 50)

        if "validate_otp" in source_lower:
            priority += 3
        elif "login_password" in source_lower:
            priority += 2
        elif "login_start" in source_lower:
            priority += 1

        return priority

    def _remember_resume_candidate(self, source_name: str, candidate: Optional[str]) -> None:
        candidate_text = str(candidate or "").strip()
        if not candidate_text:
            return

        resume_source = self._classify_resume_source(source_name, candidate_text)
        if not resume_source:
            return

        current_resume = str(self._workspace_context.get("resume_url") or "").strip()
        if not current_resume:
            self._workspace_context["resume_url"] = candidate_text
            self._workspace_context["resume_source"] = resume_source
            return

        if current_resume == candidate_text:
            return

        current_priority = self._resume_candidate_priority(
            str(self._workspace_context.get("resume_source") or ""),
            current_resume,
        )
        candidate_priority = self._resume_candidate_priority(source_name, candidate_text)

        if candidate_priority > current_priority:
            self._log(
                f"Resume candidate upgraded: "
                f"current={self._sanitize_url_for_log(current_resume) or '-'}; "
                f"new={self._sanitize_url_for_log(candidate_text)}"
            )
            self._workspace_context["resume_url"] = candidate_text
            self._workspace_context["resume_source"] = resume_source
            return

        self._log(
            f"Resume candidate ignored: "
            f"current={self._sanitize_url_for_log(current_resume) or '-'}; "
            f"new={self._sanitize_url_for_log(candidate_text)}"
        )

    def _log_navigation_snapshot(self, label: str) -> None:
        self._log(
            f"{label}: callback={self._sanitize_url_for_log(self._workspace_context.get('callback_url')) or '-'}, "
            f"resume={self._sanitize_url_for_log(self._workspace_context.get('resume_url')) or '-'}, "
            f"terminal={self._sanitize_url_for_log(self._workspace_context.get('redirect_terminal_url')) or '-'}, "
            f"candidates={len(self._workspace_context.get('navigation_urls', []) or [])}"
        )

    def _log_navigation_candidates(self, label: str, limit: int = 8) -> None:
        items = self._workspace_context.get("navigation_urls", []) or []
        if not items:
            self._log(f"{label}: no navigation candidates recorded")
            return
        tail = items[-limit:]
        formatted = " | ".join(
            f"{item.get('source')}={self._sanitize_url_for_log(item.get('url'))}"
            for item in tail
        )
        self._log(f"{label}: {formatted}")

    def _remember_workspace_payload(self, name: str, payload: Optional[Dict[str, Any]]) -> None:
        if not isinstance(payload, dict):
            return

        self._workspace_context.setdefault("payloads", {})[name] = payload
        self._log(
            f"Payload[{name}] keys: {','.join(sorted(payload.keys())) or '-'}"
        )

        callback_url = self._extract_callback_url_from_data(payload)
        if callback_url and not self._workspace_context.get("callback_url"):
            self._workspace_context["callback_url"] = callback_url
            self._oauth_resume_source = self._oauth_resume_source or f"callback_found_from_{name}_payload"

        resume_url = self._extract_resume_url_from_data(payload)
        if resume_url:
            self._remember_resume_candidate(f"{name}_continue_url", resume_url)

    def _remember_navigation_candidate(
        self,
        source_name: str,
        url: Optional[str],
        base_url: Optional[str] = None,
    ) -> None:
        candidate = str(url or "").strip()
        if not candidate:
            return

        if base_url:
            import urllib.parse

            candidate = urllib.parse.urljoin(base_url, candidate)

        self._workspace_context.setdefault("navigation_urls", []).append(
            {"source": source_name, "url": candidate}
        )

        if "code=" in candidate and "state=" in candidate and not self._workspace_context.get("callback_url"):
            self._workspace_context["callback_url"] = candidate
            if (
                not self._oauth_resume_source
                or (
                    "about_you" in source_name.lower()
                    and self._oauth_resume_source in {
                        "continue_url_resume",
                        "resume_url_found_after_validate_otp",
                        "resume_url_found_from_navigation",
                    }
                )
            ):
                self._oauth_resume_source = f"callback_found_from_{source_name}"
            return

        resume_source = self._classify_resume_source(source_name, candidate)
        if resume_source:
            self._remember_resume_candidate(source_name, candidate)
        if resume_source == "login_challenge_resume":
            self._workspace_context["last_login_challenge_url"] = candidate

    def _extract_consent_script_asset_urls(self, base_url: str, text: Any) -> List[str]:
        normalized = self._normalize_response_text(text)
        if not normalized:
            return []

        import urllib.parse

        urls: List[str] = []
        seen = set()
        for match in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', normalized, re.IGNORECASE):
            src = str(match.group(1) or "").strip()
            if not src:
                continue
            candidate = urllib.parse.urljoin(base_url or "https://auth.openai.com", src)
            parsed = urllib.parse.urlsplit(candidate)
            if parsed.scheme not in {"http", "https"}:
                continue
            if parsed.netloc and parsed.netloc != "auth.openai.com":
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            urls.append(candidate)
        return urls

    def _inspect_consent_script_assets(self, source_name: str, final_url: str, response_text: Any) -> None:
        script_urls = self._extract_consent_script_asset_urls(final_url, response_text)
        if not script_urls:
            self._log(f"Navigation source={source_name}: consent_script_assets=0")
            return

        self._log(
            "Navigation source="
            f"{source_name}: consent_script_assets={len(script_urls)} "
            + " | ".join(self._sanitize_url_for_log(url) for url in script_urls[:6])
        )

        import os
        import urllib.parse

        hint_values: List[str] = []
        hint_seen = set()
        for script_index, script_url in enumerate(script_urls[:6], start=1):
            try:
                headers = {"accept": "*/*"}
                user_agent = self._get_session_user_agent()
                if user_agent:
                    headers["user-agent"] = user_agent
                if final_url:
                    headers["referer"] = final_url

                asset_response = self.session.get(
                    script_url,
                    headers=headers,
                    timeout=15,
                )
                asset_text = self._normalize_response_text(getattr(asset_response, "text", "") or "")
                if not asset_text:
                    continue

                asset_name = os.path.basename(urllib.parse.urlsplit(script_url).path) or f"asset-{script_index}"
                asset_lower = asset_text.lower()
                for token in ("clientaction", "workspace/select", "organization/select"):
                    if token not in asset_lower:
                        continue
                    hint = f"{asset_name}:{token}"
                    if hint in hint_seen:
                        continue
                    hint_seen.add(hint)
                    hint_values.append(hint)

                asset_candidates = self._extract_navigation_candidates_from_text(asset_text)
                for candidate_index, candidate in enumerate(asset_candidates, start=1):
                    self._remember_navigation_candidate(
                        f"{source_name}_script_{script_index}_candidate_{candidate_index}",
                        candidate,
                        base_url=script_url,
                    )

                asset_payloads = self._extract_embedded_payloads_from_text(asset_text)
                for payload_index, payload in enumerate(asset_payloads, start=1):
                    self._remember_workspace_payload(
                        f"{source_name}_script_{script_index}_payload_{payload_index}",
                        payload,
                    )
            except Exception as e:
                self._log(
                    "Navigation source="
                    f"{source_name}: consent_script_fetch_failed="
                    f"{self._sanitize_url_for_log(script_url)} error={e}",
                    "warning",
                )

        if hint_values:
            self._log(
                f"Navigation source={source_name}: consent_bundle_hints="
                + " | ".join(hint_values[:12])
            )

    def _remember_navigation_from_response(self, source_name: str, response: Any) -> None:
        if response is None:
            return

        self._log(
            f"Navigation source={source_name}: status={getattr(response, 'status_code', '-')}, "
            f"url={self._sanitize_url_for_log(getattr(response, 'url', '') or '-')}"
        )
        final_url = str(getattr(response, "url", "") or "").strip()
        self._remember_navigation_candidate(f"{source_name}_response_url", final_url)

        headers = getattr(response, "headers", {}) or {}
        location = headers.get("Location") if isinstance(headers, dict) else None
        self._remember_navigation_candidate(f"{source_name}_location", location, base_url=final_url)

        history = getattr(response, "history", []) or []
        for idx, history_item in enumerate(history, start=1):
            history_url = str(getattr(history_item, "url", "") or "").strip()
            self._remember_navigation_candidate(f"{source_name}_history_{idx}_url", history_url)
            history_headers = getattr(history_item, "headers", {}) or {}
            history_location = history_headers.get("Location") if isinstance(history_headers, dict) else None
            self._remember_navigation_candidate(
                f"{source_name}_history_{idx}_location",
                history_location,
                base_url=history_url,
            )

        response_text = getattr(response, "text", "") or ""
        text_candidates = self._extract_navigation_candidates_from_text(response_text)
        if text_candidates:
            self._log(
                f"Navigation source={source_name}: text_candidates="
                f"{' | '.join(self._sanitize_url_for_log(candidate) for candidate in text_candidates[:6])}"
            )
        for idx, candidate in enumerate(text_candidates, start=1):
            self._remember_navigation_candidate(f"{source_name}_text_{idx}", candidate, base_url=final_url)

        app_router_fragments = self._extract_app_router_text_fragments_from_text(response_text)
        if app_router_fragments:
            self._log(
                f"Navigation source={source_name}: app_router_fragments={len(app_router_fragments)}"
            )

        embedded_payloads = self._extract_embedded_payloads_from_text(response_text)
        if embedded_payloads:
            self._log(
                f"Navigation source={source_name}: embedded_payloads={len(embedded_payloads)}"
            )
        for idx, payload in enumerate(embedded_payloads, start=1):
            self._remember_workspace_payload(f"{source_name}_text_payload_{idx}", payload)

        if self._is_consent_url(final_url):
            normalized_text = self._normalize_response_text(response_text)
            self._log(
                f"Navigation source={source_name}: consent_diag text_len={len(normalized_text)}, "
                f"text_candidates={len(text_candidates)}, "
                f"embedded_payloads={len(embedded_payloads)}, "
                f"app_router_fragments={len(app_router_fragments)}"
            )
            self._inspect_consent_script_assets(source_name, final_url, response_text)
            if normalized_text and not text_candidates and not embedded_payloads and not app_router_fragments:
                excerpt = re.sub(r"\s+", " ", normalized_text)[:240]
                self._log(f"Navigation source={source_name}: consent_excerpt={excerpt}")

    def _normalize_response_text(self, text: Any) -> str:
        raw_text = str(text or "").strip()
        if not raw_text:
            return ""

        import html
        import re

        normalized = html.unescape(raw_text)
        normalized = re.sub(
            r"\\+u([0-9a-fA-F]{4})",
            lambda match: chr(int(match.group(1), 16)),
            normalized,
        )
        normalized = normalized.replace("\\/", "/")
        return normalized

    def _extract_json_block_from_text(self, text: str, start_index: int) -> Optional[str]:
        candidate = str(text or "")
        if start_index < 0 or start_index >= len(candidate):
            return None

        index = start_index
        while index < len(candidate) and candidate[index].isspace():
            index += 1
        if index >= len(candidate) or candidate[index] not in "{[":
            return None

        opening = candidate[index]
        closing = "}" if opening == "{" else "]"
        depth = 0
        in_string = False
        escaped = False

        for cursor in range(index, len(candidate)):
            char = candidate[cursor]
            if in_string:
                if escaped:
                    escaped = False
                    continue
                if char == "\\":
                    escaped = True
                    continue
                if char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue
            if char == opening:
                depth += 1
                continue
            if char == closing:
                depth -= 1
                if depth == 0:
                    return candidate[index:cursor + 1]

        return None

    def _parse_embedded_payload_candidate(self, payload_text: str) -> List[Dict[str, Any]]:
        candidate = str(payload_text or "").strip()
        if not candidate:
            return []

        try:
            decoded = json.loads(candidate)
        except Exception:
            return []

        if isinstance(decoded, dict):
            return [decoded]
        if isinstance(decoded, list):
            return [item for item in decoded if isinstance(item, dict)]
        return []

    def _extract_embedded_payloads_from_text(self, text: Any) -> List[Dict[str, Any]]:
        normalized = self._normalize_response_text(text)
        if not normalized:
            return []

        import re

        payloads: List[Dict[str, Any]] = []
        seen = set()

        def remember(payload_text: str) -> None:
            for payload in self._parse_embedded_payload_candidate(payload_text):
                payload_key = json.dumps(payload, sort_keys=True, ensure_ascii=True)
                if payload_key in seen:
                    continue
                seen.add(payload_key)
                payloads.append(payload)

        for match in re.finditer(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            normalized,
            re.IGNORECASE | re.DOTALL,
        ):
            remember(match.group(1))

        assignment_patterns = [
            r'window\.__STATE__\s*=\s*',
            r'window\.__NEXT_DATA__\s*=\s*',
            r'__NEXT_DATA__\s*=\s*',
            r'__STATE__\s*=\s*',
        ]
        for pattern in assignment_patterns:
            for match in re.finditer(pattern, normalized):
                payload_text = self._extract_json_block_from_text(normalized, match.end())
                if payload_text:
                    remember(payload_text)

        for payload_text in self._extract_app_router_push_payloads_from_text(normalized):
            remember(payload_text)

        for fragment in self._extract_app_router_text_fragments_from_text(normalized):
            remember(fragment)
            for payload_text in self._extract_json_payload_candidates_from_fragment(fragment):
                remember(payload_text)

        return payloads

    def _extract_navigation_candidates_from_text(self, text: Any) -> List[str]:
        normalized = self._normalize_response_text(text)
        if not normalized:
            return []

        import re
        patterns = [
            r'["\']((?:https?://|/)[^"\']+)["\']',
            r'((?:https?://|/)[^\s<>"\']+)',
        ]

        candidates: List[str] = []
        seen = set()

        def remember_from_text_blob(candidate_text: str) -> None:
            for pattern in patterns:
                for match in re.finditer(pattern, candidate_text):
                    candidate = next((group for group in match.groups() if group), "").strip()
                    if not candidate:
                        continue
                    candidate = candidate.rstrip(".,;)}]\\")
                    if candidate in seen:
                        continue
                    if "code=" in candidate and "state=" in candidate:
                        seen.add(candidate)
                        candidates.append(candidate)
                        continue
                    if self._classify_resume_source("response_text", candidate):
                        seen.add(candidate)
                        candidates.append(candidate)

        remember_from_text_blob(normalized)
        for fragment in self._extract_app_router_text_fragments_from_text(normalized):
            remember_from_text_blob(fragment)

        return candidates

    def _extract_workspace_id_from_data(self, data: Any) -> Optional[str]:
        if isinstance(data, dict):
            workspaces = data.get("workspaces")
            if isinstance(workspaces, list):
                for workspace in workspaces:
                    if isinstance(workspace, dict):
                        workspace_id = str(workspace.get("id") or "").strip()
                        if workspace_id:
                            return workspace_id
            workspace = data.get("workspace")
            if isinstance(workspace, dict):
                workspace_id = str(workspace.get("id") or "").strip()
                if workspace_id:
                    return workspace_id
            if isinstance(workspace, str) and workspace.strip():
                return workspace.strip()
            for value in data.values():
                workspace_id = self._extract_workspace_id_from_data(value)
                if workspace_id:
                    return workspace_id
        elif isinstance(data, list):
            for item in data:
                workspace_id = self._extract_workspace_id_from_data(item)
                if workspace_id:
                    return workspace_id
        return None

    def _extract_callback_url_from_data(self, data: Any) -> Optional[str]:
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and "code=" in value and "state=" in value:
                    if key in {"callback_url", "url", "redirect_url", "location", "continue_url"}:
                        return value.strip()
                callback_url = self._extract_callback_url_from_data(value)
                if callback_url:
                    return callback_url
        elif isinstance(data, list):
            for item in data:
                callback_url = self._extract_callback_url_from_data(item)
                if callback_url:
                    return callback_url
        return None

    def _extract_resume_url_from_data(self, data: Any) -> Optional[str]:
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and self._classify_resume_source(str(key), value):
                    return value.strip()
                resume_url = self._extract_resume_url_from_data(value)
                if resume_url:
                    return resume_url
        elif isinstance(data, list):
            for item in data:
                resume_url = self._extract_resume_url_from_data(item)
                if resume_url:
                    return resume_url
        return None

    def _classify_resume_source(self, source_name: str, candidate: str) -> Optional[str]:
        candidate_lower = str(candidate or "").strip().lower()
        source_lower = str(source_name or "").strip().lower()
        if not candidate_lower:
            return None
        if "login_challenge=" in candidate_lower:
            return "login_challenge_resume"
        if "consent_challenge=" in candidate_lower:
            return "consent_challenge_resume"
        if "/sign-in-with-chatgpt/" in candidate_lower or "/consent" in candidate_lower:
            return "consent_resume"
        if "/workspace/select" in candidate_lower:
            return "workspace_select_resume"
        if "/organization/select" in candidate_lower:
            return "organization_select_resume"
        if "continue_url" in source_lower:
            return "continue_url_resume"
        if "/api/oauth/oauth2/auth" in candidate_lower:
            if "validate_otp" in source_lower:
                return "resume_url_found_after_validate_otp"
            if "login_password" in source_lower:
                return "resume_url_reused_from_login_password"
            return "resume_url_found_from_navigation"
        return None

    def _is_login_page_url(self, url: Optional[str]) -> bool:
        candidate = str(url or "").strip().lower()
        if not candidate:
            return False
        return candidate.endswith("/log-in") or "/log-in?" in candidate or "auth.openai.com/log-in" in candidate

    def _is_consent_url(self, url: Optional[str]) -> bool:
        candidate = str(url or "").strip().lower()
        if not candidate:
            return False
        return "/sign-in-with-chatgpt/" in candidate or "/consent" in candidate

    def _mark_workspace_resolution(self, source: str, error: Optional[str] = None) -> None:
        self._workspace_resolution_source = source
        self._workspace_resolution_error = error

    def _get_workspace_id(self) -> Optional[str]:
        """获取 Workspace ID"""
        try:
            auth_cookie = self.session.cookies.get("oai-client-auth-session")
            if auth_cookie:
                decoded_segments, failed_segments = self._decode_auth_cookie_json_segments(auth_cookie)
                self._log(
                    f"Workspace 解析: auth_cookie decoded_segments={len(decoded_segments)}, "
                    f"failed_segments={failed_segments}"
                )
                for payload in decoded_segments:
                    workspace_id = self._extract_workspace_id_from_data(payload)
                    if workspace_id:
                        self._mark_workspace_resolution("auth_cookie")
                        self._log(f"Workspace ID: {workspace_id}")
                        return workspace_id

                if decoded_segments:
                    self._mark_workspace_resolution(
                        "auth_cookie_decoded_without_workspace",
                        "获取 Workspace ID 失败",
                    )
                    if failed_segments:
                        self._log("授权 Cookie 仅部分可解码，且没有 workspace 信息", "warning")
                    else:
                        self._log("授权 Cookie 里没有 workspace 信息", "warning")
                else:
                    self._mark_workspace_resolution("auth_cookie_unreadable", "获取 Workspace ID 失败")
                    self._log("授权 Cookie 无法解码成 JSON", "warning")
            else:
                self._mark_workspace_resolution("auth_cookie_missing", "获取 Workspace ID 失败")
                self._log("未能获取到授权 Cookie", "warning")

            next_auth_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
            if next_auth_cookie:
                self._log("Workspace 解析: 检查 next-auth session token")
                for segment in next_auth_cookie.split("."):
                    payload = _decode_jwt_segment(segment)
                    workspace_id = self._extract_workspace_id_from_data(payload)
                    if workspace_id:
                        self._mark_workspace_resolution("next_auth_session_token")
                        self._log(f"Workspace ID: {workspace_id}")
                        return workspace_id

            for cookie_name in ["auth-session-minimized", "unified_session_manifest"]:
                cookie_value = self.session.cookies.get(cookie_name)
                if not cookie_value:
                    continue

                payloads = self._decode_cookie_json_payloads(cookie_value)
                self._log(
                    f"Workspace 解析: 检查 {cookie_name}, decoded_payloads={len(payloads)}"
                )
                for payload in payloads:
                    workspace_id = self._extract_workspace_id_from_data(payload)
                    if workspace_id:
                        self._mark_workspace_resolution(cookie_name)
                        self._log(f"Workspace ID: {workspace_id}")
                        return workspace_id
                if payloads:
                    key_preview = " | ".join(
                        ",".join(sorted(payload.keys())[:6]) or "-"
                        for payload in payloads[:2]
                        if isinstance(payload, dict)
                    )
                    if key_preview:
                        self._log(
                            f"Workspace 解析: {cookie_name} payload keys={key_preview}"
                        )

            for source_name, payload in self._workspace_context.get("payloads", {}).items():
                workspace_id = self._extract_workspace_id_from_data(payload)
                if workspace_id:
                    self._mark_workspace_resolution(f"{source_name}_payload")
                    self._log(f"Workspace ID: {workspace_id}")
                    return workspace_id
            if self._workspace_context.get("payloads"):
                self._log(
                    "Workspace 解析: payload 来源未命中 "
                    + ",".join(sorted(self._workspace_context.get("payloads", {}).keys()))
                )

            resolved_workspace_id = str(self._workspace_context.get("resolved_workspace_id") or "").strip()
            if resolved_workspace_id:
                self._mark_workspace_resolution("resolved_workspace_cache")
                self._log(f"Workspace ID: {resolved_workspace_id}")
                return resolved_workspace_id

            token_info = self._workspace_context.get("token_info") or {}
            id_token = token_info.get("id_token") or ""
            if id_token:
                self._log("Workspace 解析: 检查 id_token claims")
                claims = _jwt_claims_no_verify(id_token)
                workspace_id = self._extract_workspace_id_from_data(claims)
                if workspace_id:
                    self._mark_workspace_resolution("id_token_claims")
                    self._log(f"Workspace ID: {workspace_id}")
                    return workspace_id

            if self._workspace_context.get("callback_url"):
                self._mark_workspace_resolution("callback_available_before_workspace", "获取 Workspace ID 失败")
                self._log("已经拿到 callback URL，但还没有 workspace 信息", "warning")
                self._set_recovery_debug_summary(
                    "callback_without_workspace",
                    callback_url=self._workspace_context.get("callback_url"),
                )
                return None

            if not self._workspace_resolution_source:
                self._mark_workspace_resolution("workspace_not_found", "获取 Workspace ID 失败")
            self._log("当前所有已知来源都没有 workspace 信息", "error")
            self._set_recovery_debug_summary(
                "workspace_not_found",
                resolution_source=self._workspace_resolution_source,
                terminal_url=self._workspace_context.get("redirect_terminal_url"),
            )
            return None

        except Exception as e:
            self._mark_workspace_resolution("workspace_lookup_exception", str(e))
            self._log(f"获取 Workspace ID 失败: {e}", "error")
            return None

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        """选择 Workspace"""
        try:
            import urllib.parse

            select_body = f'{{"workspace_id":"{workspace_id}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers=self._build_oauth_json_headers(
                    "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
                ),
                data=select_body,
            )

            try:
                response_data = response.json()
            except Exception:
                response_data = None

            self._remember_workspace_payload("select_workspace", response_data)
            self._remember_navigation_from_response("select_workspace", response)

            callback_url = str(self._workspace_context.get("callback_url") or "").strip()
            if callback_url:
                return callback_url

            if response.status_code in [301, 302, 303, 307, 308]:
                location = str((response.headers or {}).get("Location") or "").strip()
                if location:
                    return urllib.parse.urljoin(OPENAI_API_ENDPOINTS["select_workspace"], location)

            if response.status_code != 200:
                self._log(f"选择 workspace 失败: {response.status_code}", "error")
                self._log(f"响应: {response.text[:200]}", "warning")
                return None

            continue_url = str((response_data or {}).get("continue_url") or "").strip()
            if not continue_url:
                self._log("workspace/select 响应里缺少 continue_url", "error")
                return None

            self._workspace_context["continue_url"] = continue_url
            self._log(f"Continue URL: {continue_url[:100]}...")
            return continue_url

        except Exception as e:
            self._log(f"选择 Workspace 失败: {e}", "error")
            return None

    def _extract_org_selection_from_data(self, data: Any) -> Tuple[Optional[str], Optional[str]]:
        if isinstance(data, dict):
            orgs = data.get("orgs")
            if isinstance(orgs, list):
                for org in orgs:
                    if not isinstance(org, dict):
                        continue
                    org_id = str(org.get("id") or "").strip()
                    project_id = None
                    projects = org.get("projects")
                    if isinstance(projects, list):
                        for project in projects:
                            if not isinstance(project, dict):
                                continue
                            project_id = str(project.get("id") or "").strip() or None
                            if project_id:
                                break
                    if org_id:
                        return org_id, project_id
            for value in data.values():
                org_id, project_id = self._extract_org_selection_from_data(value)
                if org_id:
                    return org_id, project_id
        elif isinstance(data, list):
            for item in data:
                org_id, project_id = self._extract_org_selection_from_data(item)
                if org_id:
                    return org_id, project_id
        return None, None

    def _select_organization(
        self,
        org_id: str,
        project_id: Optional[str] = None,
        *,
        referer: Optional[str] = None,
    ) -> Optional[str]:
        try:
            import urllib.parse

            body: Dict[str, Any] = {"org_id": org_id}
            if project_id:
                body["project_id"] = project_id

            response = self.session.post(
                "https://auth.openai.com/api/accounts/organization/select",
                headers=self._build_oauth_json_headers(
                    referer or "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
                ),
                data=json.dumps(body),
            )

            try:
                response_data = response.json()
            except Exception:
                response_data = None

            self._remember_workspace_payload("select_organization", response_data)
            self._remember_navigation_from_response("select_organization", response)

            callback_url = str(self._workspace_context.get("callback_url") or "").strip()
            if callback_url:
                return callback_url

            if response.status_code in [301, 302, 303, 307, 308]:
                location = str((response.headers or {}).get("Location") or "").strip()
                if location:
                    return urllib.parse.urljoin(
                        "https://auth.openai.com/api/accounts/organization/select",
                        location,
                    )

            if response.status_code != 200:
                self._log(f"选择 organization 失败: {response.status_code}", "error")
                self._log(f"响应: {response.text[:200]}", "warning")
                return None

            continue_url = str((response_data or {}).get("continue_url") or "").strip()
            if continue_url:
                self._workspace_context["continue_url"] = continue_url
                self._log(f"Organization Continue URL: {continue_url[:100]}...")
                return continue_url

            self._log("organization/select 响应里缺少 continue_url", "error")
            return None

        except Exception as e:
            self._log(f"选择 Organization 失败: {e}", "error")
            return None

    def _probe_callback_via_allow_redirects(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
    ) -> Optional[str]:
        try:
            import re

            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            }
            session_headers = getattr(self.session, "headers", None) or {}
            user_agent = session_headers.get("User-Agent") or session_headers.get("user-agent")
            if user_agent:
                headers["User-Agent"] = user_agent
            if referer:
                headers["Referer"] = referer

            response = self.session.get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=15,
            )
            self._remember_navigation_from_response("allow_redirect_probe", response)
            callback_url = str(self._workspace_context.get("callback_url") or "").strip()
            if callback_url:
                self._log(
                    "allow_redirect probe 命中 callback: "
                    f"{self._sanitize_url_for_log(callback_url)}"
                )
                return callback_url
        except Exception as e:
            import re

            maybe_localhost = re.search(r'(https?://localhost[^\s\'"]+)', str(e))
            if maybe_localhost:
                callback_url = maybe_localhost.group(1)
                self._remember_navigation_candidate("allow_redirect_probe_exception", callback_url)
                self._log(
                    "allow_redirect probe 从异常中命中 callback: "
                    f"{self._sanitize_url_for_log(callback_url)}"
                )
                return callback_url
            self._log(f"allow_redirect probe 失败: {e}", "warning")

        return str(self._workspace_context.get("callback_url") or "").strip() or None

    def _continue_from_workspace_selection(self, workspace_id: str) -> Optional[str]:
        continue_url = self._select_workspace(workspace_id)
        callback_url = str(self._workspace_context.get("callback_url") or "").strip()
        if callback_url:
            return callback_url
        if not continue_url:
            return None

        select_payload = self._workspace_context.get("payloads", {}).get("select_workspace") or {}
        org_id, project_id = self._extract_org_selection_from_data(select_payload)
        page_type = ""
        if isinstance(select_payload, dict):
            page_type = str((select_payload.get("page") or {}).get("type") or "").strip().lower()
        payload_text = ""
        if select_payload:
            try:
                payload_text = json.dumps(select_payload, ensure_ascii=True).lower()
            except Exception:
                payload_text = str(select_payload).lower()

        requires_org_selection = bool(org_id) and (
            "organization" in str(continue_url).lower()
            or "organization" in page_type
            or "organization" in payload_text
        )
        if requires_org_selection:
            self._log("workspace/select 响应提示还需要 organization 选择，继续补齐")
            continue_url = self._select_organization(org_id, project_id, referer=continue_url)
            callback_url = str(self._workspace_context.get("callback_url") or "").strip()
            if callback_url:
                return callback_url
            if not continue_url:
                return None

        callback_url = self._follow_redirects(continue_url)
        if callback_url:
            return callback_url
        callback_url = self._probe_callback_via_allow_redirects(continue_url, referer=continue_url)
        if callback_url:
            return callback_url
        return str(self._workspace_context.get("callback_url") or "").strip() or None

    def _attempt_direct_consent_recovery(self, consent_url: Optional[str] = None) -> Optional[str]:
        import urllib.parse

        consent_url = str(consent_url or "https://auth.openai.com/sign-in-with-chatgpt/codex/consent").strip()
        if consent_url.startswith("/"):
            consent_url = urllib.parse.urljoin("https://auth.openai.com", consent_url)
        self._oauth_resume_source = "direct_consent_fallback"
        self._log(
            "当前会话命中 consent 链路，尝试 direct consent fallback: "
            f"{self._sanitize_url_for_log(consent_url)}",
            "warning",
        )

        callback_url = self._follow_redirects(consent_url)
        if callback_url:
            self._set_recovery_debug_summary(
                "direct_consent_callback_resolved",
                consent_url=consent_url,
                callback_url=callback_url,
            )
            return callback_url

        callback_url = str(self._workspace_context.get("callback_url") or "").strip()
        if callback_url:
            self._set_recovery_debug_summary(
                "direct_consent_cached_callback_resolved",
                consent_url=consent_url,
                callback_url=callback_url,
            )
            return callback_url

        callback_url = self._probe_callback_via_allow_redirects(consent_url, referer=consent_url)
        if callback_url:
            self._set_recovery_debug_summary(
                "direct_consent_allow_redirect_callback_resolved",
                consent_url=consent_url,
                callback_url=callback_url,
            )
            return callback_url

        workspace_id = self._get_workspace_id()
        if workspace_id:
            self._workspace_context["resolved_workspace_id"] = workspace_id
            callback_url = self._continue_from_workspace_selection(workspace_id)
            if callback_url:
                self._set_recovery_debug_summary(
                    "direct_consent_workspace_resolved",
                    consent_url=consent_url,
                    workspace_id=workspace_id,
                    callback_url=callback_url,
                )
                return callback_url

        self._set_recovery_debug_summary(
            "direct_consent_fallback_failed",
            consent_url=consent_url,
            resolution_source=self._workspace_resolution_source,
            terminal_url=self._workspace_context.get("redirect_terminal_url"),
        )
        self._log("direct consent fallback 未拿到 callback/workspace", "warning")
        return None

    def _continue_same_session_after_resume(self, label: str) -> Optional[str]:
        callback_url = str(self._workspace_context.get("callback_url") or "").strip()
        if callback_url:
            self._set_recovery_debug_summary(
                f"{label}_cached_callback_resolved",
                callback_url=callback_url,
            )
            return callback_url

        workspace_id = self._get_workspace_id()
        if workspace_id:
            self._workspace_context["resolved_workspace_id"] = workspace_id
            self._log(
                f"{label}: 当前会话已拿到 workspace={workspace_id}，继续走 workspace/select"
            )
            callback_url = self._continue_from_workspace_selection(workspace_id)
            if callback_url:
                self._set_recovery_debug_summary(
                    f"{label}_workspace_resolved",
                    workspace_id=workspace_id,
                    callback_url=callback_url,
                )
                return callback_url

        consent_url = self._find_cached_resume_candidate("consent_resume")
        if consent_url:
            self._log(
                f"{label}: 当前会话命中 consent 候选，继续同会话恢复: "
                f"{self._sanitize_url_for_log(consent_url)}",
                "warning",
            )
            callback_url = self._attempt_direct_consent_recovery(consent_url)
            if callback_url:
                return callback_url

        return None

    def _follow_redirects(self, start_url: str) -> Optional[str]:
        """跟随重定向链，寻找回调 URL"""
        try:
            import urllib.parse

            current_url = start_url
            referer_url: Optional[str] = None
            max_redirects = 6
            self._workspace_context["redirect_terminal_url"] = None
            self._workspace_context["redirect_terminal_status"] = None
            self._workspace_context["reentered_login"] = False

            for i in range(max_redirects):
                self._remember_navigation_candidate(f"redirect_{i+1}_request_url", current_url)
                self._log(
                    f"重定向 {i+1}/{max_redirects}: {self._sanitize_url_for_log(current_url)[:160]}"
                )

                headers = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Upgrade-Insecure-Requests": "1",
                }
                session_headers = getattr(self.session, "headers", None) or {}
                user_agent = session_headers.get("User-Agent") or session_headers.get("user-agent")
                if user_agent:
                    headers["User-Agent"] = user_agent
                if referer_url:
                    headers["Referer"] = referer_url

                response = self.session.get(
                    current_url,
                    headers=headers,
                    allow_redirects=False,
                    timeout=15
                )
                self._remember_navigation_from_response(f"redirect_{i+1}", response)

                final_url = str(getattr(response, "url", "") or current_url).strip() or current_url
                referer_url = final_url
                self._workspace_context["redirect_terminal_url"] = final_url
                self._workspace_context["redirect_terminal_status"] = response.status_code
                self._log(
                    f"重定向响应: status={response.status_code}, final={self._sanitize_url_for_log(final_url) or '-'}"
                )

                callback_url = str(self._workspace_context.get("callback_url") or "").strip()
                if callback_url:
                    self._set_recovery_debug_summary(
                        "callback_found_in_redirect_chain",
                        start_url=start_url,
                        callback_url=callback_url,
                    )
                    return callback_url

                location = response.headers.get("Location") or ""

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    login_challenge_url = self._find_cached_resume_candidate("login_challenge_resume")
                    started_from_login_challenge = (
                        self._classify_resume_source("start_url", start_url) == "login_challenge_resume"
                    )
                    if self._is_login_page_url(final_url) and (not login_challenge_url or started_from_login_challenge):
                        self._workspace_context["reentered_login"] = True
                        self._set_recovery_debug_summary(
                            "bare_login_reentry",
                            start_url=start_url,
                            terminal_url=final_url,
                        )
                    elif self._is_login_page_url(final_url):
                        self._set_recovery_debug_summary(
                            "login_reentry_with_cached_login_challenge",
                            start_url=start_url,
                            terminal_url=final_url,
                            login_challenge_url=login_challenge_url,
                        )
                    return None

                if not location:
                    login_challenge_url = self._find_cached_resume_candidate("login_challenge_resume")
                    started_from_login_challenge = (
                        self._classify_resume_source("start_url", start_url) == "login_challenge_resume"
                    )
                    if self._is_login_page_url(final_url) and (not login_challenge_url or started_from_login_challenge):
                        self._workspace_context["reentered_login"] = True
                        self._set_recovery_debug_summary(
                            "bare_login_reentry_without_location",
                            start_url=start_url,
                            terminal_url=final_url,
                        )
                    elif self._is_login_page_url(final_url):
                        self._set_recovery_debug_summary(
                            "login_reentry_with_cached_login_challenge",
                            start_url=start_url,
                            terminal_url=final_url,
                            login_challenge_url=login_challenge_url,
                        )
                    return None

                # 构建下一个 URL
                next_url = urllib.parse.urljoin(current_url, location)
                self._workspace_context.setdefault("redirect_locations", []).append(next_url)
                self._remember_navigation_candidate(f"redirect_{i+1}_next_url", next_url)
                self._log(f"重定向目标: {self._sanitize_url_for_log(next_url)}")

                callback_url = str(self._workspace_context.get("callback_url") or "").strip()
                if callback_url:
                    self._set_recovery_debug_summary(
                        "callback_found_from_redirect_location",
                        start_url=start_url,
                        callback_url=callback_url,
                    )
                    return callback_url

                current_url = next_url

            self._log("未能在重定向链中找到回调 URL", "error")
            self._set_recovery_debug_summary(
                "redirect_chain_exhausted",
                start_url=start_url,
                terminal_url=self._workspace_context.get("redirect_terminal_url"),
            )
            return None

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None

            self._log("处理 OAuth 回调，最后一哆嗦，稳住别抖...")
            token_info = self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier
            )

            self._workspace_context["token_info"] = token_info
            self._log("OAuth 授权成功，通关文牒到手")
            return token_info

        except Exception as e:
            self._log(f"处理 OAuth 回调失败: {e}", "error")
            return None

    def _is_recoverable_outlook_account(self, account) -> bool:
        if not account:
            return False
        if account.email_service != EmailServiceType.OUTLOOK.value:
            return False
        if account.status != "failed":
            return False
        extra_data = account.extra_data or {}
        return (
            extra_data.get("register_failed_reason") == "token_recovery_pending"
            and extra_data.get("recovery_ready") is True
            and bool(account.password)
        )

    def _load_recoverable_outlook_account(self) -> Optional[Dict[str, Any]]:
        if self.email_service.service_type != EmailServiceType.OUTLOOK or not self.email:
            return None

        try:
            with get_db() as db:
                account = crud.get_account_by_email(db, self.email)
                if not self._is_recoverable_outlook_account(account):
                    return None
                extra_data = dict(account.extra_data or {})
                return {
                    "id": account.id,
                    "email": account.email,
                    "password": account.password,
                    "account_created": bool(extra_data.get("account_created")),
                }
        except Exception as e:
            self._log(f"检查 Outlook 可恢复账号失败: {e}", "warning")
            return None

    def _persist_recoverable_outlook_account(self, error_message: str) -> bool:
        if self.email_service.service_type != EmailServiceType.OUTLOOK:
            return False
        if not self.email or not self.password:
            return False
        if not self._account_created and not self._recovery_mode:
            return False

        try:
            with get_db() as db:
                existing = crud.get_account_by_email(db, self.email)
                base_extra = dict(existing.extra_data or {}) if existing else {}
                workspace_resolution_source = (
                    self._workspace_resolution_source
                    or base_extra.get("last_workspace_resolution_source")
                )
                workspace_resolution_error = (
                    self._workspace_resolution_error
                    or base_extra.get("last_workspace_resolution_error")
                )
                base_extra.update(
                    {
                        "recovery_ready": True,
                        "account_created": True,
                        "token_acquired": False,
                        "workspace_acquired": False,
                        "register_failed_reason": "token_recovery_pending",
                        "last_recovery_error": error_message,
                        "last_otp_stage": self._otp_stage,
                        "last_oauth_resume_source": self._oauth_resume_source
                        or base_extra.get("last_oauth_resume_source"),
                        "last_workspace_resolution_source": workspace_resolution_source,
                        "last_workspace_resolution_error": workspace_resolution_error,
                        "last_recovery_debug_summary": self._workspace_context.get("last_recovery_debug_summary")
                        or base_extra.get("last_recovery_debug_summary"),
                    }
                )

                if existing:
                    crud.update_account(
                        db,
                        existing.id,
                        password=self.password,
                        client_id=get_settings().openai_client_id,
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id")
                        if self.email_info
                        else existing.email_service_id,
                        proxy_used=self.proxy_url,
                        status="failed",
                        extra_data=base_extra,
                        source=existing.source or "register",
                    )
                    self._recovery_account_id = existing.id
                    return True

                account = crud.create_account(
                    db,
                    email=self.email,
                    password=self.password,
                    client_id=get_settings().openai_client_id,
                    email_service=self.email_service.service_type.value,
                    email_service_id=self.email_info.get("service_id") if self.email_info else None,
                    proxy_used=self.proxy_url,
                    extra_data=base_extra,
                    status="failed",
                    source="register",
                )
                self._recovery_account_id = account.id
                return True
        except Exception as e:
            self._log(f"保存可恢复 Outlook 账号失败: {e}", "warning")
            return False

    def _fail_with_recovery(self, result: RegistrationResult, error_message: str) -> RegistrationResult:
        result.error_message = error_message
        self._persist_recoverable_outlook_account(error_message)
        return result

    def _prepare_relogin_otp_flow(self, label: str) -> None:
        self._token_acquisition_requires_login = True
        self._set_verification_stage("relogin_otp")
        if self.email_service.service_type == EmailServiceType.OUTLOOK:
            resend_ok = self._send_verification_code(
                stage="relogin_otp",
                referer="https://auth.openai.com/log-in/password",
                allow_failure=True,
            )
            if not resend_ok:
                self._log(f"{label}: relogin_otp resend failed, falling back to polling", "warning")

    def _start_saved_password_recovery(self) -> Tuple[bool, str]:
        self._recovery_mode = True
        self._is_existing_account = True
        self._reset_auth_flow()

        did, sen_token = self._prepare_authorize_flow("恢复登录")
        if not did:
            return False, "恢复登录时获取 Device ID 失败"
        if not sen_token:
            return False, "恢复登录时 Sentinel POW 验证失败"

        login_start_result = self._submit_login_start(did, sen_token, screen_hint="login")
        if not login_start_result.success:
            return False, f"恢复登录提交邮箱失败: {login_start_result.error_message}"
        if login_start_result.page_type != OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            return False, f"恢复登录未进入密码页面: {login_start_result.page_type or 'unknown'}"

        password_result = self._submit_login_password()
        if not password_result.success:
            return False, f"恢复登录提交密码失败: {password_result.error_message}"
        if not password_result.is_existing_account:
            return False, f"恢复登录未进入验证码页面: {password_result.page_type or 'unknown'}"

        self._prepare_relogin_otp_flow("恢复登录")
        return True, ""

    def _attempt_session_bound_reauth(self) -> Optional[str]:
        if self._session_bound_reauth_attempted:
            self._oauth_resume_source = "session_bound_reauth_exhausted"
            self._set_recovery_debug_summary(
                "session_bound_reauth_exhausted",
                terminal_url=self._workspace_context.get("redirect_terminal_url"),
            )
            self._log("会话内重登已尝试过一次，停止继续恢复", "warning")
            return None

        self._session_bound_reauth_attempted = True
        self._oauth_resume_source = "session_bound_reauth"
        self._reset_workspace_context()
        self._log("恢复链路回到裸登录页，尝试在当前会话内重做登录", "warning")

        if not self.session and not self._init_session():
            self._set_recovery_debug_summary("session_bound_reauth_session_init_failed")
            return None

        did = self.session.cookies.get("oai-did") if self.session else None
        reused_did = bool(did)
        if reused_did:
            self._log(f"会话内重登: 复用当前 Device ID {did}")
        else:
            if not self.oauth_start:
                self._oauth_resume_source = "session_bound_reauth_missing_oauth_start"
                self._set_recovery_debug_summary("session_bound_reauth_missing_oauth_start")
                self._log("会话内重登缺少 OAuth 上下文，无法补取 Device ID", "warning")
                return None
            self._log("会话内重登: 当前会话没有 Device ID，补取一次")
            did = self._get_device_id()
            if not did:
                self._oauth_resume_source = "session_bound_reauth_device_id_failed"
                self._set_recovery_debug_summary("session_bound_reauth_device_id_failed")
                return None

        sen_token = self._check_sentinel(did)
        if not sen_token:
            self._oauth_resume_source = "session_bound_reauth_sentinel_failed"
            self._set_recovery_debug_summary(
                "session_bound_reauth_sentinel_failed",
                reused_did=reused_did,
            )
            return None

        login_start_result = self._submit_login_start(did, sen_token)
        if not login_start_result.success:
            self._oauth_resume_source = "session_bound_reauth_login_start_failed"
            self._set_recovery_debug_summary(
                "session_bound_reauth_login_start_failed",
                reused_did=reused_did,
                error=login_start_result.error_message,
            )
            return None

        if login_start_result.page_type == OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
            password_result = self._submit_login_password()
            if not password_result.success:
                self._oauth_resume_source = "session_bound_reauth_password_failed"
                self._set_recovery_debug_summary(
                    "session_bound_reauth_password_failed",
                    reused_did=reused_did,
                    error=password_result.error_message,
                )
                return None
            if not password_result.is_existing_account:
                self._oauth_resume_source = "session_bound_reauth_failed_to_reach_otp"
                self._set_recovery_debug_summary(
                    "session_bound_reauth_failed_to_reach_otp",
                    page_type=password_result.page_type,
                )
                return None
        elif login_start_result.page_type != OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
            self._oauth_resume_source = "session_bound_reauth_failed_to_reach_otp"
            self._set_recovery_debug_summary(
                "session_bound_reauth_failed_to_reach_otp",
                page_type=login_start_result.page_type,
            )
            return None

        self._session_bound_reauth_otp_cycles += 1
        if self._session_bound_reauth_otp_cycles > 1:
            self._oauth_resume_source = "session_bound_reauth_exhausted"
            self._set_recovery_debug_summary("session_bound_reauth_exhausted", reason="otp_cycle_limit")
            self._log("会话内重登 OTP 已达到上限，停止继续恢复", "warning")
            return None

        self._prepare_relogin_otp_flow("会话内重登")
        code = self._get_verification_code(
            stage="relogin_otp",
            timeout=self._get_verification_timeout("relogin_otp"),
        )
        if not code:
            self._oauth_resume_source = "session_bound_reauth_otp_timeout"
            self._set_recovery_debug_summary("session_bound_reauth_otp_timeout")
            return None

        if not self._validate_verification_code(code):
            self._oauth_resume_source = "session_bound_reauth_validate_failed"
            self._set_recovery_debug_summary("session_bound_reauth_validate_failed")
            return None

        self._oauth_resume_source = "session_bound_reauth"
        callback_url, _workspace_id, resolution_error = self._resolve_oauth_callback_url()
        if callback_url:
            self._oauth_resume_source = "session_bound_reauth"
            self._set_recovery_debug_summary(
                "session_bound_reauth_callback_resolved",
                callback_url=callback_url,
                reused_did=reused_did,
            )
            return callback_url

        if self._workspace_context.get("reentered_login"):
            self._oauth_resume_source = "session_bound_reauth_reentered_login"
            self._set_recovery_debug_summary(
                "session_bound_reauth_reentered_login",
                terminal_url=self._workspace_context.get("redirect_terminal_url"),
                reused_did=reused_did,
            )
            return None

        self._oauth_resume_source = "session_bound_reauth_failed"
        self._set_recovery_debug_summary(
            "session_bound_reauth_failed",
            error=resolution_error,
            reused_did=reused_did,
        )
        return None

    def _attempt_authorize_replay(
        self,
        *,
        allow_session_bound_reauth: bool = True,
        reentry_log_level: str = "error",
    ) -> Optional[str]:
        if not self.oauth_start:
            self._oauth_resume_source = "authorize_replay_failed"
            self._log("OAuth 续跑失败：当前没有可重放的 authorize URL", "error")
            return None

        callback_url = self._follow_redirects(self.oauth_start.auth_url)
        if callback_url:
            if self._oauth_resume_source != "session_bound_reauth":
                self._oauth_resume_source = "callback_found_from_authorize_replay"
            return callback_url

        login_challenge_url = self._find_cached_resume_candidate("login_challenge_resume")
        if login_challenge_url:
            if self._oauth_resume_source != "session_bound_reauth":
                self._oauth_resume_source = "login_challenge_resume"
            self._log(
                "authorize replay 后拿到 login_challenge，尝试二次恢复: "
                f"{self._sanitize_url_for_log(login_challenge_url)}"
            )
            callback_url = self._follow_redirects(login_challenge_url)
            if callback_url:
                return callback_url

        if self._workspace_context.get("reentered_login"):
            if allow_session_bound_reauth:
                callback_url = self._attempt_session_bound_reauth()
                if callback_url:
                    return callback_url
            if self._oauth_resume_source != "session_bound_reauth_reentered_login":
                self._oauth_resume_source = "fresh_authorize_replay_reentered_login"
            self._log("OAuth 恢复链路重新进入登录页，无法继续当前流程", reentry_log_level)
            return None

        self._oauth_resume_source = "authorize_replay_failed"
        self._log("OAuth 续跑失败：重放 authorize 链路后仍未拿到 callback", "error")
        return None

    def _resume_oauth_callback(self) -> Optional[str]:
        callback_url = str(self._workspace_context.get("callback_url") or "").strip()
        if callback_url:
            self._oauth_resume_source = self._oauth_resume_source or "callback_found_from_cached_navigation"
            self._set_recovery_debug_summary("cached_callback_available", callback_url=callback_url)
            return callback_url

        resume_url = str(self._workspace_context.get("resume_url") or "").strip()
        if resume_url:
            resume_source = str(self._workspace_context.get("resume_source") or "").strip()
            if not resume_source:
                resume_source = (
                    self._classify_resume_source("cached_resume_url", resume_url)
                    or "resume_url_found_after_validate_otp"
                )
            self._oauth_resume_source = resume_source
            self._log(
                f"尝试沿缓存的 OAuth 恢复链路继续: {resume_source} -> "
                f"{self._sanitize_url_for_log(resume_url)}"
            )
            callback_url = self._follow_redirects(resume_url)
            if callback_url:
                return callback_url
            callback_url = self._continue_same_session_after_resume("resume_url_follow_up")
            if callback_url:
                return callback_url
            upgraded_resume_url = str(self._workspace_context.get("resume_url") or "").strip()
            if (
                upgraded_resume_url
                and upgraded_resume_url != resume_url
                and not self._is_about_you_url(upgraded_resume_url)
            ):
                upgraded_resume_source = (
                    str(self._workspace_context.get("resume_source") or "").strip()
                    or self._classify_resume_source("upgraded_resume_url", upgraded_resume_url)
                    or "resume_url_found_from_navigation"
                )
                if self._oauth_resume_source != "session_bound_reauth":
                    self._oauth_resume_source = upgraded_resume_source
                self._log(
                    "恢复链路在页面提取后升级续跑 URL，继续尝试: "
                    f"{upgraded_resume_source} -> {self._sanitize_url_for_log(upgraded_resume_url)}"
                )
                callback_url = self._follow_redirects(upgraded_resume_url)
                if callback_url:
                    return callback_url
                callback_url = self._continue_same_session_after_resume("upgraded_resume_follow_up")
                if callback_url:
                    return callback_url
            login_challenge_url = self._find_cached_resume_candidate("login_challenge_resume")
            if login_challenge_url and login_challenge_url != resume_url:
                if self._oauth_resume_source != "session_bound_reauth":
                    self._oauth_resume_source = "login_challenge_resume"
                self._log(
                    "恢复链路命中过 login_challenge，中转页再次续跑: "
                    f"{self._sanitize_url_for_log(login_challenge_url)}"
                )
                callback_url = self._follow_redirects(login_challenge_url)
                if callback_url:
                    return callback_url
                callback_url = self._continue_same_session_after_resume("login_challenge_follow_up")
                if callback_url:
                    return callback_url
            if self._workspace_context.get("reentered_login"):
                callback_url = self._attempt_session_bound_reauth()
                if callback_url:
                    return callback_url
                self._log("OAuth 恢复链路重新进入登录页，停止继续恢复", "warning")
                return None

        return self._attempt_authorize_replay()

    def _activate_recovered_account(self, result: RegistrationResult) -> bool:
        if not self._recovery_mode:
            return False

        try:
            with get_db() as db:
                account = crud.get_account_by_email(db, result.email)
                if not account:
                    return False

                merged_extra = dict(account.extra_data or {})
                merged_extra.update(
                    {
                        "email_service": self.email_service.service_type.value,
                        "proxy_used": self.proxy_url,
                        "registered_at": datetime.now().isoformat(),
                        "is_existing_account": True,
                        "token_acquired_via_relogin": self._token_acquisition_requires_login,
                        "recovery_ready": False,
                        "account_created": True,
                        "token_acquired": True,
                        "workspace_acquired": bool(result.workspace_id),
                        "last_oauth_resume_source": self._oauth_resume_source
                        or merged_extra.get("last_oauth_resume_source"),
                        "last_workspace_resolution_source": self._workspace_resolution_source
                        or merged_extra.get("last_workspace_resolution_source"),
                        "last_workspace_resolution_error": self._workspace_resolution_error
                        or merged_extra.get("last_workspace_resolution_error"),
                        "last_recovery_debug_summary": self._workspace_context.get("last_recovery_debug_summary")
                        or merged_extra.get("last_recovery_debug_summary"),
                    }
                )

                account.password = result.password
                account.client_id = get_settings().openai_client_id
                account.session_token = result.session_token
                account.email_service = self.email_service.service_type.value
                account.email_service_id = (
                    self.email_info.get("service_id") if self.email_info else account.email_service_id
                )
                account.account_id = result.account_id
                account.workspace_id = result.workspace_id
                account.access_token = result.access_token
                account.refresh_token = result.refresh_token
                account.id_token = result.id_token
                account.proxy_used = self.proxy_url
                account.status = "active"
                account.extra_data = merged_extra
                account.source = account.source or result.source
                db.commit()
                db.refresh(account)
                result.metadata = merged_extra
                result.metadata["database_saved"] = True
                return True
        except Exception as e:
            self._log(f"更新恢复账号失败: {e}", "warning")
            return False

    def run(self) -> RegistrationResult:
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            self._is_existing_account = False
            self._token_acquisition_requires_login = False
            self._otp_stage = "signup_otp"
            self._otp_sent_at = None
            self._last_registration_error = None
            self._account_created = False
            self._about_you_resume_attempts = 0
            self._about_you_user_exists_without_resume_attempts = 0
            self._recovery_mode = False
            self._recovery_account_id = None
            self._session_bound_reauth_attempted = False
            self._session_bound_reauth_otp_cycles = 0
            self._reset_workspace_context()

            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                return result

            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            result.email = self.email

            recoverable_account = self._load_recoverable_outlook_account()
            if recoverable_account:
                self._recovery_account_id = recoverable_account["id"]
                self.password = recoverable_account["password"]
                self._account_created = bool(recoverable_account.get("account_created"))
                login_ready, login_error = self._start_saved_password_recovery()
                if not login_ready:
                    return self._fail_with_recovery(result, login_error)
            else:
                did, sen_token = self._prepare_authorize_flow("首次授权")
                if not did:
                    result.error_message = "获取 Device ID 失败"
                    return result
                if not sen_token:
                    result.error_message = "Sentinel POW 验证失败"
                    return result

                signup_result = self._submit_signup_form(did, sen_token)
                if not signup_result.success:
                    result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                    return result

                if self._is_existing_account:
                    self._log("检测到已注册账号，直接走登录拿 token")
                else:
                    password_ok, _ = self._register_password(did, sen_token)
                    if not password_ok:
                        result.error_message = self._last_registration_error or "注册密码失败"
                        return result

                    if not self._send_verification_code():
                        result.error_message = "发送验证码失败"
                        return result

                    code = self._get_verification_code()
                    if not code:
                        result.error_message = "获取验证码失败"
                        return result

                    if not self._validate_verification_code(code):
                        result.error_message = "验证码校验失败"
                        return result

                    if not self._create_user_account():
                        result.error_message = "创建用户账户失败"
                        return result

                    self._account_created = True
                    self._log("新账号建号完成，先保留当前会话，交给统一 token 阶段决定是否需要重新登录")

            # 根据邮箱类型选择 token 获取路径
            service_type_raw = getattr(self.email_service, "service_type", "")
            service_type_value = str(
                getattr(service_type_raw, "value", service_type_raw) or ""
            ).strip().lower()

            if service_type_value == "outlook":
                # Outlook 专属链路：全量走 3 级 OTP 重试 + chatgpt 桥接补全
                if self._is_existing_account:
                    self._log("检测到已注册 Outlook 邮箱，使用专属 token 获取路径（已注册账号重登 + 3 级 OTP 重试）")
                else:
                    self._log("检测到 Outlook 邮箱，使用专属 token 获取路径（注册后重登 + 3 级 OTP 重试）")
                # 恢复模式下（已走过 _start_saved_password_recovery）可能已处于 OTP 阶段，
                # 不需要重新 _restart_login_flow；非恢复模式则需要重登
                if not self._recovery_mode:
                    login_ready, login_error = self._restart_login_flow()
                    if not login_ready:
                        return self._fail_with_recovery(result, login_error)
                if not self._complete_token_exchange_outlook(result):
                    return self._fail_with_recovery(result, result.error_message)
            else:
                # 临时邮箱及其他：走原有通用路径
                if not self._complete_token_exchange(result):
                    return self._fail_with_recovery(result, result.error_message)

            result.success = True
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now().isoformat(),
                "is_existing_account": self._is_existing_account,
                "token_acquired_via_relogin": self._token_acquisition_requires_login,
                "recovery_ready": False,
                "account_created": True,
                "token_acquired": True,
                "workspace_acquired": bool(result.workspace_id),
                "last_oauth_resume_source": self._oauth_resume_source,
                "last_workspace_resolution_source": self._workspace_resolution_source,
                "last_workspace_resolution_error": self._workspace_resolution_error,
                "last_recovery_debug_summary": self._workspace_context.get("last_recovery_debug_summary"),
            }

            if self._recovery_mode:
                self._activate_recovered_account(result)

            return result

        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return self._fail_with_recovery(result, result.error_message)


    def save_to_database(self, result: RegistrationResult) -> bool:
        """
        保存注册结果到数据库

        Args:
            result: 注册结果

        Returns:
            是否保存成功
        """
        if not result.success:
            return False

        try:
            # 获取默认 client_id
            settings = get_settings()

            with get_db() as db:
                # 保存账户信息
                account = crud.create_account(
                    db,
                    email=result.email,
                    password=result.password,
                    client_id=settings.openai_client_id,
                    session_token=result.session_token,
                    email_service=self.email_service.service_type.value,
                    email_service_id=self.email_info.get("service_id") if self.email_info else None,
                    account_id=result.account_id,
                    workspace_id=result.workspace_id,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    id_token=result.id_token,
                    proxy_used=self.proxy_url,
                    extra_data=result.metadata,
                    source=result.source
                )

                self._log(f"账户已存进数据库，落袋为安，ID: {account.id}")
                return True

        except Exception as e:
            self._log(f"保存到数据库失败: {e}", "error")
            return False
