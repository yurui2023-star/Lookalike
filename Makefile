.PHONY: install data lint test run eda pipeline

install:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"

data:
	.venv/bin/python -m lookalike.data.sample_dataset

lint:
	.venv/bin/ruff check src tests scripts

test:
	.venv/bin/pytest

run:
	.venv/bin/lookalike-api

eda:
	.venv/bin/python scripts/eda_report.py

pipeline:
	.venv/bin/python scripts/full_process.py
