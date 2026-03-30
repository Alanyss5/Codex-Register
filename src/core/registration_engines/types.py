"""Shared registration-engine constants and normalization helpers."""

from __future__ import annotations

from typing import Optional


REGISTRATION_ENGINE_PROTOCOL = "protocol"
REGISTRATION_ENGINE_BROWSER = "browser"

_ALIASES = {
    REGISTRATION_ENGINE_PROTOCOL: REGISTRATION_ENGINE_PROTOCOL,
    "api": REGISTRATION_ENGINE_PROTOCOL,
    "http": REGISTRATION_ENGINE_PROTOCOL,
    REGISTRATION_ENGINE_BROWSER: REGISTRATION_ENGINE_BROWSER,
    "drission": REGISTRATION_ENGINE_BROWSER,
    "drissionpage": REGISTRATION_ENGINE_BROWSER,
}


def normalize_registration_engine(engine_name: Optional[str]) -> str:
    """Normalize user-provided registration engine name."""
    if engine_name is None:
        return REGISTRATION_ENGINE_PROTOCOL

    normalized = str(engine_name).strip().lower()
    if not normalized:
        return REGISTRATION_ENGINE_PROTOCOL

    try:
        return _ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported registration engine: {engine_name}") from exc
