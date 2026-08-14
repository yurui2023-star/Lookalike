.PHONY: install data lint test run eda pipeline scoring-domain tier-plan

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

scoring-domain:
	.venv/bin/python scripts/scoring_domain_experiment.py \
		--base-rows 2000000 \
		--evidence docs/evidence/scoring_domain_experiment.md

tier-plan:
	.venv/bin/python scripts/tier_model_plan.py \
		--base-rows 2000000 \
		--evidence docs/evidence/tier_model_plan.md
