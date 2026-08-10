# AGENTS.md

## Cursor Cloud specific instructions

### Product overview

Single-service **Lookalike Model API** (Python / FastAPI). Ranks candidate user profiles by similarity to a seed audience. See `README.md` for setup, endpoints, and example requests.

### Services

| Service | Port | Start command |
|---------|------|---------------|
| Lookalike API | 8000 | `.venv/bin/lookalike-api` or `.venv/bin/uvicorn lookalike.main:app --reload --host 0.0.0.0 --port 8000` |

No database or external services are required.

### First-time VM note

Ubuntu images may ship without `python3-venv`. If `python3 -m venv .venv` fails, install it once (not part of the update script):

```bash
sudo apt-get update && sudo apt-get install -y python3.12-venv
```

### Lint / test / run

Activate or prefix commands with `.venv/bin/`:

```bash
make install   # python3 -m venv .venv && pip install -e ".[dev]"
make lint      # ruff check src tests
make test      # pytest
make run       # lookalike-api on port 8000
```

Interactive API docs: http://localhost:8000/docs

### Gotchas

- Always use the project virtualenv (`.venv/bin/...`); system Python does not include project dependencies.
- The scoring logic lives in `src/lookalike/model.py` and normalizes features using seed min/max before distance scoring — small seed sets (1–2 users) are supported.
