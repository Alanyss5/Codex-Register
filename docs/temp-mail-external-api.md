# Temp Mail 域名池与外部注册 API 说明

本文档说明本项目最近新增/补强的两部分能力：

1. `temp_mail` 邮箱服务支持域名池与 Worker 域名 API
2. 外部调用方可通过 HTTP API 创建注册批次、查询状态、取消任务

配套文档：

- [External API 调用示例](external-api-examples.md)
- [External API 第三方对接速览](external-api-quickstart.md)
- [External API 网站内管理与 API Key 生成方案](external-api-webui-management-plan.md)

---

## 1. Temp Mail 域名池

### 1.1 适用场景

项目内新增了 `temp_mail` 邮箱类型，用于接入你自建的临时邮箱服务，例如：

- `https://apmail.889110.xyz/`

服务端支持通过管理接口返回当前可用域名池，例如：

- `GET /admin/domains`

请求头：

- `x-admin-auth: <admin_password>`

### 1.2 支持的配置项

`temp_mail` 服务配置支持以下字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `base_url` | 是 | 临时邮箱服务地址，如 `https://apmail.889110.xyz` |
| `admin_password` | 是 | 管理接口密码，对应请求头 `x-admin-auth` |
| `domains` | 否 | 域名池，数组或逗号分隔字符串，优先级最高 |
| `domain` | 否 | 单个回退域名 |
| `enable_prefix` | 否 | 是否保留旧版 `tmp` 前缀，默认 `false` |
| `timeout` | 否 | 请求超时，默认 `30` |
| `max_retries` | 否 | 请求重试次数，默认 `3` |

### 1.3 域名解析优先级

创建邮箱时，域名来源按以下优先级解析：

1. `config.domains`
2. Worker `GET /admin/domains`
3. `config.domain`

如果三者都拿不到可用域名，则创建邮箱失败。

### 1.4 随机邮箱生成规则

- 默认生成**纯随机**本地名，不再强制 `tmp` 前缀
- 如果配置 `enable_prefix=true`，则继续使用旧版 `tmpxxxxx` 风格
- 最终会从当前可用域名池中随机选择一个域名拼接地址

### 1.5 可用服务展示

以下两个接口会展示 `temp_mail` 的域名摘要：

- Web UI: `GET /api/registration/available-services`
- 外部 API: `GET /api/external/capabilities`

返回中会包含：

- `domain_count`
- `domains_preview`
- `domain_source`

其中 `domain_source` 可能为：

- `config_domains`
- `worker_api`
- `config_fallback`
- `none`

> 注意：接口只暴露安全摘要，不会把 `admin_password` 等敏感信息返回给前端或外部调用方。

---

## 2. 外部注册 API

### 2.1 功能概览

外部 API 允许调用方不直接操作 Web UI，而是通过 HTTP 请求：

- 查询当前可用邮箱类型与上传目标
- 创建异步注册批次
- 查询批次状态
- 取消批次

### 2.2 鉴权方式

需要在系统设置中启用：

- `external_api_enabled`
- `external_api_key`

请求头使用：

```http
X-API-Key: <your_api_key>
```

未启用时返回 `403`；密钥错误时返回 `401`。

### 2.2.1 API Key 从哪里来

当前项目里，外部 API Key 不是“调用方自己申请”的，也不是接口动态下发的。

它来自系统管理员在项目配置中设置的：

- `external_api_enabled`
- `external_api_key`

也就是说：

1. 管理员先在系统里启用外部 API
2. 管理员在系统里填写一个 `external_api_key`
3. 再把这个值发给第三方调用方

第三方真正拿到的就是这个管理员配置的值，然后放到：

```http
X-API-Key: 你拿到的那个值
```

> 当前版本没有单独的“获取 API Key / 轮换 API Key”开放接口。  
> 如果要换 Key，需要由系统管理员修改配置后，再重新发给对接方。

### 2.3 能力查询

接口：

```http
GET /api/external/capabilities
```

用于返回：

- 支持的邮箱类型
- 当前可用邮箱服务
- 当前可用上传目标
- 外部 API 是否启用

其中邮箱类型包含数据库内启用的：

- `outlook` / `moe_mail` / `temp_mail` / `duck_mail` / `freemail` / `imap_mail`

其中临时邮箱能力在外部 API 中统一使用 `temp_mail` 枚举，不再暴露旧的 `tempmail` 占位。

能力接口返回的每个可选服务，都会带：

- `id`
- `name`
- `priority`

对于 `temp_mail`，还会带：

- `domain_count`
- `domains_preview`
- `domain_source`

### 2.3.1 service_id 不是 0/1 猜出来的

第三方在创建批次时看到的：

- `email.service_id`
- `upload.service_id`

都不是让你自己猜 “0 代表哪个、1 代表哪个”。

正确方式是：

1. 先调 `GET /api/external/capabilities`
2. 读取返回里的服务列表
3. 按 `name` 找到你想用的服务
4. 把该服务对应的 `id` 填回创建请求里的 `service_id`

