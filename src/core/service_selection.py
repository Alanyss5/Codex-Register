
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..database import crud


@dataclass
class EmailItemAssignment:
    item_index: int
    service_id: Optional[int]
    service_type: str
    failure_reason: Optional[str] = None
    config_snapshot: Optional[dict[str, Any]] = None


@dataclass
class UploadTarget:
    provider: str
    service_id: int
    service_name: str
    config_snapshot: dict[str, Any]


def _get_enabled_email_services(db: Session, email_type: str):
    return crud.get_email_services(db, service_type=email_type, enabled=True)


def _get_email_service_or_raise(db: Session, email_type: str, requested_service_id: int):
    service = crud.get_email_service_by_id(db, requested_service_id)
    if not service:
        raise ValueError(f"email service {requested_service_id} not found")
    if not service.enabled:
        raise ValueError(f"email service {requested_service_id} is disabled")
    if service.service_type != email_type:
        raise ValueError(f"email service {requested_service_id} does not belong to type {email_type}")
    return service


def build_email_item_assignments(
    db: Session,
    *,
    email_type: str,
    count: int,
    requested_service_id: Optional[int] = None,
) -> list[EmailItemAssignment]:
    if count < 1:
        raise ValueError('count must be >= 1')

    if requested_service_id is not None:
        service = _get_email_service_or_raise(db, email_type, requested_service_id)
        if email_type == 'outlook' and count > 1:
            raise ValueError('outlook requested_service_id cannot be reused when count > 1')
        return [
            EmailItemAssignment(
                item_index=index,
                service_id=service.id,
                service_type=email_type,
                config_snapshot=(service.config or {}).copy(),
            )
            for index in range(count)
        ]

    services = _get_enabled_email_services(db, email_type)
    if not services:
        raise ValueError(f'no enabled email services for type {email_type}')

    if email_type == 'outlook':
        assignments: list[EmailItemAssignment] = []
        for index in range(count):
            if index < len(services):
                service = services[index]
                assignments.append(
                    EmailItemAssignment(
                        item_index=index,
                        service_id=service.id,
                        service_type=email_type,
                        config_snapshot=(service.config or {}).copy(),
                    )
                )
            else:
                assignments.append(
                    EmailItemAssignment(
                        item_index=index,
                        service_id=None,
                        service_type=email_type,
                        failure_reason='no_available_email_service',
                    )
                )
        return assignments

    if email_type == 'temp_mail':
        min_priority = min(service.priority for service in services)
        pool = [service for service in services if service.priority == min_priority]
        return [
            EmailItemAssignment(
                item_index=index,
                service_id=(selected := random.choice(pool)).id,
                service_type=email_type,
                config_snapshot=(selected.config or {}).copy(),
            )
            for index in range(count)
        ]

    service = services[0]
    return [
        EmailItemAssignment(
            item_index=index,
            service_id=service.id,
            service_type=email_type,
            config_snapshot=(service.config or {}).copy(),
        )
        for index in range(count)
    ]


def resolve_upload_target(db: Session, *, provider: str, requested_service_id: Optional[int] = None) -> UploadTarget:
    provider = (provider or '').strip().lower()
    provider_to_crud = {
        'cpa': (crud.get_cpa_service_by_id, lambda session: crud.get_cpa_services(session, enabled=True)),
        'sub2api': (crud.get_sub2api_service_by_id, lambda session: crud.get_sub2api_services(session, enabled=True)),
        'tm': (crud.get_tm_service_by_id, lambda session: crud.get_tm_services(session, enabled=True)),
    }
    if provider not in provider_to_crud:
        raise ValueError(f'unsupported upload provider: {provider}')

    get_one, list_enabled = provider_to_crud[provider]
    if requested_service_id is not None:
        service = get_one(db, requested_service_id)
        if not service:
            for other_provider, (other_get_one, _) in provider_to_crud.items():
                if other_provider == provider:
                    continue
                if other_get_one(db, requested_service_id):
                    raise ValueError(f'upload service {requested_service_id} does not belong to provider {provider}')
            raise ValueError(f'upload service {requested_service_id} not found')
        if not getattr(service, 'enabled', False):
            raise ValueError(f'upload service {requested_service_id} is disabled')
        service_pool = {svc.id for svc in list_enabled(db)}
        if service.id not in service_pool:
            raise ValueError(f'upload service {requested_service_id} does not belong to provider {provider}')
    else:
        services = list_enabled(db)
        if not services:
            raise ValueError(f'no enabled upload services for provider {provider}')
        service = services[0]

    if provider == 'cpa':
        config_snapshot = {'api_url': service.api_url, 'api_token': service.api_token}
    else:
        config_snapshot = {'api_url': service.api_url, 'api_key': service.api_key}

    return UploadTarget(
        provider=provider,
        service_id=service.id,
        service_name=service.name,
        config_snapshot=config_snapshot,
    )
