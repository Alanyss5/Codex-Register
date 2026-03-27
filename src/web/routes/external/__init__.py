"""External API router package."""

from fastapi import APIRouter

from .capabilities import router as capabilities_router
from .registration import router as registration_router

router = APIRouter()
router.include_router(capabilities_router, prefix="/capabilities", tags=["external-capabilities"])
router.include_router(registration_router, prefix="/registration", tags=["external-registration"])
