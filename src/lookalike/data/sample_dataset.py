"""Fallback synthetic dataset (only when data/Bank_Marketing_Dataset.csv is missing)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from lookalike.config import DEFAULT_DATA_FILE, ID_COL, TARGET_COL


def generate_bank_marketing_dataset(n_rows: int = 2000, random_state: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(random_state)
    jobs = ["admin.", "blue-collar", "entrepreneur", "management", "retired", "student"]
    marital = ["married", "single", "divorced"]
    education = ["primary", "secondary", "tertiary", "unknown"]
    contact = ["cellular", "telephone", "unknown"]
    month = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    poutcome = ["unknown", "failure", "success", "other"]

    age = rng.integers(18, 80, size=n_rows)
    balance = rng.normal(1500, 3000, size=n_rows).clip(-2000, 50000)
    duration = rng.integers(0, 1800, size=n_rows)
    campaign = rng.integers(1, 8, size=n_rows)
    pdays = rng.choice([-1, *range(1, 30)], size=n_rows, p=[0.7] + [0.3 / 29] * 29)
    previous = rng.integers(0, 6, size=n_rows)
    engagement = rng.uniform(0, 1, size=n_rows)

    subscribed = (
        (age > 30).astype(int)
        + (balance > 1000).astype(int)
        + (duration > 200).astype(int)
        + (engagement > 0.6).astype(int)
        + rng.integers(0, 2, size=n_rows)
    ) >= 3

    return pd.DataFrame(
        {
            ID_COL: [f"CIF{i:06d}" for i in range(1, n_rows + 1)],
            "age": age,
            "job": rng.choice(jobs, size=n_rows),
            "marital": rng.choice(marital, size=n_rows),
            "education": rng.choice(education, size=n_rows),
            "default": rng.choice(["no", "yes"], size=n_rows, p=[0.95, 0.05]),
            "balance": balance.round(2),
            "housing": rng.choice(["no", "yes"], size=n_rows),
            "loan": rng.choice(["no", "yes"], size=n_rows, p=[0.85, 0.15]),
            "contact": rng.choice(contact, size=n_rows),
            "day": rng.integers(1, 31, size=n_rows),
            "month": rng.choice(month, size=n_rows),
            "duration": duration,
            "campaign": campaign,
            "pdays": pdays,
            "previous": previous,
            "poutcome": rng.choice(poutcome, size=n_rows),
            "engagement_score": engagement.round(4),
            TARGET_COL: subscribed.astype(int),
            "ResponsePropensity": rng.uniform(0, 1, size=n_rows).round(4),
        }
    )


def ensure_sample_dataset(path: Path | None = None) -> Path:
    path = path or DEFAULT_DATA_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        generate_bank_marketing_dataset().to_csv(path, index=False)
    return path


if __name__ == "__main__":
    output = ensure_sample_dataset()
    print(f"Sample dataset written to {output}")
