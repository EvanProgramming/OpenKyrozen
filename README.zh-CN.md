<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/DeepSeek%20%7C%20OpenAI%20%7C%20Claude%20%7C%20Gemini%20%7C%20Ollama-API-green?logo=openai" alt="Multi-Provider">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-brightgreen" alt="License">
  <img src="https://img.shields.io/badge/CI-passing-brightgreen" alt="CI">
</p>

<h1 align="center">✨ OpenKyrozen ✨</h1>
<p align="center"><strong>自学习 AI 智能体 — DeepSeek · OpenAI · Claude · Gemini · Ollama</strong></p>
<p align="center">一款终端原生的全自主 AI 智能体，能够<em>从每次交互中学习</em>，<br>操作文件系统、管理 Git、修复 Bug，并随时间不断自我进化。</p>

<p align="center">
  🌐 <a href="README.md">English</a> ·
  <strong>简体中文</strong> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a>
</p>

---

## 📑 目录

- [什么是 OpenKyrozen？](#什么是-openkyrozen)
- [🚀 安装](#-安装)
  - [前置条件](#前置条件)
  - [方式 A：从源码安装](#方式-a从源码安装推荐)
  - [方式 B：从本地目录 pip 安装](#方式-b从本地目录-pip-安装)
- [📖 使用指南](#-使用指南)
  - [终端模式](#终端模式)
  - [对话内命令](#对话内命令)
  - [Web UI 模式](#web-ui-模式)
- [🏗 架构](#-架构)
  - [任务复杂度路由](#任务复杂度路由)
  - [模型自动选择](#模型自动选择)
  - [服务商管理](#服务商管理)
- [🛠 工具参考](#-工具参考)
  - [文件与系统](#文件与系统)
  - [网页](#网页)
  - [Git（15 种工具）](#git15-种工具)
  - [记忆](#记忆)
- [🧠 专用工作流](#-专用工作流)
  - [Bug 修复](#bug-修复六步协议)
  - [Git 操作](#git-操作安全优先)
  - [复杂任务](#复杂任务不会提前停止)
- [🧬 自学习系统](#-自学习系统)
- [🌐 Web UI 与 REST API](#-web-ui-与-rest-api)
- [🔌 插件系统](#-插件系统)
- [🔐 安全](#-安全)
- [⚙️ 配置参考](#️-配置参考)
- [🔧 开发](#-开发)
- [📁 项目结构](#-项目结构)
- [🙏 站在巨人的肩膀上](#-站在巨人的肩膀上)
- [📄 许可证](#-许可证)

---

## 什么是 OpenKyrozen？

OpenKyrozen 是一款在终端中运行的**自学习 AI 智能体**。与普通聊天机器人不同，它能够：

- **内置 26 种工具** — 读写文件、执行 Shell 命令、搜索网页、管理 Git 仓库
- **持续学习** — 20 项自学习功能在后台运行，提取事实、发明技能、优化策略
- **兼容多种大模型** — DeepSeek、OpenAI、Claude、Gemini，或本地 Ollama 模型
- **跨平台运行** — macOS、Linux、Windows（自动检测终端能力）
- **内置 Web 界面** — 浏览器端聊天界面，带 REST API 用于集成

把它想象成一个越用越聪明的 AI 队友。

---

## 🚀 安装

### 前置条件

- **Python 3.12 或 3.13**（Python 3.14+ 与 OpenAI SDK 存在已知的导入问题）
- 任一支持的服务商 API 密钥：

| 服务商 | 获取密钥 | 费用 |
|--------|---------|------|
| **DeepSeek** | [platform.deepseek.com](https://platform.deepseek.com) | ~$0.27/百万输入 token |
| **OpenAI** | [platform.openai.com](https://platform.openai.com) | ~$2.50/百万输入 token |
| **Anthropic (Claude)** | [console.anthropic.com](https://console.anthropic.com) | ~$3.00/百万输入 token |
| **Google (Gemini)** | [aistudio.google.com](https://aistudio.google.com) | ~$0.15/百万输入 token |
| **Ollama** | [ollama.com](https://ollama.com) | 免费（本地运行） |

### 方式 A：从源码安装（推荐）

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen

# macOS / Linux
make install
make run

# Windows
setup.bat
run.bat
```

### 方式 B：从本地目录 pip 安装

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
pip install .

# 安装后可在任意位置运行：
kyrozen          # 终端智能体
kyrozen-web      # Web 服务器
```

> **注意：** PyPI 发布 (`pip install openkyrozen`) 即将推出。目前请从本地目录或克隆仓库安装。

首次启动时会提示输入 API 密钥。智能体会自动检测服务商，并将加密密钥保存至 `~/.kyrozen_config.json`。

---

## 📖 使用指南

### 终端模式

启动后会显示横幅和 `You:` 提示符。像平常说话一样输入即可——智能体支持简体中文、英文、日文和韩文。

```text
You: 读取 README，告诉我这个项目是做什么的
You: 创建一个名为 hello.py 的文件，打印 "Hello World"
You: 搜索网页，查找 Python 最新版本发布日期
You: 修复 main.py 第 200 行附近的 bug
You: 提交所有更改，写一条好的 commit 信息
```

Kyrozen 会：
1. 分类你的请求（简单 / 中等 / 复杂）
2. 选择最适合当前任务的大模型
3. 按需创建执行计划
4. 逐步执行工具操作
5. 在实时任务面板中显示进度
6. 总结完成的工作

### 对话内命令

| 命令 | 功能 |
|------|------|
| `/quit` 或 `/exit` | 退出智能体 |
| `/provider` | 切换大模型服务商（交互菜单） |
| `/api_key` | 更改 API 密钥 |
| `/learn` | 立即扫描项目文件存入记忆 |
| `/forget` | 查看最近的学习记录；`/forget 关键词` 删除错误学习 |
| `/update` | 从 git 拉取最新版本 |
| `/self-learning` | 开关各项自学习功能 |

### Web UI 模式

```bash
python server.py --port 8000
# 打开 http://localhost:8000

# 局域网或容器访问时，必须设置令牌并显式监听外部地址：
KYROZEN_SERVER_TOKEN=change-me python server.py --host 0.0.0.0 --port 8000

# 或通过 Docker：
docker build -t openkyrozen .
docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-... -e KYROZEN_SERVER_TOKEN=change-me openkyrozen
```

Web 界面提供暗色主题的聊天 UI，支持实时流式输出、费用追踪和会话管理。

---

## 🏗 架构

```
用户输入
    │
    ▼
┌─────────────────┐
│   任务分类器    │──► 简单 / 中等 / 复杂
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   模型选择器    │──► deepseek-chat / deepseek-reasoner / gpt-4o / claude / gemini / llama
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   LLM 服务商    │──► 5 个后端，带自动回退链
└────────┬────────┘
         │  响应 + 工具调用
         ▼
┌─────────────────┐
│   工具执行器    │──► 26 种内置工具（文件 I/O、Shell、Git、网页、记忆）
└────────┬────────┘
         │  工具结果反馈给 LLM
         │  （每轮最多 50 次工具调用）
         ▼
┌─────────────────┐
│     响应输出    │──► 用户看到答案 + 任务摘要
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    自学习系统   │──► 后台：提取事实、评分记忆、构建知识图谱
└─────────────────┘
```

### 任务复杂度路由

Kyrozen 自动对每个请求进行分类并调整行为：

| 级别 | 示例触发词 | 智能体行为 |
|------|-----------|-----------|
| **简单** | "你好"、"Python 是什么"、"谢谢" | 直接回复，零规划开销 |
| **中等** | "列出文件并读取 README" | 创建编号计划，按顺序执行工具 |
| **复杂** | "审计此仓库"、"修复 bug 并提交"、"构建一个 Web 应用" | 完整计划 → 任务列表 → 进度追踪 → 不会提前停止 |

### 模型自动选择

智能体针对简单任务和复杂任务选择不同模型。你可以覆盖这些默认值：

```bash
export KYROZEN_MODEL_SIMPLE=deepseek-chat
export KYROZEN_MODEL_COMPLEX=deepseek-reasoner
```

| 服务商 | 简单任务（默认） | 复杂任务（默认） |
|--------|-----------------|-----------------|
| DeepSeek | `deepseek-chat` | `deepseek-reasoner` |
| OpenAI | `gpt-4o` | `gpt-4o` |
| Anthropic | `claude-sonnet-4-20250514` | `claude-sonnet-4-20250514` |
| Google | `gemini-2.5-flash` | `gemini-2.5-pro` |
| Ollama | `llama3.2` | `llama3.2` |

### 服务商管理

随时切换服务商——对话内使用 `/provider`，或通过环境变量：

```bash
export KYROZEN_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

如果主服务商失败，Kyrozen 会通过回退链自动切换（例如 DeepSeek → OpenAI → Claude）。限流错误（HTTP 429）会触发带抖动的指数退避。

---

## 🛠 工具参考

所有 26 种工具在 JSON 动作块中接受纯字符串 `args` 字段：

```json
{"action": "read_file", "args": "README.md"}
```

简短别名同样有效——`bash`、`cmd`、`sh` → `run_cmd`；`status`、`diff`、`log` → `git_status` 等。

### 文件与系统

| 工具 | 描述 | 示例 |
|------|------|------|
| `read_file` | 读取文件内容 | `"README.md"` |
| `write_file` | 创建或覆写文件 | `"path|content"` |
| `list_dir` | 列出目录内容 | `"."` |
| `list_tree` | 递归目录树 | `"src/"` |
| `find_files` | 基于 Glob 的文件搜索 | `"*.py|."` |
| `run_cmd` | 执行 Shell 命令 | `"python --version"` |

### 网页

| 工具 | 描述 | 示例 |
|------|------|------|
| `search_web` | 互联网搜索（Google → DDG → Wikipedia） | `"Python 最新版本"` |
| `read_webpage` | 抓取 URL 文本内容 | `"https://example.com"` |

### Git（15 种工具）

| 工具 | 功能 |
|------|------|
| `git_status` | 查看工作区状态 |
| `git_diff` | 未暂存 / 已暂存 / 提交间差异 |
| `git_log` | 提交历史（`--oneline --decorate`） |
| `git_branch` | 列出 / 创建 / 删除分支 |
| `git_add` | 暂存文件 |
| `git_commit` | 提交并附带消息 |
| `git_push` / `git_pull` | 远程同步 |
| `git_checkout` | 切换分支或恢复文件 |
| `git_stash` | 暂存 / 恢复 / 列出工作区更改 |
| `git_reset` | 重置 HEAD（`--soft` 安全，`--hard` 会警告） |
| `git_show` | 查看提交详情（`--stat`） |
| `git_remote` | 列出 / 添加 / 删除远程仓库 |
| `git_clone` | 克隆仓库 |
| `analyze_remote_repo` | 克隆 + 读取所有文件 → 结构化摘要 |

### 记忆

| 工具 | 描述 |
|------|------|
| `search_memory` | 基于语义搜索已存储的知识 |
| `check_stored_data` | 记忆统计信息和最近事实 |

---

## 🧠 专用工作流

### Bug 修复（六步协议）

当你粘贴报错或调用栈时，Kyrozen 会自动激活：

1. **复现** — 读取相关代码，重新运行失败的命令
2. **诊断** — 解析调用栈，确定根本原因
3. **假设** — 在做任何修改之前陈述修复方案
4. **修复** — 应用最小的代码更改
5. **验证** — 重新运行失败的命令；如果失败则回到第 2 步
6. **说明** — 告诉你哪里出了问题、做了什么更改以及为什么

修复后，Kyrozen 会跟踪结果。如果你说"谢谢，修好了"，它会记录成功。如果你说"还是有问题"，它会记录失败并触发更深层的分析。

### Git 操作（安全优先）

- 始终先运行 `git_status`
- 提交前检查 `git_diff`
- 使用约定式提交前缀：`fix:`、`feat:`、`refactor:`、`chore:`
- 未经明确请求绝不强制推送
- 切换分支前自动暂存未提交的更改
- `git reset --hard` 前发出警告

### 复杂任务（不会提前停止）

对于多步骤工作（重构、项目生成器、代码库审计）：

- 将请求分解为可验证的子任务
- 创建编号计划
- 构建与每个计划步骤对应的 JSON 任务列表
- 使用 `TaskDone` 标记追踪进度
- 完成时自动生成摘要

---

## 🧬 自学习系统

这是 Kyrozen 与众不同的地方。v2 的自学习是自动的，但必须经过证据门控：系统先记录观察，再生成候选提案，经过重复证据或验证后才激活，并保留回滚信息。记忆中的文字永远不能自动授予新权限。

### 工作原理

空闲时 Kyrozen 运行有界的学习周期。项目扫描采用增量方式，后台任务有并发限制，失败会记录为事件而不会静默丢弃。大部分功能可以通过 `/self-learning` 开关。

| # | 功能 | 学习内容 |
|---|------|---------|
| 1 | **对话学习** | 从聊天中提取事实、偏好和模式 |
| 2 | **项目文件扫描** | 将每个 `.py` 文件读入记忆以供上下文使用 |
| 3 | **过期条目老化** | 删除有关已不存在文件的事实 |
| 4 | **工具自动调试** | 分析工具失败原因，找出根本原因 |
| 5 | **记忆整合** | 去重并总结已存储的事实 |
| 6 | **工具审查** | 建议移除使用率低的工具 |
| 7 | **定向探索** | 查找未文档化的函数并推断其用途 |
| 8 | **空闲反思** | 复杂任务后反思哪些做得好 |
| 9 | **策略凝练** | Token 用量较高时提炼效率技巧 |
| 10 | **新技术自动补丁** | 当你提到未知库时查询网络 |
| 11 | **技能发明** | 从过去的成功中创建可复用的技能模板 |
| 12 | **上下文压缩** | 当上下文超过 30K 字符时总结旧对话 |
| 13 | **修复验证** | 追踪 Bug 修复成功率随时间的变化 |
| 14 | **动态工具创建** | `DefineTool:` 语法让智能体构建新工具 |
| 15 | **用户偏好模型** | 检测你的编码风格、首选语言、详细程度 |
| 16 | **自主巡检** | 检查过期包、代码异味、gitignore 缺失 |
| 17 | **记忆重要性评分** | 对条目评分 0-10；高分条目获得优先权 |
| 18 | **知识图谱** | 从已存储的事实构建实体→关系映射 |
| 19 | **技能组合** | 将多个已学习技能串联为工作流 |
| 20 | **错误学习回滚** | `/forget` 命令删除错误的学习内容 |

### 记忆存储

OpenKyrozen v2 使用 **SQLite 作为事实主库**（`~/.kyrozen/v2/openkyrozen.sqlite3`），ChromaDB 只作为可重建的语义索引。记忆包含类型、作用域、置信度、来源事件和生命周期状态；workspace 与 session 相互隔离。即使 ChromaDB 不可用，SQLite 仍会提供持久化关键词检索。

导入旧版 Chroma 记忆而不删除原数据：

```bash
python main.py migrate v1 ./chroma_memory
```

该命令会创建 `.v1-backup` 备份，并将 v2 数据写入 `~/.kyrozen/v2/`（也可通过 `KYROZEN_DB_PATH` 指定）。

任务会跨重启保存。`TaskDone` 只是完成请求，只有工具结果、测试、文件检查或明确确认提供证据后，任务才会进入成功状态。可使用 `/learning status`、`/learning explain <proposal_id>` 和 `/learning rollback <proposal_id>` 管理学习提案。

---

## 🌐 Web UI 与 REST API

```bash
pip install fastapi uvicorn
python server.py --port 8000
# 打开 http://localhost:8000

# 局域网或容器访问时，必须设置令牌并显式监听外部地址：
KYROZEN_SERVER_TOKEN=change-me python server.py --host 0.0.0.0 --port 8000
```

### REST API 端点

| 方法 | 端点 | 描述 |
|------|------|------|
| `GET` | `/` | 暗色主题聊天 Web UI |
| `POST` | `/api/chat` | 发送消息，获取 JSON 响应 |
| `POST` | `/api/chat/stream` | SSE 流式聊天 |
| `GET` | `/api/memory?q=关键词` | 搜索已存储的记忆 |
| `GET` | `/api/v2/memory?q=关键词` | 返回带来源、置信度和作用域的结构化记忆 |
| `GET/POST` | `/api/v2/tasks` | 持久化任务查询与创建 |
| `GET` | `/api/v2/learning` | 查看学习提案 |
| `POST` | `/api/v2/learning/{id}/rollback` | 回滚已激活的学习提案 |
| `GET` | `/api/v2/events` | 查看运行时、会话、任务和学习审计事件 |
| `GET` | `/api/cost` | Token 用量和费用摘要 |
| `GET` | `/api/health` | 服务商状态 + 记忆计数 |
| `GET` | `/api/voice/speak?text=...` | 通过系统 TTS 进行文本转语音 |
| `POST` | `/api/voice/transcribe` | 语音转文本（透传） |
| `POST` | `/api/webhooks/register` | 注册 webhook URL |
| `GET` | `/api/webhooks` | 列出已注册的 webhook |
| `POST` | `/api/webhooks/test` | 触发测试 webhook |
| `POST` | `/mcp` | 模型上下文协议（JSON-RPC 2.0） |

API 和 MCP 路由在本机回环访问时可以不使用令牌；任何非本机部署都必须
设置 `KYROZEN_SERVER_TOKEN`，并通过 `Authorization: Bearer <token>` 或
`X-Kyrozen-Token` 发送。MCP/Web 默认使用 `workspace` 能力，`full` 才会开放
不可逆 Git reset 和动态 Python 工具；高影响 Git 操作仍受确认模式保护。

### Docker 部署

```bash
docker build -t openkyrozen .
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-your-key \
  -e KYROZEN_SERVER_TOKEN=change-me \
  -v $(pwd)/chroma_memory:/app/chroma_memory \
  openkyrozen
```

---

## 🔌 插件系统

在 `plugins/` 目录下创建一个带 `register()` 函数的 `.py` 文件：

```python
# plugins/my_plugin.py
class MyPlugin:
    def on_startup(self, agent=None, **kwargs):
        print("插件已加载！")

    def on_turn_start(self, user_input, **kwargs):
        print(f"用户说：{user_input[:50]}")

    def on_tool_execute(self, action, args, result, **kwargs):
        print(f"工具 {action}({args[:30]}) → {result[:30]}")

def register():
    return MyPlugin()
```

可用钩子：`on_startup`、`on_turn_start`、`on_turn_end`、`on_tool_execute`。

参考 `plugins/turn_logger.py` 获取完整示例。

---

## 🔐 安全

| 特性 | 保护内容 |
|------|---------|
| **危险命令过滤** | 拦截 `rm -rf`、`mkfs`、Fork 炸弹、Windows 破坏性命令 |
| **API 密钥加密** | 使用随机安装密钥进行 Fernet 加密；配置文件和密钥文件权限为 `0600` |
| **提示注入防护** | CLI、API 和 MCP 消息都会过滤已知模式 |
| **工作区边界** | 文件和目录工具拒绝访问当前工作区之外的路径 |
| **API 认证** | 非本机 API/MCP 访问必须设置 `KYROZEN_SERVER_TOKEN` |
| **能力配置文件** | 本地 CLI 保留完整 Agent 工具集；Web/MCP 默认提供丰富的 `workspace` 权限，不可逆 Git reset 和动态工具由 `full` 显式开启 |
| **Git 安全** | 绝不强制推送；CLI 对高影响 Git 操作进行确认并记录决定 |
| **审计日志** | 所有聊天/API 事件记录到 `kyrozen_audit.log`，带时间戳 |
| **Python 版本守卫** | 拒绝在 Python 3.14+ 上启动 |
| **工具失败记忆** | 记住过去的失败并避免重复 |

---

## ⚙️ 配置参考

### 环境变量

| 变量 | 描述 | 默认值 |
|------|------|--------|
| `KYROZEN_PROVIDER` | LLM 服务商 | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | — |
| `OPENAI_API_KEY` | OpenAI API 密钥 | — |
| `ANTHROPIC_API_KEY` | Anthropic API 密钥 | — |
| `GEMINI_API_KEY` | Google Gemini API 密钥 | — |
| `KYROZEN_API_KEY` | 通用 API 密钥（覆盖服务商特定密钥） | — |
| `KYROZEN_MODEL_SIMPLE` | 简单/中等任务模型 | 服务商默认值 |
| `KYROZEN_MODEL_COMPLEX` | 复杂任务模型 | 服务商默认值 |
| `KYROZEN_BASE_URL` | 自定义 API 基础 URL | 服务商默认值 |
| `KYROZEN_EXECUTION_SURFACE` | 执行面（`cli` 或 `web`） | `cli` |
| `KYROZEN_ALLOW_DYNAMIC_TOOLS` | 允许 LLM 生成 Python 工具（`1`/`true`） | CLI：开启；Web/MCP：关闭 |
| `KYROZEN_APPROVAL_MODE` | CLI 高影响 Git 操作确认模式（`dangerous`/`never`） | `dangerous` |
| `KYROZEN_WEB_CAPABILITIES` | Web 聊天能力：`readonly`、`workspace` 或 `full` | `workspace` |
| `KYROZEN_MCP_CAPABILITIES` | MCP 能力：`readonly`、`workspace` 或 `full` | `workspace` |

本地 CLI 有意保持类似 Codex 或 OpenClaw 的高权限 Agent 能力：可以读写当前工作区、运行 Shell、访问网络并操作 Git。Web 和 MCP 默认同样提供丰富的 `workspace` 能力，但不可逆的 `git_reset` 以及 LLM 生成的 Python 工具需要显式启用 `full` 或 `KYROZEN_ALLOW_DYNAMIC_TOOLS=1`。认证和危险命令过滤仍然有效。

### 配置文件（`~/.kyrozen_config.json`）

```json
{
  "provider": "deepseek",
  "api_key": "<加密>",
  "model_simple": "deepseek-chat",
  "model_complex": "deepseek-reasoner",
  "encrypted": true,
  "encryption": "fernet"
}
```

文件自动管理。在对话中使用 `/provider` 或 `/api_key` 交互式更新。

---

## 🔧 开发

```bash
# 快速验证
make check

# 仅语法检查
make lint

# 调试模式（格式错误陷阱）
make debug

# 首次 API 密钥设置
make init

# Python 版本变更后重建虚拟环境
make reinstall

# 启动 Web 服务器
make web

# Git 辅助命令
make git-status
make git-log
make commit msg='feat: 描述'
make push
```

### CI/CD

GitHub Actions 在每次推送和 PR 时自动运行：
- Python 3.12 和 3.13 语法检查
- 工具清单验证
- 服务商导入检查
- Docker 构建验证

### pip 包

```bash
# 从本地目录安装（PyPI 发布即将推出）
pip install .                   # 核心 + CLI
pip install '.[web]'            # + Web UI
pip install '.[all]'            # + Claude + Gemini + Web
```

---

## 📁 项目结构

```
OpenKyrozen/
├── main.py              # 核心智能体循环、自学习、对话逻辑
├── tools.py             # 26 种内置工具（文件、Shell、Git、网页）
├── providers.py         # 多 LLM 抽象层（5 个服务商 + 回退）
├── memory.py            # ChromaDB 支持的向量记忆
├── server.py            # FastAPI Web 服务器 + REST API + 聊天 UI
├── pyproject.toml       # pip 包配置
├── Dockerfile           # Docker 镜像定义
├── Makefile             # 构建自动化（macOS/Linux）
├── setup.bat / run.bat  # Windows 批处理脚本
├── plugins/             # 插件目录（基于钩子）
├── prompts/             # 提示词模板（角色、指令、示例）
├── chroma_memory/       # ChromaDB 持久化存储（自动创建）
└── .github/workflows/   # CI/CD 流水线
```

---

## 🙏 站在巨人的肩膀上

OpenKyrozen 建立在优秀的开源项目之上。我们感谢每一位维护者和贡献者。

| 项目 | 仓库 | 用途 |
|------|------|------|
| **Aider** | [paul-gauthier/aider](https://github.com/paul-gauthier/aider) | 启发了多轮智能体循环、工具调用模式和 Git 安全约定 |
| **CodeWhale** | [deepseek-ai/codewhale](https://github.com/deepseek-ai/codewhale) | 智能体运行时架构、子智能体委托和验证规范 |
| **Chroma** | [chroma-core/chroma](https://github.com/chroma-core/chroma) | 支撑长期记忆和语义回忆的向量数据库 |
| **FastAPI** | [fastapi/fastapi](https://github.com/fastapi/fastapi) | Web 服务器、REST API 和实时流式端点 |
| **Rich** | [Textualize/rich](https://github.com/Textualize/rich) | 终端 UI——面板、进度条、语法高亮和实时显示 |
| **OpenAI Python** | [openai/openai-python](https://github.com/openai/openai-python) | DeepSeek、OpenAI 和 Ollama 服务商的统一 API 客户端 |
| **Uvicorn** | [encode/uvicorn](https://github.com/encode/uvicorn) | 生产环境 Web 部署的 ASGI 服务器 |
| **googlesearch-python** | [Nv7-GitHub/googlesearch](https://github.com/Nv7-GitHub/googlesearch) | DuckDuckGo 不可用时的网页搜索回退 |

> *"如果我看得更远，那是因为我站在巨人的肩膀上。"* — 艾萨克·牛顿

---

## 📄 许可证

MIT 许可证。详见 `LICENSE` 文件。

---

<p align="center">
  <sub>用 ❤️ 为希望拥有会学习的 AI 的开发者打造</sub>
</p>
