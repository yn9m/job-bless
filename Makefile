.PHONY: help install run run-local login apply apply-local up-local web web-local test clean

PYTHON = .venv/Scripts/python
PYTEST = .venv/Scripts/pytest

help:
	@echo "Available commands:"
	@echo "  make install     - Install Python virtual environment & dependencies"
	@echo "  make login       - Open browser for interactive manual HH account login & save session state"
	@echo "  make apply-local - Run auto-applier on collected vacancies locally without employer questions"
	@echo "  make web-local   - Start the web UI locally (http://127.0.0.1:8080)"
	@echo "  make run-local   - Run unified Python application locally (configs/config.local.yaml)"
	@echo "  make test        - Run pytest test suite"
	@echo "  make clean       - Clean temporary files & bytecode"

install:
	uv venv .venv
	uv pip install --python .venv -e .

login:
	@echo "=========================================="
	@echo "Launching Chrome for manual HH account login..."
	@echo "=========================================="
	$(PYTHON) -m src.browser.login_session configs/config.local.yaml

apply:
	$(PYTHON) main.py apply configs/config.yaml

apply-local:
	@echo "=========================================="
	@echo "Starting Auto-Applier on HH vacancies..."
	@echo "=========================================="
	$(PYTHON) main.py apply configs/config.local.yaml

web:
	$(PYTHON) main.py web configs/config.yaml

web-local:
	@echo "=========================================="
	@echo "Web UI: http://127.0.0.1:8080"
	@echo "=========================================="
	$(PYTHON) main.py web configs/config.local.yaml

run:
	$(PYTHON) main.py run configs/config.yaml

run-local:
	@echo "=========================================="
	@echo "Starting unified Python Career Agent..."
	@echo "=========================================="
	$(PYTHON) main.py run configs/config.local.yaml

up-local: run-local

test:
	$(PYTEST)

clean:
	rm -rf .pytest_cache __pycache__ src/**/__pycache__ tests/__pycache__
