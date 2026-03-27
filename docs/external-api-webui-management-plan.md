# External API 网站内管理与 API Key 生成方案

本文档是**内部实现方案文档**，用于后续开发“在网站设置页直接启用/关闭 External API，并生成/管理 API Key”功能。

这不是给第三方调用方看的文档。  
第三方对接请看：

- [第三方技术对接文档](third-party-integration-guide.md)
- [External API 调用示例](external-api-examples.md)
- [Temp Mail 域名池与外部注册 API 说明](temp-mail-external-api.md)

---

## 1. 目标

在网站设置页中直接提供一块 **External API 管理区域**，让管理员可以：

- 启用 / 关闭 External API
- 查看当前是否已配置 API Key
- 一键生成新的 API Key
- 手动设置自定义 API Key
- 复制对接所需的 Base URL / Header / 文案

目标结果是：

1. 管理员不需要手动改数据库或配置文件
2. 第三方对接信息可以直接从网站界面获得
3. API Key 的展示与重置行为可控，不泄漏旧 Key

---

## 2. 当前基础

项目当前已经具备以下底座能力：

### 2.1 配置项已存在

系统配置中已经有：

- `external_api_enabled`
- `external_api_key`

### 2.2 外部鉴权已存在

当前 External API 鉴权已经生效，固定使用：

```http
X-API-Key: <api_key>
```

并读取 `external_api_enabled`、`external_api_key` 进行校验。

### 2.3 设置页架构已存在

当前设置页已经采用成熟的三层模式：

- `templates/settings.html`
- `static/js/settings.js`
- `src/web/routes/settings.py`

因此本次不是重做设置系统，而是在现有模式上新增一个“外部 API”分区。

---

## 3. 页面设计

### 3.1 新增标签页

在设置页 tabs 中新增一个标签：

- `外部 API`

该标签页内新增一张独立卡片，用于配置和展示 External API 状态。

### 3.2 页面展示内容

页面需要展示：

- `启用 External API` 开关
- 当前状态：
  - 未启用
  - 已启用但未配置 Key
  - 已启用且已配置 Key
- Base URL
- Auth Header
- 当前 Key 状态（仅掩码）
- 操作按钮

### 3.3 页面按钮

页面提供以下按钮：

- `保存设置`
- `生成新 API Key`
- `复制当前 Key`
- `复制对接信息`

说明：

- `复制当前 Key` 仅在“本次刚生成 / 本次刚手动保存新 Key”后可用
- 页面刷新后只保留掩码，不长期展示完整旧 Key

### 3.4 页面固定文案

页面上必须明确展示：

- `Base URL: ${location.origin}/api`
- `Auth Header: X-API-Key`
- “重新生成后旧 Key 立即失效”
- “第三方应先调用 /api/external/capabilities 获取 service_id 对应关系”

---

## 4. 后端接口设计

在 `src/web/routes/settings.py` 中新增一组专用接口：

- `GET /api/settings/external-api`
- `POST /api/settings/external-api`
- `POST /api/settings/external-api/generate`

### 4.1 GET /api/settings/external-api

作用：

- 返回当前 External API 状态与展示信息

返回字段：

```json
{
  "enabled": true,
  "has_api_key": true,
  "api_key_masked": "sk_live_****abcd",
  "api_key_header": "X-API-Key",
  "base_url": "http://example.com/api"
}
```

规则：

- 不返回完整旧 Key
- `base_url` 可后端返回，也可前端覆盖展示为 `${location.origin}/api`
- `api_key_header` 固定为 `X-API-Key`

### 4.2 POST /api/settings/external-api

作用：

- 保存启用状态
- 可选手动设置新的 API Key

请求体：

```json
{
  "enabled": true,
  "api_key": "custom_key_optional"
}
```

规则：

- `enabled` 必填
- `api_key` 可选
- 若 `api_key` 未传或为空字符串，则保持原值不变
- 若 `api_key` 有值，则覆盖旧 Key

返回体：

```json
{
  "success": true,
  "message": "External API settings updated",
  "generated_api_key": "only_when_new_key_is_set",
  "api_key_masked": "sk_live_****abcd"
}
```

规则：

- 只有本次确实设置了新 Key 时，才返回一次性明文
- 否则只返回掩码

### 4.3 POST /api/settings/external-api/generate

作用：

- 一键生成新 API Key

生成策略固定为：

```python
secrets.token_urlsafe(32)
```

返回体：

```json
{
  "success": true,
  "message": "External API key regenerated",
  "generated_api_key": "new_generated_key",
  "api_key_masked": "new_****mask"
}
```

