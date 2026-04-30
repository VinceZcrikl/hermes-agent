# Hermes 读取 X (Twitter) 内容机制分析

> 分支：`claude/analyze-hermes-x-reading-UyIHl`
> 仓库：`hermes-agent`
> 范围：仅分析"读取"路径（read post / search / timeline / mentions / user lookup），不涉及发帖、点赞等写操作

---

## 一、结论速览

Hermes 读取 X 内容**只有一条路径**：

```
LLM → terminal 工具 → 子进程执行 xurl CLI → X API v2 → JSON stdout → 回灌进 Agent 上下文
```

- 唯一集成入口：`skills/social-media/xurl/SKILL.md`
- 没有 fallback：没有 nitter、syndication、WebFetch 抓 x.com、也没有 gateway 适配器
- v0.11.0 起替换了旧的 `xitter` skill（第三方 Python CLI 包装）→ 改用 X 开发者团队官方的 `xurl`（OAuth 2.0 PKCE，token 自动刷新）

---

## 二、技术栈与文件定位

### 1. Skill 定义层

| 文件 | 作用 |
| --- | --- |
| `skills/social-media/xurl/SKILL.md` | 唯一的 X 集成 skill。前置依赖：`commands: [xurl]`（第 9 行）；列出全部读写命令、安全规则、错误排查。 |
| `skills/social-media/DESCRIPTION.md` | 分类描述。 |

读取相关命令（来自 SKILL.md 第 122–155 行 Quick Reference）：

| 操作 | 命令 |
| --- | --- |
| 读单条推文 | `xurl read POST_ID` 或 `xurl read https://x.com/user/status/...` |
| 搜索推文 | `xurl search "QUERY" -n 10` |
| 主页时间线 | `xurl timeline -n 20` |
| @ 提及 | `xurl mentions -n 10` |
| 用户资料 | `xurl user @handle` |
| 自身身份 | `xurl whoami` |
| 收藏夹 / 点赞列表 | `xurl bookmarks -n 10` / `xurl likes -n 10` |
| 关注 / 粉丝 | `xurl following -n 20` / `xurl followers -n 20` |
| 私信 | `xurl dms -n 10` |
| 任意 v2 端点 | `xurl /2/users/me`、`xurl -X GET /2/...` |

所有响应均为 X API v2 标准 JSON（SKILL.md 第 312 行起）：
```json
{ "data": { "id": "1234567890", "text": "Hello world!" } }
```

### 2. Skill 发现 / 注入层

| 位置 | 作用 |
| --- | --- |
| `agent/prompt_builder.py:654` `build_skills_system_prompt(...)` | 启动时遍历 skills 目录，解析 SKILL.md frontmatter（name/description/platforms/metadata.tags），构建 skills 索引并注入系统提示。 |
| `agent/skill_utils.py` | frontmatter 解析、平台匹配、缓存键计算。 |
| `agent/skill_preprocessing.py` | skill 内容预处理（替换内联 shell 片段等）。 |
| `tools/skills_tool.py:846` `skill_view(...)` | 当 LLM 决定使用某 skill 时（如 `/xurl` 或显式调用），返回完整 SKILL.md 给 Agent 阅读。 |
| `tools/skills_hub.py` / `tools/skills_sync.py` / `tools/skills_guard.py` | skill 仓库同步、用户扩展、防越权访问。 |

注入流程：
1. 进程启动 → 扫描 `~/.hermes/skills/`（首次从仓库内 `skills/` 播种）。
2. 把每个 skill 的 `name + description` 摘要拼成一段 `SKILLS_GUIDANCE`，塞进 system prompt。
3. 当 LLM 返回 `/xurl` 这类调用，Agent 通过 `skill_view` 工具加载完整 SKILL.md，让模型继续推理具体命令。

> 因此 LLM **不是** 通过专用工具调用 X，而是通过"读 skill → 自己拼 shell 命令 → 用 terminal 工具执行"的两步范式。

### 3. 命令执行层

| 位置 | 作用 |
| --- | --- |
| `model_tools.py:594` `handle_function_call(...)` | LLM 工具调用统一分发。 |
| `tools/registry.py` | 工具注册与 dispatch。 |
| `tools/terminal_tool.py:1502` `terminal_tool(...)` | 真正执行 shell 命令的入口；支持 local subprocess、Docker、Modal、SSH、Singularity 等多种沙箱后端（见 `tools/terminal_tool.py:1763` 局部 subprocess 块）。 |

执行流：
1. LLM 输出 `terminal({"command": "xurl read 1234567890"})` 工具调用。
2. `handle_function_call` → `registry.dispatch` → `terminal_tool`。
3. 根据当前环境配置启动子进程（默认 local `subprocess.run`）。
4. 捕获 stdout / stderr 与 exit code；非 0 退出码会冒泡到 Agent，但 X API 错误本身仍是 stdout JSON（SKILL.md 第 362–368 行说明）。
5. 标准化后的输出作为 tool result 写入消息历史。

### 4. 凭证 / 安全层

