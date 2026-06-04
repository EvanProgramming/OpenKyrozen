.PHONY: install run clean

install:
	@echo "Creating virtual environment..."
	python3 -m venv venv
	. venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt
	@echo ""
	@echo "OpenKyrozen installed. Run 'make run' or 'python main.py'"

run:
	. venv/bin/activate && python main.py

clean:
	rm -rf venv chroma_memory __pycache__
