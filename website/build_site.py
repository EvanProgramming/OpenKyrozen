from html import escape
from pathlib import Path
import json
import shutil

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BASE = "https://kyrozen.chat"
GITHUB = "https://github.com/EvanProgramming/OpenKyrozen"
DOCS = GITHUB + "/tree/main/docs"

NAV = {
    "en": {"home": "Overview", "features": "Capabilities", "install": "Install", "faq": "FAQ", "docs": "Docs", "language": "简体中文"},
    "zh-cn": {"home": "首页", "features": "能力", "install": "安装", "faq": "常见问题", "docs": "文档", "language": "English"},
}
PATHS = {
    "en": {"home": "/", "features": "/features/", "install": "/install/", "faq": "/faq/", "docs": "/docs/"},
    "zh-cn": {"home": "/zh-cn/", "features": "/zh-cn/features/", "install": "/zh-cn/install/", "faq": "/zh-cn/faq/", "docs": "/zh-cn/docs/"},
}
INSTALL_COMMANDS = {
    "unix": "curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.7/install.sh | sh",
    "windows": "irm https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.7/install.ps1 | iex",
}

PAGES = {
    "en": {
        "home": {
            "title": "OpenKyrozen | Local-First Terminal AI Agent",
            "description": "OpenKyrozen is an open-source, local-first terminal AI agent for coding and project work, with local memory, model choice, and explicit safety controls.",
            "eyebrow": "OPEN SOURCE · LOCAL-FIRST · OUTCOME-LED",
            "headline": "An agent that learns from verified work.",
            "intro": "OpenKyrozen works in your project, remembers useful context, and improves repeatable workflows from evidence. You choose the model. Its capabilities stay bounded by explicit safety controls.",
            "body": """
<section class="section intro-section">
  <div class="section-heading"><p class="eyebrow">BUILT FOR REAL WORK</p><h2>From a request to a result you can inspect.</h2></div>
  <p class="section-lead">OpenKyrozen is an open-source terminal AI agent for coding, research, and project work. It keeps useful state locally and records what actually happened, so progress can build on more than a good-sounding answer.</p>
</section>
<section class="section">
  <div class="section-heading"><p class="eyebrow">THREE PARTS, ONE WORKFLOW</p><h2>Act. Remember. Improve.</h2></div>
  <div class="card-grid three">
    <article class="feature-card"><span class="card-index">01</span><h3>Act in your workspace</h3><p>Read and change project files, run commands, work with Git, browse the web, and inspect project context through guarded tools.</p><a href="/features/">Explore capabilities <span aria-hidden="true">↗</span></a></article>
    <article class="feature-card"><span class="card-index">02</span><h3>Keep useful context</h3><p>Sessions, tasks, memory claims, and learning evidence use durable local storage, so a new conversation need not start from zero.</p><a href="/features/#memory">See how it remembers <span aria-hidden="true">↗</span></a></article>
    <article class="feature-card"><span class="card-index">03</span><h3>Learn from outcomes</h3><p>It can turn repeated, verified workflows into reusable skills. Learning cannot silently expand permissions or approve actions.</p><a href="/features/#learning">Read the learning model <span aria-hidden="true">↗</span></a></article>
  </div>
</section>
<section class="section workflow-section">
  <div class="section-heading"><p class="eyebrow">A PRACTICAL LEARNING LOOP</p><h2>Experience becomes evidence before it becomes a habit.</h2></div>
  <div class="steps">
    <article><span>01</span><div><h3>Do the work</h3><p>OpenKyrozen uses the tools allowed for the active mode and workspace.</p></div></article>
    <article><span>02</span><div><h3>Record what happened</h3><p>Outcomes, corrections, tool receipts, and acceptance evidence provide a traceable basis for learning.</p></div></article>
    <article><span>03</span><div><h3>Reuse only what holds up</h3><p>Candidate improvements pass evidence gates; they do not grant new capabilities or rewrite model weights.</p></div></article>
  </div>
</section>
<section class="section split-section" id="control">
  <div><p class="eyebrow">CONTROL IS PART OF THE DESIGN</p><h2>Helpful autonomy, with clear boundaries.</h2></div>
  <div><p>Ask and Plan stay read-only. Agent actions pass through capability and approval checks. Learning can improve how work is approached, but it cannot grant permissions or approve its own work.</p><a class="text-link" href="/docs/self-evolution/">Read the safety and learning details <span aria-hidden="true">↗</span></a></div>
</section>
<section class="closing-cta">
  <div><p class="eyebrow">START WITH YOUR OWN WORKSPACE</p><h2>Bring an agent into the work you already do.</h2><p>Install OpenKyrozen, choose a provider, and start with a project task.</p></div>
  <a class="button button-primary" href="/install/">Get started <span aria-hidden="true">↗</span></a>
</section>
""",
        },
        "features": {
            "title": "OpenKyrozen Features | Memory, Models & Safety",
            "description": "Explore OpenKyrozen’s terminal AI agent features: project tools, persistent local memory, hosted or Ollama models, verified learning, and safety controls.",
            "eyebrow": "CAPABILITIES",
            "headline": "Capable by default. Careful by design.",
            "intro": "Explore a terminal AI agent for coding, research, and workspace tasks, with project tools, persistent memory, model choice, and explicit limits on actions and learning.",
            "body": """
<section class="section feature-list">
  <article class="feature-row"><span class="card-index">01</span><div><p class="eyebrow">TERMINAL-NATIVE</p><h2>Work where your project lives.</h2><p>Use the Bubble Tea terminal interface or the command-line agent to explore a workspace, edit files, run commands, work with Git, and inspect project history. An optional local web UI, REST API, and MCP interface are available for other workflows.</p></div><div class="feature-tags"><span>CLI + TUI</span><span>Optional Web API</span><span>MCP</span></div></article>
  <article class="feature-row" id="learning"><span class="card-index">02</span><div><p class="eyebrow">EVIDENCE-BASED LEARNING</p><h2>Improve from outcomes, not guesswork.</h2><p>OpenKyrozen records corrections, tool receipts, and acceptance evidence. Its learning system can propose reusable skills and workflow improvements after repeated work, then applies evidence gates before promotion. It does not grant capabilities, approve actions, or modify model weights.</p><a class="text-link" href="/docs/self-evolution/">How self-learning works <span aria-hidden="true">↗</span></a></div><div class="feature-tags"><span>Receipts</span><span>Acceptance evidence</span><span>Promotion gates</span></div></article>
  <article class="feature-row" id="memory"><span class="card-index">03</span><div><p class="eyebrow">DURABLE CONTEXT</p><h2>Carry useful knowledge across sessions.</h2><p>SQLite is the authoritative store for sessions, events, tasks, memory claims, and learning state. The optional Chroma index is derived and can be rebuilt; it is not the only copy of durable records.</p><a class="text-link" href="/docs/architecture/">Read the architecture guide <span aria-hidden="true">↗</span></a></div><div class="feature-tags"><span>SQLite</span><span>Scoped memory</span><span>Rebuildable index</span></div></article>
  <article class="feature-row"><span class="card-index">04</span><div><p class="eyebrow">YOUR MODEL CHOICE</p><h2>Bring the provider that fits the task.</h2><p>Configure supported hosted providers or a local Ollama model, set defaults, and use documented fallback behavior. Credentials belong in environment variables or OpenKyrozen’s encrypted configuration flow.</p><a class="text-link" href="/docs/configuration/">See provider configuration <span aria-hidden="true">↗</span></a></div><div class="feature-tags"><span>Hosted models</span><span>Ollama</span><span>Configurable routing</span></div></article>
  <article class="feature-row"><span class="card-index">05</span><div><p class="eyebrow">EXPLICIT SAFETY</p><h2>Permissions and approvals remain in charge.</h2><p>Ask and Plan modes are read-only. Agent mode uses capability checks and approval controls for actions. Sub-agents and background learning use the same execution boundaries as foreground work.</p><a class="text-link" href="/docs/usage/">Understand modes and controls <span aria-hidden="true">↗</span></a></div><div class="feature-tags"><span>Read-only modes</span><span>Capability gates</span><span>Approval checks</span></div></article>
</section>
<section class="closing-cta"><div><p class="eyebrow">READY TO TRY IT?</p><h2>Start with one real task.</h2><p>Install the agent and point it at a project you know.</p></div><a class="button button-primary" href="/install/">Installation guide <span aria-hidden="true">↗</span></a></section>
""",
        },
        "install": {
            "title": "Install OpenKyrozen | macOS, Linux & Windows",
            "description": "Install the open-source OpenKyrozen terminal AI agent on macOS, Linux, or Windows. Python 3.12 or 3.13; copy the shell or PowerShell command.",
            "eyebrow": "INSTALLATION",
            "headline": "A short path from install to useful work.",
            "intro": "OpenKyrozen supports Python 3.12 and 3.13. The installer prepares the supported environment and terminal UI for you.",
            "body": """
<section class="section install-section" id="start">
  <div class="section-heading"><p class="eyebrow">RECOMMENDED · macOS OR LINUX</p><h2>Install from a terminal.</h2><p>Run the installer, then launch OpenKyrozen. The first run guides you through provider setup.</p></div>
  <div class="code-card"><div class="code-label"><span>Shell</span><button class="copy-button" type="button" data-copy="cmd-unix">Copy</button></div><pre><code id="cmd-unix">curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.7/install.sh | sh
kyrozen</code></pre></div>
  <div class="section-heading compact"><p class="eyebrow">WINDOWS POWERSHELL</p><h2>Use the Windows installer.</h2></div>
  <div class="code-card"><div class="code-label"><span>PowerShell</span><button class="copy-button" type="button" data-copy="cmd-windows">Copy</button></div><pre><code id="cmd-windows">irm https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.7/install.ps1 | iex
kyrozen</code></pre></div>
  <p class="copy-status" aria-live="polite"></p>
</section>
<section class="section split-section">
  <div><p class="eyebrow">FIRST LAUNCH</p><h2>Choose a provider and start in a project.</h2></div>
  <div><ol class="number-list"><li>Launch <code>kyrozen</code> and choose a supported model provider.</li><li>Configure a provider key when needed. Keys are kept in environment variables or the encrypted configuration flow.</li><li>Ask a question about your project, or select Agent mode when you want it to take actions under the configured approvals.</li></ol><p class="note">The installer stores private application state under <code>~/.kyrozen</code>. A cloud model provider receives the prompt content you send to it; choose a local provider such as Ollama when that fits your setup.</p></div>
</section>
<section class="section">
  <div class="section-heading"><p class="eyebrow">OTHER PATHS</p><h2>Prefer to work from source?</h2></div>
  <div class="card-grid two">
    <article class="feature-card"><span class="card-index">A</span><h3>Clone the repository</h3><p>For development, install the project dependencies and run the agent from the checkout.</p><div class="code-inline">git clone https://github.com/EvanProgramming/OpenKyrozen.git<br>cd OpenKyrozen<br>make install<br>make run</div><a href="https://github.com/EvanProgramming/OpenKyrozen">Source repository <span aria-hidden="true">↗</span></a></article>
    <article class="feature-card"><span class="card-index">B</span><h3>Install the verified wheel</h3><p>Use the project’s published release wheel with uv and Python 3.12.</p><div class="code-inline">uv tool install --python 3.12 --force --with fastapi --with uvicorn https://github.com/EvanProgramming/OpenKyrozen/releases/download/v2.0.7/openkyrozen-2.0.7-py3-none-any.whl</div><a href="https://github.com/EvanProgramming/OpenKyrozen/releases">Browse releases <span aria-hidden="true">↗</span></a></article>
  </div>
</section>
<section class="closing-cta"><div><p class="eyebrow">NEXT</p><h2>See what OpenKyrozen can do.</h2><p>Learn about the tools, memory, learning system, and safety model.</p></div><a class="button button-primary" href="/features/">Explore capabilities <span aria-hidden="true">↗</span></a></section>
""",
        },
        "faq": {
            "title": "OpenKyrozen FAQ | Models, Data, Safety & Install",
            "description": "Answers about OpenKyrozen, an open-source terminal AI agent: model providers, local data, self-learning, safety controls, Python versions, and installation.",
            "eyebrow": "FAQ",
            "headline": "Straight answers before you install.",
            "intro": "A quick guide to how OpenKyrozen works, what it stores, and where its boundaries are.",
            "body": """
<section class="section faq-list">
  <details open><summary>What is OpenKyrozen?</summary><p>OpenKyrozen is an open-source, local-first AI agent built around terminal workflows. It can work with project files, commands, Git, web research, memory, and other configured tools.</p></details>
  <details><summary>What does “self-learning” mean here?</summary><p>OpenKyrozen records outcomes, corrections, tool receipts, and acceptance evidence. Repeated verified work can inform reusable skills and workflow improvements. Promotion is guarded; learning cannot grant permissions, create new runtime capabilities, approve actions, or modify model weights.</p></details>
  <details><summary>Does OpenKyrozen keep all data on my computer?</summary><p>Application state is stored locally by default, with SQLite as the authoritative store. When you use a hosted model provider, the prompt content needed for that request is sent to that provider. A local model such as Ollama can be configured when you want local inference.</p></details>
  <details><summary>Which model providers can I use?</summary><p>OpenKyrozen supports multiple hosted provider families and local Ollama. You can configure provider defaults and fallback behavior. See the configuration guide for the current list and setup details.</p><a class="text-link" href="/docs/configuration/">Provider configuration <span aria-hidden="true">↗</span></a></details>
  <details><summary>Can it run commands or change files without asking?</summary><p>Ask and Plan modes are read-only. Agent actions go through capability checks and approval controls. Review the active mode and approval settings before granting access to high-impact tools.</p></details>
  <details><summary>Does it include a web interface?</summary><p>The terminal UI is the primary experience. An optional local web UI, REST API, and MCP integration are also available.</p></details>
  <details><summary>Which operating systems and Python versions are supported?</summary><p>The project documents installation for macOS, Linux, and Windows, and supports Python 3.12 and 3.13.</p></details>
  <details><summary>Is OpenKyrozen open source?</summary><p>Yes. The project is released under the MIT License.</p><a class="text-link" href="https://github.com/EvanProgramming/OpenKyrozen">View the source on GitHub <span aria-hidden="true">↗</span></a></details>
</section>
<section class="closing-cta"><div><p class="eyebrow">MORE DETAIL</p><h2>Read the source and the docs.</h2><p>OpenKyrozen’s behavior is documented alongside the code.</p></div><a class="button button-primary" href="https://github.com/EvanProgramming/OpenKyrozen">Visit GitHub <span aria-hidden="true">↗</span></a></section>
""",
        },
    },
    "zh-cn": {
        "home": {
            "title": "OpenKyrozen｜本地优先的终端 AI 智能体",
            "description": "了解 OpenKyrozen 开源终端 AI 智能体：用于编程与项目工作的本地优先工具，支持本地记忆、模型选择和明确的安全控制。",
            "eyebrow": "开源 · 本地优先 · 以结果为依据",
            "headline": "从已验证的工作中学习的智能体。",
            "intro": "OpenKyrozen 在你的项目中工作，记住有用的上下文，并根据证据改进可重复的流程。模型由你选择，能力始终受明确的安全控制约束。",
            "body": """
<section class="section intro-section">
  <div class="section-heading"><p class="eyebrow">为真实工作而构建</p><h2>从需求到可检查的结果。</h2></div>
  <p class="section-lead">OpenKyrozen 是一款开源终端 AI 智能体，可用于编程、研究和项目工作。它在本地保存有用状态，并记录实际发生的事情，让后续工作建立在事实之上，而不只是听起来不错的回答。</p>
</section>
<section class="section">
  <div class="section-heading"><p class="eyebrow">三个环节，一个工作流</p><h2>执行 · 记忆 · 改进。</h2></div>
  <div class="card-grid three">
    <article class="feature-card"><span class="card-index">01</span><h3>在你的工作区中执行</h3><p>通过受控工具读取和修改项目文件、运行命令、使用 Git、浏览网页并查看项目上下文。</p><a href="/zh-cn/features/">了解产品能力 <span aria-hidden="true">↗</span></a></article>
    <article class="feature-card"><span class="card-index">02</span><h3>保留有用的上下文</h3><p>会话、任务、记忆声明和学习证据使用持久化的本地存储，因此新对话不必每次从零开始。</p><a href="/zh-cn/features/#memory">了解记忆机制 <span aria-hidden="true">↗</span></a></article>
    <article class="feature-card"><span class="card-index">03</span><h3>从结果中学习</h3><p>经过验证、反复使用的工作流可以沉淀为可复用技能。学习过程不能悄悄扩大权限或批准操作。</p><a href="/zh-cn/features/#learning">了解学习机制 <span aria-hidden="true">↗</span></a></article>
  </div>
</section>
<section class="section workflow-section">
  <div class="section-heading"><p class="eyebrow">务实的学习闭环</p><h2>先把经验变成证据，再沉淀为习惯。</h2></div>
  <div class="steps">
    <article><span>01</span><div><h3>完成工作</h3><p>OpenKyrozen 按当前模式和工作区允许的范围使用工具。</p></div></article>
    <article><span>02</span><div><h3>记录实际结果</h3><p>结果、纠正、工具回执和验收证据，为学习提供可追溯的依据。</p></div></article>
    <article><span>03</span><div><h3>只复用经得起验证的方法</h3><p>候选改进必须通过证据门槛；它们不能增加能力权限，也不会改写模型权重。</p></div></article>
  </div>
</section>
<section class="section split-section" id="control">
  <div><p class="eyebrow">控制是设计的一部分</p><h2>自主协助，也有清晰边界。</h2></div>
  <div><p>Ask 和 Plan 模式保持只读。Agent 操作受能力权限和审批检查约束。学习可以改进工作方法，但不能自行获得权限或批准自己的操作。</p><a class="text-link" href="/docs/self-evolution/">查看安全与学习细节 <span aria-hidden="true">↗</span></a></div>
</section>
<section class="closing-cta">
  <div><p class="eyebrow">从你自己的工作区开始</p><h2>把智能体带进日常工作。</h2><p>安装 OpenKyrozen，选择模型服务商，然后从一个项目任务开始。</p></div>
  <a class="button button-primary" href="/zh-cn/install/">开始使用 <span aria-hidden="true">↗</span></a>
</section>
""",
        },
        "features": {
            "title": "OpenKyrozen 功能｜记忆、模型与安全控制",
            "description": "了解 OpenKyrozen 终端 AI 智能体的项目工具、本地持久记忆、托管模型与 Ollama、经验证的学习机制和安全控制。",
            "eyebrow": "产品能力",
            "headline": "能力广泛，边界清晰。",
            "intro": "了解这款用于编程、研究和工作区任务的终端 AI 智能体，以及它的项目工具、持久记忆、模型选择和操作边界。",
            "body": """
<section class="section feature-list">
  <article class="feature-row"><span class="card-index">01</span><div><p class="eyebrow">终端原生</p><h2>在项目所在的位置工作。</h2><p>使用 Bubble Tea 终端界面或命令行智能体探索工作区、编辑文件、运行命令、操作 Git 并查看项目历史。还可选择本地 Web UI、REST API 和 MCP 接口，以适配其他工作流。</p></div><div class="feature-tags"><span>CLI + TUI</span><span>可选 Web API</span><span>MCP</span></div></article>
  <article class="feature-row" id="learning"><span class="card-index">02</span><div><p class="eyebrow">基于证据的学习</p><h2>根据结果改进，而不是猜测。</h2><p>OpenKyrozen 会记录纠正、工具回执和验收证据。重复的工作可以形成可复用技能和流程改进，但晋升前必须经过证据检查。它不会增加能力权限、批准操作或修改模型权重。</p><a class="text-link" href="/docs/self-evolution/">自学习如何运作 <span aria-hidden="true">↗</span></a></div><div class="feature-tags"><span>工具回执</span><span>验收证据</span><span>晋升门槛</span></div></article>
  <article class="feature-row" id="memory"><span class="card-index">03</span><div><p class="eyebrow">持久上下文</p><h2>在不同会话间保留有用知识。</h2><p>SQLite 是会话、事件、任务、记忆声明和学习状态的权威存储。可选的 Chroma 索引是派生数据，可以重建，不是持久记录的唯一副本。</p><a class="text-link" href="/docs/architecture/">阅读架构说明 <span aria-hidden="true">↗</span></a></div><div class="feature-tags"><span>SQLite</span><span>作用域记忆</span><span>可重建索引</span></div></article>
  <article class="feature-row"><span class="card-index">04</span><div><p class="eyebrow">模型由你选择</p><h2>为任务选择合适的服务商。</h2><p>配置托管模型服务商或本地 Ollama 模型，设置默认项并使用文档说明的回退行为。凭据应存放在环境变量或 OpenKyrozen 的加密配置流程中。</p><a class="text-link" href="/docs/configuration/">查看服务商配置 <span aria-hidden="true">↗</span></a></div><div class="feature-tags"><span>托管模型</span><span>Ollama</span><span>可配置路由</span></div></article>
  <article class="feature-row"><span class="card-index">05</span><div><p class="eyebrow">明确的安全控制</p><h2>权限与审批始终有效。</h2><p>Ask 和 Plan 模式只读。Agent 模式中的操作受能力权限和审批控制。子智能体与后台学习也遵守与前台任务相同的执行边界。</p><a class="text-link" href="/docs/usage/">了解模式与控制 <span aria-hidden="true">↗</span></a></div><div class="feature-tags"><span>只读模式</span><span>能力权限</span><span>审批检查</span></div></article>
</section>
<section class="closing-cta"><div><p class="eyebrow">现在就试试</p><h2>从一个真实任务开始。</h2><p>安装智能体，并将它连接到你熟悉的项目。</p></div><a class="button button-primary" href="/zh-cn/install/">安装指南 <span aria-hidden="true">↗</span></a></section>
""",
        },
        "install": {
            "title": "安装 OpenKyrozen｜macOS、Linux 与 Windows",
            "description": "在 macOS、Linux 或 Windows 安装 OpenKyrozen 开源终端 AI 智能体。支持 Python 3.12 或 3.13，提供 Shell 和 PowerShell 命令。",
            "eyebrow": "安装",
            "headline": "从安装到开始工作的短路径。",
            "intro": "OpenKyrozen 支持 Python 3.12 和 3.13。安装程序会为你准备受支持的环境和终端界面。",
            "body": """
<section class="section install-section" id="start">
  <div class="section-heading"><p class="eyebrow">推荐 · macOS 或 Linux</p><h2>在终端中安装。</h2><p>运行安装程序后启动 OpenKyrozen。首次启动会引导你配置模型服务商。</p></div>
  <div class="code-card"><div class="code-label"><span>Shell</span><button class="copy-button" type="button" data-copy="cmd-unix">复制</button></div><pre><code id="cmd-unix">curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.7/install.sh | sh
kyrozen</code></pre></div>
  <div class="section-heading compact"><p class="eyebrow">WINDOWS POWERSHELL</p><h2>使用 Windows 安装程序。</h2></div>
  <div class="code-card"><div class="code-label"><span>PowerShell</span><button class="copy-button" type="button" data-copy="cmd-windows">复制</button></div><pre><code id="cmd-windows">irm https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.7/install.ps1 | iex
kyrozen</code></pre></div>
  <p class="copy-status" aria-live="polite"></p>
</section>
<section class="section split-section">
  <div><p class="eyebrow">首次启动</p><h2>选择服务商，然后在项目中开始。</h2></div>
  <div><ol class="number-list"><li>运行 <code>kyrozen</code> 并选择支持的模型服务商。</li><li>需要时配置服务商密钥。密钥保存在环境变量或加密配置流程中。</li><li>询问项目相关问题；如果希望智能体执行操作，请在已配置审批规则的前提下选择 Agent 模式。</li></ol><p class="note">安装程序将私有应用状态保存在 <code>~/.kyrozen</code>。使用托管模型时，请求所需的提示内容会发送给相应服务商。如果适合你的环境，也可以配置 Ollama 本地模型进行推理。</p></div>
</section>
<section class="section">
  <div class="section-heading"><p class="eyebrow">其他方式</p><h2>更喜欢从源码运行？</h2></div>
  <div class="card-grid two">
    <article class="feature-card"><span class="card-index">A</span><h3>克隆代码仓库</h3><p>用于开发的方式：安装项目依赖并从源码目录运行智能体。</p><div class="code-inline">git clone https://github.com/EvanProgramming/OpenKyrozen.git<br>cd OpenKyrozen<br>make install<br>make run</div><a href="https://github.com/EvanProgramming/OpenKyrozen">源代码仓库 <span aria-hidden="true">↗</span></a></article>
    <article class="feature-card"><span class="card-index">B</span><h3>安装已验证的 wheel</h3><p>使用 uv 和 Python 3.12 安装项目发布的 wheel。</p><div class="code-inline">uv tool install --python 3.12 --force --with fastapi --with uvicorn https://github.com/EvanProgramming/OpenKyrozen/releases/download/v2.0.7/openkyrozen-2.0.7-py3-none-any.whl</div><a href="https://github.com/EvanProgramming/OpenKyrozen/releases">查看发布版本 <span aria-hidden="true">↗</span></a></article>
  </div>
</section>
<section class="closing-cta"><div><p class="eyebrow">下一步</p><h2>了解 OpenKyrozen 能做什么。</h2><p>查看工具、记忆、学习系统与安全模型。</p></div><a class="button button-primary" href="/zh-cn/features/">浏览产品能力 <span aria-hidden="true">↗</span></a></section>
""",
        },
        "faq": {
            "title": "OpenKyrozen 常见问题｜模型、数据、安全与安装",
            "description": "解答 OpenKyrozen 终端 AI 智能体的模型服务商、本地数据、自学习机制、安全控制、Python 版本和安装问题。",
            "eyebrow": "常见问题",
            "headline": "安装前，先把重要问题说清楚。",
            "intro": "快速了解 OpenKyrozen 如何工作、保存什么，以及它有哪些边界。",
            "body": """
<section class="section faq-list">
  <details open><summary>OpenKyrozen 是什么？</summary><p>OpenKyrozen 是一款开源、本地优先的 AI 智能体，围绕终端工作流构建。它可以使用项目文件、命令、Git、网页研究、记忆和其他已配置工具。</p></details>
  <details><summary>这里的“自学习”是什么意思？</summary><p>OpenKyrozen 会记录结果、纠正、工具回执和验收证据。经过重复验证的工作可以形成可复用技能和流程改进，晋升过程受证据门槛约束。学习不能增加权限、创建新的运行时能力、批准操作或修改模型权重。</p></details>
  <details><summary>所有数据都会留在我的电脑上吗？</summary><p>默认情况下，应用状态保存在本地，SQLite 是权威存储。使用托管模型服务商时，该请求所需的提示内容会发送给相应服务商。如果希望本地推理，可以配置 Ollama 等本地模型。</p></details>
  <details><summary>可以使用哪些模型服务商？</summary><p>OpenKyrozen 支持多种托管模型服务商，也支持本地 Ollama。你可以配置默认服务商和回退行为。当前列表与设置细节见配置指南。</p><a class="text-link" href="/docs/configuration/">模型服务商配置 <span aria-hidden="true">↗</span></a></details>
  <details><summary>它会不会不经询问就运行命令或修改文件？</summary><p>Ask 和 Plan 模式为只读。Agent 操作受能力权限和审批控制。启用高影响工具前，请检查当前模式与审批设置。</p></details>
  <details><summary>它有网页界面吗？</summary><p>终端 UI 是主要使用方式。另外还提供可选的本地 Web UI、REST API 和 MCP 集成。</p></details>
  <details><summary>支持哪些操作系统和 Python 版本？</summary><p>项目提供 macOS、Linux 和 Windows 的安装说明，支持 Python 3.12 和 3.13。</p></details>
  <details><summary>OpenKyrozen 是开源软件吗？</summary><p>是。项目使用 MIT 许可证发布。</p><a class="text-link" href="https://github.com/EvanProgramming/OpenKyrozen">在 GitHub 查看源码 <span aria-hidden="true">↗</span></a></details>
</section>
<section class="closing-cta"><div><p class="eyebrow">查看更多细节</p><h2>阅读文档与源代码。</h2><p>OpenKyrozen 的行为与实现都和代码一起公开说明。</p></div><a class="button button-primary" href="https://github.com/EvanProgramming/OpenKyrozen">访问 GitHub <span aria-hidden="true">↗</span></a></section>
""",
        },
    },
}

