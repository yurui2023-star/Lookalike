# Cursor Cloud specific instructions

### Product overview

**Lookalike monthly batch scoring** (Design v4.0 target): no frontend; pre-production trains/publishes models; production scores the Smart Sales candidate snapshot monthly and writes scores to Sales CDP ClickHouse. See `docs/Lookalike_Detailed_Design_v4.0_BatchScoring.html`.

The **running code** is still v2.1 MVP+P1-lite (EDA, train, process/upload/generate). Do not treat upload/Segment/React as the production contract. Reuse Adapter + leakage denylist + LightGBM predict.

### Services

| Service | Port | Start command |
|---------|------|---------------|
| Lookalike API | 8000 | `.venv/bin/lookalike-api` |

No database required for MVP/P1-lite (in-memory process store). Artifacts go to `output/`.

### First-time VM note

If `python3 -m venv .venv` fails, install once:

```bash
sudo apt-get update && sudo apt-get install -y python3.12-venv
```

### Lint / test / run

```bash
make install
make lint
make test
make run
```

### Process async flow (P1)

Durable store under `data/store/` (processes, versions, candidate CSVs, score tables).

```bash
# 1) train
curl -X POST localhost:8000/api/v1/model/train -H 'Content-Type: application/json' -d '{}'
# 2) create process + upload candidates + generate
curl -X POST localhost:8000/api/v1/processes -H 'Content-Type: application/json' -d '{"name":"demo"}'
curl -X POST localhost:8000/api/v1/processes/{id}/candidates/upload -F file=@data/Bank_Marketing_Dataset.csv
curl -X POST localhost:8000/api/v1/processes/{id}/generate -H 'Content-Type: application/json' -d '{}'
# 3) poll version / dashboard
curl localhost:8000/api/v1/versions/{vid}
curl localhost:8000/api/v1/versions/{vid}/dashboard
```

Async generate uses FastAPI `BackgroundTasks` (Celery reserved for later scale-out).

### Gotchas

- Feature Adapter strips denylist fields (e.g. `ResponsePropensity`, `ClientID`); hard-fails if they remain.
- Similarity threshold is a **post-scoring filter** only (not a create-process input).
- Process/version metadata survives API restart via `data/store/` (gitignored).
- v4.0 retires file upload, Segment scoring, and the Lookalike frontend. CDP ClickHouse write is the production result path (not a P2 extra).
- Current process/upload/generate APIs remain in the repo until the Run API landing in v4.0 §15.
