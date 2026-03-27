"""
Outlook 邮箱服务主类
支持多种 IMAP/API 连接方式，自动故障切换
"""

import logging
import threading
import time
from typing import Optional, Dict, Any, List

from ..base import BaseEmailService, EmailServiceError, EmailServiceStatus, EmailServiceType
from ...config.constants import EmailServiceType as ServiceType
from ...config.settings import get_settings
from .account import OutlookAccount
from .base import ProviderType, EmailMessage
from .email_parser import EmailParser, get_email_parser
from .health_checker import HealthChecker, FailoverManager
from .providers.base import OutlookProvider, ProviderConfig
from .providers.imap_old import IMAPOldProvider
from .providers.imap_new import IMAPNewProvider
from .providers.graph_api import GraphAPIProvider


logger = logging.getLogger(__name__)


# 默认提供者优先级
# IMAP_OLD 最兼容（只需 login.live.com token），IMAP_NEW 次之，Graph API 最后
# 原因：部分 client_id 没有 Graph API 权限，但有 IMAP 权限
DEFAULT_PROVIDER_PRIORITY = [
    ProviderType.IMAP_OLD,
    ProviderType.IMAP_NEW,
    ProviderType.GRAPH_API,
]


def get_email_code_settings() -> dict:
    """获取验证码等待配置"""
    settings = get_settings()
    return {
        "timeout": settings.email_code_timeout,
        "poll_interval": settings.email_code_poll_interval,
    }


