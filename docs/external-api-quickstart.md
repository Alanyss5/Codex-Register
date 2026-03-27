# External API 第三方对接速览

如果你是第三方调用方，只需要记住下面这套最小流程。

详细字段说明见：

- [Temp Mail 域名池与外部注册 API 说明](temp-mail-external-api.md)
- [External API 调用示例](external-api-examples.md)

---

## 1. 先查能力

调用：

```http
GET /api/external/capabilities
```

目的：

- 看当前支持哪些邮箱类型
- 看有哪些上传目标可用
- 看 `temp_mail` 是否有可用服务

你要重点关注返回中的：

- `email_types[].type`
- `email_types[].services[].id`
- `email_types[].services[].name`
- `upload_providers[].provider`
- `upload_providers[].services[].id`
- `upload_providers[].services[].name`

示例：

```json
{
  "email_types": [
    {
      "type": "temp_mail",
      "available": true,
      "services": [
        {"id": 3, "name": "Worker temp mail A", "priority": 0},
        {"id": 7, "name": "Worker temp mail B", "priority": 0}
      ]
    }
  ],
  "upload_providers": [
    {
      "provider": "sub2api",
      "available": true,
      "services": [
        {"id": 1, "name": "sub2-main", "priority": 0},
        {"id": 4, "name": "sub2-backup", "priority": 10}
      ]
    }
  ]
}
```

这时：

- `email.service_id=3` 表示选 `Worker temp mail A`
- `email.service_id=7` 表示选 `Worker temp mail B`
- `upload.service_id=1` 表示选 `sub2-main`
- `upload.service_id=4` 表示选 `sub2-backup`

> 不是 0/1 猜出来的，而是以 `capabilities` 当前返回的 `id + name` 为准。

---

## 2. 再创建批次

调用：

```http
POST /api/external/registration/batches
```

你只需要决定 4 件事：

1. 要注册多少个：`count`
2. 用哪种邮箱：`email.type`
3. 是否上传：`upload.enabled`
4. 怎么执行：`execution.mode`

最小示例：

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

如果你不传：

- `email.service_id`
- `upload.service_id`

则系统会自动选择当前可用服务。

如果你想固定某一个邮箱服务或上传目标，就必须先通过 `capabilities` 拿到对应 `id`。

---

## 3. 保存 `batch_uuid`

创建成功后，响应里会返回：

```json
{
  "batch_uuid": "..."
}
```

后续所有查询和取消都依赖这个值。

---

## 4. 轮询状态

调用：

```http
GET /api/external/registration/batches/{batch_uuid}
```

你只需要关注：

- `status`
- `requested_count`
- `completed_count`
- `success_count`
- `failed_count`
- `upload_success_count`
- `upload_failed_count`
- `recent_errors`

终态只有这几个：

- `completed`
- `completed_partial`
- `failed`
- `cancelled`

---

## 5. 如需终止就取消

调用：

```http
POST /api/external/registration/batches/{batch_uuid}/cancel
```

仅 `pending` / `running` 批次可以取消。

---

## 6. 推荐调用策略

### 推荐 1：始终传 `idempotency_key`

这样你的上游系统在重试创建请求时，不会重复创建同一批任务。

### 推荐 2：先看 `capabilities`，再决定参数

不要写死：

- 邮箱类型
- 上传 provider
- service_id

否则后台一改配置，你的调用方就会直接打空。

尤其不要假设：

- `1` 永远是某个邮箱
- `1` 永远是某个上传渠道

`service_id` 的意义完全取决于当前环境里的实际配置。

### 推荐 3：把“成功标准”定义成批次维度

不要要求“必须全部成功”。

更合理的是读取：

- `success_count`
- `failed_count`

再由你的系统决定是否接受这次结果。

---

## 7. 常见错误处理

| 状态码 | 含义 | 建议处理 |
| --- | --- | --- |
| `401` | API Key 错误 | 检查请求头 |
| `403` | 外部 API 未启用 | 联系后台启用 |
| `404` | 批次不存在 | 检查 `batch_uuid` |
| `422` | 请求体格式不合法 | 修正参数结构 |
| `400` | 业务参数非法 | 修正业务参数 |
| `503` | 服务暂时不可用 | 稍后重试 |

---

## 8. 第三方最小接入步骤

建议接入顺序：

1. 接入 `GET /capabilities`
2. 接入 `POST /batches`
3. 接入 `GET /batches/{batch_uuid}`
4. 最后再接 `POST /cancel`

这样最快能形成闭环。

---

## 9. 一句话理解

外部调用方只负责：

- 下发批次
- 轮询状态
- 读取成功/失败统计

系统内部负责：

- 选邮箱
- 真正执行注册
- 上传账号
- 失败回收与重启恢复

---

## 10. API Key 从哪里拿

外部调用方使用的：

```http
X-API-Key: 你的APIKey
```

这个值不是通过接口获取的，而是由系统管理员在本项目配置里设置后，直接发给对接方。

也就是说：

- 对接方自己拿不到
- 必须由项目维护者提供
- 如果项目维护者修改了 Key，对接方也要同步更新请求头