也就是说，真正的映射关系来自 `capabilities` 返回值，而不是文档里写死。

例如能力返回里可能是：

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

那你在创建批次时：

- 传 `email.service_id=3`，表示使用 `Worker temp mail A`
- 传 `upload.service_id=4`，表示上传到 `sub2-backup`

> 结论：`service_id` 只认 `capabilities` 当前返回的 `id`，不要手写猜测，也不要假设某个数字永远对应某个服务。

### 2.4 创建注册批次

接口：

```http
POST /api/external/registration/batches
```

请求体示例：

```json
{
  "count": 5,
  "idempotency_key": "demo-batch-001",
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
    "mode": "pipeline",
    "concurrency": 1,
    "interval_min": 5,
    "interval_max": 15
  }
}
```

字段约束：

- `count`: `1 ~ 100`
- `email.type`: 必填
- `email.service_id`: 可选
- `upload.provider`: 仅在 `upload.enabled=true` 时必填
- `upload.provider`: 仅支持 `sub2api` / `cpa` / `tm`
- `execution.mode`: `pipeline` 或 `parallel`
- `execution.concurrency`: `1 ~ 50`
- `execution.interval_max >= execution.interval_min`

关于 `service_id` 的默认行为：

- `email.service_id` 不传：系统按该邮箱类型的当前可用规则自动选
- `upload.service_id` 不传：系统按该上传 provider 当前启用且优先级最高的服务自动选

返回规则：

- 首次创建成功：HTTP `202`
- 如果 `idempotency_key` 命中历史批次：HTTP `200`

### 2.5 查询批次状态

接口：

```http
GET /api/external/registration/batches/{batch_uuid}
```

返回字段包含：

- `batch_uuid`
- `status`
- `requested_count`
- `completed_count`
- `success_count`
- `failed_count`
- `upload_success_count`
- `upload_failed_count`
- `failure_reason`
- `recent_errors`

状态可能包括：

- `pending`
- `running`
- `completed`
- `completed_partial`
- `failed`
- `cancelled`

### 2.6 取消批次

接口：

```http
POST /api/external/registration/batches/{batch_uuid}/cancel
```

规则：

- `pending` / `running` 可取消
- 已结束批次不可重复取消，返回 `400`
- 批次不存在返回 `404`

### 2.7 错误码约定

| 场景 | 状态码 |
| --- | --- |
| API 未启用 | `403` |
| API Key 错误 | `401` |
| 请求体验证失败 | `422` |
| 批次不存在 | `404` |
| 业务参数不合法 | `400` |
| 外部批次服务不可用 | `503` |

---

## 3. 外部批次执行行为

### 3.1 持久化

外部批次及其子项会持久化到数据库：

- `external_registration_batches`
- `external_registration_batch_items`

同时为每个子项生成独立 `registration_task_uuid`，便于复用现有注册执行链路。

### 3.2 幂等

如果创建批次时传入 `idempotency_key`，系统会优先复用已有批次，避免重复创建。

### 3.3 重启恢复

服务启动时会尝试恢复中断批次：

- `pending` / `running` 的外部批次会被标记为失败
- 失败原因记为 `service_restarted`

这样可以避免系统重启后外部调用方看到“永远卡在运行中”的脏状态。

### 3.4 上传行为

如果启用了上传：

- 注册成功后才会执行上传
- 会优先按注册结果里的 `email` 查账号
- 如找不到，再按 `account_id` 回退查找
- 上传结果会分别统计成功数和失败数

---

## 4. 当前测试覆盖

本轮功能已补充并验证以下覆盖：

- `temp_mail` 本地名生成与域名随机选择
- `temp_mail` 域名解析优先级与回退逻辑
- Worker 域名 API 集成路径
- `available-services` / `capabilities` 的域名摘要展示
- 外部 API 鉴权、参数校验、错误码映射
- 外部批次创建、幂等、状态汇总、取消、重启恢复
- 外部批次执行中的预失败项、上传成功/失败、`account_id` 回退查找
- 服务选择的拒绝分支与边界约束

全量测试验证结果：

- `134 passed`

---

## 5. 使用建议

### 5.1 如果你想让 `temp_mail` 自动从多个域名中随机取

建议只配置：

- `base_url`
- `admin_password`
- `domain`（仅作回退）

然后由 Worker `GET /admin/domains` 统一返回真实域名池。

### 5.2 如果你想固定只用部分域名

直接在该邮箱服务配置中填写：

```json
{
  "domains": ["a.example.com", "b.example.com"]
}
```

此时系统会优先使用配置内域名池，而不会再请求 Worker 域名接口。

### 5.3 如果你要给第三方系统对接

优先使用：

- `GET /api/external/capabilities`
- `POST /api/external/registration/batches`
- `GET /api/external/registration/batches/{batch_uuid}`
- `POST /api/external/registration/batches/{batch_uuid}/cancel`

这样调用方只负责“下发任务”和“查结果”，实际邮箱、上传、执行模式等策略仍由系统内配置统一兜底。