def page_url(lang, slug):
    return BASE + PATHS[lang][slug]

def nav_html(lang, current):
    labels = NAV[lang]
    links = []
    for slug in ("home", "features", "install", "faq", "docs"):
        label = labels[slug]
        current_attr = ' aria-current="page"' if slug == current else ""
        links.append(f'<a href="{PATHS[lang][slug]}"{current_attr}>{escape(label)}</a>')
    other = "zh-cn" if lang == "en" else "en"
    other_path = PATHS[other][current]
    return f"""
<header class="site-header">
  <a class="brand" href="{PATHS[lang]['home']}" aria-label="OpenKyrozen home"><span class="brand-mark" aria-hidden="true"><i></i><b></b></span><span>openkyrozen<span class="brand-period">.</span></span></a>
  <button class="menu-toggle" type="button" aria-expanded="false" aria-controls="primary-nav"><span class="sr-only">Menu</span><span></span><span></span></button>
  <nav class="site-nav" id="primary-nav" aria-label="Primary navigation">
    {''.join(links)}
    <a class="language-link" href="{other_path}" lang="{'zh-CN' if other == 'zh-cn' else 'en'}">{escape(labels['language'])}</a>
    <a class="nav-github" href="{GITHUB}" target="_blank" rel="noopener noreferrer">GitHub <span aria-hidden="true">↗</span></a>
  </nav>
</header>"""

