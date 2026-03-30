"""Session-scoped persona and fingerprint generation for browser registration."""

from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from ..openai.browser_profile import BrowserProfile, get_random_profile


@dataclass(frozen=True)
class ScreenProfile:
    width: int
    height: int
    avail_width: int
    avail_height: int
    color_depth: int = 24
    pixel_depth: int = 24


@dataclass(frozen=True)
class SessionPersona:
    country: str
    locale: str
    accept_language: str
    timezone_id: str
    profile: BrowserProfile
    screen: ScreenProfile
    platform: str
    vendor: str
    architecture: str
    bitness: str
    device_pixel_ratio: float
    hardware_concurrency: int
    device_memory: int
    max_touch_points: int
    webgl_vendor: str
    webgl_renderer: str
    canvas_seed: int
    audio_seed: int
    fonts_bucket: str

    @property
    def user_agent(self) -> str:
        return self.profile.user_agent

    def user_agent_metadata(self) -> Dict[str, Any]:
        brands: List[Dict[str, str]] = []
        for brand, version in re.findall(r'"([^"]+)";v="([^"]+)"', self.profile.sec_ch_ua):
            brands.append({"brand": brand, "version": version})

        return {
            "brands": brands,
            "fullVersion": self.profile.full_version,
            "platform": "Windows",
            "platformVersion": self.profile.sec_ch_ua_platform_version.strip('"') or "10.0.0",
            "architecture": self.architecture,
            "bitness": self.bitness,
            "model": "",
            "mobile": False,
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "country": self.country,
            "locale": self.locale,
            "accept_language": self.accept_language,
            "timezone_id": self.timezone_id,
            "platform": self.platform,
            "vendor": self.vendor,
            "screen": f"{self.screen.width}x{self.screen.height}",
            "device_pixel_ratio": self.device_pixel_ratio,
            "hardware_concurrency": self.hardware_concurrency,
            "device_memory": self.device_memory,
            "max_touch_points": self.max_touch_points,
            "user_agent": self.profile.user_agent,
            "impersonate": self.profile.impersonate,
            "webgl_vendor": self.webgl_vendor,
            "webgl_renderer": self.webgl_renderer,
            "fonts_bucket": self.fonts_bucket,
        }

    def init_script(self) -> str:
        languages = [self.locale]
        if "-" in self.locale:
            languages.append(self.locale.split("-", 1)[0])

        payload = {
            "platform": self.platform,
            "vendor": self.vendor,
            "language": self.locale,
            "languages": languages,
            "hardwareConcurrency": self.hardware_concurrency,
            "deviceMemory": self.device_memory,
            "userAgent": self.user_agent,
            "maxTouchPoints": self.max_touch_points,
            "screen": asdict(self.screen),
            "devicePixelRatio": self.device_pixel_ratio,
            "webglVendor": self.webgl_vendor,
            "webglRenderer": self.webgl_renderer,
            "canvasSeed": self.canvas_seed,
            "audioSeed": self.audio_seed,
            "fontsBucket": self.fonts_bucket,
            "userAgentMetadata": self.user_agent_metadata(),
        }
        payload_json = json.dumps(payload, ensure_ascii=False)

        return f"""
(() => {{
  const cfg = {payload_json};
  const define = (obj, key, value) => {{
    try {{
      Object.defineProperty(obj, key, {{ get: () => value, configurable: true }});
    }} catch (e) {{}}
  }};

  define(Navigator.prototype, 'webdriver', undefined);
  define(Navigator.prototype, 'platform', cfg.platform);
  define(Navigator.prototype, 'vendor', cfg.vendor);
  define(Navigator.prototype, 'language', cfg.language);
  define(Navigator.prototype, 'languages', cfg.languages);
  define(Navigator.prototype, 'hardwareConcurrency', cfg.hardwareConcurrency);
  define(Navigator.prototype, 'deviceMemory', cfg.deviceMemory);
  define(Navigator.prototype, 'userAgent', cfg.userAgent);
  define(Navigator.prototype, 'maxTouchPoints', cfg.maxTouchPoints);
  define(window, 'devicePixelRatio', cfg.devicePixelRatio);

  define(screen, 'width', cfg.screen.width);
  define(screen, 'height', cfg.screen.height);
  define(screen, 'availWidth', cfg.screen.avail_width);
  define(screen, 'availHeight', cfg.screen.avail_height);
  define(screen, 'colorDepth', cfg.screen.color_depth);
  define(screen, 'pixelDepth', cfg.screen.pixel_depth);

  if (!window.chrome) {{
    Object.defineProperty(window, 'chrome', {{
      value: {{ runtime: {{}}, app: {{}}, csi: () => ({{}}), loadTimes: () => ({{}}) }},
      configurable: true,
    }});
  }}

  define(Navigator.prototype, 'userAgentData', {{
    brands: cfg.userAgentMetadata.brands,
    mobile: false,
    platform: cfg.userAgentMetadata.platform,
    getHighEntropyValues: async () => cfg.userAgentMetadata,
    toJSON: () => cfg.userAgentMetadata,
  }});

  const fakePluginArray = [
    {{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }},
    {{ name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' }},
    {{ name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }},
  ];
  define(Navigator.prototype, 'plugins', fakePluginArray);
  define(Navigator.prototype, 'mimeTypes', [{{ type: 'application/pdf' }}]);

  const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (originalQuery) {{
    window.navigator.permissions.query = (parameters) => (
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({{ state: Notification.permission }})
        : originalQuery(parameters)
    );
  }}

  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(parameter) {{
    if (parameter === 37445) return cfg.webglVendor;
    if (parameter === 37446) return cfg.webglRenderer;
    return getParameter.call(this, parameter);
  }};
}})();
""".strip()


