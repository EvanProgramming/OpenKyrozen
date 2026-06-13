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
- Python **3.12** or **3.13** (Python 3.14 has a known import issue)
- A **DeepSeek API key** – get one free at [platform.deepseek.com](https://platform.deepseek.com)

### 🍎 macOS / 🐧 Linux

**Install with Make (recommended):**
```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
make install
make run
```

**Manual install:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

### 🪟 Windows

**One‑click setup:**
```cmd
setup.bat
```

**Run the agent:**
```cmd
run.bat
```

**Alternatively (manual):**
```cmd
python -m venv venv
venv\Scripts\activate
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
