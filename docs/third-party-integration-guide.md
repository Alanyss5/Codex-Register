# 第三方技术对接文档

这份文档是给第三方技术人员直接对接用的。  
按本文档接入即可完成：

- 查询当前可用邮箱/上传服务
- 创建注册批次
- 查询批次状态
- 取消批次

如需更详细字段说明，可再参考：

- [Temp Mail 域名池与外部注册 API 说明](temp-mail-external-api.md)
- [External API 调用示例](external-api-examples.md)
- [External API 网站内管理与 API Key 生成方案](external-api-webui-management-plan.md)

---

## 1. 对接方会拿到什么

项目方需要提供给第三方以下信息：

### 1.1 Base URL

例如：

```text
http://your-domain:8000/api
```

### 1.2 鉴权请求头

```http
X-API-Key: your_api_key
```

### 1.3 API Key 从哪里来

这个 `your_api_key` 不是第三方自己申请的，也不是通过接口获取的。

它来自项目管理员在系统中配置的：

- `external_api_enabled`
- `external_api_key`

然后由项目方把这个 Key 发给第三方使用。

> 当前版本没有开放“获取 API Key / 轮换 API Key”接口。  
> 如果更换了 Key，项目方需要重新通知第三方同步更新。

---

## 2. 对接流程总览

第三方只需要实现这 4 个接口：

1. `GET /external/capabilities`
2. `POST /external/registration/batches`
3. `GET /external/registration/batches/{batch_uuid}`
4. `POST /external/registration/batches/{batch_uuid}/cancel`

推荐接入顺序：

1. 先接 `capabilities`
2. 再接 `create batch`
3. 再接 `get batch status`
4. 最后接 `cancel batch`

---

## 3. 查询当前可用能力

### 3.1 请求

```http
GET /api/external/capabilities
X-API-Key: your_api_key
```

### 3.2 作用

这个接口用于告诉第三方当前环境里：

- 支持哪些邮箱类型
- 各邮箱类型下有哪些具体服务
- 支持哪些上传 provider
- 每个 provider 下有哪些具体服务

### 3.3 典型返回示例

```json
{
  "email_types": [
    {
      "type": "tempmail",
      "available": true,
      "count": 1,
      "services": [
        {
          "id": null,
          "name": "Tempmail.lol",
          "type": "tempmail"
        }
      ]
    },
    {
      "type": "temp_mail",
      "available": true,
      "count": 2,
      "services": [
        {
          "id": 3,
          "name": "Worker temp mail A",
          "type": "temp_mail",
          "priority": 0,
          "domain_count": 2,
          "domains_preview": ["a.example.com", "b.example.com"],
          "domain_source": "worker_api"
        },
        {
          "id": 7,
          "name": "Worker temp mail B",
          "type": "temp_mail",
          "priority": 0,
          "domain_count": 1,
          "domains_preview": ["c.example.com"],
          "domain_source": "config_domains"
        }
      ]
    }
  ],
  "upload_providers": [
    {
      "provider": "sub2api",
      "available": true,
      "count": 2,
      "services": [
        {"id": 1, "name": "sub2-main", "priority": 0},
        {"id": 4, "name": "sub2-backup", "priority": 10}
      ]
    },
    {
      "provider": "cpa",
      "available": true,
      "count": 1,
      "services": [
        {"id": 2, "name": "cpa-main", "priority": 0}
      ]
    }
  ],
  "settings": {
    "external_api_enabled": true
  }
}
```

---

## 4. `service_id` 怎么识别

这是第三方最容易误解的地方。

### 4.1 不是靠猜 0 / 1 / 2

`service_id` 不是固定编号，也不是文档里写死的顺序编号。  
它必须以当前环境里 `capabilities` 返回的实际 `id` 为准。

例如上面的返回里：

- `email.service_id = 3` 表示 `Worker temp mail A`
- `email.service_id = 7` 表示 `Worker temp mail B`
- `upload.service_id = 1` 表示 `sub2-main`
- `upload.service_id = 4` 表示 `sub2-backup`

### 4.2 正确使用方式

第三方应始终：

1. 先请求 `GET /external/capabilities`
2. 按 `type/provider + name` 找到目标服务
3. 读取对应的 `id`
4. 创建批次时把该 `id` 传给 `service_id`

### 4.3 不传 `service_id` 会怎样

如果不传 `service_id`：

- `email.service_id` 不传：系统自动选该邮箱类型当前可用服务
- `upload.service_id` 不传：系统自动选该上传 provider 当前启用且优先级最高的服务

如果第三方不需要固定某个具体服务，建议直接不传。

---

## 5. 创建注册批次

### 5.1 请求

```http
POST /api/external/registration/batches
Content-Type: application/json
X-API-Key: your_api_key
```

### 5.2 请求体示例：最简版本

```json
{
  "count": 10,
  "email": {
    "type": "temp_mail"
  },
  "upload": {
    "enabled": false
  },
  "execution": {
    "mode": "pipeline",
    "concurrency": 1,
    "interval_min": 0,
    "interval_max": 0
  }
}
```

### 5.3 请求体示例：指定邮箱服务并上传

