"""External API authentication dependency."""

from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Header, HTTPException, status

from ...config.settings import get_settings


def _get_external_api_key_value() -> str:
    settings = get_settings()
    api_key = getattr(settings, "external_api_key", None)
    if api_key is None:
        return ""
    try:
        return api_key.get_secret_value()
    except AttributeError:
        return str(api_key)


def require_external_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Validate external API access policy and API key."""
    settings = get_settings()
    if not getattr(settings, "external_api_enabled", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="External API is disabled",
        )

    expected_key = _get_external_api_key_value()
    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="External API key is not configured",
        )

    provided = x_api_key or ""
    if not secrets.compare_digest(provided, expected_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
