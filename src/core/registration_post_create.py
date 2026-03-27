from __future__ import annotations

from typing import Any, Callable, Dict, Optional


POST_CREATE_RESUME_SOURCE = "post_create_continue"
POST_CREATE_REENTERED_LOGIN_SOURCE = "post_create_continue_reentered_login"


def is_post_create_continue_url(url: Optional[str]) -> bool:
    candidate = str(url or "").strip().lower()
    if not candidate:
        return False
    return any(
        token in candidate
        for token in (
            "/add-phone",
            "/sign-in-with-chatgpt/",
            "/consent",
            "/workspace/select",
            "/organization/select",
        )
    )


def extract_post_create_continue_url(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str):
                candidate = value.strip()
                key_text = str(key).strip().lower()
                if key_text == "type" and candidate.lower() in {"add_phone", "add-phone"}:
                    return "https://auth.openai.com/add-phone"
                if key_text in {"continue_url", "url", "location", "redirect_url"} and is_post_create_continue_url(candidate):
                    return candidate
                if is_post_create_continue_url(candidate):
                    return candidate
            candidate = extract_post_create_continue_url(value)
            if candidate:
                return candidate
    elif isinstance(data, list):
        for item in data:
            candidate = extract_post_create_continue_url(item)
            if candidate:
                return candidate
    return None


def get_locked_post_create_continue_url(workspace_context: Dict[str, Any]) -> str:
    return str(workspace_context.get("post_create_continue_url") or "").strip()


def lock_post_create_continue_url(
    workspace_context: Dict[str, Any],
    candidate: Optional[str],
    *,
    source_name: str,
    log_fn: Callable[[str], None],
    sanitize_url: Callable[[Optional[str]], str],
) -> bool:
    candidate_text = str(candidate or "").strip()
    if not is_post_create_continue_url(candidate_text):
        return False

    current_locked = get_locked_post_create_continue_url(workspace_context)
    current_resume = str(workspace_context.get("resume_url") or "").strip()

    if current_locked == candidate_text and current_resume == candidate_text:
        workspace_context["post_create_continue_source"] = source_name
        return True

    if current_resume and current_resume != candidate_text:
        log_fn(
            "post_create_continue_overrode_cached_resume: "
            f"current={sanitize_url(current_resume) or '-'}; "
            f"new={sanitize_url(candidate_text)}"
        )

    if current_locked and current_locked != candidate_text:
        log_fn(
            "post_create_continue_upgraded: "
            f"current={sanitize_url(current_locked)}; "
            f"new={sanitize_url(candidate_text)}"
        )
    else:
        log_fn(f"post_create_continue_locked: {sanitize_url(candidate_text)}")

    workspace_context["post_create_continue_url"] = candidate_text
    workspace_context["post_create_continue_source"] = source_name
    workspace_context["continue_url"] = candidate_text
    workspace_context["resume_url"] = candidate_text
    workspace_context["resume_source"] = POST_CREATE_RESUME_SOURCE
    return True


def should_preserve_locked_post_create_continue(
    workspace_context: Dict[str, Any],
    candidate: Optional[str],
) -> bool:
    current_locked = get_locked_post_create_continue_url(workspace_context)
    if not current_locked:
        return False

    current_resume = str(workspace_context.get("resume_url") or "").strip()
    candidate_text = str(candidate or "").strip()
    if not candidate_text or candidate_text == current_resume:
        return False

    if current_resume != current_locked:
        return False

    return not is_post_create_continue_url(candidate_text)


def build_post_create_failure_details(
    *,
    locked_url: Optional[str],
    terminal_url: Optional[str],
    resolution_error: Optional[str],
    reentered_login: bool,
) -> Dict[str, Optional[str]]:
    if reentered_login:
        error_message = "账号已创建，但 post-create 续跑重新进入登录页"
        summary_reason = POST_CREATE_REENTERED_LOGIN_SOURCE
    else:
        error_message = "账号已创建，但 post-create 续跑失败"
        summary_reason = "post_create_continue_failed"

    return {
        "error_message": error_message,
        "resume_source": summary_reason,
        "summary_reason": summary_reason,
        "workspace_resolution_source": POST_CREATE_RESUME_SOURCE,
        "locked_url": str(locked_url or "").strip() or None,
        "terminal_url": str(terminal_url or "").strip() or None,
        "resolution_error": str(resolution_error or "").strip() or None,
    }
