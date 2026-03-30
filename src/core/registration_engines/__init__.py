"""Registration engine selection and browser-based engine support."""

from .factory import create_registration_runner
from .types import REGISTRATION_ENGINE_BROWSER, REGISTRATION_ENGINE_PROTOCOL, normalize_registration_engine

__all__ = [
    "REGISTRATION_ENGINE_BROWSER",
    "REGISTRATION_ENGINE_PROTOCOL",
    "normalize_registration_engine",
    "create_registration_runner",
]
