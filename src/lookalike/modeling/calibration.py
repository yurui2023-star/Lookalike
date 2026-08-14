"""Score calibration and calibration diagnostics.

A Lookalike score is only usable by marketing if it means the same thing everywhere:
across score bands, across months, and across the scoring tiers of the customer base. A model
fitted on the dense screened pool systematically over-estimates the sparse tiers it never saw
during training, so the calibration gap has to be measured per tier and corrected before the
score is exposed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

EPSILON = 1e-12


def _to_series(values: object, name: str) -> pd.Series:
    series = pd.Series(values, dtype="float64").reset_index(drop=True)
    series.name = name
    return series


def _logit(probability: np.ndarray) -> np.ndarray:
    clipped = np.clip(probability, EPSILON, 1 - EPSILON)
    return np.log(clipped / (1 - clipped))


def _sigmoid(value: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-value))


def calibration_report(
    y_true: object,
    y_score: object,
    segment: object | None = None,
    weights: object | None = None,
) -> pd.DataFrame:
    """Compare predicted mean score against observed rate, overall and per segment."""
    truth = _to_series(y_true, "label")
    score = _to_series(y_score, "score")
    if len(truth) != len(score):
        raise ValueError("y_true and y_score must have the same length")
    weight = (
        _to_series(weights, "weight")
        if weights is not None
        else pd.Series(1.0, index=truth.index, name="weight")
    )
    labels = (
        pd.Series(segment).reset_index(drop=True)
        if segment is not None
        else pd.Series("all", index=truth.index)
    )

    frame = pd.DataFrame(
        {"label": truth, "score": score, "weight": weight, "segment": labels}
    )
    rows = []
    groups = list(frame.groupby("segment", dropna=False, observed=True))
    groups.append(("ALL", frame))
    for name, group in groups:
        total_weight = float(group["weight"].sum())
        if total_weight <= 0:
            continue
        actual = float((group["label"] * group["weight"]).sum() / total_weight)
        predicted = float((group["score"] * group["weight"]).sum() / total_weight)
        rows.append(
            {
                "segment": name,
                "rows": len(group),
                "actual_rate": actual,
                "predicted_rate": predicted,
                "ratio": predicted / actual if actual > 0 else float("nan"),
                "gap": predicted - actual,
            }
        )
    return pd.DataFrame(rows)


def expected_calibration_error(
    y_true: object,
    y_score: object,
    bins: int = 10,
    weights: object | None = None,
) -> float:
    """Weighted mean absolute gap between predicted and observed rate across score bins."""
    truth = _to_series(y_true, "label")
    score = _to_series(y_score, "score")
    weight = (
        _to_series(weights, "weight")
        if weights is not None
        else pd.Series(1.0, index=truth.index, name="weight")
    )
    if truth.empty:
        raise ValueError("y_true is empty")

    order = score.rank(method="first", ascending=True)
    bucket = np.minimum(((order - 1) * bins // len(score)).astype(int), bins - 1)
    frame = pd.DataFrame(
        {"label": truth, "score": score, "weight": weight, "bucket": bucket}
    )
    total_weight = float(frame["weight"].sum())
    error = 0.0
    for _, group in frame.groupby("bucket", observed=True):
        group_weight = float(group["weight"].sum())
        if group_weight <= 0:
            continue
        actual = float((group["label"] * group["weight"]).sum() / group_weight)
        predicted = float((group["score"] * group["weight"]).sum() / group_weight)
        error += (group_weight / total_weight) * abs(predicted - actual)
    return float(error)


@dataclass
class SegmentCalibrator:
    """Per-segment recalibration of scores onto observed conversion rates.

    ``method="offset"`` shifts the log-odds of each segment by a constant so the mean
    predicted rate matches the observed rate. It needs very few positives, which matters for
    the sparse tiers of the base. ``method="isotonic"`` fits a monotone mapping per segment
    and needs more data, so it falls back to the offset when a segment is too small.
    """

    method: str = "offset"
    min_positives: int = 30
    offsets: dict[object, float] = field(default_factory=dict)
    isotonic: dict[object, object] = field(default_factory=dict)
    global_offset: float = 0.0

    def fit(
        self,
        y_true: object,
        y_score: object,
        segment: object,
        weights: object | None = None,
    ) -> SegmentCalibrator:
        if self.method not in {"offset", "isotonic"}:
            raise ValueError("method must be 'offset' or 'isotonic'")

        truth = _to_series(y_true, "label")
        score = _to_series(y_score, "score")
        weight = (
            _to_series(weights, "weight")
            if weights is not None
            else pd.Series(1.0, index=truth.index, name="weight")
        )
        labels = pd.Series(segment).reset_index(drop=True)
        frame = pd.DataFrame(
            {"label": truth, "score": score, "weight": weight, "segment": labels}
        )

        self.global_offset = _fit_offset(frame)
        self.offsets = {}
        self.isotonic = {}
        for name, group in frame.groupby("segment", dropna=False, observed=True):
            positives = float((group["label"] * group["weight"]).sum())
            if positives < 1:
                continue
            self.offsets[name] = _fit_offset(group)
            if self.method == "isotonic" and positives >= self.min_positives:
                from sklearn.isotonic import IsotonicRegression

                model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
                model.fit(group["score"], group["label"], sample_weight=group["weight"])
                self.isotonic[name] = model
        return self

    def transform(self, y_score: object, segment: object) -> pd.Series:
        score = _to_series(y_score, "score")
        labels = pd.Series(segment).reset_index(drop=True)
        result = pd.Series(np.nan, index=score.index, dtype="float64")

        for name, group in score.groupby(labels, dropna=False, observed=True):
            model = self.isotonic.get(name)
            if model is not None:
                result.loc[group.index] = model.predict(group.to_numpy())
                continue
            offset = self.offsets.get(name, self.global_offset)
            result.loc[group.index] = _sigmoid(_logit(group.to_numpy()) + offset)

        result.name = "calibrated_score"
        return result

    def fit_transform(
        self,
        y_true: object,
        y_score: object,
        segment: object,
        weights: object | None = None,
    ) -> pd.Series:
        return self.fit(y_true, y_score, segment, weights).transform(y_score, segment)


def _fit_offset(frame: pd.DataFrame, iterations: int = 80) -> float:
    """Log-odds shift that makes the mean calibrated score equal the observed rate.

    ``mean(sigmoid(logit(p) + offset))`` increases monotonically in ``offset``, so a bisection
    finds the exact shift. Solving it numerically (instead of shifting the mean log-odds)
    matters for rare events, where the sigmoid is strongly non-linear.
    """
    total_weight = float(frame["weight"].sum())
    if total_weight <= 0:
        return 0.0

    weights = frame["weight"].to_numpy()
    actual = float((frame["label"].to_numpy() * weights).sum() / total_weight)
    logits = _logit(frame["score"].to_numpy())

    def mean_rate(offset: float) -> float:
        return float((_sigmoid(logits + offset) * weights).sum() / total_weight)

    low, high = -50.0, 50.0
    if actual <= 0:
        return low
    if actual >= 1:
        return high
    for _ in range(iterations):
        middle = (low + high) / 2
        if mean_rate(middle) < actual:
            low = middle
        else:
            high = middle
    return float((low + high) / 2)
