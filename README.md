<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/DeepSeek-API-green?logo=openai" alt="DeepSeek">
  <img src="https://img.shields.io/badge/license-MIT-brightgreen" alt="License">
</p>

<h1 align="center">✨ OpenKyrozen ✨</h1>
<p align="center"><strong>Self‑learning AI Agent powered by DeepSeek API</strong></p>

---

## 🚀 Quick start

### Prerequisites
- Python **3.10** or newer
- A **DeepSeek API key** – get one free at [platform.deepseek.com](https://platform.deepseek.com)

### 📦 Install with Make (recommended)
```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
make install
```

### ▶️ Run
```bash
make run
```

The first time you launch, you will be prompted to enter your DeepSeek API key.  
The key is automatically saved to `~/.kyrozen_config.json` and reused on future runs.

### 🛠 Manual install (without Make)
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

---

## 🧠 How it works

OpenKyrozen is an intelligent agent that:

- **Uses tools** – read/write files, run shell commands, search the web, clone repos, and more.
- **Learns automatically** – scans project files and remembers important facts from conversations (every 2 minutes).
- **Creates new tools** – you can teach it skills on the fly using the `DefineTool:` syntax.
- **Persistent memory** – stores knowledge in ChromaDB (or falls back to in‑memory storage).

---

## ⌨️ Commands (during a conversation)

| Command        | Description                                   |
|----------------|-----------------------------------------------|
| `/quit` / `/exit` | Exit the agent.                           |
| `/learn`       | Immediately reload project files into memory. |
| `/api_key`     | Change your DeepSeek API key on the fly.      |

---

## 🤖 Self‑learning

No manual saving is needed. OpenKyrozen:

- **Scans your workspace** – each `.py` file is read and stored.
- **Analyses conversations** – the agent reflects on recent dialogue and extracts useful facts, user preferences, and patterns.
- **Improves over time** – the more you use it, the smarter it gets.

---

## 📄 License

Distributed under the **MIT License**. See `LICENSE` for details.
