"""Domain resolution helpers for Temp-Mail service."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DomainFetcher = Callable[[], Any]


def normalize_domains(value: Any) -> List[str]:
    """Normalize domains from list/string payload into unique ordered list."""
    if value is None:
        return []

    values: Iterable[Any]
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",")]
    elif isinstance(value, Sequence):
        values = value
    else:
        return []

    seen = set()
    domains: List[str] = []
    for item in values:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if "." not in text:
            continue
        if text in seen:
            continue
        seen.add(text)
        domains.append(text)
    return domains


def _domains_from_worker_response(payload: Any) -> List[str]:
    if isinstance(payload, dict):
        return normalize_domains(payload.get("domains"))
    return normalize_domains(payload)


def _resolve_with_source(config: Optional[Dict[str, Any]], fetch_domains: Optional[DomainFetcher]) -> Tuple[List[str], str]:
    cfg = config or {}

    configured_pool = normalize_domains(cfg.get("domains"))
    if configured_pool:
        return configured_pool, "config_domains"

    if fetch_domains is not None:
        try:
            worker_domains = _domains_from_worker_response(fetch_domains())
            if worker_domains:
                return worker_domains, "worker_api"
        except Exception as exc:  # pragma: no cover - exercised via tests
            logger.debug("Temp-Mail domains endpoint unavailable, fallback to config.domain: %s", exc)

    fallback = normalize_domains(cfg.get("domain"))
    if fallback:
        return fallback, "config_fallback"

    return [], "none"


def resolve_temp_mail_domains(
    config: Optional[Dict[str, Any]],
    *,
    fetch_domains: Optional[DomainFetcher] = None,
) -> List[str]:
    """Resolve available domains for a temp_mail service config."""
    domains, _ = _resolve_with_source(config, fetch_domains)
    return domains


def summarize_temp_mail_domains(
    config: Optional[Dict[str, Any]],
    *,
    fetch_domains: Optional[DomainFetcher] = None,
    preview_limit: int = 3,
) -> Dict[str, Any]:
    """Build safe domain summary for API output/capabilities."""
    domains, source = _resolve_with_source(config, fetch_domains)
    preview_count = max(0, int(preview_limit))
    preview = domains[:preview_count] if preview_count else []
    primary = domains[0] if domains else None
    return {
        "domain": primary,
        "domain_count": len(domains),
        "domains_preview": preview,
        "domain_source": source,
        "domains": domains,
    }
