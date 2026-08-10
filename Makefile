.PHONY: install lint test run

install:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"

lint:
	.venv/bin/ruff check src tests

test:
	.venv/bin/pytest

run:
	.venv/bin/lookalike-api