| 来源 | 关键约束 |
| --- | --- |
| `~/.xurl`（YAML） | 用户在 agent 会话**外部**手工填写；保存 OAuth2 token，自动刷新。 |
| SKILL.md 第 35–51 行 "Secret Safety (MANDATORY)" | 严禁 Agent 读取 / 打印 / 上传该文件；严禁使用 `--bearer-token` 等内联密钥参数；严禁 `--verbose / -v`（会泄露 auth header）。 |
| SKILL.md 第 373–379 行 "Agent Workflow" | Agent 启动序列：`xurl --help` → `xurl auth status` → 校验默认 app 是否绑定 oauth2 → 用最便宜的读操作（`whoami` / `user` / `search -n 3`）做连通性自检 → 才允许写操作。 |

---

## 三、读取路径数据流图

```
┌────────────────────────────────────────────────────────────────────┐
│ system prompt (启动期注入)                                          │
│   └─ SKILLS_GUIDANCE                                               │
│        └─ "xurl: X/Twitter via xurl CLI: post, search, DM, ..."    │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  (LLM 决定读 X)
┌────────────────────────────────────────────────────────────────────┐
│ skill_view("xurl")  →  返回完整 SKILL.md 给模型                     │
│   tools/skills_tool.py:846                                         │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  (LLM 拼出 shell 命令)
┌────────────────────────────────────────────────────────────────────┐
│ terminal({"command": "xurl search 'golang' -n 5"})                 │
│   model_tools.py:594  →  registry  →  tools/terminal_tool.py:1502  │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  (subprocess)
┌────────────────────────────────────────────────────────────────────┐
│ xurl 二进制                                                         │
│   ├─ 读取 ~/.xurl (OAuth2 token, 自动刷新)                          │
│   └─ HTTPS → api.x.com/2/...                                        │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼  (JSON stdout)
┌────────────────────────────────────────────────────────────────────┐
│ tool result → message history → 下一轮 LLM 推理                     │
└────────────────────────────────────────────────────────────────────┘
```

---

## 四、与旧 `xitter` skill 的差异（v0.11.0）

`RELEASE_v0.11.0.md:292`：

> `xitter` replaced with `xurl` — the official X API CLI (#12303)

| 维度 | 旧 `xitter` | 新 `xurl` |
| --- | --- | --- |
| 来源 | 第三方 Python CLI 封装 | X 开发者平台官方 Rust CLI |
| 认证 | 通常 bearer token | OAuth 2.0 PKCE，token 自动刷新；多 app / 多账号 |
| 覆盖面 | 有限子集 | 覆盖 X API v2 全部端点（含 raw curl-style） |
| 仓库内残留 | 仅 doc 提及（`SKILL.md:31`、release notes、website docs），**无运行时代码** | — |

---

## 五、其他可能的 X 读取路径核查（结论：均不存在）

- `gateway/platforms/`：托管的是 telegram / discord / slack / matrix / feishu 等，**没有 X 适配器**。Hermes gateway 既不能接收 X webhook，也不能把 X 事件路由到 LLM。
- HTTP 客户端：仓库内未发现对 `api.x.com` / `api.twitter.com` / `nitter.*` / `syndication.twitter.com` 的直连。
- `WebFetch` / 浏览器工具：可以让模型主动去抓 `x.com/...` 页面，但 X 现在对未登录访问反爬严重；这只是兜底途径，并非"机制"，且 SKILL.md 也未推荐。

---

## 六、维护与改进建议

1. **缺失的 gateway 适配器**：当前 `xurl` 仅被 Agent 主动拉取调用，无被动入站事件。如果未来要让 Hermes 像响应 Telegram 消息一样响应 mentions / DM，需要在 `gateway/platforms/` 新增 X 适配器（可基于 `/2/tweets/search/stream` 长连接或定期轮询 `xurl mentions`）。
2. **错误分类**：`xurl` 的 API 错误（`CreditsDepleted`、`client-not-enrolled`、403 scope 缺失等）目前依赖 LLM 阅读 JSON 自行判断；可在 `agent/error_classifier.py` 增加规则，把这些信号识别为不可重试错误，避免 Agent 反复尝试浪费配额（X API 是付费的）。
3. **测试覆盖**：`tests/` 里没有针对 X 读取路径的单测/契约测试。可补一个使用录制 fixture 的 `test_xurl_read.py`，覆盖：
   - skill 文档可被 `skill_view` 加载
   - terminal 工具能在沙箱中执行 `xurl --help`（不需要真实 token）
   - JSON 解析对常见错误形态健壮
4. **安全 lint**：在仓库的预提交 / CI 中增加一条规则，禁止在示例或文档里出现 `--bearer-token=` 等内联密钥参数（与 SKILL.md 第 47 行的"Forbidden flags"列表对齐）。

---

## 七、引用清单

- `skills/social-media/xurl/SKILL.md`（全文，415 行）
- `agent/prompt_builder.py:654` `build_skills_system_prompt`
- `agent/skill_utils.py`、`agent/skill_preprocessing.py`
- `tools/skills_tool.py:846` `skill_view`
- `tools/skills_hub.py`、`tools/skills_sync.py`、`tools/skills_guard.py`
- `model_tools.py:594` `handle_function_call`
- `tools/registry.py`
- `tools/terminal_tool.py:1502` `terminal_tool`（subprocess 执行块见第 1763 行附近）
- `RELEASE_v0.11.0.md:292`（xitter → xurl 迁移）
