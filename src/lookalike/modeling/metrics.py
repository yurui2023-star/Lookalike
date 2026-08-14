"""Ranking, lift, stability and experiment-sizing metrics for the Lookalike model plan.

Metric naming follows BRD v2.4 section 5.2 (KS table columns
``positive_recall`` / ``negative_recall`` / ``cum_positive_recall`` /
``cum_negative_recall`` / ``ks``).
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

EPSILON = 1e-10


def _as_arrays(y_true: object, y_score: object) -> tuple[np.ndarray, np.ndarray]:
    truth = np.asarray(pd.Series(y_true).astype(float).to_numpy())
    score = np.asarray(pd.Series(y_score).astype(float).to_numpy())
    if truth.shape != score.shape:
        raise ValueError(f"y_true and y_score length mismatch: {truth.shape} vs {score.shape}")
    if truth.size == 0:
        raise ValueError("y_true is empty")
    return truth, score


def _rank_bins(score: np.ndarray, bins: int) -> np.ndarray:
    """Assign rows to ``bins`` groups of near-equal size, group 0 = highest score."""
    order = np.argsort(-score, kind="mergesort")
    ranks = np.empty(score.size, dtype=np.int64)
    ranks[order] = np.arange(score.size)
    group = (ranks * bins) // score.size
    return np.minimum(group, bins - 1)


def ks_table(y_true: object, y_score: object, bins: int = 10) -> pd.DataFrame:
    """Return the decile KS table used for model diagnostics and documentation."""
    truth, score = _as_arrays(y_true, y_score)
    total_positive = float(truth.sum())
    total_negative = float(truth.size - total_positive)
    if total_positive == 0 or total_negative == 0:
        raise ValueError("KS table requires both positive and negative samples")

    group = _rank_bins(score, bins)
    rows: list[dict[str, float]] = []
    for index in range(bins):
        mask = group == index
        bin_total = int(mask.sum())
        if bin_total == 0:
            continue
        bin_positive = float(truth[mask].sum())
        bin_negative = float(bin_total - bin_positive)
        rows.append(
            {
                "bucket": index + 1,
                "min_score": float(score[mask].min()),
                "max_score": float(score[mask].max()),
                "total": bin_total,
                "positive": bin_positive,
                "negative": bin_negative,
                "positive_rate": bin_positive / bin_total,
                "positive_recall": bin_positive / total_positive,
                "negative_recall": bin_negative / total_negative,
            }
        )

    table = pd.DataFrame(rows)
    table["cum_positive_recall"] = table["positive_recall"].cumsum()
    table["cum_negative_recall"] = table["negative_recall"].cumsum()
    table["ks"] = (table["cum_positive_recall"] - table["cum_negative_recall"]).abs()
    return table


def ks_statistic(y_true: object, y_score: object) -> float:
    """Exact KS statistic (max distance between cumulative positive/negative recall)."""
    truth, score = _as_arrays(y_true, y_score)
    total_positive = float(truth.sum())
    total_negative = float(truth.size - total_positive)
    if total_positive == 0 or total_negative == 0:
        raise ValueError("KS requires both positive and negative samples")

    order = np.argsort(-score, kind="mergesort")
    sorted_truth = truth[order]
    sorted_score = score[order]
    cum_positive = np.cumsum(sorted_truth) / total_positive
    cum_negative = np.cumsum(1.0 - sorted_truth) / total_negative
    # Rows sharing a score cannot be separated, so only compare at tie-group boundaries.
    boundary = np.append(sorted_score[:-1] != sorted_score[1:], True)
    return float(np.max(np.abs(cum_positive - cum_negative)[boundary]))


def lift_table(y_true: object, y_score: object, bins: int = 10) -> pd.DataFrame:
    """Decile lift table: per-bucket and cumulative lift plus capture rate."""
    truth, score = _as_arrays(y_true, y_score)
    overall_rate = float(truth.mean())
    if overall_rate <= 0:
        raise ValueError("Lift requires at least one positive sample")

    group = _rank_bins(score, bins)
    rows: list[dict[str, float]] = []
    for index in range(bins):
        mask = group == index
        bin_total = int(mask.sum())
        if bin_total == 0:
            continue
        bin_positive = float(truth[mask].sum())
        rows.append(
            {
                "bucket": index + 1,
                "total": bin_total,
                "positive": bin_positive,
                "positive_rate": bin_positive / bin_total,
                "lift": (bin_positive / bin_total) / overall_rate,
            }
        )

    table = pd.DataFrame(rows)
    table["cum_total"] = table["total"].cumsum()
    table["cum_positive"] = table["positive"].cumsum()
    table["cum_positive_rate"] = table["cum_positive"] / table["cum_total"]
    table["cum_lift"] = table["cum_positive_rate"] / overall_rate
    table["capture_rate"] = table["cum_positive"] / float(truth.sum())
    return table


def lift_at(y_true: object, y_score: object, top_fraction: float = 0.2) -> dict[str, float]:
    """Cumulative lift, response rate and capture rate for the top ``top_fraction``."""
    if not 0 < top_fraction <= 1:
        raise ValueError("top_fraction must be in (0, 1]")
    truth, score = _as_arrays(y_true, y_score)
    overall_rate = float(truth.mean())
    if overall_rate <= 0:
        raise ValueError("Lift requires at least one positive sample")

    top_n = max(1, math.ceil(truth.size * top_fraction))
    order = np.argsort(-score, kind="mergesort")[:top_n]
    top_positive = float(truth[order].sum())
    top_rate = top_positive / top_n
    return {
        "top_fraction": float(top_fraction),
        "selected": int(top_n),
        "positives_selected": top_positive,
        "response_rate": top_rate,
        "overall_rate": overall_rate,
        "lift": top_rate / overall_rate,
        "capture_rate": top_positive / float(truth.sum()),
    }


def segment_lift(
    y_true: object,
    y_score: object,
    segment: object,
    top_fraction: float = 0.2,
    min_positives: int = 20,
) -> pd.DataFrame:
    """Lift computed *within* each segment.

    The business diagnosis shows one dimension (product holding) dominating conversion,
    so a strong global lift can be produced by that dimension alone. Within-segment lift
    shows whether the model still ranks customers usefully inside a homogeneous band.
    """
    truth, score = _as_arrays(y_true, y_score)
    labels = pd.Series(segment).reset_index(drop=True)
    if len(labels) != truth.size:
        raise ValueError("segment length must match y_true")

    rows: list[dict[str, object]] = []
    for value in labels.dropna().unique():
        mask = (labels == value).to_numpy()
        seg_truth = truth[mask]
        seg_score = score[mask]
        positives = float(seg_truth.sum())
        row: dict[str, object] = {
            "segment": value,
            "total": int(seg_truth.size),
            "positives": positives,
            "base_rate": float(seg_truth.mean()) if seg_truth.size else float("nan"),
        }
        if positives >= min_positives and seg_truth.size > 1 and positives < seg_truth.size:
            result = lift_at(seg_truth, seg_score, top_fraction=top_fraction)
            row["response_rate"] = result["response_rate"]
            row["lift"] = result["lift"]
            row["capture_rate"] = result["capture_rate"]
            row["evaluable"] = True
        else:
            row["response_rate"] = float("nan")
            row["lift"] = float("nan")
            row["capture_rate"] = float("nan")
            row["evaluable"] = False
        rows.append(row)

    table = pd.DataFrame(rows)
    return table.sort_values("segment").reset_index(drop=True)


def psi(expected: object, actual: object, bins: int = 10) -> float:
    """Population Stability Index between a reference and a current distribution."""
    reference = pd.Series(expected).dropna().astype(float).to_numpy()
    current = pd.Series(actual).dropna().astype(float).to_numpy()
    if reference.size == 0 or current.size == 0:
        raise ValueError("PSI requires non-empty distributions")

    quantiles = np.linspace(0, 1, bins + 1)
    edges = np.unique(np.quantile(reference, quantiles))
    if edges.size < 2:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf

    reference_share = np.histogram(reference, bins=edges)[0] / reference.size
    current_share = np.histogram(current, bins=edges)[0] / current.size
    reference_share = np.clip(reference_share, EPSILON, None)
    current_share = np.clip(current_share, EPSILON, None)
    ratio = np.log(current_share / reference_share)
    return float(np.sum((current_share - reference_share) * ratio))


def psi_by_feature(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    columns: list[str] | None = None,
    bins: int = 10,
) -> pd.DataFrame:
    """Feature-level PSI between two frames (pre-launch train vs OOT stability check)."""
    numeric = columns or [
        col
        for col in expected.select_dtypes(include=["number"]).columns
        if col in actual.columns
    ]
    rows = [{"feature": col, "psi": psi(expected[col], actual[col], bins=bins)} for col in numeric]
    table = pd.DataFrame(rows, columns=["feature", "psi"])
    if table.empty:
        return table
    return table.sort_values("psi", ascending=False).reset_index(drop=True)


def ab_test_sample_size(
    baseline_rate: float,
    relative_lift: float,
    alpha: float = 0.05,
    power: float = 0.8,
) -> dict[str, float]:
    """Per-arm sample size for a two-proportion A/B test of the BRD primary KPI.

    ``relative_lift`` is expressed as the BRD does (0.2 == the ``>= 1.2 x baseline`` target).
    """
    if not 0 < baseline_rate < 1:
        raise ValueError("baseline_rate must be in (0, 1)")
    if relative_lift <= 0:
        raise ValueError("relative_lift must be positive")

    treatment_rate = baseline_rate * (1 + relative_lift)
    if treatment_rate >= 1:
        raise ValueError("baseline_rate * (1 + relative_lift) must stay below 1")

    normal = NormalDist()
    z_alpha = normal.inv_cdf(1 - alpha / 2)
    z_beta = normal.inv_cdf(power)
    variance = baseline_rate * (1 - baseline_rate) + treatment_rate * (1 - treatment_rate)
    delta = treatment_rate - baseline_rate
    per_arm = ((z_alpha + z_beta) ** 2) * variance / (delta**2)
    return {
        "baseline_rate": baseline_rate,
        "treatment_rate": treatment_rate,
        "absolute_delta": delta,
        "alpha": alpha,
        "power": power,
        "per_arm": math.ceil(per_arm),
        "total": math.ceil(per_arm) * 2,
    }
