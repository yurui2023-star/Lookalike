"""Per-tier feature screening (BRD v2.4 section 3.3 applied inside each scoring tier).

Screening the whole base at once hides the problem the tier models exist to solve: a feature
can be well populated for the core pool and empty for single-product or dormant customers.
A global missing rate of 40% may be 5% in ``tier_a_core`` and 92% in ``tier_b_extended``,
and a global IV can be produced entirely by the gap between tiers rather than by ranking
power inside them. Every threshold here is therefore evaluated per tier.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

DEFAULT_MISSING_LIMIT = 0.95
DEFAULT_IDENTICAL_LIMIT = 0.95
DEFAULT_IV_LIMIT = 0.02
DEFAULT_FLAG_MISSING_FROM = 0.30
# Below this many positives the IV of a rare-event tier is dominated by sampling noise, so it
# is reported but never used to drop a feature.
DEFAULT_MIN_POSITIVES_FOR_IV = 200
# Laplace smoothing applied to bin counts; without it an empty-positive bin drives WOE to
# +/- infinity and inflates IV by orders of magnitude.
IV_SMOOTHING = 0.5


def feature_profile(
    frame: pd.DataFrame,
    features: Sequence[str],
    label_col: str | None = None,
) -> pd.DataFrame:
    """Missing rate, identical rate, distinct values and IV for each feature."""
    rows: list[dict[str, object]] = []
    total = len(frame)
    for name in features:
        if name not in frame.columns:
            rows.append(
                {
                    "feature": name,
                    "rows": total,
                    "missing_rate": 1.0,
                    "identical_rate": 1.0,
                    "nunique": 0,
                    "iv": 0.0,
                    "available": False,
                }
            )
            continue

        column = frame[name]
        missing_rate = float(column.isna().mean()) if total else 1.0
        counts = column.value_counts(normalize=True, dropna=False)
        identical_rate = float(counts.max()) if len(counts) else 1.0
        iv = 0.0
        if label_col is not None and label_col in frame.columns:
            iv = _safe_iv(frame, name, label_col)
        rows.append(
            {
                "feature": name,
                "rows": total,
                "missing_rate": missing_rate,
                "identical_rate": identical_rate,
                "nunique": int(column.nunique(dropna=True)),
                "iv": iv,
                "available": True,
            }
        )
    return pd.DataFrame(rows)


def smoothed_iv(
    frame: pd.DataFrame,
    feature: str,
    label_col: str,
    n_bins: int = 10,
    smoothing: float = IV_SMOOTHING,
) -> float:
    """Information Value with Laplace-smoothed bin counts.

    The unsmoothed formula divides by an empty-positive bin. That is the normal case in a
    rare-event tier (a handful of positives per 10,000 rows): WOE hits ±∞ or a 1e-10
    epsilon, and IV is no longer a measure of signal. Smoothing keeps the statistic finite
    and comparable across tiers of very different prevalence.
    """
    subset = frame[[feature, label_col]].dropna(subset=[feature])
    if subset.empty:
        return 0.0

    column = subset[feature]
    if pd.api.types.is_numeric_dtype(column) and column.nunique() > n_bins:
        try:
            bins = pd.qcut(column, q=n_bins, duplicates="drop")
        except ValueError:
            bins = pd.cut(column, bins=n_bins, duplicates="drop")
    else:
        bins = column.astype("object")

    grouped = subset.groupby(bins, observed=True)[label_col].agg(["count", "sum"])
    if grouped.empty:
        return 0.0

    positives = grouped["sum"].to_numpy(dtype=float)
    negatives = (grouped["count"] - grouped["sum"]).to_numpy(dtype=float)
    total_positive, total_negative = positives.sum(), negatives.sum()
    if total_positive <= 0 or total_negative <= 0:
        return 0.0

    bin_count = len(grouped)
    positive_share = (positives + smoothing) / (total_positive + smoothing * bin_count)
    negative_share = (negatives + smoothing) / (total_negative + smoothing * bin_count)
    woe = np.log(positive_share / negative_share)
    return float(np.sum((positive_share - negative_share) * woe))


def _safe_iv(frame: pd.DataFrame, feature: str, label_col: str) -> float:
    try:
        return smoothed_iv(frame, feature, label_col)
    except Exception:
        return 0.0


def _min_cohort_iv(
    frame: pd.DataFrame,
    feature: str,
    label_col: str,
    stability_col: str,
    min_positives: int = 30,
) -> float:
    """Lowest IV across cohorts; a feature must hold up in every observation period."""
    if feature not in frame.columns:
        return 0.0
    values: list[float] = []
    for _, group in frame.groupby(stability_col, observed=True):
        if int(group[label_col].sum()) < min_positives:
            continue
        values.append(_safe_iv(group, feature, label_col))
    if not values:
        return _safe_iv(frame, feature, label_col)
    return float(min(values))


def screen_features(
    frame: pd.DataFrame,
    features: Sequence[str],
    label_col: str,
    *,
    missing_limit: float = DEFAULT_MISSING_LIMIT,
    identical_limit: float = DEFAULT_IDENTICAL_LIMIT,
    iv_limit: float = DEFAULT_IV_LIMIT,
    flag_missing_from: float = DEFAULT_FLAG_MISSING_FROM,
    always_keep: Sequence[str] = (),
    min_positives_for_iv: int = DEFAULT_MIN_POSITIVES_FOR_IV,
    stability_col: str | None = None,
) -> pd.DataFrame:
    """Apply the BRD 3.3 thresholds and return a keep/drop decision with a reason.

    Two guards protect the sparse tiers, where a handful of positives can push a useless
    feature above the IV threshold:

    * when the tier holds fewer than ``min_positives_for_iv`` positives, IV is reported but
      never used to drop a feature;
    * when ``stability_col`` (typically the observation cohort) is given, the binding value is
      the *lowest* IV across cohorts, so a feature has to be predictive in every period.
    """
    profile = feature_profile(frame, features, label_col)
    protected = set(always_keep)
    positives = int(frame[label_col].sum()) if label_col in frame.columns else 0
    iv_reliable = positives >= min_positives_for_iv
    profile["tier_positives"] = positives
    profile["iv_reliable"] = iv_reliable

    if stability_col is not None and stability_col in frame.columns:
        profile["iv_min_cohort"] = [
            _min_cohort_iv(frame, name, label_col, stability_col) for name in profile["feature"]
        ]
        criterion = profile["iv_min_cohort"]
    else:
        profile["iv_min_cohort"] = float("nan")
        criterion = profile["iv"]
    profile["iv_criterion"] = criterion

    decisions: list[str] = []
    reasons: list[str] = []
    flags: list[bool] = []
    for row in profile.to_dict("records"):
        name = row["feature"]
        if not row["available"]:
            decisions.append("drop")
            reasons.append("column absent")
        elif name in protected:
            decisions.append("keep")
            reasons.append("protected")
        elif row["missing_rate"] > missing_limit:
            decisions.append("drop")
            reasons.append(f"missing rate {row['missing_rate']:.1%} > {missing_limit:.0%}")
        elif row["identical_rate"] > identical_limit:
            decisions.append("drop")
            reasons.append(f"identical rate {row['identical_rate']:.1%} > {identical_limit:.0%}")
        elif iv_reliable and row["iv_criterion"] < iv_limit:
            decisions.append("drop")
            reasons.append(f"IV {row['iv_criterion']:.4f} < {iv_limit}")
        else:
            decisions.append("keep")
            reasons.append("passed" if iv_reliable else f"passed (IV unreliable, {positives} pos)")
        flags.append(
            decisions[-1] == "keep" and flag_missing_from <= row["missing_rate"] <= missing_limit
        )

    profile["decision"] = decisions
    profile["reason"] = reasons
    profile["add_missing_indicator"] = flags
    return profile


def tier_feature_plan(
    frame: pd.DataFrame,
    tier_col: str,
    label_col: str,
    features_by_tier: Mapping[str, Sequence[str]],
    **screening_kwargs: object,
) -> pd.DataFrame:
    """Run the screening inside each tier and stack the results."""
    plans = []
    for tier, features in features_by_tier.items():
        subset = frame.loc[frame[tier_col] == tier]
        if subset.empty:
            continue
        plan = screen_features(subset, features, label_col, **screening_kwargs)  # type: ignore[arg-type]
        plan.insert(0, "tier", tier)
        plans.append(plan)
    if not plans:
        return pd.DataFrame(
            columns=["tier", "feature", "missing_rate", "identical_rate", "iv", "decision"]
        )
    return pd.concat(plans, ignore_index=True)


def plan_summary(plan: pd.DataFrame) -> pd.DataFrame:
    """Kept/dropped counts per tier, with the dominant drop reason."""
    rows = []
    for tier, group in plan.groupby("tier", observed=True):
        dropped = group.loc[group["decision"] == "drop"]
        reason_kind = dropped["reason"].str.split(" ").str[0]
        rows.append(
            {
                "tier": tier,
                "evaluated": len(group),
                "kept": int((group["decision"] == "keep").sum()),
                "dropped": len(dropped),
                "dropped_missing": int((reason_kind == "missing").sum()),
                "dropped_identical": int((reason_kind == "identical").sum()),
                "dropped_low_iv": int((reason_kind == "IV").sum()),
                "dropped_absent": int((reason_kind == "column").sum()),
                "missing_indicators": int(group["add_missing_indicator"].sum()),
            }
        )
    return pd.DataFrame(rows)


def selected_features(plan: pd.DataFrame, tier: str) -> list[str]:
    """Features kept for one tier, ordered by IV descending."""
    subset = plan.loc[(plan["tier"] == tier) & (plan["decision"] == "keep")]
    return subset.sort_values("iv", ascending=False)["feature"].tolist()


def compare_tiers(plan: pd.DataFrame, left: str, right: str) -> pd.DataFrame:
    """Side-by-side view of how the same feature behaves in two tiers."""
    columns = ["feature", "missing_rate", "identical_rate", "iv", "decision"]
    left_frame = plan.loc[plan["tier"] == left, columns].set_index("feature")
    right_frame = plan.loc[plan["tier"] == right, columns].set_index("feature")
    merged = left_frame.join(right_frame, lsuffix=f"_{left}", rsuffix=f"_{right}", how="outer")
    return merged.reset_index()