规则：

- 新 Key 生成后立即覆盖旧 Key
- 旧 Key 立即失效
- 完整明文只在本次响应里返回一次

---

## 5. 前端交互规则

在 `static/js/settings.js` 中新增一组 External API 专用逻辑。

### 5.1 页面加载

设置页加载时：

1. 请求 `GET /api/settings/external-api`
2. 渲染启用状态
3. 渲染 `has_api_key`
4. 渲染掩码值
5. 渲染 Base URL / Header

### 5.2 保存行为

点击“保存设置”时：

- 如果 Key 输入框为空：
  - 仅保存 `enabled`
  - 不修改现有 Key
- 如果 Key 输入框非空：
  - 将该值作为新 Key 保存
  - 成功后前端显示本次返回的明文 Key

### 5.3 生成行为

点击“生成新 API Key”时：

1. 弹确认提示
2. 调用 `POST /api/settings/external-api/generate`
3. 成功后显示：
   - 新生成的完整 Key
   - 复制按钮
   - “旧 Key 已失效”提示

### 5.4 复制行为

#### 复制当前 Key

仅当页面内存中存在“本次刚生成 / 刚保存”的完整 Key 时启用。

#### 复制对接信息

自动拼接一段文本，包含：

- Base URL
- `X-API-Key`
- 4 个 external API 接口
- `service_id` 来自 capabilities 的说明

建议复制内容固定为：

```text
Base URL:
http://your-domain:8000/api

Auth Header:
X-API-Key: your_api_key

Available Endpoints:
GET /external/capabilities
POST /external/registration/batches
GET /external/registration/batches/{batch_uuid}
POST /external/registration/batches/{batch_uuid}/cancel

注意：
service_id 不要猜，必须来自 GET /external/capabilities 返回的 id。
```

### 5.5 Base URL 显示规则

前端展示统一使用：

```js
`${location.origin}/api`
```

这样可以兼容：

- 直接 IP 访问
- 域名访问
- 反向代理

---

## 6. 安全策略

本期实现固定采用以下策略：

### 6.1 不返回完整旧 Key

- `GET /api/settings/external-api` 不允许返回完整旧 Key
- 页面刷新后只显示掩码

### 6.2 一次性明文展示

完整明文只允许在以下场景返回一次：

- 手动设置了新 Key
- 点击生成了新 Key

### 6.3 重新生成立即失效

- 新 Key 生效后旧 Key 立即作废
- 不支持新旧 Key 并存

### 6.4 本期不做项

本期明确不做：

- 多 Key 并存
- Key 历史记录
- Key 过期时间
- Key 权限分级
- Key 审计日志
- 第三方自助申请 Key

---

## 7. 测试与验收

### 7.1 后端测试

至少覆盖以下场景：

- `GET /api/settings/external-api`
  - 未启用、未配置 Key
  - 已启用、已配置 Key
- `POST /api/settings/external-api`
  - 仅更新 `enabled`
  - 手动设置新 Key
- `POST /api/settings/external-api/generate`
  - 成功生成新 Key
- 鉴权联动
  - 新 Key 可用
  - 旧 Key 失效
  - External API 关闭后统一返回 `403`

### 7.2 前端测试

至少覆盖：

- 设置页新增“外部 API”标签
- 区块成功渲染
- 能展示 Base URL / Header / 掩码状态
- 能显示生成按钮、保存按钮、复制按钮

### 7.3 集成验收

手工验收闭环：

1. 在设置页启用 External API
2. 点击生成新 Key
3. 用新 Key 调 `/api/external/capabilities` 成功
4. 用旧 Key 调用返回 `401`
5. 关闭 External API 后调用返回 `403`

---

## 8. 实施范围与不做项

### 本次实施范围

- 网站设置页内管理 External API 开关
- 网站内生成 / 手动设置 API Key
- 网站内复制对接信息
- 与现有 External API 鉴权打通

### 本次不做

- 对接方账号体系
- 多租户 API Key
- Key 权限模型
- 调用次数限流
- WebHook / 回调机制
- API 文档自动生成页面

---

## 9. 实施结论

这是一个 **中低复杂度** 的增强项。

原因：

- 配置项已经存在
- 鉴权已经存在
- 设置页框架已经存在
- 不需要数据库迁移

因此实现重点不在底层，而在：

- 设置接口补全
- 设置页交互补全
- Key 展示与重置策略收口

按当前代码结构，适合直接在现有 settings 模块内增量实现。
