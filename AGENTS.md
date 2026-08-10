# Cursor Cloud specific instructions

### Product overview

**Lookalike Audience & Lead Generation API** — implements BRD flows for EDA, feature IV analysis, LightGBM training on sample (seed) data, and similarity scoring of 100% of candidate records. Reference logic from `scripts/full_process.py` and `scripts/eda_report.py`.

### Services

| Service | Port | Start command |
|---------|------|---------------|
| Lookalike API | 8000 | `.venv/bin/lookalike-api` |

No database required. Generated artifacts go to `output/` (EDA Excel).

### First-time VM note

If `python3 -m venv .venv` fails, install once:

```bash
sudo apt-get update && sudo apt-get install -y python3.12-venv
```

Generate sample CSV (or let the API create it on startup):

```bash
make data
```

### Lint / test / run

```bash
make install
make lint
make test
make run
```

CLI pipelines:

```bash
make eda        # scripts/eda_report.py
make pipeline   # scripts/full_process.py
```

### Gotchas

- `POST /api/v1/lookalike/score` auto-trains using bundled sample data if no model is loaded yet.
- Similarity threshold is a **post-scoring filter** (BRD): all candidates are scored; threshold only affects `matches` / dashboard counts.
- LightGBM training uses the in-memory pipeline singleton; restarting the API clears the trained model.