def home_install_html(lang):
    if lang == "en":
        kicker, select_label, copy_label = "QUICK INSTALL", "Choose operating system", "Copy command"
        source_label, copied, success, fallback = "View source", "Copied", "Command copied.", "Select and copy the command from the code block."
    else:
        kicker, select_label, copy_label = "快速安装", "选择操作系统", "复制命令"
        source_label, copied, success, fallback = "查看源码", "已复制", "命令已复制。", "请从命令框中手动选择并复制。"
    return f"""<div class="hero-install">
          <div class="hero-install-head">
            <span class="hero-install-kicker">{kicker}</span>
            <label class="hero-platform-label" for="install-platform"><span class="sr-only">{select_label}</span><select id="install-platform" name="platform" aria-label="{select_label}"><option value="unix">macOS / Linux</option><option value="windows">Windows · PowerShell</option></select></label>
          </div>
          <div class="hero-command-row"><pre class="hero-command"><code id="hero-install-command">{INSTALL_COMMANDS['unix']}</code></pre><button class="copy-button hero-copy-button" type="button" data-copy="hero-install-command" data-copy-status="hero-install-status" data-copy-done="{copied}" data-copy-success="{success}" data-copy-fallback="{fallback}">{copy_label}</button></div>
          <p class="copy-status hero-install-status" id="hero-install-status" aria-live="polite"></p>
        </div>
        <div class="hero-actions"><a class="button button-quiet" href="{GITHUB}" target="_blank" rel="noopener noreferrer">{source_label} <span aria-hidden="true">↗</span></a></div>
        <p class="hero-meta">Python 3.12 / 3.13 · macOS · Linux · Windows</p>"""

