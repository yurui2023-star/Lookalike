# Lookalike Model

Minimal **lookalike audience scoring** service. Given a seed audience and a list of candidate users, the API ranks candidates by proximity to the seed profile centroid (age, income, engagement, purchase frequency), using min-max normalization derived from the seed set.

## Stack

- **Python 3.11+** with **FastAPI** and **Uvicorn**
- **NumPy** for feature normalization and distance scoring
- **pytest** + **httpx** for tests
- **ruff** for linting

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run (development)

```bash
source .venv/bin/activate
lookalike-api
# or: uvicorn lookalike.main:app --reload --host 0.0.0.0 --port 8000
```

API docs: http://localhost:8000/docs

## Example request

```bash
curl -s http://localhost:8000/api/v1/lookalike/score \
  -H 'Content-Type: application/json' \
  -d '{
    "seed_users": [
      {"user_id": "seed-1", "age": 32, "income": 85000, "engagement_score": 0.9, "purchase_frequency": 8},
      {"user_id": "seed-2", "age": 35, "income": 92000, "engagement_score": 0.85, "purchase_frequency": 7}
    ],
    "candidates": [
      {"user_id": "candidate-close", "age": 33, "income": 88000, "engagement_score": 0.88, "purchase_frequency": 7.5},
      {"user_id": "candidate-far", "age": 22, "income": 42000, "engagement_score": 0.2, "purchase_frequency": 1}
    ],
    "top_k": 2
  }'
```

## Test & lint

```bash
pytest
ruff check src tests
```
