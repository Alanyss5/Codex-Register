# register.py 精简分析报告

## 概况

| 指标 | 数值 |
|------|------|
| 总行数 | 4282 |
| 方法总数 | 103 |
| 100+ 行巨型方法 | 8 个 |
| >50 行方法 | 15 个 |
| <10 行微型方法 | 12 个 |

## 功能分区与行数占比

| 区域 | 行范围 | 行数 | 占比 | 说明 |
|------|--------|------|------|------|
| 核心注册流程 | L93-L556 | 464 | 11% | 初始化、OAuth、登录、密码提交 |
| **Token 交换** | L557-L827 | **271** | **6%** | `_complete_token_exchange` 一个方法 |
| 登录重启 | L828-L881 | 54 | 1% | `_restart_login_flow` |
| **Cookie/Session 处理** | L882-L978 | 97 | 2% | 提取 session_token、Cookie 工具 |
| **chatgpt.com 会话桥接** | L979-L1591 | **613** | **14%** | 会话捕获、重定向跟踪、桥接登录、Outlook 特化 |
| 密码注册 + 邮箱标记 | L1592-L1689 | 98 | 2% | |
| OTP 验证 + 日志 | L1690-L1873 | 184 | 4% | stage 管理、验证码获取、debug 日志 |
| **日志/调试工具** | L1874-L2136 | 263 | 6% | Cookie debug、账号创建、域名黑名单 |
| **导航/载荷解析** | L2137-L2659 | **523** | **12%** | JSON 解码、文本提取、URL 分类、导航候选 |
| **Consent/Script 资产检查** | L2659-L2956 | **298** | **7%** | Script 资产解析、embedded payload 提取 |
| **Workspace 选择** | L2957-L3368 | **412** | **10%** | workspace/org 获取、选择、callback 探测 |
| **恢复/重试路径** | L3369-L3940 | **572** | **13%** | consent 恢复、重定向跟踪、Outlook 恢复、会话内重登 |
| 主入口 + 保存 | L3941-L4282 | 342 | 8% | authorize replay、run、save_to_database | 

## 精简建议

### 1. 导航/载荷解析提取为独立模块（省 ~520 行）

**区域**: L2137-L2659，共 **523 行、15 个方法**

这些方法全部是纯工具函数，与 `RegistrationEngine` 状态无关：
- `_decode_auth_cookie_json_segments`、`_decode_cookie_json_payloads`
- `_collect_interesting_text_fragments_from_data`
- `_extract_app_router_push_payloads_from_text`
- `_extract_json_payload_candidates_from_fragment`
- `_sanitize_url_for_log`
- `_remember_resume_candidate`、`_remember_navigation_candidate` 等

> **操作**：提取到 `src/core/navigation_parser.py`，RegistrationEngine 调用即可。

---

### 2. Consent/Script 资产检查合并（省 ~100 行）

**区域**: L2659-L2956，共 **298 行**

包含：
- `_inspect_consent_script_assets` (74行) — 下载并解析 JS 脚本
- `_extract_embedded_payloads_from_text` (47行)
- `_extract_navigation_candidates_from_text` (37行)
- `_normalize_response_text` (17行)
- 多个小型解析方法

> **操作**：与"导航解析"合并移到同一个工具模块中。

---

### 3. chatgpt.com 会话桥接提取为独立模块（省 ~600 行）

**区域**: L879-L1591，共 **613 行**

标明 `# chatgpt.com 会话桥接方法 (从 dou-jiang/codex-console 移植)`。 

包含：
- `_capture_auth_session_tokens` (125行)
- `_follow_chatgpt_auth_redirects` (77行)
- `_bootstrap_chatgpt_signin_for_session` (119行)
- `_bridge_login_for_session_token` (180行)
- `_complete_token_exchange_outlook` (103行)

> **操作**：提取到 `src/core/chatgpt_bridge.py`，作为独立类或 mixin。

---

### 4. `_complete_token_exchange` 拆分（当前 271 行）

这是最大的单个方法，包含多层嵌套的条件分支（about-you 处理、user_exists 检测、authorize replay、consent fallback 等）。

> **操作**：将 about-you 分支、user_exists 恢复逻辑各提取为子方法，可减 100+ 行。

---

### 5. 恢复/重试路径整理（省 ~200 行）

**区域**: L3369-L3940

- `_attempt_session_bound_reauth` (130行) — 已经很独立
- `_follow_redirects` (128行) — 通用重定向跟踪
- `_persist_recoverable_outlook_account` (74行) — Outlook 专用

> **操作**：`_follow_redirects` 是通用 HTTP 工具，可移到 `http_client.py`。Outlook 恢复逻辑可移到单独模块。

---

## 精简预估

| 策略 | 预估节省 | 操作复杂度 |
|------|----------|-----------|
| 导航/载荷解析 → `navigation_parser.py` | ~520 行 | 低（纯提取） |
| chatgpt 桥接 → `chatgpt_bridge.py` | ~600 行 | 中（需处理状态依赖） |
| Consent/Script → 合并到导航解析 | ~100 行 | 低 |
| `_complete_token_exchange` 拆分 | ~100 行 | 中 |
| `_follow_redirects` → `http_client.py` | ~130 行 | 低 |

**总计可精简约 1450 行**，register.py 预计从 4282 行降到 ~2800 行。

> [!IMPORTANT]
> 所有精简操作都是**代码搬迁和拆分**，不删除任何功能，只是让 register.py 更聚焦于核心注册编排逻辑。
