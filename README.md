# Lookalike Audience & Lead Generation

Python service implementing the **Lookalike Audience & Lead Generation** BRD:

- **Sample (Seed) analysis**: EDA Excel report, IV ranking, variable filtering (FR-05)
- **Model training**: LightGBM on cleaned/filtered sample data
- **Lookalike scoring**: score 100% of candidate records with Similarity Score 0–1 (FR-06)
- **Dashboard snapshot**: AUC/AP metrics and feature importance (FR-07)

Based on uploaded reference implementations: `full_process.py` (pipeline) and `eda_report.py` (EDA).

## Stack

- Python 3.11+
- FastAPI + Uvicorn
- pandas, LightGBM, scikit-learn, openpyxl

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
# Optional: only if data/Bank_Marketing_Dataset.csv is missing
python -m lookalike.data.sample_dataset
```

## Run API

```bash
make run
# http://localhost:8000/docs
```

## CLI scripts (original notebooks/scripts)

```bash
python scripts/eda_report.py
python scripts/full_process.py
```

## Key API endpoints

| Method | Path | BRD |
|--------|------|-----|
| POST | `/api/v1/eda` | Data exploration → Excel |
| POST | `/api/v1/features/analyze` | FR-05 Feature importance (IV) |
| POST | `/api/v1/model/train` | Train project model on sample data |
| POST | `/api/v1/lookalike/score` | FR-06 Score all candidates |
| GET | `/api/v1/dashboard` | FR-07 Metrics & feature importance |

### Example: train + score with bundled sample data

```bash
curl -s -X POST http://localhost:8000/api/v1/model/train -H 'Content-Type: application/json' -d '{}'

curl -s -X POST 'http://localhost:8000/api/v1/lookalike/score?use_sample_data=true&similarity_threshold=0.5'
```

Similarity threshold filters results **after** scoring (per BRD); all candidates receive a score.

## Test & lint

```bash
make test
make lint
```

## Documentation

### Detailed Design v4.0 (preferred — production target)

Headless monthly batch scoring: no frontend, no file upload, no Segment scoring. Pre-production trains and publishes models; production scores the Smart Sales candidate snapshot and writes results to Sales CDP ClickHouse.

| Format | File |
|----------|------|
| 中文 HTML | [`docs/Lookalike_Detailed_Design_v4.0_BatchScoring.html`](docs/Lookalike_Detailed_Design_v4.0_BatchScoring.html) |
| 中文 Markdown | [`docs/Lookalike_Detailed_Design_v4.0_BatchScoring.md`](docs/Lookalike_Detailed_Design_v4.0_BatchScoring.md) |
| 中文 Word | [`docs/Lookalike_Detailed_Design_v4.0_BatchScoring.docx`](docs/Lookalike_Detailed_Design_v4.0_BatchScoring.docx) |
| English HTML | [`docs/Lookalike_Detailed_Design_v4.0_BatchScoring_EN.html`](docs/Lookalike_Detailed_Design_v4.0_BatchScoring_EN.html) |

Aligned with **MB Bank System Architecture Document_Lookalike V1.0**. Summary of the v2.1 → v4.0 change: [`docs/DESIGN_OPTIMIZATION.md`](docs/DESIGN_OPTIMIZATION.md).

### Historical (superseded for production scope)

- [`docs/Lookalike_Detailed_Design_v2.1_Complete_ZH.html`](docs/Lookalike_Detailed_Design_v2.1_Complete_ZH.html) / [EN](docs/Lookalike_Detailed_Design_v2.1_Complete_EN.html) — interactive Process / upload / P2 React
- [`docs/DESIGN_PLAN.md`](docs/DESIGN_PLAN.md) — earlier “cron + seed SQL” proposal (do not implement as-is)
- [`docs/DESIGN_PLAN_REVIEW.md`](docs/DESIGN_PLAN_REVIEW.md) — review of that proposal (leakage / idempotency findings still apply in pre-production)
- [`docs/Lookalike_Detailed_Design_v2.1_Optimized.html`](docs/Lookalike_Detailed_Design_v2.1_Optimized.html) — v2.0 → v2.1 delta
- [`docs/Lookalike_Detailed_Design_v2.html`](docs/Lookalike_Detailed_Design_v2.html) — full-stack v2.0 archive

## Current code vs v4.0 target

The running API is still the v2.1 MVP+P1-lite (train / process / CSV upload / async generate). That path is **retired in the v4.0 production contract**. Reusable pieces to keep: Feature Adapter, leakage denylist, LightGBM predict, frozen preprocessing.

Production target (not fully implemented yet): scheduling-platform `POST /api/v1/lookalike/runs`, Scoring Workers, idempotent write to CDP ClickHouse using `lookalike_key = {run_batch_id}x{profile_id}`.

```bash
# Process async flow
curl -X POST localhost:8000/api/v1/model/train -H 'Content-Type: application/json' -d '{}'
curl -X POST localhost:8000/api/v1/processes -H 'Content-Type: application/json' -d '{"name":"demo"}'
# upload candidates, then:
curl -X POST localhost:8000/api/v1/processes/{id}/generate -H 'Content-Type: application/json' -d '{}'
curl localhost:8000/api/v1/versions/{vid}/dashboard
```

## Data

Production dataset: **`data/Bank_Marketing_Dataset.csv`**

| Property | Value |
|----------|-------|
| Rows | 100,000 |
| Columns | 45 (incl. `ClientID`, target, features) |
| Target | `TermDepositSubscribed` (0/1, ~30% positive) |
| ID | `ClientID` (maps to BRD HostCif / CIF) |
| Excluded from modeling | `ResponsePropensity` (dropped automatically) |

Key feature groups: demographics (`Age`, `Gender`, `MaritalStatus`, …), financials (`AnnualIncome`, `NetWorth`, `CreditScore`, …), product holdings, transaction/behavior, and marketing contact history.

If the CSV file is missing locally, `python -m lookalike.data.sample_dataset` generates a small **synthetic fallback** for development only.

### Run pipeline on real data

```bash
python scripts/eda_report.py      # writes output/eda_report.xlsx
python scripts/full_process.py    # clean → IV filter → LightGBM (AUC ~0.67 on full set)
```

Column reference: `src/lookalike/data/schema.py`.
