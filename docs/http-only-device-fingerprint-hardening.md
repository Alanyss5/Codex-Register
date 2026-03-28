# HTTP-Only 设备指纹补强建议

本文档汇总三类信息后的统一结论，用于指导当前项目在**不引入真实浏览器兜底**前提下，继续补强 OpenAI 注册链路的稳定性：

1. 当前项目现状（`register_198.py`、`src_23_extracted/*`）
2. `F:\MyTools` 中可借鉴的实现
3. 联网查到的公开资料、官方说明与公开逆向项目

---

## 1. 范围与目标

本方案只讨论 **HTTP-only 注册链路**，不讨论：

- AdsPower / 外部浏览器
- Playwright / Selenium 真实浏览器兜底
- 完整 JS 浏览器指纹复刻

目标是：

- 提高注册链路的一致性
- 降低 401 / challenge / callback 异常概率
- 在现有 `curl_cffi` + OpenAI 专用挑战框架上继续增强

---

## 2. 统一结论

### 2.1 当前项目已经做对的部分

当前项目已经具备以下基础能力：

- `curl_cffi impersonate` 的浏览器网络栈拟态
- 基础 Chrome 风格请求头
- 先获取 `oai-did`
- Sentinel PoW 求解
- `openai-sentinel-token`
- 部分 OAuth JSON 请求里的 `oai-device-id`
- 部分链路里的 `traceparent` / `x-datadog-*`
- 会话内尽量复用已有 `oai-did`

结论：当前项目不是裸接口脚本，已经具备继续补强的基础。

### 2.2 官方与公开资料给出的方向

- `curl_cffi` 适合做 **TLS / HTTP2 / JA3 / 浏览器网络层拟态**
- 但 `curl_cffi` **不能解决 JS 浏览器指纹**
- OpenAI 官方网络建议明确涉及：
  - `auth.openai.com`
  - `chatgpt.com`
  - `challenges.cloudflare.com`
  - `rum.browser-intake-datadoghq.com`
- 公开逆向项目显示，实际链路可能还会涉及：
  - `chat-requirements`
  - turnstile / challenge
  - 前端行为 / 埋点一致性

结论：既然不走真实浏览器，就必须把 **HTTP-only 的一致性** 做到尽量完整。

### 2.3 `F:\MyTools` 最值得借鉴的点

`F:\MyTools` 中真正高价值的不是“更多随机”，而是：

1. **整套浏览器指纹绑定为统一 profile**
   - `impersonate`
   - `User-Agent`
   - `sec-ch-ua`
   - `sec-ch-ua-full-version`
   - platform/version 类字段

2. **`oai-did` 同步到 `chatgpt.com` 与 `auth.openai.com`**

3. **Sentinel 按 flow 生成**
   - `authorize_continue`
   - `password_verify`

4. **Sentinel 请求本身补充 client hints**

5. **OAuth 阶段附带更完整的前端语义参数**
   - `ext-oai-did`
   - `auth_session_logging_id`

结论：最该借鉴的是 **一致性设计**，不是整包搬代码。

---

## 3. 应优先补什么

以下按优先级排序。

### P0：必须补

#### 3.1 统一 session 级浏览器 profile

为每个注册任务固定一份浏览器 profile，至少统一以下字段：

- `impersonate`
- `User-Agent`
- `Accept-Language`
- `sec-ch-ua`
- `sec-ch-ua-mobile`
- `sec-ch-ua-platform`
- `sec-ch-ua-full-version`
- `sec-ch-ua-platform-version`

要求：

- 同一次注册任务从头到尾固定
- 所有关键请求共用，不允许不同阶段各自拼装

原因：

- 当前最大的风险之一不是“缺 header”，而是**同一会话像多个不同浏览器**

#### 3.2 `oai-did` 跨域同步

原则：

- 优先使用服务端返回的 `oai-did`
- 将同一个 did 同步到：
  - `chatgpt.com`
  - `.auth.openai.com`
  - `auth.openai.com`

原因：

- 注册链路天然跨域
- did 只存在单一域时，容易出现链路不一致

#### 3.3 Sentinel 改为按 flow 生成

至少区分以下 flow：

- `authorize_continue`
- `password_verify`

