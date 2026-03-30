
"""External registration batch routes."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...deps.external_auth import require_external_api_key

router = APIRouter(dependencies=[Depends(require_external_api_key)])


class ExternalEmailSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., min_length=1)
    service_id: Optional[int] = None


class ExternalUploadSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    provider: Optional[Literal["sub2api", "cpa", "tm"]] = None
    service_id: Optional[int] = None


class ExternalExecutionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str = "protocol"
    mode: Literal["pipeline", "parallel"] = "pipeline"
    concurrency: int = Field(default=1, ge=1, le=50)
    interval_min: int = Field(default=5, ge=0)
    interval_max: int = Field(default=30, ge=0)

    @model_validator(mode="after")
    def validate_interval_bounds(self):
        if self.interval_max < self.interval_min:
            raise ValueError("interval_max must be greater than or equal to interval_min")
        return self


class ExternalBatchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(..., ge=1)
    idempotency_key: Optional[str] = None
    email: ExternalEmailSelection
    upload: ExternalUploadSelection = Field(default_factory=ExternalUploadSelection)
    execution: ExternalExecutionOptions = Field(default_factory=ExternalExecutionOptions)

    @model_validator(mode="after")
    def validate_upload(self):
        if self.upload.enabled and not self.upload.provider:
            raise ValueError("upload.provider is required when upload.enabled is true")
        return self


def _create_external_batch(payload: Dict[str, Any], background_tasks: Optional[BackgroundTasks] = None) -> Dict[str, Any]:
    try:
        from ....core.external_batches.service import create_external_registration_batch  # type: ignore
    except Exception as exc:  # pragma: no cover - handled by route
        raise RuntimeError("external_batch_service_unavailable") from exc
    return create_external_registration_batch(payload, background_tasks=background_tasks)


def _get_external_batch(batch_uuid: str) -> Dict[str, Any]:
    try:
        from ....core.external_batches.service import get_external_registration_batch_status  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("external_batch_service_unavailable") from exc
    return get_external_registration_batch_status(batch_uuid)


def _cancel_external_batch(batch_uuid: str) -> Dict[str, Any]:
    try:
        from ....core.external_batches.service import cancel_external_registration_batch  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("external_batch_service_unavailable") from exc
    return cancel_external_registration_batch(batch_uuid)


def _translate_error(exc: Exception) -> HTTPException:
    detail = str(exc)
    code = status.HTTP_404_NOT_FOUND if detail == "batch_not_found" else status.HTTP_400_BAD_REQUEST
    return HTTPException(status_code=code, detail=detail)


@router.post("/batches", status_code=status.HTTP_202_ACCEPTED, summary="Create external registration batch")
def create_external_registration_batch(request: ExternalBatchCreateRequest, background_tasks: BackgroundTasks = None, response: Response = None) -> Dict[str, Any]:
    try:
        result = _create_external_batch(request.model_dump(), background_tasks=background_tasks)
        if result.get("idempotent_replay") is True and response is not None:
            response.status_code = status.HTTP_200_OK
        return result
    except ValueError as exc:
        raise _translate_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/batches/{batch_uuid}", summary="Get external registration batch status")
def get_external_registration_batch(batch_uuid: str) -> Dict[str, Any]:
    try:
        return _get_external_batch(batch_uuid)
    except ValueError as exc:
        raise _translate_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/batches/{batch_uuid}/cancel", summary="Cancel external registration batch")
def cancel_external_registration_batch(batch_uuid: str) -> Dict[str, Any]:
    try:
        return _cancel_external_batch(batch_uuid)
    except ValueError as exc:
        raise _translate_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
