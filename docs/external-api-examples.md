# External API 调用示例

本文档提供外部注册 API 的最常用调用示例，便于脚本、面板或第三方系统直接接入。

相关主说明见：

- [Temp Mail 域名池与外部注册 API 说明](temp-mail-external-api.md)

---

## 1. 基本约定

假设你的服务地址为：

```text
http://127.0.0.1:8000/api
```

假设外部 API Key 为：

```text
your_api_key
```

所有请求都需要带：

```http
X-API-Key: your_api_key
```

这个值的来源是项目管理员在系统配置里设置的 `external_api_key`。  
当前版本没有开放“申请/查询 API Key”接口，第三方只能由管理员直接发放。

---

## 2. 查询能力

### curl

```bash
curl -X GET "http://127.0.0.1:8000/api/external/capabilities" \
  -H "X-API-Key: your_api_key"
```

### Python

```python
import requests

resp = requests.get(
    "http://127.0.0.1:8000/api/external/capabilities",
    headers={"X-API-Key": "your_api_key"},
    timeout=30,
)
print(resp.status_code)
print(resp.json())
```

假设返回里有：

```json
{
  "email_types": [
    {
      "type": "temp_mail",
      "services": [
        {"id": 3, "name": "Worker temp mail A"},
        {"id": 7, "name": "Worker temp mail B"}
      ]
    }
  ],
  "upload_providers": [
    {
      "provider": "sub2api",
      "services": [
        {"id": 1, "name": "sub2-main"},
        {"id": 4, "name": "sub2-backup"}
      ]
    }
  ]
}
```

那么：

- `email.service_id=3` 就是 `Worker temp mail A`
- `email.service_id=7` 就是 `Worker temp mail B`
- `upload.service_id=1` 就是 `sub2-main`
- `upload.service_id=4` 就是 `sub2-backup`

不要自己猜数字含义，始终以 `capabilities` 返回结果为准。

---

## 3. 创建注册批次

### 3.1 使用 temp_mail，不上传

#### curl

```bash
curl -X POST "http://127.0.0.1:8000/api/external/registration/batches" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "count": 3,
    "email": {
      "type": "temp_mail"
    },
    "upload": {
      "enabled": false
    },
    "execution": {
      "mode": "pipeline",
      "concurrency": 1,
      "interval_min": 5,
      "interval_max": 10
    }
  }'
```

#### Python

```python
import requests

payload = {
    "count": 3,
    "email": {"type": "temp_mail"},
    "upload": {"enabled": False},
    "execution": {
        "mode": "pipeline",
        "concurrency": 1,
        "interval_min": 5,
        "interval_max": 10,
    },
}

resp = requests.post(
    "http://127.0.0.1:8000/api/external/registration/batches",
    headers={
        "X-API-Key": "your_api_key",
        "Content-Type": "application/json",
    },
    json=payload,
    timeout=30,
)

print(resp.status_code)
print(resp.json())
```

---

### 3.2 指定某个邮箱服务并自动上传到 sub2api

#### curl

```bash
curl -X POST "http://127.0.0.1:8000/api/external/registration/batches" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_api_key" \
  -d '{
    "count": 5,
    "idempotency_key": "batch-demo-001",
    "email": {
      "type": "temp_mail",
      "service_id": 1
    },
    "upload": {
      "enabled": true,
      "provider": "sub2api",
      "service_id": 1
    },
    "execution": {
      "mode": "parallel",
      "concurrency": 3,
      "interval_min": 0,
      "interval_max": 0
    }
  }'
```

#### Python

```python
import requests

payload = {
    "count": 5,
    "idempotency_key": "batch-demo-001",
    "email": {
        "type": "temp_mail",
        "service_id": 1,
    },
    "upload": {
        "enabled": True,
        "provider": "sub2api",
        "service_id": 1,
    },
    "execution": {
        "mode": "parallel",
        "concurrency": 3,
        "interval_min": 0,
        "interval_max": 0,
    },
}

resp = requests.post(
    "http://127.0.0.1:8000/api/external/registration/batches",
    headers={
        "X-API-Key": "your_api_key",
        "Content-Type": "application/json",
    },
    json=payload,
    timeout=30,
)

data = resp.json()
print(resp.status_code, data)
batch_uuid = data["batch_uuid"]
```

> 说明：
>
> - 首次创建一般返回 `202`
> - 如果 `idempotency_key` 命中历史批次，返回 `200`

---

### 3.3 不指定 service_id，交给系统自动选择

```python
import requests

payload = {
    "count": 2,
    "email": {
        "type": "temp_mail"
    },
    "upload": {
        "enabled": True,
        "provider": "sub2api"
    },
    "execution": {
        "mode": "pipeline",
        "concurrency": 1,
        "interval_min": 0,
        "interval_max": 0
    }
}

resp = requests.post(
    "http://127.0.0.1:8000/api/external/registration/batches",
    headers={
        "X-API-Key": "your_api_key",
        "Content-Type": "application/json",
    },
    json=payload,
    timeout=30,
)

print(resp.status_code)
print(resp.json())
```

适用场景：

- 你不关心固定某个邮箱服务
- 你不关心固定某个上传目标
- 只希望系统按当前可用配置自动兜底选择

---

## 附：`failure_category` 示例

`failure_category` 是给程序消费的稳定字段，允许值只有：