必要时再扩展后续 flow。

原因：

- `F:\MyTools` 与公开实现都表明，不同认证阶段不应简单复用同一类 sentinel 语义

#### 3.4 给 Sentinel 请求补 client hints

Sentinel 请求应与本次任务 profile 保持一致，至少补：

- `sec-ch-ua`
- `sec-ch-ua-mobile`
- `sec-ch-ua-platform`

原因：

- 当前项目的 sentinel 请求仍偏简化
- 这一步最像“浏览器前端发出的挑战请求”

---

### P1：建议补

#### 3.5 统一 auth JSON 请求 header builder

把以下内容统一注入到所有关键 auth JSON 请求：

- `oai-device-id`
- `traceparent`
- `tracestate`
- `x-datadog-*`

不要只在部分接口补，建议统一覆盖：

- authorize / continue
- password verify
- workspace select
- organization select
- 其他关键 auth.openai.com JSON 接口

#### 3.6 灰度评估 `ext-oai-did` / `auth_session_logging_id`

建议：

- 先做可开关的灰度支持
- 不作为硬依赖
- 通过日志观察引入后是否改善成功率

原因：

- 这两个参数更像“前端语义拟真增强”
- 有价值，但存在随上游前端变动而漂移的风险

#### 3.7 降低“本地伪造 did”权重

原则：

- 服务端 did 为主
- 本地生成 did 只做最后 fallback
- fallback 后整条链路也必须固定复用同一个 did

---

### P2：运维层同样重要

#### 3.8 把网络环境视为指纹系统的一部分

建议：

- 避免 TLS 解密代理
- 优先稳定、干净的出口
- 同一次注册链路尽量固定一个代理出口
- 尽量避免频繁切换 IP

原因：

- HTTP-only 模式下，网络环境本身就是风控输入

---

## 4. 当前不建议做什么

### 4.1 不建议继续堆“随机”

例如：

- 随机 UA 但不绑定 `sec-ch-*`
- 随机 Chrome 版本但 `impersonate` 不匹配
- 请求头在不同阶段各自手搓

这些通常会让链路更不像一个真实浏览器。

### 4.2 不建议整包照搬 `F:\MyTools` vendor 注册器

原因：

- vendor 代码混有特定时期的 endpoint / 参数假设
- 当前项目已有自己修好的流程
- 直接搬迁容易回归

正确方式：

- 抽取机制
- 按模块吸收

### 4.3 不建议走半吊子的“浏览器仿真”

既然本方案明确不引入真实浏览器，就不要把目标设成完整 JS 指纹复刻。

正确目标应是：

- 最大化 HTTP-only 一致性
- 尽量贴近同一浏览器会话发出的请求

---

## 5. 推荐的落地顺序

建议按以下顺序推进：

### 第一批

1. 统一 session 级浏览器 profile
2. `oai-did` 跨域同步
3. `password_verify` 增加 flow-specific sentinel
4. sentinel 请求补 `sec-ch-*`

### 第二批

5. 统一 auth JSON 请求 header builder
6. 扩大 `oai-device-id + trace/datadog` 覆盖范围
7. 灰度引入 `ext-oai-did` / `auth_session_logging_id`

### 第三批

8. 结合线上日志做结果回归：
   - 401 发生在哪一步
   - callback 失败集中在哪一步
   - did fallback 是否明显更容易失败
   - 哪类代理出口成功率更高

---

## 6. 当前最推荐优先讨论的补齐项

如果只选最值得先做的几项，我建议优先讨论：

1. **统一 session 级浏览器 profile**
2. **did 跨 `chatgpt.com / auth.openai.com` 同步**
3. **`password_verify` 增加 flow-specific sentinel**
4. **sentinel 请求补 client hints**
5. **统一 auth JSON 请求 header builder**

---

## 7. 参考来源

- `curl_cffi` impersonate 文档  
  https://curl-cffi.readthedocs.io/en/v0.11.4/impersonate.html

- OpenAI 官方网络建议  
  https://help.openai.com/en/articles/9247338-network-recommendations-for-chatgpt-errors-on-web-and-apps

- 公开逆向参考  
  https://github.com/realasfngl/ChatGPT

- 本地对照项目  
  `F:\MyTools`