def document(lang, slug, page):
    canonical = page_url(lang, slug)
    en_url = page_url("en", slug)
    zh_url = page_url("zh-cn", slug)
    software_url = page_url(lang, "home") + "#software"
    graph = [
        {
            "@type": "WebSite",
            "@id": BASE + "/#website",
            "name": "OpenKyrozen",
            "url": BASE + "/",
            "inLanguage": ["en", "zh-CN"],
        },
        {
            "@type": "WebPage",
            "@id": canonical + "#webpage",
            "name": page["title"],
            "url": canonical,
            "description": page["description"],
            "inLanguage": "en" if lang == "en" else "zh-CN",
            "isPartOf": {"@id": BASE + "/#website"},
            "about": {"@id": software_url},
        },
    ]
    if slug == "home":
        graph[1]["mainEntity"] = {"@id": software_url}
        graph.append({
            "@type": "SoftwareApplication",
            "@id": software_url,
            "name": "OpenKyrozen",
            "applicationCategory": "DeveloperApplication",
            "operatingSystem": "macOS, Linux, Windows",
            "description": page["description"],
            "url": page_url(lang, "home"),
            "codeRepository": GITHUB,
            "license": "https://opensource.org/license/mit",
        })
    schema_data = {"@context": "https://schema.org", "@graph": graph}
    schema = '<script type="application/ld+json">' + json.dumps(schema_data, ensure_ascii=False) + '</script>'
    label = "OpenKyrozen — A local-first terminal agent" if lang == "en" else "OpenKyrozen — 本地优先的终端智能体"
    image_alt = "OpenKyrozen terminal interface showing a project, task progress, and workspace context" if lang == "en" else "OpenKyrozen 终端界面，展示项目、任务进度与工作区上下文"
    main_label = "Main content" if lang == "en" else "主要内容"
    return f"""<!doctype html>
<html lang="{'en' if lang == 'en' else 'zh-CN'}" class="no-js">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(page['title'])}</title>
  <meta name="description" content="{escape(page['description'], quote=True)}">
  <meta property="og:site_name" content="OpenKyrozen">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{escape(page['title'], quote=True)}">
  <meta property="og:description" content="{escape(page['description'], quote=True)}">
  <meta property="og:url" content="{canonical}">
  <meta property="og:locale" content="{'en_US' if lang == 'en' else 'zh_CN'}">
  <meta property="og:locale:alternate" content="{'zh_CN' if lang == 'en' else 'en_US'}">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="{escape(page['title'], quote=True)}">
  <meta name="twitter:description" content="{escape(page['description'], quote=True)}">
  <meta name="theme-color" content="#060A0D">
  <link rel="canonical" href="{canonical}">
  <link rel="alternate" hreflang="en" href="{en_url}">
  <link rel="alternate" hreflang="zh-CN" href="{zh_url}">
  <link rel="alternate" hreflang="x-default" href="{en_url}">
  <link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
  <link rel="stylesheet" href="/assets/site.css">
  {schema}
  <script type="module" src="/assets/site.js"></script>
</head>
<body>
  <a class="skip-link" href="#main">{main_label}</a>
  {nav_html(lang, slug)}
  <main id="main" aria-label="{main_label}">
    <section class="hero page-hero {'home-hero' if slug == 'home' else ''}">
      <div class="hero-copy">
        <p class="eyebrow"><span class="status-dot" aria-hidden="true"></span>{escape(page['eyebrow'])}</p>
        <h1>{escape(page['headline'])}</h1>
        <p class="hero-intro">{escape(page['intro'])}</p>
        {home_install_html(lang) if slug == 'home' else ''}
      </div>
      {('<figure class="hero-visual"><div class="visual-topline"><span>OPENKYROZEN / TUI</span><span>PROJECT CONTEXT</span></div><img src="/assets/cockpit.svg" alt="' + escape(image_alt, quote=True) + '" width="1200" height="720"><figcaption><span class="status-dot" aria-hidden="true"></span>' + ('Terminal-native · Evidence-backed · Local-first' if lang == 'en' else '终端原生 · 证据驱动 · 本地优先') + '</figcaption></figure>') if slug == 'home' else ''}
    </section>
    {page['body']}
  </main>
  <footer class="site-footer">
    <a class="brand" href="{PATHS[lang]['home']}"><span class="brand-mark" aria-hidden="true"><i></i><b></b></span><span>openkyrozen<span class="brand-period">.</span></span></a>
    <p>{label} · <a href="https://github.com/EvanProgramming/OpenKyrozen">MIT License</a></p>
    <div class="footer-links"><a href="{PATHS[lang]['features']}">{escape(NAV[lang]['features'])}</a><a href="{PATHS[lang]['install']}">{escape(NAV[lang]['install'])}</a><a href="{PATHS[lang]['faq']}">{escape(NAV[lang]['faq'])}</a><a href="{PATHS[lang]['docs']}">{escape(NAV[lang]['docs'])}</a><a href="{GITHUB}" target="_blank" rel="noopener noreferrer">GitHub ↗</a></div>
    <span class="footer-note">© OpenKyrozen</span>
  </footer>
</body>
</html>
"""

