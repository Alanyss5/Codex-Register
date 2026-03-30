"""
LuckMail OpenAPI 兼容客户端。

用于在外部 `luckmail` SDK 不存在时，直接基于官方 API 文档提供最小可用实现。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urljoin

from ..core.http_client import HTTPClient, HTTPClientError, RequestConfig


class LuckMailAPIError(Exception):
    """LuckMail API 请求失败。"""


def _to_namespace(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_namespace(item) for item in value]
    return value


def _compact_mapping(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not data:
        return {}
    return {
        key: value
        for key, value in data.items()
        if value is not None and value != ""
    }


class _LuckMailUserClient:
    def __init__(self, client: "LuckMailClient"):
        self._client = client

    def get_balance(self):
        data = self._client._request_json("GET", "/balance")
        return _to_namespace(data or {})

    def get_orders(self, page: int = 1, page_size: int = 20, **kwargs):
        params = _compact_mapping({"page": page, "page_size": page_size, **kwargs})
        data = self._client._request_json("GET", "/orders", params=params)
        return _to_namespace(data or {})

    def create_appeal(self, **payload):
        data = self._client._request_json("POST", "/appeal/create", json=_compact_mapping(payload))
        return _to_namespace(data or {})

    def get_purchases(self, page: int = 1, page_size: int = 20, **kwargs):
        params = _compact_mapping({"page": page, "page_size": page_size, **kwargs})
        data = self._client._request_json("GET", "/email/purchases", params=params)
        return _to_namespace(data or {})

    def create_order(self, **payload):
        data = self._client._request_json("POST", "/order/create", json=_compact_mapping(payload))
        return _to_namespace(data or {})

    def purchase_emails(self, **payload):
        data = self._client._request_json("POST", "/email/purchase", json=_compact_mapping(payload))
        return _to_namespace(data or {})

    def get_token_code(self, token: str):
        data = self._client._request_json(
            "GET",
            f"/email/token/{token}/code",
            require_auth=False,
        )
        return _to_namespace(data or {})

    def get_token_mails(self, token: str):
        data = self._client._request_json(
            "GET",
            f"/email/token/{token}/mails",
            require_auth=False,
        )
        return _to_namespace(data or {})

    def get_order_code(self, order_no: str):
        data = self._client._request_json("GET", f"/order/{order_no}/code")
        return _to_namespace(data or {})

    def set_purchase_disabled(self, purchase_id: int, disabled: int):
        data = self._client._request_json(
            "PUT",
            f"/email/purchases/{purchase_id}/disabled",
            json={"disabled": int(disabled)},
        )
        return _to_namespace(data or {})

    def cancel_order(self, order_no: str):
        data = self._client._request_json("POST", f"/order/{order_no}/cancel")
        return _to_namespace(data or {})


class LuckMailClient:
    """基于 LuckMail OpenAPI 的最小客户端实现。"""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30, max_retries: int = 3):
        if not base_url:
            raise ValueError("LuckMailClient 缺少 base_url")
        if not api_key:
            raise ValueError("LuckMailClient 缺少 api_key")

        self.base_url = str(base_url).rstrip("/") + "/"
        self.api_key = str(api_key).strip()
        self.http_client = HTTPClient(
            proxy_url=None,
            config=RequestConfig(
                timeout=max(int(timeout or 30), 1),
                max_retries=max(int(max_retries or 3), 1),
            ),
        )
        self.user = _LuckMailUserClient(self)

    def _build_url(self, path: str) -> str:
        normalized = "api/v1/openapi/" + str(path or "").lstrip("/")
        return urljoin(self.base_url, normalized)

    def _auth_headers(self, require_auth: bool) -> Iterable[Dict[str, str]]:
        if not require_auth:
            yield {}
            return
        yield {"X-API-Key": self.api_key}
        yield {"Authorization": f"Bearer {self.api_key}"}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        require_auth: bool = True,
    ) -> Any:
        url = self._build_url(path)
        last_error: Optional[Exception] = None

        for headers in self._auth_headers(require_auth):
            try:
                response = self.http_client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                )
            except HTTPClientError as exc:
                last_error = exc
                continue

            try:
                payload = response.json()
            except Exception as exc:
                raise LuckMailAPIError(f"LuckMail 返回了无法解析的响应: {exc}") from exc

            code = payload.get("code", 0) if isinstance(payload, dict) else 0
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("message") or "").strip()

            unauthorized = response.status_code in (401, 403) or code in (1002, 1003)
            if require_auth and unauthorized:
                last_error = LuckMailAPIError(message or f"认证失败: HTTP {response.status_code}")
                continue

            if response.status_code >= 400:
                raise LuckMailAPIError(message or f"LuckMail 请求失败: HTTP {response.status_code}")

            if isinstance(payload, dict) and code not in (0, None):
                raise LuckMailAPIError(message or f"LuckMail 接口返回错误码: {code}")

            if isinstance(payload, dict):
                return payload.get("data")
            return payload

        if last_error is not None:
            raise last_error
        raise LuckMailAPIError("LuckMail 请求失败")
