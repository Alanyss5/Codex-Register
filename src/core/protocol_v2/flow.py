from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urljoin, urlparse


@dataclass
class FlowState:
    page_type: str = ""
    method: str = "GET"
    continue_url: str = ""
    current_url: str = ""
    external_url: str = ""
    callback_url: str = ""


def normalize_flow_url(url: Optional[str], auth_base: str = "https://auth.openai.com") -> str:
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return urljoin(f"{auth_base}/", text.lstrip("/"))


def _page_type_from_url(url: str) -> str:
    lowered = (url or "").lower()
    if not lowered:
        return ""
    if "localhost:1455/auth/callback" in lowered or "/auth/callback?code=" in lowered:
        return "callback"
    if "chatgpt.com" in lowered and "/api/auth/session" not in lowered:
        return "chatgpt_home"
    if "email-verification" in lowered or "email-otp" in lowered:
        return "email_otp_verification"
    if "about-you" in lowered:
        return "about_you"
    if "create-account/password" in lowered or "/u/signup/password" in lowered:
        return "create_account_password"
    if "/login" in lowered or "log-in" in lowered:
        return "login"
    return ""


def extract_flow_state(
    data: Optional[Mapping[str, Any]] = None,
    current_url: str = "",
    auth_base: str = "https://auth.openai.com",
    default_method: str = "GET",
) -> FlowState:
    payload = data or {}
    page = payload.get("page") or {}
    continue_url = normalize_flow_url(
        payload.get("continue_url")
        or payload.get("url")
        or payload.get("external_url")
        or payload.get("callback_url"),
        auth_base=auth_base,
    )
    current = normalize_flow_url(current_url or continue_url, auth_base=auth_base)
    callback_url = normalize_flow_url(payload.get("callback_url"), auth_base=auth_base)
    page_type = str(page.get("type") or payload.get("page_type") or "").strip()
    if not page_type:
        page_type = _page_type_from_url(continue_url or current or callback_url)

    external_url = ""
    target = continue_url or current or callback_url
    if target:
        host = (urlparse(target).netloc or "").lower()
        if host and "auth.openai.com" not in host:
            external_url = target

    state = FlowState(
        page_type=page_type,
        method=str(payload.get("method") or default_method or "GET").upper(),
        continue_url=continue_url,
        current_url=current,
        external_url=external_url,
        callback_url=callback_url,
    )
    if not state.current_url:
        state.current_url = state.continue_url or state.external_url or state.callback_url
    if not state.page_type:
        state.page_type = _page_type_from_url(state.current_url)
    return state


def describe_flow_state(state: FlowState) -> str:
    return (
        f"page_type={state.page_type or '-'} "
        f"method={state.method or '-'} "
        f"continue_url={state.continue_url or '-'} "
        f"current_url={state.current_url or '-'}"
    )
