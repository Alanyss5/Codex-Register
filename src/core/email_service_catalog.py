"""Helpers to build email-service catalog payloads in a reusable way."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional

from ..config.settings import get_settings
from ..database import crud
from ..database.session import get_db
from ..services.temp_mail import TempMailService
from ..services.temp_mail_domain_provider import summarize_temp_mail_domains


DomainFetcher = Callable[[], Any]
_TEMP_MAIL_ORDER = ["outlook", "moe_mail", "temp_mail", "duck_mail", "freemail", "imap_mail"]


def build_temp_mail_service_entry(
    service: Any,
    *,
    fetch_domains: Optional[DomainFetcher] = None,
    preview_limit: int = 3,
) -> Dict[str, Any]:
    """Build one temp_mail service item with safe domain summary."""
    config = getattr(service, "config", None) or {}
    summary = summarize_temp_mail_domains(
        config,
        fetch_domains=fetch_domains,
        preview_limit=preview_limit,
    )
    summary.pop("domains", None)

    return {
        "id": getattr(service, "id", None),
        "name": getattr(service, "name", None),
        "type": "temp_mail",
        "priority": getattr(service, "priority", 0),
        **summary,
    }


def build_temp_mail_catalog(
    services: Iterable[Any],
    *,
    fetch_domains_by_service: Optional[Callable[[Any], Optional[DomainFetcher]]] = None,
    preview_limit: int = 3,
) -> Dict[str, Any]:
    """Build a compact temp_mail catalog block for capabilities APIs."""
    entries = []
    for service in services:
        fetcher = fetch_domains_by_service(service) if fetch_domains_by_service else None
        entries.append(
            build_temp_mail_service_entry(
                service,
                fetch_domains=fetcher,
                preview_limit=preview_limit,
            )
        )

    return {
        "available": len(entries) > 0,
        "count": len(entries),
        "services": entries,
    }


def _safe_temp_mail_fetcher(service: Any) -> Optional[DomainFetcher]:
    config = (getattr(service, "config", None) or {}).copy()
    try:
        temp_mail = TempMailService(config=config, name=getattr(service, "name", None))
    except Exception:
        return None

    def _fetch() -> Any:
        return temp_mail._fetch_domains_from_worker()  # noqa: SLF001 - internal reuse for summary

    return _fetch


def _generic_email_service_entry(service: Any) -> Dict[str, Any]:
    return {
        "id": getattr(service, "id", None),
        "name": getattr(service, "name", None),
        "type": getattr(service, "service_type", None),
        "priority": getattr(service, "priority", 0),
    }


def _provider_payload(provider: str, services: Iterable[Any]) -> Dict[str, Any]:
    items = [
        {"id": svc.id, "name": svc.name, "priority": svc.priority}
        for svc in services
    ]
    return {
        "provider": provider,
        "available": len(items) > 0,
        "count": len(items),
        "services": items,
    }


def build_external_capabilities() -> Dict[str, Any]:
    """Build external API capabilities payload from current DB/settings state."""
    settings = get_settings()

    with get_db() as db:
        enabled_email_services = crud.get_email_services(db, enabled=True, limit=1000)
        grouped: Dict[str, list[Any]] = {}
        for service in enabled_email_services:
            grouped.setdefault(service.service_type, []).append(service)

        email_types = [
            {
                "type": "tempmail",
                "available": True,
                "count": 1,
                "services": [{"id": None, "name": "Tempmail.lol", "type": "tempmail"}],
            }
        ]

        ordered_types = [service_type for service_type in _TEMP_MAIL_ORDER if service_type in grouped]
        remaining_types = sorted(service_type for service_type in grouped.keys() if service_type not in _TEMP_MAIL_ORDER)

        for service_type in [*ordered_types, *remaining_types]:
            services = grouped.get(service_type, [])
            if service_type == "temp_mail":
                email_types.append(
                    {
                        "type": service_type,
                        **build_temp_mail_catalog(
                            services,
                            fetch_domains_by_service=_safe_temp_mail_fetcher,
                        ),
                    }
                )
            else:
                entries = [_generic_email_service_entry(svc) for svc in services]
                email_types.append(
                    {
                        "type": service_type,
                        "available": len(entries) > 0,
                        "count": len(entries),
                        "services": entries,
                    }
                )

        upload_providers = [
            _provider_payload("cpa", crud.get_cpa_services(db, enabled=True)),
            _provider_payload("sub2api", crud.get_sub2api_services(db, enabled=True)),
            _provider_payload("tm", crud.get_tm_services(db, enabled=True)),
        ]

    return {
        "email_types": email_types,
        "upload_providers": upload_providers,
        "settings": {
            "external_api_enabled": getattr(settings, "external_api_enabled", False),
        },
    }
