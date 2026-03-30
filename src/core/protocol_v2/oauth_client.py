from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


class OAuthProtocolClient:
    """Minimal OAuth fallback client for existing-account login paths."""

    def __init__(self, http_client, callback_logger=None):
        self.http_client = http_client
        self.callback_logger = callback_logger or (lambda message: None)

    def _log(self, message: str) -> None:
        self.callback_logger(message)

    def login_and_get_tokens(
        self,
        email: str,
        password: str,
        mailbox_client=None,
    ) -> Tuple[bool, Dict[str, Any] | str]:
        self._log(f"OAuth fallback not implemented for {email}")
        return False, "oauth fallback not implemented"
