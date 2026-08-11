# Cursor Cloud specific instructions

### Product overview

**Lookalike Audience & Lead Generation API** (Design v2.1 MVP + P1-lite): EDA, IV filtering, LightGBM training, Feature Adapter + leakage denylist, process versions with async generate. See `docs/Lookalike_Detailed_Design_v2.1_Optimized.html`.

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

### Process async flow (P1-lite)

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

Async generate uses FastAPI `BackgroundTasks` (Celery reserved for full P1/P2).

### Gotchas

- Feature Adapter strips `ResponsePropensity` and `ClientID`; leakage denylist hard-fails if they remain.
- Similarity threshold is a **post-scoring filter** only (not a create-process input).
- In-memory process store resets when the API process restarts.
- Full React frontend is **P2** — not in this codebase yet.
