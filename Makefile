.PHONY: install run clean lint test check git-status git-diff git-log web

# Prefer the known-stable Python 3.12, but use the active supported Python
# 3.13 on clean runners that do not provide 3.12. Python 3.14 remains
# intentionally out of scope because of the OpenAI SDK import deadlock.
PYTHON ?= python3.12
_PYTHON_REQUEST := $(PYTHON)
PYTHON := $(shell if [ "$(_PYTHON_REQUEST)" != "python3.12" ]; then printf '%s' "$(_PYTHON_REQUEST)"; elif command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 12), (3, 13)) else 1)' >/dev/null 2>&1; then command -v python; elif command -v python3.12 >/dev/null 2>&1 && python3.12 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then command -v python3.12; elif command -v python3.13 >/dev/null 2>&1 && python3.13 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' >/dev/null 2>&1; then command -v python3.13; else printf '%s' "$(_PYTHON_REQUEST)"; fi)
VENV_PYTHON := $(if $(wildcard venv/bin/python),./venv/bin/python,$(PYTHON))

# Detect Windows (native cmd) and redirect to .bat files
ifeq ($(OS),Windows_NT)
$(error This Makefile is for macOS/Linux/WSL. On native Windows, use: setup.bat, run.bat)
endif

install:
	@echo "Creating virtual environment with $(PYTHON)..."
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "Error: $(PYTHON) not found. Install Python 3.12 first."; exit 1; }
	$(PYTHON) -m venv venv
	./venv/bin/python -m pip install --upgrade pip && ./venv/bin/python -m pip install -r requirements.txt
	@echo ""
	@echo "OpenKyrozen installed. Run 'make run' or 'python main.py'"

run:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "Error: venv requires $(PYTHON). Run 'make install' with $(PYTHON) installed."; exit 1; }
	$(VENV_PYTHON) main.py

web:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "Error: venv requires $(PYTHON)."; exit 1; }
	$(VENV_PYTHON) -m pip install fastapi uvicorn -q && $(VENV_PYTHON) server.py

debug:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "Error: venv requires $(PYTHON). Run 'make install' first."; exit 1; }
	$(VENV_PYTHON) main_debug.py

init:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "Error: venv requires $(PYTHON). Run 'make install' first."; exit 1; }
	$(VENV_PYTHON) main.py --init

# Upgrade the venv to use a different Python version (e.g. after macOS upgrade)
reinstall:
	@echo "Rebuilding venv with $(PYTHON)..."
	rm -rf venv
	$(PYTHON) -m venv venv
	./venv/bin/python -m pip install --upgrade pip && ./venv/bin/python -m pip install -r requirements.txt

clean:
	rm -rf venv chroma_memory __pycache__

# Syntax check
lint:
	$(VENV_PYTHON) -m compileall -q main.py main_debug.py server.py tools.py memory.py event_store.py task_engine.py learning_engine.py learning_benchmark.py migration.py scheduler.py skill_registry.py browser_manager.py instruction_loader.py agent_config.py subagents.py capability_tokens.py tool_registry.py dynamic_tools.py plugin_runtime.py
	@echo "Python syntax OK."
	@echo "All files pass syntax check."

# Unit tests
test:
	$(VENV_PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

# Quick verification
check:
	@echo "Checking Python syntax..."
	@$(VENV_PYTHON) -m py_compile main.py main_debug.py server.py tools.py memory.py event_store.py task_engine.py learning_engine.py learning_benchmark.py migration.py scheduler.py skill_registry.py browser_manager.py instruction_loader.py agent_config.py subagents.py capability_tokens.py tool_registry.py dynamic_tools.py plugin_runtime.py
	@echo "  Python modules: OK"
	@echo "Checking git tools..."
	@$(VENV_PYTHON) -c "from tools import AVAILABLE_TOOLS; git = [k for k in AVAILABLE_TOOLS if k.startswith('git_')]; print(f'  {len(git)} git tools, {len(AVAILABLE_TOOLS)} total tools')"
	@echo "All checks passed."

# Git helpers
git-status:
	git status

git-diff:
	git diff

git-log:
	git log --oneline --decorate -10

# Commit & push (interactive — requires message)
commit:
	@if [ -z "$(msg)" ]; then echo "Usage: make commit msg='your message'"; exit 1; fi
	git add -A
	git commit -m "$(msg)"
	@echo "Committed. Use 'make push' to push."

push:
	git push origin main

pull:
	git pull origin main
