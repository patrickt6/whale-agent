.PHONY: help install test lint format typecheck demo check

help:
	@echo "Targets:"
	@echo "  install    install the package and dev dependencies"
	@echo "  test       run the test suite"
	@echo "  lint       run ruff check"
	@echo "  format     run ruff format"
	@echo "  typecheck  run mypy"
	@echo "  demo       run the digest offline, no network, no email"
	@echo "  check      lint + typecheck + test"

install:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"

test:
	./whale test

lint:
	.venv/bin/ruff check .

format:
	.venv/bin/ruff format .

typecheck:
	.venv/bin/mypy src

demo:
	./whale digest --demo

check: lint typecheck test