class OutlookService(BaseEmailService):
    """
    Outlook 邮箱服务
    支持多种 IMAP/API 连接方式，自动故障切换
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 Outlook 服务

        Args:
            config: 配置字典，支持以下键:
                - accounts: Outlook 账户列表
                - provider_priority: 提供者优先级列表
                - health_failure_threshold: 连续失败次数阈值
                - health_disable_duration: 禁用时长（秒）
                - timeout: 请求超时时间
                - proxy_url: 代理 URL
            name: 服务名称
        """
        super().__init__(ServiceType.OUTLOOK, name)

        # 默认配置
        default_config = {
            "accounts": [],
            "provider_priority": [p.value for p in DEFAULT_PROVIDER_PRIORITY],
            "health_failure_threshold": 5,
            "health_disable_duration": 60,
            "timeout": 30,
            "proxy_url": None,
        }

        self.config = {**default_config, **(config or {})}

        # 解析提供者优先级
        self.provider_priority = [
            ProviderType(p) for p in self.config.get("provider_priority", [])
        ]
        if not self.provider_priority:
            self.provider_priority = DEFAULT_PROVIDER_PRIORITY

        # 提供者配置
        self.provider_config = ProviderConfig(
            timeout=self.config.get("timeout", 30),
            proxy_url=self.config.get("proxy_url"),
            health_failure_threshold=self.config.get("health_failure_threshold", 3),
            health_disable_duration=self.config.get("health_disable_duration", 300),
        )

        # 获取默认 client_id（供无 client_id 的账户使用）
        try:
            _default_client_id = get_settings().outlook_default_client_id
        except Exception:
            _default_client_id = "24d9a0ed-8787-4584-883c-2fd79308940a"

        # 解析账户
        self.accounts: List[OutlookAccount] = []
        self._current_account_index = 0
        self._account_lock = threading.Lock()

        # 支持两种配置格式
        if "email" in self.config and "password" in self.config:
            account = OutlookAccount.from_config(self.config)
            if not account.client_id and _default_client_id:
                account.client_id = _default_client_id
            if account.validate():
                self.accounts.append(account)
        else:
            for account_config in self.config.get("accounts", []):
                account = OutlookAccount.from_config(account_config)
                if not account.client_id and _default_client_id:
                    account.client_id = _default_client_id
                if account.validate():
                    self.accounts.append(account)

        if not self.accounts:
            logger.warning("未配置有效的 Outlook 账户")

        # 健康检查器和故障切换管理器
        self.health_checker = HealthChecker(
            failure_threshold=self.provider_config.health_failure_threshold,
            disable_duration=self.provider_config.health_disable_duration,
        )
        self.failover_manager = FailoverManager(
            health_checker=self.health_checker,
            priority_order=self.provider_priority,
        )

        # 邮件解析器
        self.email_parser = get_email_parser()

        # 提供者实例缓存: (email, provider_type) -> OutlookProvider
        self._providers: Dict[tuple, OutlookProvider] = {}
        self._provider_lock = threading.Lock()

        # IMAP 连接限制（防止限流）
        self._imap_semaphore = threading.Semaphore(5)

        # 邮件投递去重机制：避免重复消费同一封邮件，但允许新邮件复用相同验证码
        self._used_email_deliveries: Dict[tuple, set] = {}
        self._verification_stage_by_email: Dict[str, str] = {}
        self._last_verification_debug: Dict[str, Dict[str, Any]] = {}

    def _get_provider(
        self,
        account: OutlookAccount,
        provider_type: ProviderType,
    ) -> OutlookProvider:
        """
        获取或创建提供者实例

        Args:
            account: Outlook 账户
            provider_type: 提供者类型

        Returns:
            提供者实例
        """
        cache_key = (account.email.lower(), provider_type)

        with self._provider_lock:
            if cache_key not in self._providers:
                provider = self._create_provider(account, provider_type)
                self._providers[cache_key] = provider

            return self._providers[cache_key]

    def _create_provider(
        self,
        account: OutlookAccount,
        provider_type: ProviderType,
    ) -> OutlookProvider:
        """
        创建提供者实例

        Args:
            account: Outlook 账户
            provider_type: 提供者类型

        Returns:
            提供者实例
        """
        if provider_type == ProviderType.IMAP_OLD:
            return IMAPOldProvider(account, self.provider_config)
        elif provider_type == ProviderType.IMAP_NEW:
            return IMAPNewProvider(account, self.provider_config)
        elif provider_type == ProviderType.GRAPH_API:
            return GraphAPIProvider(account, self.provider_config)
        else:
            raise ValueError(f"未知的提供者类型: {provider_type}")

    def _get_provider_priority_for_account(self, account: OutlookAccount) -> List[ProviderType]:
        """根据账户是否有 OAuth，返回适合的提供者优先级列表"""
        if account.has_oauth():
            return self.provider_priority
        else:
            # 无 OAuth，直接走旧版 IMAP（密码认证），跳过需要 OAuth 的提供者
            return [ProviderType.IMAP_OLD]

    def _try_providers_for_emails(
        self,
        account: OutlookAccount,
        count: int = 20,
        only_unseen: bool = True,
    ) -> List[EmailMessage]:
        """
        尝试多个提供者获取邮件

        Args:
            account: Outlook 账户
            count: 获取数量
            only_unseen: 是否只获取未读

        Returns:
            邮件列表
        """
        errors = []

        # 根据账户类型选择合适的提供者优先级
        priority = self._get_provider_priority_for_account(account)

        # 按优先级尝试各提供者
        for provider_type in priority:
            # 检查提供者是否可用
            if not self.health_checker.is_available(provider_type):
                logger.debug(
                    f"[{account.email}] {provider_type.value} 不可用，跳过"
                )
                continue

            try:
                provider = self._get_provider(account, provider_type)

                with self._imap_semaphore:
                    with provider:
                        emails = provider.get_recent_emails(count, only_unseen)

                        if emails:
                            # 成功获取邮件
                            self.health_checker.record_success(provider_type)
                            logger.debug(
                                f"[{account.email}] {provider_type.value} 获取到 {len(emails)} 封邮件"
                            )
                            return emails

            except Exception as e:
                error_msg = str(e)
                errors.append(f"{provider_type.value}: {error_msg}")
                self.health_checker.record_failure(provider_type, error_msg)
                logger.warning(
                    f"[{account.email}] {provider_type.value} 获取邮件失败: {e}"
                )

        logger.error(
            f"[{account.email}] 所有提供者都失败: {'; '.join(errors)}"
        )
        return []

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        选择可用的 Outlook 账户

        Args:
            config: 配置参数（未使用）

        Returns:
            包含邮箱信息的字典
        """
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有可用的 Outlook 账户"))
            raise EmailServiceError("没有可用的 Outlook 账户")

        # 轮询选择账户
        with self._account_lock:
            account = self.accounts[self._current_account_index]
            self._current_account_index = (self._current_account_index + 1) % len(self.accounts)

        email_info = {
            "email": account.email,
            "service_id": account.email,
            "account": {
                "email": account.email,
                "has_oauth": account.has_oauth()
            }
        }

        logger.info(f"选择 Outlook 账户: {account.email}")
        self.update_status(True)
        return email_info

    def set_verification_stage(self, email: str, stage: str) -> None:
        """Track the current OTP stage for each Outlook mailbox.

        When switching stages (e.g. signup_otp → relogin_otp), carry over
        already-consumed email deliveries so the new stage won't re-consume
        OTP emails that were already used in the previous stage.
        """
        email_lower = email.lower()
        old_stage = self._verification_stage_by_email.get(email_lower)
        self._verification_stage_by_email[email_lower] = stage

        # 跨阶段继承已消费的邮件投递记录
        if old_stage and old_stage != stage:
            old_key = (email_lower, old_stage)
            new_key = (email_lower, stage)
            old_used = self._used_email_deliveries.get(old_key, set())
            if old_used:
                if new_key not in self._used_email_deliveries:
                    self._used_email_deliveries[new_key] = set()
                self._used_email_deliveries[new_key].update(old_used)
                logger.info(
                    f"[{email}] 阶段切换 {old_stage} → {stage}，"
                    f"继承 {len(old_used)} 条已消费邮件记录"
                )

    def _is_preferred_openai_otp_sender(self, email: EmailMessage) -> bool:
        sender = (email.sender or "").lower()
        return "otp@" in sender and ".openai.com" in sender

    def _build_candidate_summary(
        self,
        email: EmailMessage,
        *,
        target_email: str,
        otp_sent_at: Optional[float],
    ) -> Dict[str, Any]:
        is_openai = self.email_parser.is_openai_verification_email(email, target_email=target_email)
        code = self.email_parser.extract_verification_code(email) if is_openai else None
        received_ts = int(email.received_timestamp or 0)
        otp_ts = int(otp_sent_at or 0)
        return {
            "sender": email.sender,
            "subject": email.subject,
            "received_timestamp": received_ts,
            "delta_from_otp_sent": received_ts - otp_ts if otp_ts else None,
            "is_openai_verification": is_openai,
            "is_preferred_sender": self._is_preferred_openai_otp_sender(email),
            "code": code,
        }

    def _format_candidate_summary(self, summary: Dict[str, Any]) -> str:
        sender = str(summary.get("sender") or "-")
        received_ts = summary.get("received_timestamp")
        delta = summary.get("delta_from_otp_sent")
        code = str(summary.get("code") or "-")
        preferred = "preferred" if summary.get("is_preferred_sender") else "generic"
        return (
            f"sender={sender}, ts={received_ts}, delta={delta}, "
            f"code={code}, kind={preferred}"
        )

    def get_last_verification_debug(self, email: str) -> Dict[str, Any]:
        return dict(self._last_verification_debug.get(email.lower(), {}))

    @staticmethod
    def _is_email_within_otp_window(email: EmailMessage, min_timestamp: int) -> bool:
        received_timestamp = int(getattr(email, "received_timestamp", 0) or 0)
        return not (
            min_timestamp > 0
            and received_timestamp > 0
            and received_timestamp < min_timestamp
        )

    def _is_email_delivery_unused(
        self,
        email: EmailMessage,
        used_email_deliveries: set,
    ) -> bool:
        return self.email_parser.email_delivery_key(email) not in used_email_deliveries

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = None,
        pattern: str = None,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从 Outlook 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用
            timeout: 超时时间（秒）
            pattern: 验证码正则表达式（未使用）
            otp_sent_at: OTP 发送时间戳

        Returns:
            验证码字符串
        """
        # 查找对应的账户
        account = None
        for acc in self.accounts:
            if acc.email.lower() == email.lower():
                account = acc
                break

        if not account:
            self.update_status(False, EmailServiceError(f"未找到邮箱对应的账户: {email}"))
            return None

        # 获取验证码等待配置
        code_settings = get_email_code_settings()
        actual_timeout = timeout or code_settings["timeout"]
        poll_interval = code_settings["poll_interval"]

        logger.info(
            f"[{email}] 开始获取验证码，超时 {actual_timeout}s，"
            f"提供者优先级: {[p.value for p in self.provider_priority]}"
        )

        # 初始化邮件投递去重集合
        stage = self._verification_stage_by_email.get(email.lower(), "signup_otp")
        logger.info(f"[{email}] verification stage: {stage}")
        used_delivery_key = (email.lower(), stage)
        if used_delivery_key not in self._used_email_deliveries:
            self._used_email_deliveries[used_delivery_key] = set()
        used_email_deliveries = self._used_email_deliveries[used_delivery_key]
        debug_state = {
            "stage": stage,
            "poll_count": 0,
            "otp_sent_at": int(otp_sent_at or 0),
            "min_timestamp": int((otp_sent_at - 60) if otp_sent_at else 0),
            "deferred_generic_only_polls": 0,
            "fresh_verification_count": 0,
            "fresh_preferred_sender_count": 0,
            "stale_preferred_sender_count": 0,
            "available_fresh_verification_count": 0,
            "available_fresh_preferred_sender_count": 0,
            "used_fresh_preferred_sender_count": 0,
            "selected_sender": None,
            "selected_code": None,
            "selected_received_timestamp": None,
            "candidate_summaries": [],
            "last_status": "waiting",
        }
        self._last_verification_debug[email.lower()] = debug_state

        # 计算最小时间戳（留出 60 秒时钟偏差）
        min_timestamp = (otp_sent_at - 60) if otp_sent_at else 0

        start_time = time.time()
        poll_count = 0

        while time.time() - start_time < actual_timeout:
            poll_count += 1
            debug_state["poll_count"] = poll_count

            # 渐进式邮件检查：前 3 次只检查未读
            only_unseen = poll_count <= 3

            try:
                # 尝试多个提供者获取邮件
                emails = self._try_providers_for_emails(
                    account,
                    count=15,
                    only_unseen=only_unseen,
                )

                if emails:
                    logger.debug(
                        f"[{email}] 第 {poll_count} 次轮询获取到 {len(emails)} 封邮件"
                    )
                    candidate_summaries = [
                        self._build_candidate_summary(
                            item,
                            target_email=email,
                            otp_sent_at=otp_sent_at,
                        )
                        for item in emails[:5]
                    ]
                    debug_state["candidate_summaries"] = candidate_summaries
                    if candidate_summaries:
                        logger.info(
                            f"[{email}] OTP candidates poll={poll_count}: "
                            + " | ".join(
                                self._format_candidate_summary(item)
                                for item in candidate_summaries
                            )
                        )

                    candidate_emails = emails
                    if stage == "relogin_otp":
                        verification_emails = [
                            item
                            for item in emails
                            if self.email_parser.is_openai_verification_email(
                                item,
                                target_email=email,
                            )
                        ]
                        fresh_verification_emails = [
                            item
                            for item in verification_emails
                            if self._is_email_within_otp_window(item, min_timestamp)
                        ]
                        preferred_sender_emails = [
                            item
                            for item in verification_emails
                            if self._is_preferred_openai_otp_sender(item)
                        ]
                        fresh_preferred_sender_emails = [
                            item
                            for item in preferred_sender_emails
                            if self._is_email_within_otp_window(item, min_timestamp)
                        ]
                        available_fresh_verification_emails = [
                            item
                            for item in fresh_verification_emails
                            if self._is_email_delivery_unused(item, used_email_deliveries)
                        ]
                        available_fresh_preferred_sender_emails = [
                            item
                            for item in fresh_preferred_sender_emails
                            if self._is_email_delivery_unused(item, used_email_deliveries)
                        ]
                        stale_preferred_sender_count = (
                            len(preferred_sender_emails) - len(fresh_preferred_sender_emails)
                        )
                        used_fresh_preferred_sender_count = (
                            len(fresh_preferred_sender_emails) - len(available_fresh_preferred_sender_emails)
                        )
                        debug_state["fresh_verification_count"] = len(fresh_verification_emails)
                        debug_state["fresh_preferred_sender_count"] = len(fresh_preferred_sender_emails)
                        debug_state["stale_preferred_sender_count"] = stale_preferred_sender_count
                        debug_state["available_fresh_verification_count"] = len(available_fresh_verification_emails)
                        debug_state["available_fresh_preferred_sender_count"] = len(available_fresh_preferred_sender_emails)
                        debug_state["used_fresh_preferred_sender_count"] = used_fresh_preferred_sender_count

                        if available_fresh_preferred_sender_emails:
                            candidate_emails = available_fresh_preferred_sender_emails
                            logger.info(
                                f"[{email}] relogin_otp 命中首选 OTP 发件人，优先使用 "
                                f"{len(available_fresh_preferred_sender_emails)} 封当前窗口内且未消费的 otp@*.openai.com 邮件"
                            )
                        elif available_fresh_verification_emails and poll_count < 3:
                            debug_state["deferred_generic_only_polls"] += 1
                            debug_state["last_status"] = "deferred_generic_only"
                            if stale_preferred_sender_count:
                                logger.info(
                                    f"[{email}] relogin_otp 仅发现 {len(available_fresh_verification_emails)} 封当前窗口内通用验证码，"
                                    f"同时过滤掉 {stale_preferred_sender_count} 封过旧 otp@*.openai.com 邮件，继续等待"
                                )
                            if used_fresh_preferred_sender_count:
                                logger.info(
                                    f"[{email}] relogin_otp 当前窗口内有 {used_fresh_preferred_sender_count} 封 preferred 邮件已消费，"
                                    "本轮不再重复使用"
                                )
                            logger.info(
                                f"[{email}] relogin_otp 暂缓消费通用验证码邮件，继续等待 otp@*.openai.com"
                            )
                            time.sleep(poll_interval)
                            continue
                        elif available_fresh_verification_emails:
                            candidate_emails = available_fresh_verification_emails
                            if stale_preferred_sender_count:
                                logger.info(
                                    f"[{email}] relogin_otp 当前窗口内没有可用的 otp@*.openai.com 邮件，"
                                    f"改用 {len(available_fresh_verification_emails)} 封当前窗口内通用验证码，"
                                    f"已过滤 {stale_preferred_sender_count} 封过旧 preferred 邮件"
                                )
                            if used_fresh_preferred_sender_count:
                                logger.info(
                                    f"[{email}] relogin_otp 当前窗口内的 preferred 邮件都已消费，"
                                    f"改用 {len(available_fresh_verification_emails)} 封未消费验证码"
                                )
                        elif verification_emails:
                            candidate_emails = verification_emails

                    # 从邮件中查找验证码
                    match = self.email_parser.find_verification_code_in_emails(
                        candidate_emails,
                        target_email=email,
                        min_timestamp=min_timestamp,
                        used_email_keys=used_email_deliveries,
                    )

                    if match:
                        code, delivery_key = match
                        used_email_deliveries.add(delivery_key)
                        selected_email = next(
                            (
                                item
                                for item in candidate_emails
                                if self.email_parser.email_delivery_key(item) == delivery_key
                            ),
                            None,
                        )
                        if selected_email is not None:
                            debug_state["selected_sender"] = selected_email.sender
                            debug_state["selected_received_timestamp"] = int(selected_email.received_timestamp or 0)
                        debug_state["selected_code"] = code
                        debug_state["last_status"] = "selected"
                        elapsed = int(time.time() - start_time)
                        logger.info(
                            f"[{email}] 找到验证码: {code}，"
                            f"总耗时 {elapsed}s，轮询 {poll_count} 次"
                        )
                        self.update_status(True)
                        return code

            except Exception as e:
                logger.warning(f"[{email}] 检查出错: {e}")

            # 等待下次轮询
            time.sleep(poll_interval)

        elapsed = int(time.time() - start_time)
        debug_state["last_status"] = "timeout"
        logger.warning(f"[{email}] 验证码超时 ({actual_timeout}s)，共轮询 {poll_count} 次")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """列出所有可用的 Outlook 账户"""
        return [
            {
                "email": account.email,
                "id": account.email,
                "has_oauth": account.has_oauth(),
                "type": "outlook"
            }
            for account in self.accounts
        ]

    def delete_email(self, email_id: str) -> bool:
        """删除邮箱（Outlook 不支持删除账户）"""
        logger.warning(f"Outlook 服务不支持删除账户: {email_id}")
        return False

    def check_health(self) -> bool:
        """检查 Outlook 服务是否可用"""
        if not self.accounts:
            self.update_status(False, EmailServiceError("没有配置的账户"))
            return False

        # 测试第一个账户的连接
        test_account = self.accounts[0]

        # 尝试任一提供者连接
        for provider_type in self.provider_priority:
            try:
                provider = self._get_provider(test_account, provider_type)
                if provider.test_connection():
                    self.update_status(True)
                    return True
            except Exception as e:
                logger.warning(
                    f"Outlook 健康检查失败 ({test_account.email}, {provider_type.value}): {e}"
                )

        self.update_status(False, EmailServiceError("健康检查失败"))
        return False

    def get_provider_status(self) -> Dict[str, Any]:
        """获取提供者状态"""
        return self.failover_manager.get_status()

    def get_account_stats(self) -> Dict[str, Any]:
        """获取账户统计信息"""
        total = len(self.accounts)
        oauth_count = sum(1 for acc in self.accounts if acc.has_oauth())

        return {
            "total_accounts": total,
            "oauth_accounts": oauth_count,
            "password_accounts": total - oauth_count,
            "accounts": [acc.to_dict() for acc in self.accounts],
            "provider_status": self.get_provider_status(),
        }

    def add_account(self, account_config: Dict[str, Any]) -> bool:
        """添加新的 Outlook 账户"""
        try:
            account = OutlookAccount.from_config(account_config)
            if not account.validate():
                return False

            self.accounts.append(account)
            logger.info(f"添加 Outlook 账户: {account.email}")
            return True
        except Exception as e:
            logger.error(f"添加 Outlook 账户失败: {e}")
            return False

    def remove_account(self, email: str) -> bool:
        """移除 Outlook 账户"""
        for i, acc in enumerate(self.accounts):
            if acc.email.lower() == email.lower():
                self.accounts.pop(i)
                logger.info(f"移除 Outlook 账户: {email}")
                return True
        return False

    def reset_provider_health(self):
        """重置所有提供者的健康状态"""
        self.health_checker.reset_all()
        logger.info("已重置所有提供者的健康状态")

    def force_provider(self, provider_type: ProviderType):
        """强制使用指定的提供者"""
        self.health_checker.force_enable(provider_type)
        # 禁用其他提供者
        for pt in ProviderType:
            if pt != provider_type:
                self.health_checker.force_disable(pt, 60)
        logger.info(f"已强制使用提供者: {provider_type.value}")
