"""
Helpers for OpenAI Sentinel proof-of-work tokens.

实现两步制 Sentinel 协议：
  Step 1: 发送 requirements_token 到 sentinel API，获取服务端挑战
  Step 2: 用服务端下发的 seed + difficulty 进行 FNV-1a 求解
"""

from __future__ import annotations

import base64
import json
import random
import time
import uuid
from typing import Optional, Sequence


class SentinelPOWError(RuntimeError):
    """Raised when a Sentinel proof-of-work token cannot be solved."""


# ---------------------------------------------------------------------------
# Sentinel SDK 指纹配置 — 模拟真实浏览器环境
# ---------------------------------------------------------------------------

_SCREEN_SIGNATURES = ("1920x1080", "2560x1440", "1366x768", "1536x864", "1440x900")

_NAV_PROPERTIES = (
    "vendorSub", "productSub", "vendor", "maxTouchPoints",
    "scheduling", "userActivation", "doNotTrack", "geolocation",
    "connection", "plugins", "mimeTypes", "pdfViewerEnabled",
    "webkitTemporaryStorage", "webkitPersistentStorage",
    "hardwareConcurrency", "cookieEnabled", "credentials",
    "mediaDevices", "permissions", "locks", "ink",
)

_DOCUMENT_PROPERTIES = ("location", "implementation", "URL", "documentURI", "compatMode")

_WINDOW_PROPERTIES = (
    "Object", "Function", "Array", "Number", "parseFloat", "undefined",
)

# Sentinel SDK script URL — 必须填充，真实浏览器永远不会为空
_SENTINEL_SDK_URL = "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js"


# ---------------------------------------------------------------------------
# FNV-1a 哈希 (与 OpenAI Sentinel 对齐)
# ---------------------------------------------------------------------------

def _fnv1a_32(text: str) -> str:
    """FNV-1a 32-bit hash with finalizer, 返回 8 位十六进制。"""
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    h ^= (h >> 16)
    h = (h * 2246822507) & 0xFFFFFFFF
    h ^= (h >> 13)
    h = (h * 3266489909) & 0xFFFFFFFF
    h ^= (h >> 16)
    h &= 0xFFFFFFFF
    return format(h, "08x")


# ---------------------------------------------------------------------------
# Sentinel Token Generator
# ---------------------------------------------------------------------------

class SentinelTokenGenerator:
    """对齐参考实现的 Sentinel PoW 求解器。"""

    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, user_agent: str):
        self.device_id = device_id
        self.user_agent = user_agent
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    def _get_config(self) -> list:
        """构建浏览器指纹数组，与真实 Sentinel SDK 保持一致。"""
        now_str = time.strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
            time.gmtime(),
        )
        # perf_now: 模拟 performance.now()，随机化以避免进程 uptime 泄漏
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now

        nav_prop = random.choice(_NAV_PROPERTIES)
        nav_val = f"{nav_prop}-undefined"

        return [
            random.choice(_SCREEN_SIGNATURES),        # [0]  screen
            now_str,                                    # [1]  timestamp
            4294705152,                                 # [2]  constant
            random.random(),                            # [3]  nonce (被求解循环覆盖)
            self.user_agent,                            # [4]  UA
            _SENTINEL_SDK_URL,                          # [5]  SDK script URL
            None,                                       # [6]  null
            None,                                       # [7]  null
            "en-US",                                    # [8]  language
            "en-US,en",                                 # [9]  languages → 被耗时覆盖
            random.random(),                            # [10] random
            nav_val,                                    # [11] navigator property
            random.choice(_DOCUMENT_PROPERTIES),        # [12] document property
            random.choice(_WINDOW_PROPERTIES),          # [13] window property
            perf_now,                                   # [14] performance.now()
            self.sid,                                   # [15] session ID
            "",                                         # [16] empty
            random.choice([4, 8, 12, 16]),              # [17] hardwareConcurrency
            time_origin,                                # [18] time origin
        ]

    @staticmethod
    def _base64_encode(data) -> str:
        """JSON 序列化后 base64 编码。"""
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_check(
        self, start_time: float, seed: str, difficulty: str, config: list, nonce: int
    ) -> Optional[str]:
        """单次 PoW 尝试：设置 nonce，计算哈希，检查是否满足难度。"""
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        data = self._base64_encode(config)
        hash_hex = _fnv1a_32(seed + data)
        diff_len = len(difficulty)
        if hash_hex[:diff_len] <= difficulty:
            return data + "~S"
        return None

    def generate_token(self, seed: str = None, difficulty: str = None) -> str:
        """
        FNV-1a PoW 求解。

        Args:
            seed: 服务端下发的种子（不传则用本地随机 seed）
            difficulty: 服务端下发的难度（不传则默认 "0"）

        Returns:
            gAAAAAB 前缀的 PoW 解答
        """
        seed = seed if seed is not None else self.requirements_seed
        difficulty = str(difficulty or "0")
        start_time = time.time()
        config = self._get_config()

        for i in range(self.MAX_ATTEMPTS):
            result = self._run_check(start_time, seed, difficulty, config, i)
            if result:
                return "gAAAAAB" + result

        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self) -> str:
        """
        生成初始 requirements token (gAAAAAC 前缀)。

        用于 Sentinel 两步制的第一步：告诉服务端 "我需要一个挑战"。
        """
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        data = self._base64_encode(config)
        return "gAAAAAC" + data


# ---------------------------------------------------------------------------
# 便捷函数 — 保持向后兼容
# ---------------------------------------------------------------------------

def build_sentinel_pow_token(user_agent: str, device_id: str = None) -> str:
    """
    构建 requirements token（Sentinel 两步制的第一步）。

    Returns:
        gAAAAAC 前缀的 requirements token
    """
    gen = SentinelTokenGenerator(
        device_id=device_id or str(uuid.uuid4()),
        user_agent=user_agent,
    )
    return gen.generate_requirements_token()
