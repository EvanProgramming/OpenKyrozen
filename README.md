# OpenKyrozen
Self‑learning AI Agent powered by DeepSeek API

## Quick start

### Prerequisites
- Python 3.10 or newer
- DeepSeek API key (get one at [platform.deepseek.com](https://platform.deepseek.com))

### Install
```bash
git clone https://github.com/your-username/OpenKyrozen.git
cd OpenKyrozen
make install
```

### Run
```bash
make run
```

On first run you will be prompted to enter your DeepSeek API key.  The key is saved in `~/.kyrozen_config.json` for future sessions.

### Manual install (without Make)
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Commands (inside the agent)
- `/quit` or `/exit` – quit
- `/learn` – reload project files into memory
- `/api_key` – change API key

## Self‑learning
OpenKyrozen automatically learns from project files and conversation history in the background (every 2 minutes).  No manual saving required.

## License
MIT