_US_SCREENS: List[ScreenProfile] = [
    ScreenProfile(1366, 768, 1366, 728),
    ScreenProfile(1440, 900, 1440, 860),
    ScreenProfile(1536, 864, 1536, 824),
    ScreenProfile(1920, 1080, 1920, 1040),
]

_HARDWARE_PROFILES = [
    (4, 4, 0, 1.0),
    (8, 8, 0, 1.0),
    (8, 8, 1, 1.25),
    (12, 8, 0, 1.25),
    (16, 16, 0, 1.5),
]

_WEBGL_PROFILES = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0)"),
]

_TIMEZONE_MAP = {
    "US": ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"],
    "CA": ["America/Toronto", "America/Vancouver"],
    "GB": ["Europe/London"],
    "DE": ["Europe/Berlin"],
    "FR": ["Europe/Paris"],
    "JP": ["Asia/Tokyo"],
    "KR": ["Asia/Seoul"],
    "SG": ["Asia/Singapore"],
}


def _build_accept_language(locale: str) -> str:
    if "-" in locale:
        base = locale.split("-", 1)[0]
        return f"{locale},{base};q=0.9"
    return f"{locale},en;q=0.9"


def build_persona(runtime_country: Optional[str], runtime_language: str) -> SessionPersona:
    country = (runtime_country or "US").upper()
    locale = runtime_language or "en-US"
    if country == "US" and locale == "en":
        locale = "en-US"

    profile = get_random_profile()
    screen = random.choice(_US_SCREENS)
    hardware_concurrency, device_memory, max_touch_points, dpr = random.choice(_HARDWARE_PROFILES)
    webgl_vendor, webgl_renderer = random.choice(_WEBGL_PROFILES)
    timezone_options = _TIMEZONE_MAP.get(country, ["America/New_York" if locale.startswith("en") else "UTC"])
    timezone_id = random.choice(timezone_options)

    return SessionPersona(
        country=country,
        locale=locale,
        accept_language=_build_accept_language(locale),
        timezone_id=timezone_id,
        profile=profile,
        screen=screen,
        platform="Win32",
        vendor="Google Inc.",
        architecture=profile.sec_ch_ua_arch.strip('"') or "x86",
        bitness=profile.sec_ch_ua_bitness.strip('"') or "64",
        device_pixel_ratio=dpr,
        hardware_concurrency=hardware_concurrency,
        device_memory=device_memory,
        max_touch_points=max_touch_points,
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
        canvas_seed=random.randint(10_000, 999_999),
        audio_seed=random.randint(10_000, 999_999),
        fonts_bucket="windows-modern",
    )
