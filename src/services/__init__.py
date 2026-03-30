"""
Email services module
"""

from .base import (
    BaseEmailService,
    EmailServiceError,
    EmailServiceStatus,
    EmailServiceFactory,
    create_email_service,
    EmailServiceType,
)
from .tempmail import TempmailService
from .yyds_mail import YYDSMailService
from .outlook import OutlookService
from .moe_mail import MeoMailEmailService
from .temp_mail import TempMailService
from .duck_mail import DuckMailService
from .freemail import FreemailService
from .imap_mail import ImapMailService
from .cloudmail import CloudMailService
from .luckmail_mail import LuckMailService

# Register services
EmailServiceFactory.register(EmailServiceType.TEMPMAIL, TempmailService)
EmailServiceFactory.register(EmailServiceType.YYDS_MAIL, YYDSMailService)
EmailServiceFactory.register(EmailServiceType.OUTLOOK, OutlookService)
EmailServiceFactory.register(EmailServiceType.MOE_MAIL, MeoMailEmailService)
EmailServiceFactory.register(EmailServiceType.TEMP_MAIL, TempMailService)
EmailServiceFactory.register(EmailServiceType.DUCK_MAIL, DuckMailService)
EmailServiceFactory.register(EmailServiceType.FREEMAIL, FreemailService)
EmailServiceFactory.register(EmailServiceType.IMAP_MAIL, ImapMailService)
EmailServiceFactory.register(EmailServiceType.CLOUDMAIL, CloudMailService)
EmailServiceFactory.register(EmailServiceType.LUCKMAIL, LuckMailService)

# Re-export Outlook module extras
from .outlook.base import (
    ProviderType,
    EmailMessage,
    TokenInfo,
    ProviderHealth,
    ProviderStatus,
)
from .outlook.account import OutlookAccount
from .outlook.providers import (
    OutlookProvider,
    IMAPOldProvider,
    IMAPNewProvider,
    GraphAPIProvider,
)

__all__ = [
    'BaseEmailService',
    'EmailServiceError',
    'EmailServiceStatus',
    'EmailServiceFactory',
    'create_email_service',
    'EmailServiceType',
    'TempmailService',
    'YYDSMailService',
    'OutlookService',
    'MeoMailEmailService',
    'TempMailService',
    'DuckMailService',
    'FreemailService',
    'ImapMailService',
    'CloudMailService',
    'LuckMailService',
    'ProviderType',
    'EmailMessage',
    'TokenInfo',
    'ProviderHealth',
    'ProviderStatus',
    'OutlookAccount',
    'OutlookProvider',
    'IMAPOldProvider',
    'IMAPNewProvider',
    'GraphAPIProvider',
]