def main():
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True, exist_ok=True)
    for lang, pages in PAGES.items():
        for slug, page in pages.items():
            route = PATHS[lang][slug]
            out_dir = DIST if route == "/" else DIST / route.strip("/")
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "index.html").write_text(document(lang, slug, page), encoding="utf-8")

    shutil.copytree(ROOT / "assets", DIST / "assets", dirs_exist_ok=True)

    (DIST / "robots.txt").write_text("User-agent: *\nAllow: /\nSitemap: " + BASE + "/sitemap.xml\n", encoding="utf-8")
    urls = []
    for slug in ("home", "features", "install", "faq"):
        en_url, zh_url = page_url("en", slug), page_url("zh-cn", slug)
        for loc in (en_url, zh_url):
            urls.append(
                "  <url><loc>" + escape(loc) + "</loc>"
                '<xhtml:link rel="alternate" hreflang="en" href="' + escape(en_url) + '"/>'
                '<xhtml:link rel="alternate" hreflang="zh-CN" href="' + escape(zh_url) + '"/>'
                '<xhtml:link rel="alternate" hreflang="x-default" href="' + escape(en_url) + '"/>'
                "</url>"
            )
    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n' + "\n".join(urls) + "\n</urlset>\n"
    (DIST / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    print("Generated 8 localized pages, sitemap.xml, and robots.txt.")

if __name__ == "__main__":
    main()
