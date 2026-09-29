<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white" alt="Python 3.12 或 3.13">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-555555" alt="macOS、Linux 和 Windows">
  <img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT 许可证">
</p>

<p align="center">
  <img src="docs/openkyrozen-banner.svg" alt="OpenKyrozen 终端动态字标" width="960">
</p>

<h1 align="center">OpenKyrozen</h1>

<p align="center"><strong>一个本地优先的终端智能体：可以执行任务、记住重要信息，并根据已验证结果改进。</strong></p>

## 安装

OpenKyrozen 支持 Python **3.12 和 3.13**。Python 3.14 暂不支持。

### macOS 或 Linux

```bash
curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.4/install.sh | sh
kyrozen
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.4/install.ps1 | iex
kyrozen
```

安装程序会准备受支持的 Python 环境、终端 UI，并把私有状态保存到 ~/.kyrozen；不会读取或打印 API key。

首次启动时选择模型服务商并输入密钥。例如：

```bash
export DEEPSEEK_API_KEY=your-key
kyrozen
```

### 从源码运行

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
make install
make run
```

轻量开发环境使用 make install-core。Windows 源码环境使用 setup.bat 和 run.bat。

### 直接安装已验证的 release wheel

```bash
uv tool install --python 3.12 --force --with fastapi --with uvicorn https://github.com/EvanProgramming/OpenKyrozen/releases/download/v2.0.4/openkyrozen-2.0.4-py3-none-any.whl
```

## 快速开始

```text
你：读取这个项目并解释它的架构
你：修复 tests/test_server.py 中失败的测试
你：搜索最新的 Python 发布日期
你：为新增 REST endpoint 创建一个计划
```

常用命令：

```text
/provider              切换模型服务商
/mode ask|plan|agent   选择只读、规划或执行模式
/project               查看当前工作区
/skills                查看已安装的技能
/learning status       查看自学习状态
/quit                  退出
```

启动可选的 Web UI：

```bash
kyrozen-web
```

默认打开 http://localhost:8000。若要操作指定项目，使用 kyrozen --project /path/to/project 或 kyrozen-web --project /path/to/project。直接运行 kyrozen 使用 ~/.kyrozen/workspace 全局工作区。

## OpenKyrozen 的主要能力

- 在工作区中读取和修改文件、运行 Shell、使用 Git、搜索网页、操作浏览器会话、查询项目图和 GitHub。
- Ask 与 Plan 只允许读取和网络操作；Agent 执行已接受或明确请求的任务，并继续遵守 capability 与 approval 限制。
- 支持 18 个常用模型服务商，并支持模型默认值和故障回退。
- SQLite 是会话、事件、任务、claims 和学习状态的权威存储；ChromaDB 只是可重建的可选索引。
- 自学习记录结果证据，只提升经过验证的改进，不会偷偷增加权限或修改模型权重。

当前运行时提供 **39 个工具**，其中包括 **14 个 Git 工具**。完整的工具、endpoint 和 MCP schema 以[生成的运行时清单](docs/tool-inventory.md)为准。

## 文档

| 主题 | 文档 |
| --- | --- |
| 命令、模式和工作区 | [使用指南](docs/usage.md) |
| 运行时流程和安全边界 | [架构](docs/architecture.md) |
| 服务商、环境变量和状态目录 | [配置](docs/configuration.md) |
| Web、REST 和 MCP | [API 指南](docs/api.md) |
| 自学习、记忆和证据 | [自进化指南](docs/self-evolution.md) |
| 与其他开源智能体比较 | [比较](docs/comparison.md) |
| 开发、测试和发布 | [开发指南](docs/development.md) |

默认 DeepSeek 示例为 "model_simple": "deepseek-flash" 和 "model_complex": "deepseek-v4-pro"。

## 安全与开发

请使用环境变量或加密的 ~/.kyrozen_config.json 保存服务商凭据。若 Web 服务暴露到 localhost 之外，请设置 KYROZEN_SERVER_TOKEN，并检查 capability 与 approval 配置。

```bash
make check
make docs-check
make test
make lint
```

详见 AGENTS.md、[安全说明](docs/configuration.md)和[开发指南](docs/development.md)。

## 许可证

OpenKyrozen 使用 [MIT License](LICENSE)。

<p align="center"><sub><a href="README.md">English</a> · 简体中文 · <a href="README.ja.md">日本語</a> · <a href="README.ko.md">한국어</a></sub></p>
