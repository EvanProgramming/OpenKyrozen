.PHONY: install run clean lint test check git-status git-diff git-log

install:
	@echo "Creating virtual environment..."
	python3 -m venv venv
	. venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt
	@echo ""
	@echo "OpenKyrozen installed. Run 'make run' or 'python main.py'"

run:
	. venv/bin/activate && python main.py

debug:
	. venv/bin/activate && python main_debug.py

init:
	. venv/bin/activate && python main.py --init

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
	@python3 -c "compile(open('main.py').read(), 'main.py', 'exec'); print('  main.py: OK')"
	@python3 -c "compile(open('tools.py').read(), 'tools.py', 'exec'); print('  tools.py: OK')"
	@python3 -c "compile(open('memory.py').read(), 'memory.py', 'exec'); print('  memory.py: OK')"
	@echo "Checking git tools..."
	@python3 -c "from tools import AVAILABLE_TOOLS; git = [k for k in AVAILABLE_TOOLS if k.startswith('git_')]; print(f'  {len(git)} git tools, {len(AVAILABLE_TOOLS)} total tools')"
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