- `config`
- `business`
- `transient`
- `null`

### 示例 1：未失败批次

```json
{
  "batch_uuid": "0f8fad5b-d9cb-469f-a165-70867728950e",
  "status": "pending",
  "requested_count": 3,
  "completed_count": 0,
  "success_count": 0,
  "failed_count": 0,
  "upload_success_count": 0,
  "upload_failed_count": 0,
  "failure_reason": null,
  "failure_category": null,
  "recent_errors": [],
  "idempotent_replay": false
}
```

### 示例 2：可重试失败

```json
{
  "batch_uuid": "0f8fad5b-d9cb-469f-a165-70867728950e",
  "status": "failed",
  "requested_count": 3,
  "completed_count": 1,
  "success_count": 0,
  "failed_count": 1,
  "upload_success_count": 0,
  "upload_failed_count": 0,
  "failure_reason": "upstream timeout",
  "failure_category": "transient",
  "recent_errors": [
    "provider timeout"
  ],
  "idempotent_replay": false
}
```

### 示例 3：配置问题失败

```json
{
  "batch_uuid": "0f8fad5b-d9cb-469f-a165-70867728950e",
  "status": "failed",
  "requested_count": 3,
  "completed_count": 0,
  "success_count": 0,
  "failed_count": 0,
  "upload_success_count": 0,
  "upload_failed_count": 0,
  "failure_reason": "upload service not configured",
  "failure_category": "config",
  "recent_errors": [
    "upload provider sub2api has no configured service"
  ],
  "idempotent_replay": false
}
```

### 示例 4：cancel 响应

`POST /api/external/registration/batches/{batch_uuid}/cancel` 也可能返回 `failure_category`，只是为了和其他批次响应保持一致：

```json
{
  "batch_uuid": "0f8fad5b-d9cb-469f-a165-70867728950e",
  "status": "cancelled",
  "failure_reason": null,
  "failure_category": null
}
```

---

## 4. 查询批次状态

### curl

```bash
curl -X GET "http://127.0.0.1:8000/api/external/registration/batches/<batch_uuid>" \
  -H "X-API-Key: your_api_key"
```

### Python

```python
import requests
import time

batch_uuid = "replace-with-real-batch-uuid"

while True:
    resp = requests.get(
        f"http://127.0.0.1:8000/api/external/registration/batches/{batch_uuid}",
        headers={"X-API-Key": "your_api_key"},
        timeout=30,
    )
    data = resp.json()
    print(data)

    if data["status"] in {"completed", "completed_partial", "failed", "cancelled"}:
        break

    time.sleep(5)
```

---

## 5. 取消批次

### curl

```bash
curl -X POST "http://127.0.0.1:8000/api/external/registration/batches/<batch_uuid>/cancel" \
  -H "X-API-Key: your_api_key"
```

### Python

```python
import requests

batch_uuid = "replace-with-real-batch-uuid"

resp = requests.post(
    f"http://127.0.0.1:8000/api/external/registration/batches/{batch_uuid}/cancel",
    headers={"X-API-Key": "your_api_key"},
    timeout=30,
)

print(resp.status_code)
print(resp.json())
```

---

## 6. 常见返回示例

### 6.1 创建成功

```json
{
  "batch_uuid": "0f8fad5b-d9cb-469f-a165-70867728950e",
  "status": "pending",
  "requested_count": 3,
  "completed_count": 0,
  "success_count": 0,
  "failed_count": 0,
  "upload_success_count": 0,
  "upload_failed_count": 0,
  "failure_reason": null,
  "created_at": "2026-03-27T12:00:00",
  "started_at": null,
  "completed_at": null,
  "recent_errors": [],
  "idempotent_replay": false
}
```

### 6.2 参数错误

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body"],
      "msg": "Value error, upload.provider is required when upload.enabled is true",
      "input": {}
    }
  ]
}
```

### 6.3 批次不存在

```json
{
  "detail": "batch_not_found"
}
```

---

## 7. 最简 Python 封装示例

```python
import time
import requests


class ExternalRegisterClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key})

    def capabilities(self):
        return self.session.get(f"{self.base_url}/external/capabilities", timeout=30).json()

    def create_batch(self, payload: dict):
        return self.session.post(
            f"{self.base_url}/external/registration/batches",
            json=payload,
            timeout=30,
        ).json()

    def get_batch(self, batch_uuid: str):
        return self.session.get(
            f"{self.base_url}/external/registration/batches/{batch_uuid}",
            timeout=30,
        ).json()

    def cancel_batch(self, batch_uuid: str):
        return self.session.post(
            f"{self.base_url}/external/registration/batches/{batch_uuid}/cancel",
            timeout=30,
        ).json()

    def wait_batch(self, batch_uuid: str, interval: int = 5):
        while True:
            data = self.get_batch(batch_uuid)
            if data["status"] in {"completed", "completed_partial", "failed", "cancelled"}:
                return data
            time.sleep(interval)


client = ExternalRegisterClient("http://127.0.0.1:8000/api", "your_api_key")

batch = client.create_batch({
    "count": 2,
    "email": {"type": "temp_mail"},
    "upload": {"enabled": False},
    "execution": {"mode": "pipeline", "concurrency": 1, "interval_min": 0, "interval_max": 0},
})

result = client.wait_batch(batch["batch_uuid"])
print(result)
```
