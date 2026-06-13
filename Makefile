.PHONY: install run clean lint test check git-status git-diff git-log

# Use a known-stable Python version (3.14 has import deadlocks with openai)
PYTHON := python3.12

# Detect Windows (native cmd) and redirect to .bat files
ifeq ($(OS),Windows_NT)
$(error This Makefile is for macOS/Linux/WSL. On native Windows, use: setup.bat, run.bat)
endif

install:
	@echo "Creating virtual environment with $(PYTHON)..."
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "Error: $(PYTHON) not found. Install Python 3.12 first."; exit 1; }
	$(PYTHON) -m venv venv
	. venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt
	@echo ""
	@echo "OpenKyrozen installed. Run 'make run' or 'python main.py'"

run:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "Error: venv requires $(PYTHON). Run 'make install' with $(PYTHON) installed."; exit 1; }
	. venv/bin/activate && python main.py

debug:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "Error: venv requires $(PYTHON). Run 'make install' first."; exit 1; }
	. venv/bin/activate && python main_debug.py

init:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "Error: venv requires $(PYTHON). Run 'make install' first."; exit 1; }
	. venv/bin/activate && python main.py --init

# Upgrade the venv to use a different Python version (e.g. after macOS upgrade)
reinstall:
	@echo "Rebuilding venv with $(PYTHON)..."
	rm -rf venv
	$(PYTHON) -m venv venv
	. venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt

clean:
	rm -rf venv chroma_memory __pycache__

# Syntax check
lint:
	. venv/bin/activate && python -c "compile(open('main.py').read(), 'main.py', 'exec'); print('main.py OK')"
	. venv/bin/activate && python -c "compile(open('tools.py').read(), 'tools.py', 'exec'); print('tools.py OK')"
	. venv/bin/activate && python -c "compile(open('memory.py').read(), 'memory.py', 'exec'); print('memory.py OK')"
	@echo "All files pass syntax check."

# Quick verification
check:
	@echo "Checking Python syntax..."
	@./venv/bin/python -c "compile(open('main.py').read(), 'main.py', 'exec'); print('  main.py: OK')"
	@./venv/bin/python -c "compile(open('tools.py').read(), 'tools.py', 'exec'); print('  tools.py: OK')"
	@./venv/bin/python -c "compile(open('memory.py').read(), 'memory.py', 'exec'); print('  memory.py: OK')"
	@echo "Checking git tools..."
	@./venv/bin/python -c "from tools import AVAILABLE_TOOLS; git = [k for k in AVAILABLE_TOOLS if k.startswith('git_')]; print(f'  {len(git)} git tools, {len(AVAILABLE_TOOLS)} total tools')"
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