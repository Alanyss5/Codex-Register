"""Dynamic proxy fetching and audited proxy resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Optional
from urllib.parse import parse_qs, urlparse


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyExitInfo:
    ip: Optional[str]
    country: Optional[str]


@dataclass(frozen=True)
class ProxyResolutionResult:
    source: str
    proxy_url: Optional[str]
    requested_proxy_url: Optional[str]
    exit_ip: Optional[str]
    exit_country: Optional[str]
    expected_country: Optional[str]
    matches_expected_country: bool
    attempts: int
    reused_proxy: bool
    error_message: str = ""

    def summary(self) -> dict:
        return {
            "source": self.source,
            "proxy_url": self.proxy_url,
            "requested_proxy_url": self.requested_proxy_url,
            "exit_ip": self.exit_ip,
            "exit_country": self.exit_country,
            "expected_country": self.expected_country,
            "matches_expected_country": self.matches_expected_country,
            "attempts": self.attempts,
            "reused_proxy": self.reused_proxy,
            "error_message": self.error_message,
        }


_REGION_ALIAS = {
    "US": "US",
    "USA": "US",
    "UNITED STATES": "US",
}


def fetch_dynamic_proxy(
    api_url: str,
    api_key: str = "",
    api_key_header: str = "X-API-Key",
    result_field: str = "",
) -> Optional[str]:
    """Fetch a dynamic proxy URL from the upstream API."""
    try:
        from curl_cffi import requests as cffi_requests

        headers = {}
        if api_key:
            headers[api_key_header] = api_key

        response = cffi_requests.get(
            api_url,
            headers=headers,
            timeout=10,
            impersonate="chrome136",
        )

        if response.status_code != 200:
            logger.warning("dynamic proxy API returned non-200: %s", response.status_code)
            return None

        text = response.text.strip()
        proxy_url = text

        if result_field or text.startswith("{") or text.startswith("["):
            try:
                import json

                data = json.loads(text)
                if result_field:
                    for key in result_field.split("."):
                        if isinstance(data, dict):
                            data = data.get(key)
                        elif isinstance(data, list) and key.isdigit():
                            data = data[int(key)]
                        else:
                            data = None
                        if data is None:
                            break
                    proxy_url = str(data).strip() if data is not None else None
                elif isinstance(data, dict):
                    for key in ("proxy", "url", "proxy_url", "data", "ip"):
                        value = data.get(key)
                        if value:
                            proxy_url = str(value).strip()
                            break
            except Exception:
                proxy_url = text

        if not proxy_url:
            logger.warning("dynamic proxy API returned an empty proxy URL")
            return None

        if not re.match(r"^(http|socks5)://", proxy_url):
            proxy_url = f"http://{proxy_url}"

        logger.info("dynamic proxy fetched: %s", proxy_url)
        return proxy_url
    except Exception as exc:
        logger.error("failed to fetch dynamic proxy: %s", exc)
        return None


def infer_expected_country(api_url: str) -> Optional[str]:
    try:
        parsed = urlparse(api_url)
        query = parse_qs(parsed.query)
        region = (query.get("region") or [""])[0].strip().upper()
        normalized = _REGION_ALIAS.get(region, region)
        if len(normalized) == 2 and normalized.isalpha() and normalized not in {"RD", "RA"}:
            return normalized
    except Exception:
        return None
    return None


def probe_proxy_exit(proxy_url: str, timeout: int = 10) -> ProxyExitInfo:
    """Probe the current proxy exit IP and country."""
    from curl_cffi import requests as cffi_requests

    proxies = {"http": proxy_url, "https": proxy_url}
    session = cffi_requests.Session(
        proxies=proxies,
        impersonate="chrome136",
        timeout=timeout,
        verify=True,
    )
    try:
        ip = None
        country = None

        try:
            response = session.get("https://cloudflare.com/cdn-cgi/trace", timeout=timeout)
            text = response.text
            ip_match = re.search(r"ip=([^\n]+)", text)
            country_match = re.search(r"loc=([A-Z]+)", text)
            ip = ip_match.group(1).strip() if ip_match else None
            country = country_match.group(1).strip() if country_match else None
        except Exception:
            pass

        if not ip:
            try:
                response = session.get("https://api.ipify.org?format=json", timeout=timeout)
                if response.status_code == 200:
                    ip = response.json().get("ip")
            except Exception:
                pass

        return ProxyExitInfo(ip=ip, country=country)
    finally:
        session.close()


def resolve_proxy_for_task(
    settings,
    previous_proxy_url: Optional[str] = None,
    max_attempts: int = 3,
) -> ProxyResolutionResult:
    """Resolve the effective proxy for the current task with exit-country auditing."""
    expected_country = infer_expected_country(getattr(settings, "proxy_dynamic_api_url", ""))

    if getattr(settings, "proxy_dynamic_enabled", False) and getattr(settings, "proxy_dynamic_api_url", ""):
        api_key_value = getattr(settings, "proxy_dynamic_api_key", "")
        if hasattr(api_key_value, "get_secret_value"):
            api_key_value = api_key_value.get_secret_value()

        last_error = ""
        for attempt in range(1, max_attempts + 1):
            requested_proxy_url = fetch_dynamic_proxy(
                api_url=settings.proxy_dynamic_api_url,
                api_key=api_key_value or "",
                api_key_header=settings.proxy_dynamic_api_key_header,
                result_field=settings.proxy_dynamic_result_field,
            )
            if not requested_proxy_url:
                last_error = "dynamic proxy fetch returned empty"
                continue

            exit_info = probe_proxy_exit(requested_proxy_url)
            matches_expected_country = not expected_country or exit_info.country == expected_country
            reused_proxy = bool(previous_proxy_url and requested_proxy_url == previous_proxy_url)

            if matches_expected_country or attempt == max_attempts:
                return ProxyResolutionResult(
                    source="dynamic",
                    proxy_url=requested_proxy_url,
                    requested_proxy_url=requested_proxy_url,
                    exit_ip=exit_info.ip,
                    exit_country=exit_info.country,
                    expected_country=expected_country,
                    matches_expected_country=matches_expected_country,
                    attempts=attempt,
                    reused_proxy=reused_proxy,
                    error_message="" if matches_expected_country else "exit country mismatch",
                )

            last_error = f"exit country mismatch: expected={expected_country} actual={exit_info.country}"

        return ProxyResolutionResult(
            source="dynamic",
            proxy_url=None,
            requested_proxy_url=None,
            exit_ip=None,
            exit_country=None,
            expected_country=expected_country,
            matches_expected_country=False,
            attempts=max_attempts,
            reused_proxy=False,
            error_message=last_error or "dynamic proxy resolution failed",
        )

    static_proxy = getattr(settings, "proxy_url", None)
    if static_proxy:
        exit_info = probe_proxy_exit(static_proxy)
        return ProxyResolutionResult(
            source="static",
            proxy_url=static_proxy,
            requested_proxy_url=static_proxy,
            exit_ip=exit_info.ip,
            exit_country=exit_info.country,
            expected_country=None,
            matches_expected_country=True,
            attempts=1,
            reused_proxy=bool(previous_proxy_url and static_proxy == previous_proxy_url),
        )

    return ProxyResolutionResult(
        source="direct",
        proxy_url=None,
        requested_proxy_url=None,
        exit_ip=None,
        exit_country=None,
        expected_country=None,
        matches_expected_country=True,
        attempts=0,
        reused_proxy=False,
    )


def get_proxy_url_for_task() -> Optional[str]:
    """Backward-compatible helper for callers that only need the proxy URL."""
    from ..config.settings import get_settings

    settings = get_settings()
    result = resolve_proxy_for_task(settings=settings)
    return result.proxy_url


def get_proxy_resolution_for_task(previous_proxy_url: Optional[str] = None) -> ProxyResolutionResult:
    """Return the full proxy resolution record for the current task."""
    from ..config.settings import get_settings

    settings = get_settings()
    return resolve_proxy_for_task(settings=settings, previous_proxy_url=previous_proxy_url)
