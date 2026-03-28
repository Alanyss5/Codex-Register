"""
浏览器指纹 Profile 工厂

提供多版本 Chrome 浏览器指纹配置，确保 TLS impersonate、User-Agent、
sec-ch-ua 三者版本一致，消除反检测信号。
"""

import random
from dataclasses import dataclass, field
from typing import Dict, Optional


# Chrome 版本指纹池 — impersonate 标识必须与 curl_cffi 支持的版本匹配
_CHROME_PROFILES = [
    {
        "major": 131, "impersonate": "chrome131",
        "build": 6778, "patch_range": (69, 205),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    },
    {
        "major": 133, "impersonate": "chrome133a",
        "build": 6943, "patch_range": (33, 153),
        "sec_ch_ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
    },
    {
        "major": 136, "impersonate": "chrome136",
        "build": 7103, "patch_range": (48, 175),
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    },
]


@dataclass(frozen=True)
class BrowserProfile:
    """单次会话绑定的浏览器指纹 Profile"""

    impersonate: str
    major: int
    full_version: str
    user_agent: str
    sec_ch_ua: str
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_platform: str = '"Windows"'

    # ---- 通用 sec-ch-ua 三件套 ----

    def _ch_ua_headers(self) -> Dict[str, str]:
        """返回 sec-ch-ua 三件套，Chrome 每个 HTTPS 请求默认发送。"""
        return {
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": self.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self.sec_ch_ua_platform,
        }

    # ---- 页面导航请求 headers（GET HTML 页面）----

    def navigation_headers(self, referer: Optional[str] = None) -> Dict[str, str]:
        """
        浏览器导航到页面时的 headers（如 GET auth.openai.com/oauth/authorize）。
        对应 Sec-Fetch-Mode: navigate。
        """
        headers: Dict[str, str] = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "upgrade-insecure-requests": "1",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-user": "?1",
            "user-agent": self.user_agent,
        }
        headers.update(self._ch_ua_headers())

        if referer:
            headers["referer"] = referer
            headers["sec-fetch-site"] = "cross-site"
        else:
            headers["sec-fetch-site"] = "none"

        return headers

    # ---- JSON API 请求 headers（POST auth.openai.com/api/*）----

    def json_api_headers(
        self,
        referer: str,
        origin: str = "https://auth.openai.com",
        oai_did: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        JSON API 请求的统一 headers（如 signup、password_verify、validate_otp、create_account）。
        对应 Sec-Fetch-Mode: cors。
        """
        headers: Dict[str, str] = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": origin,
            "referer": referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        headers.update(self._ch_ua_headers())

        if oai_did:
            headers["oai-device-id"] = oai_did

        return headers

    # ---- Sentinel 请求 headers ----

    def sentinel_headers(self) -> Dict[str, str]:
        """
        Sentinel PoW 请求的 headers（POST sentinel.openai.com）。
        对应跨域 CORS 请求。
        """
        headers: Dict[str, str] = {
            "origin": "https://sentinel.openai.com",
            "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
            "content-type": "text/plain;charset=UTF-8",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        headers.update(self._ch_ua_headers())
        return headers


def get_random_profile() -> BrowserProfile:
    """从 Chrome 版本池中随机选择一个 Profile，整个注册会话内保持一致。"""
    cfg = random.choice(_CHROME_PROFILES)
    major = cfg["major"]
    build = cfg["build"]
    patch = random.randint(*cfg["patch_range"])
    full_ver = f"{major}.0.{build}.{patch}"
    ua = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{full_ver} Safari/537.36"
    )
    return BrowserProfile(
        impersonate=cfg["impersonate"],
        major=major,
        full_version=full_ver,
        user_agent=ua,
        sec_ch_ua=cfg["sec_ch_ua"],
    )
