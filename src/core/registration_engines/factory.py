"""Factory for protocol/browser registration runners."""

from __future__ import annotations

from typing import Optional, Callable

from ...services import BaseEmailService
from .types import REGISTRATION_ENGINE_BROWSER, normalize_registration_engine


def create_registration_runner(
    *,
    engine_name: Optional[str],
    email_service: BaseEmailService,
    proxy_url: Optional[str] = None,
    proxy_source: Optional[str] = None,
    proxy_resolution: Optional[dict] = None,
    callback_logger: Optional[Callable[[str], None]] = None,
    task_uuid: Optional[str] = None,
):
    """Return the concrete registration runner for the selected engine."""
    normalized = normalize_registration_engine(engine_name)

    if normalized == REGISTRATION_ENGINE_BROWSER:
        from .browser import BrowserRegistrationEngine

        return BrowserRegistrationEngine(
            email_service=email_service,
            proxy_url=proxy_url,
            proxy_source=proxy_source,
            proxy_resolution=proxy_resolution,
            callback_logger=callback_logger,
            task_uuid=task_uuid,
        )

    from ..register import RegistrationEngine

    return RegistrationEngine(
        email_service=email_service,
        proxy_url=proxy_url,
        proxy_source=proxy_source,
        callback_logger=callback_logger,
        task_uuid=task_uuid,
    )
