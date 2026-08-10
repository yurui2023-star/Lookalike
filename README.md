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
python -m lookalike.data.sample_dataset   # creates data/Bank_Marketing_Dataset.csv
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

## Data

Default dataset: `data/Bank_Marketing_Dataset.csv` (synthetic bank marketing records with `ClientID`, features, and `TermDepositSubscribed` target). Replace with your CDP export using the same column conventions.
