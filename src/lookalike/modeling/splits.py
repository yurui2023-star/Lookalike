"""Dataset splitting for the Lookalike training protocol.

BRD v2.4 section 5.1 asks for a stratified 60/20/20 split plus an Out-of-Time (OOT)
validation window. When several monthly observation cohorts are stacked into one panel the
same customer can appear more than once, so the random split must be group-aware
(all rows of one customer stay inside one subset) to avoid optimistic metrics.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class OutOfTimeSplit:
    """Development cohorts (train/validation/test) and the reserved later cohorts."""

    development: pd.DataFrame
    out_of_time: pd.DataFrame
    development_cohorts: list[object]
    out_of_time_cohorts: list[object]


def split_out_of_time(
    frame: pd.DataFrame,
    cohort_col: str,
    oot_cohorts: int | Sequence[object] = 1,
) -> OutOfTimeSplit:
    """Reserve the latest cohort(s) as OOT validation data."""
    if cohort_col not in frame.columns:
        raise ValueError(f"cohort column '{cohort_col}' not found")

    ordered = sorted(frame[cohort_col].dropna().unique())
    if isinstance(oot_cohorts, int):
        if oot_cohorts <= 0 or oot_cohorts >= len(ordered):
            raise ValueError(
                f"oot_cohorts must be between 1 and {len(ordered) - 1}, got {oot_cohorts}"
            )
        holdout = ordered[-oot_cohorts:]
    else:
        holdout = [cohort for cohort in ordered if cohort in set(oot_cohorts)]
        if not holdout or len(holdout) == len(ordered):
            raise ValueError("oot_cohorts must select a non-empty strict subset of cohorts")

    holdout_set = set(holdout)
    mask = frame[cohort_col].isin(holdout_set)
    return OutOfTimeSplit(
        development=frame.loc[~mask].reset_index(drop=True),
        out_of_time=frame.loc[mask].reset_index(drop=True),
        development_cohorts=[cohort for cohort in ordered if cohort not in holdout_set],
        out_of_time_cohorts=list(holdout),
    )


def stratified_split(
    frame: pd.DataFrame,
    label_col: str = "label",
    ratios: tuple[float, float, float] = (0.6, 0.2, 0.2),
    group_col: str | None = None,
    random_state: int = 42,
) -> dict[str, pd.DataFrame]:
    """Split into train/validation/test.

    Without ``group_col`` the split is stratified on the label. With ``group_col`` whole
    groups are allocated so that no customer appears in two subsets; the label is then
    balanced by ordering groups so positives are spread across subsets.
    """
    if label_col not in frame.columns:
        raise ValueError(f"label column '{label_col}' not found")
    if len(ratios) != 3 or not np.isclose(sum(ratios), 1.0):
        raise ValueError("ratios must be three values summing to 1.0")

    if group_col is None:
        return _stratified_row_split(frame, label_col, ratios, random_state)
    return _grouped_split(frame, label_col, group_col, ratios, random_state)


def _stratified_row_split(
    frame: pd.DataFrame,
    label_col: str,
    ratios: tuple[float, float, float],
    random_state: int,
) -> dict[str, pd.DataFrame]:
    from sklearn.model_selection import train_test_split

    train_ratio, validation_ratio, _ = ratios
    train, remainder = train_test_split(
        frame,
        train_size=train_ratio,
        random_state=random_state,
        stratify=frame[label_col],
    )
    validation_share = validation_ratio / (1 - train_ratio)
    validation, test = train_test_split(
        remainder,
        train_size=validation_share,
        random_state=random_state,
        stratify=remainder[label_col],
    )
    return {
        "train": train.reset_index(drop=True),
        "validation": validation.reset_index(drop=True),
        "test": test.reset_index(drop=True),
    }


def _grouped_split(
    frame: pd.DataFrame,
    label_col: str,
    group_col: str,
    ratios: tuple[float, float, float],
    random_state: int,
) -> dict[str, pd.DataFrame]:
    if group_col not in frame.columns:
        raise ValueError(f"group column '{group_col}' not found")

    stats = (
        frame.groupby(group_col)[label_col]
        .agg(["size", "sum"])
        .rename(columns={"size": "rows", "sum": "positives"})
    )
    rng = np.random.default_rng(random_state)
    stats = stats.sample(frac=1.0, random_state=random_state)
    # Interleave positive-carrying groups so every subset receives a similar positive rate.
    with_positive = stats.loc[stats["positives"] > 0]
    without_positive = stats.loc[stats["positives"] == 0]
    ordered_groups = _interleave(
        with_positive.index.tolist(), without_positive.index.tolist(), rng
    )

    total_rows = int(stats["rows"].sum())
    targets = {
        "train": ratios[0] * total_rows,
        "validation": ratios[1] * total_rows,
        "test": ratios[2] * total_rows,
    }
    assigned: dict[str, list[object]] = {name: [] for name in targets}
    filled = dict.fromkeys(targets, 0.0)
    for group in ordered_groups:
        deficits = {name: targets[name] - filled[name] for name in targets}
        chosen = max(deficits, key=lambda name: deficits[name])
        assigned[chosen].append(group)
        filled[chosen] += float(stats.loc[group, "rows"])

    return {
        name: frame.loc[frame[group_col].isin(set(groups))].reset_index(drop=True)
        for name, groups in assigned.items()
    }


def _interleave(
    primary: list[object], secondary: list[object], rng: np.random.Generator
) -> list[object]:
    """Round-robin two group lists so positives are spread evenly across allocations."""
    if not primary:
        return list(secondary)
    if not secondary:
        return list(primary)
    step = max(1, len(secondary) // len(primary))
    merged: list[object] = []
    secondary_iter = iter(secondary)
    for group in primary:
        merged.append(group)
        for _ in range(step):
            nxt = next(secondary_iter, None)
            if nxt is None:
                break
            merged.append(nxt)
    merged.extend(list(secondary_iter))
    _ = rng
    return merged


def describe_splits(
    splits: dict[str, pd.DataFrame],
    label_col: str = "label",
    group_col: str | None = None,
) -> pd.DataFrame:
    """Row counts, positive rates and (optionally) group counts per subset."""
    rows = []
    for name, subset in splits.items():
        row: dict[str, object] = {
            "subset": name,
            "rows": len(subset),
            "positives": int(subset[label_col].sum()) if len(subset) else 0,
            "positive_rate": float(subset[label_col].mean()) if len(subset) else float("nan"),
        }
        if group_col is not None and group_col in subset.columns:
            row["groups"] = int(subset[group_col].nunique())
        rows.append(row)
    return pd.DataFrame(rows)
