"""External capabilities route."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status

from ...deps.external_auth import require_external_api_key

router = APIRouter(dependencies=[Depends(require_external_api_key)])


def _get_external_capabilities() -> Dict[str, Any]:
    """Adapter hook for core capability provider (implemented separately)."""
    try:
        from ....core.email_service_catalog import build_external_capabilities  # type: ignore
    except Exception as exc:  # pragma: no cover - handled by route
        raise RuntimeError("external_capabilities_provider_unavailable") from exc

    return build_external_capabilities()


@router.get("", summary="Get external API capabilities")
def get_external_capabilities() -> Dict[str, Any]:
    try:
        payload = _get_external_capabilities()
        return payload
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
