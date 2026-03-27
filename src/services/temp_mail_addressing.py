"""Helpers for Temp-Mail local-part generation and domain choice."""

from __future__ import annotations

import random
import string
from typing import Optional, Sequence


def _rng(rng: Optional[random.Random] = None) -> random.Random:
    return rng or random.SystemRandom()


def generate_local_part(*, enable_prefix: bool = False, rng: Optional[random.Random] = None) -> str:
    """Generate a variable-length alphanumeric local-part.

    Keeps the previous style (letters+digits+letters), but defaults to no tmp prefix.
    """
    r = _rng(rng)
    letters = ''.join(r.choices(string.ascii_lowercase, k=5))
    digits = ''.join(r.choices(string.digits, k=r.randint(1, 3)))
    suffix = ''.join(r.choices(string.ascii_lowercase, k=r.randint(1, 3)))
    base = f"{letters}{digits}{suffix}"
    return f"tmp{base}" if enable_prefix else base


def choose_domain(domains: Sequence[str], *, rng: Optional[random.Random] = None) -> str:
    """Choose one domain from candidate pool."""
    if not domains:
        raise ValueError("domains is empty")
    r = _rng(rng)
    return r.choice(list(domains))
