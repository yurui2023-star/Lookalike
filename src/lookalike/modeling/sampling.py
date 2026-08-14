"""Negative sampling strategies and score prior correction.

The business diagnosis recommends training on a 1:10 ~ 1:15 positive/negative ratio.
Down-sampling changes the class prior, so raw model output is no longer a probability of
conversion. Every score exposed to the business (score bands, Hot/Warm/Cool definitions,
month-over-month comparisons) must therefore be brought back to the population scale, either
by prior correction (uniform sampling) or by inverse-probability weights (stratified
sampling across the whole scoring domain).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class NegativeSamplingResult:
    """Down-sampled training frame plus the metadata needed to undo the prior shift."""

    frame: pd.DataFrame
    positives: int
    negatives_before: int
    negatives_after: int
    negative_sampling_rate: float
    requested_ratio: float

    @property
    def achieved_ratio(self) -> float:
        return self.negatives_after / self.positives if self.positives else float("nan")

    @property
    def training_positive_rate(self) -> float:
        total = self.positives + self.negatives_after
        return self.positives / total if total else float("nan")

    def summary(self) -> dict[str, float]:
        return {
            "positives": self.positives,
            "negatives_before": self.negatives_before,
            "negatives_after": self.negatives_after,
            "requested_ratio": self.requested_ratio,
            "achieved_ratio": self.achieved_ratio,
            "negative_sampling_rate": self.negative_sampling_rate,
            "training_positive_rate": self.training_positive_rate,
        }


def downsample_negatives(
    frame: pd.DataFrame,
    label_col: str = "label",
    ratio: float = 10.0,
    random_state: int = 42,
) -> NegativeSamplingResult:
    """Keep every positive and a random ``ratio``-times-larger negative sample."""
    if label_col not in frame.columns:
        raise ValueError(f"label column '{label_col}' not found")
    if ratio <= 0:
        raise ValueError("ratio must be positive")

    labels = frame[label_col].astype(int)
    positive_frame = frame.loc[labels == 1]
    negative_frame = frame.loc[labels == 0]
    positives = len(positive_frame)
    negatives_before = len(negative_frame)
    if positives == 0 or negatives_before == 0:
        raise ValueError("both classes are required for down-sampling")

    target_negatives = min(negatives_before, int(round(positives * ratio)))
    sampled_negatives = negative_frame.sample(
        n=target_negatives, random_state=random_state, replace=False
    )
    combined = (
        pd.concat([positive_frame, sampled_negatives])
        .sample(frac=1.0, random_state=random_state)
        .reset_index(drop=True)
    )
    return NegativeSamplingResult(
        frame=combined,
        positives=positives,
        negatives_before=negatives_before,
        negatives_after=target_negatives,
        negative_sampling_rate=target_negatives / negatives_before,
        requested_ratio=float(ratio),
    )


@dataclass(frozen=True)
class StratifiedSamplingResult:
    """Training frame covering the whole scoring domain plus per-stratum IPW weights."""

    frame: pd.DataFrame
    strata: pd.DataFrame
    weight_col: str

    def summary(self) -> dict[str, float]:
        positives = float(self.strata["positives"].sum())
        return {
            "strata": len(self.strata),
            "positives": positives,
            "negatives_before": float(self.strata["negatives"].sum()),
            "negatives_after": float(self.strata["negatives_sampled"].sum()),
            "rows": len(self.frame),
            "weighted_rows": float(self.frame[self.weight_col].sum()),
        }


def stratified_negative_sample(
    frame: pd.DataFrame,
    stratum_col: str,
    label_col: str = "label",
    ratio: float | Mapping[object, float] = 10.0,
    min_negatives: int = 500,
    weight_col: str = "sample_weight",
    random_state: int = 42,
) -> StratifiedSamplingResult:
    """Keep every positive and sample negatives per stratum, with 1/rate weights.

    Training only on the dense pool leaves the model blind to the strata it will later have
    to score. Sampling every stratum and weighting each sampled negative by ``N_h / n_h``
    keeps the training set small while making the fitted probabilities valid for the whole
    scoring domain, so a single model can score the entire base.
    """
    for column in (stratum_col, label_col):
        if column not in frame.columns:
            raise ValueError(f"column '{column}' not found")

    labels = frame[label_col].astype(int)
    rows: list[pd.DataFrame] = []
    stats: list[dict[str, object]] = []

    for stratum, group in frame.groupby(stratum_col, dropna=False, observed=True):
        group_labels = labels.loc[group.index]
        positives = group.loc[group_labels == 1]
        negatives = group.loc[group_labels == 0]
        stratum_ratio = ratio[stratum] if isinstance(ratio, Mapping) else float(ratio)
        wanted = max(int(round(len(positives) * stratum_ratio)), min_negatives)
        take = min(len(negatives), wanted)
        sampled = (
            negatives.sample(n=take, random_state=random_state, replace=False)
            if take
            else negatives.iloc[:0]
        )
        weight = len(negatives) / take if take else 1.0

        if len(positives):
            rows.append(positives.assign(**{weight_col: 1.0}))
        if take:
            rows.append(sampled.assign(**{weight_col: weight}))
        stats.append(
            {
                "stratum": stratum,
                "customers": len(group),
                "positives": len(positives),
                "negatives": len(negatives),
                "negatives_sampled": take,
                "negative_sampling_rate": take / len(negatives) if len(negatives) else 1.0,
                "negative_weight": weight,
            }
        )

    if not rows:
        raise ValueError("stratified sampling produced no rows")

    combined = (
        pd.concat(rows).sample(frac=1.0, random_state=random_state).reset_index(drop=True)
    )
    return StratifiedSamplingResult(
        frame=combined,
        strata=pd.DataFrame(stats),
        weight_col=weight_col,
    )


def prior_correction(scores: object, negative_sampling_rate: float) -> pd.Series:
    """Map scores from a negative-down-sampled model back to population probabilities.

    With all positives kept and negatives kept at rate ``r``:
    ``odds_population = r * odds_sample`` and therefore
    ``p = r * p_s / (1 - p_s + r * p_s)``.
    """
    if not 0 < negative_sampling_rate <= 1:
        raise ValueError("negative_sampling_rate must be in (0, 1]")

    series = pd.Series(scores, dtype="float64")
    clipped = series.clip(lower=1e-12, upper=1 - 1e-12)
    corrected = (negative_sampling_rate * clipped) / (
        1 - clipped + negative_sampling_rate * clipped
    )
    return pd.Series(np.asarray(corrected), index=series.index, name="calibrated_score")


def score_band_summary(
    scores: object,
    bands: dict[str, tuple[float, float]],
) -> pd.DataFrame:
    """Count customers per marketer-defined score band (BRD 6.4.6 Hot/Warm/Cool preview)."""
    series = pd.Series(scores, dtype="float64")
    total = len(series)
    rows = []
    for name, (low, high) in bands.items():
        mask = (series >= low) & (series < high)
        count = int(mask.sum())
        rows.append(
            {
                "band": name,
                "min_score": low,
                "max_score": high,
                "customers": count,
                "share": count / total if total else float("nan"),
            }
        )
    return pd.DataFrame(rows)
