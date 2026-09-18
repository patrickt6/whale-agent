.PHONY: help install test lint format typecheck demo smoke check site site-deploy

help:
	@echo "Targets:"
	@echo "  install    install the package and dev dependencies"
	@echo "  test       run the test suite"
	@echo "  lint       run ruff check"
	@echo "  format     run ruff format"
	@echo "  typecheck  run mypy"
	@echo "  demo       run the digest offline, no network, no email"
	@echo "  smoke      demo, and fail if no thresholded row reaches the table"
	@echo "  check      lint + typecheck + test"
	@echo "  site       preview the website at http://localhost:8731"
	@echo "  site-deploy  publish site/ to Cloudflare (free tier)"

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

smoke:
	PYTHON=.venv/bin/python ./scripts/smoke.sh

check: lint typecheck test

site:
	python3 -m http.server 8731 -d site

site-deploy:
	cd deploy-site && npx --yes wrangler@latest deploy