```json
{
  "count": 20,
  "idempotency_key": "batch-20260327-001",
  "email": {
    "type": "temp_mail",
    "service_id": 3
  },
  "upload": {
    "enabled": true,
    "provider": "sub2api",
    "service_id": 1
  },
  "execution": {
    "mode": "parallel",
    "concurrency": 5,
    "interval_min": 0,
    "interval_max": 0
  }
}
```

### 5.4 字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `count` | 是 | 注册数量，范围 `1 ~ 100` |
| `idempotency_key` | 否 | 幂等键，建议传 |
| `email.type` | 是 | 邮箱类型，如 `temp_mail` / `tempmail` / `outlook` |
| `email.service_id` | 否 | 指定某个邮箱服务，来源于 `capabilities` |
| `upload.enabled` | 是 | 是否上传 |
| `upload.provider` | 条件必填 | `upload.enabled=true` 时必填，支持 `sub2api` / `cpa` / `tm` |
| `upload.service_id` | 否 | 指定某个上传服务，来源于 `capabilities` |
| `execution.mode` | 否 | `pipeline` 或 `parallel` |
| `execution.concurrency` | 否 | 并发数，范围 `1 ~ 50` |
| `execution.interval_min` | 否 | 最小间隔秒数 |
| `execution.interval_max` | 否 | 最大间隔秒数，必须 `>= interval_min` |

### 5.5 返回说明

- 首次成功创建：HTTP `202`
- 如果 `idempotency_key` 命中已有批次：HTTP `200`

返回里会带：

```json
{
  "batch_uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "status": "pending",
  "requested_count": 20,
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

第三方需要保存：

- `batch_uuid`

后续查询和取消都要用它。

---

## 6. 查询批次状态

### 6.1 请求

```http
GET /api/external/registration/batches/{batch_uuid}
X-API-Key: your_api_key
```

### 6.2 第三方重点关注字段

- `status`
- `requested_count`
- `completed_count`
- `success_count`
- `failed_count`
- `upload_success_count`
- `upload_failed_count`
- `failure_reason`
- `recent_errors`

### 6.3 终态定义

以下状态视为任务结束：

- `completed`
- `completed_partial`
- `failed`
- `cancelled`

建议第三方以这几个状态作为轮询终止条件。

### 6.4 推荐理解方式

如果请求注册 20 个账号：

- `success_count=20`：全部成功
- `success_count=17, failed_count=3`：部分成功
- `success_count=0, failed_count=20`：全部失败

不要要求系统必须 100% 成功；应根据成功数和失败数做业务判断。

---

## 7. 取消批次

### 7.1 请求

```http
POST /api/external/registration/batches/{batch_uuid}/cancel
X-API-Key: your_api_key
```

### 7.2 规则

- `pending` / `running` 可取消
- 已结束批次再次取消会返回 `400`
- 批次不存在返回 `404`

---

## 8. 错误码说明

| 状态码 | 含义 | 建议处理 |
| --- | --- | --- |
| `401` | API Key 错误 | 检查 `X-API-Key` |
| `403` | 外部 API 未启用 / Key 未配置 | 联系项目方 |
| `404` | 批次不存在 | 检查 `batch_uuid` |
| `422` | 请求体校验失败 | 修正 JSON 结构 |
| `400` | 业务参数错误 | 检查 `service_id` / provider / 状态 |
| `503` | 服务暂时不可用 | 稍后重试 |

---

## 9. 第三方最小实现建议

第三方系统至少实现以下逻辑：

### 9.1 初始化

- 保存 `Base URL`
- 保存 `X-API-Key`

### 9.2 下发任务前

- 调一次 `GET /external/capabilities`
- 读取当前可用邮箱类型
- 如需固定服务，读取对应 `service_id`

### 9.3 创建任务

- 调 `POST /external/registration/batches`
- 保存返回的 `batch_uuid`

### 9.4 查询任务

- 周期轮询 `GET /external/registration/batches/{batch_uuid}`
- 直到进入终态

### 9.5 终态处理

- `completed`：当作成功
- `completed_partial`：按成功数/失败数决定业务处理
- `failed`：整体失败
- `cancelled`：任务已取消

---

## 10. 推荐对接原则

### 原则 1：永远先读 `capabilities`

不要在第三方代码里写死：

- 邮箱类型
- 上传 provider
- `service_id`

### 原则 2：建议总是传 `idempotency_key`

这样上游超时重试时，不会重复创建同一批次。

### 原则 3：如果不关心具体服务，就不要传 `service_id`

直接让系统自动选，能减少耦合。

### 原则 4：把结果当作“批次结果”处理

第三方调用的是批次任务，不是单账号实时接口。  
请以批次维度读取：

- 请求数量
- 成功数量
- 失败数量
- 上传成功/失败数量

---

## 11. 一段可以直接发给技术对接方的话

你可以直接把下面这段发给对方：

```text
对接地址：
http://your-domain:8000/api

认证方式：
请求头增加：
X-API-Key: your_api_key

先调用：
GET /external/capabilities
用来获取当前可用邮箱类型、上传 provider，以及每个服务对应的 service_id 和 name。

再调用：
POST /external/registration/batches
创建注册批次。

查询状态：
GET /external/registration/batches/{batch_uuid}

取消任务：
POST /external/registration/batches/{batch_uuid}/cancel

注意：
1. service_id 不要猜，必须来自 capabilities 返回
2. 如果不指定 service_id，系统会自动选择可用服务
3. API Key 由项目方提供，不是通过接口申请
```
