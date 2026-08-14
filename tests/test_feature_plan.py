"""Tests for the v1.2 feature catalog and the per-tier feature screening."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lookalike.domain.feature_catalog import (
    CORE_FEATURES,
    COVERAGE_LOW,
    D_APP_EVENTS,
    D_CIC,
    D_INTERNAL_TXN,
    OPTIONAL_FEATURES,
    catalog_frame,
    categorical_features,
    coverage_matrix,
    delivery_summary,
    get_feature,
    monotone_constraints,
    planned_features,
    unconfirmed_source_features,
)
from lookalike.features.iv import calculate_iv_categorical
from lookalike.modeling.feature_selection import (
    compare_tiers,
    feature_profile,
    plan_summary,
    screen_features,
    selected_features,
    smoothed_iv,
    tier_feature_plan,
)

# --------------------------------------------------------------------------- catalog


def test_catalog_matches_the_v12_workbook_counts():
    assert len(CORE_FEATURES) == 69
    assert len(OPTIONAL_FEATURES) == 30
    assert len(catalog_frame()) == 99
    assert len(catalog_frame(include_optional=False)) == 69


def test_catalog_feature_names_are_unique():
    names = [spec.name for spec in CORE_FEATURES + OPTIONAL_FEATURES]
    assert len(names) == len(set(names))


def test_eleven_product_holding_features_still_lack_a_source():
    unconfirmed = unconfirmed_source_features()
    assert len(unconfirmed) == 11
    assert "total_product_count" in unconfirmed
    assert "casa_flag" in unconfirmed
    # Everything else in the workbook does name a source.
    assert "core_product_category_count" not in unconfirmed


def test_tier_b_plan_is_a_strict_subset_of_tier_a():
    tier_a = set(planned_features("tier_a_core"))
    tier_b = set(planned_features("tier_b_extended"))
    assert tier_b < tier_a
    assert len(tier_a) == 69
    assert 25 <= len(tier_b) <= 45
    # Income and spending groups are the ones that disappear in the extended tier.
    assert "avg_salary_amount_6m" in tier_a and "avg_salary_amount_6m" not in tier_b
    assert "age" in tier_b and "total_product_count" in tier_b


def test_optional_cashflow_features_never_enter_the_tier_b_plan():
    tier_b = planned_features("tier_b_extended", include_optional=True)
    assert not any(get_feature(name).optional for name in tier_b)
    tier_a = planned_features("tier_a_core", include_optional=True)
    assert len(tier_a) == 99


def test_excluding_a_delivery_workstream_shrinks_the_plan():
    full = planned_features("tier_a_core")
    without_cic = planned_features("tier_a_core", exclude_delivery={D_CIC})
    assert len(full) - len(without_cic) == 8
    v1 = planned_features(
        "tier_a_core", exclude_delivery={D_INTERNAL_TXN, D_APP_EVENTS, D_CIC}
    )
    assert len(v1) == 29


def test_monotone_constraints_align_with_feature_order():
    features = ["total_product_count", "internal_npl_flag", "marital_status"]
    assert monotone_constraints(features) == [1, -1, 0]
    assert len(monotone_constraints(planned_features("tier_a_core"))) == 69


def test_categorical_features_are_the_cat_typed_ones():
    categoricals = categorical_features(planned_features("tier_a_core"))
    assert set(categoricals) == {
        "marital_status",
        "education_level",
        "occupation_type",
        "region",
        "balance_trend_6m",
        "cic_score_trend",
        "salary_trend_6m",
        "app_usage_time_preference",
    }


def test_coverage_matrix_and_delivery_summary_are_consistent():
    matrix = coverage_matrix()
    assert matrix["total"].sum() == 69
    assert (matrix["tier_b_planned"] <= matrix["total"]).all()

    summary = delivery_summary()
    assert summary["features"].sum() == 99
    assert summary["tier_a_planned"].sum() == 99
    assert delivery_summary(include_optional=False)["tier_a_planned"].sum() == 69
    unconfirmed_row = summary.loc[summary["delivery"] == "D3_product_holding_source_tbd"].iloc[0]
    assert unconfirmed_row["source_confirmed"] == 0


def test_income_group_is_marked_low_coverage_for_the_tail():
    assert get_feature("disposable_income_3m").tier_b_coverage == COVERAGE_LOW
    assert get_feature("age").tier_b_coverage == "high"


def test_unknown_feature_and_tier_raise():
    with pytest.raises(ValueError):
        get_feature("not_a_feature")
    with pytest.raises(ValueError):
        planned_features("tier_z")


# --------------------------------------------------------------------------- screening


def make_frame(seed: int = 3, n: int = 20_000) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    signal = rng.normal(size=n)
    return pd.DataFrame(
        {
            "cohort": rng.choice(["2025Q1", "2025Q2", "2025Q3"], n),
            "tier": rng.choice(["core", "tail"], n, p=[0.3, 0.7]),
            "good_feature": signal,
            "noise_feature": rng.normal(size=n),
            "constant_feature": 1.0,
            "mostly_missing": np.where(rng.random(n) < 0.02, rng.normal(size=n), np.nan),
            "label": (signal + rng.normal(scale=0.5, size=n) > 1.0).astype(int),
        }
    )


def test_smoothed_iv_stays_finite_when_a_bin_has_no_positives():
    frame = pd.DataFrame(
        {
            "x": list(range(200)),
            "label": [0] * 190 + [1] * 10,
        }
    )
    value = smoothed_iv(frame, "x", "label")
    assert np.isfinite(value)
    assert 0 < value < 20


def test_empty_positive_bins_inflate_unsmoothed_iv():
    """Hand-checkable illustration of the empty-bin trap; not MB production data.

    10,000 rows, 3 positives (3 per 10k, same order as the 2.5/10k planning prior),
    four equal category bins, all three positives in bin A. The unsmoothed WOE in
    B/C/D uses a 1e-10 epsilon in place of ln(0) and each empty bin contributes
    ~5.4 of IV, so the total is ~17. Laplace smoothing (α=0.5) brings it under 1.
    """
    frame = pd.DataFrame(
        {
            "x": ["A"] * 2500 + ["B"] * 2500 + ["C"] * 2500 + ["D"] * 2500,
            "label": [1, 1, 1] + [0] * 9997,
        }
    )
    _, unsmoothed = calculate_iv_categorical(frame, "x", "label")
    smoothed = smoothed_iv(frame, "x", "label")
    assert unsmoothed == pytest.approx(17.275, abs=0.01)
    assert np.isfinite(smoothed)
    assert smoothed < 1.0


def test_noise_iv_exceeds_brd_threshold_with_a_few_hundred_positives():
    """Synthetic frame: 1,197 rows / 234 positives / N(0,1) noise, seed 14.

    This is the number that was previously quoted in the spec as if it came from
    a 2.5/10k tail. It is a unit-test fixture. A pure-noise feature still clears
    IV≥0.02, which is why scarce tiers must not drop on IV.
    """
    rng = np.random.default_rng(14)
    n, n_pos = 1197, 234
    label = np.zeros(n, dtype=int)
    label[:n_pos] = 1
    rng.shuffle(label)
    frame = pd.DataFrame({"noise": rng.normal(size=n), "label": label})
    assert smoothed_iv(frame, "noise", "label") == pytest.approx(0.0384, abs=0.0005)
    plan = screen_features(
        frame, ["noise"], "label", min_positives_for_iv=500
    ).set_index("feature")
    assert plan.loc["noise", "decision"] == "keep"
    assert not bool(plan.loc["noise", "iv_reliable"])


def test_smoothed_iv_is_zero_without_variation():
    frame = pd.DataFrame({"x": [1.0] * 100, "label": [0, 1] * 50})
    assert smoothed_iv(frame, "x", "label") == pytest.approx(0.0, abs=1e-9)


def test_feature_profile_reports_absent_columns():
    frame = make_frame()
    profile = feature_profile(frame, ["good_feature", "ghost"], "label").set_index("feature")
    assert bool(profile.loc["good_feature", "available"]) is True
    assert bool(profile.loc["ghost", "available"]) is False
    assert profile.loc["ghost", "missing_rate"] == 1.0


def test_screen_features_applies_each_brd_threshold():
    frame = make_frame()
    plan = screen_features(
        frame,
        ["good_feature", "noise_feature", "constant_feature", "mostly_missing", "ghost"],
        "label",
    ).set_index("feature")

    assert plan.loc["good_feature", "decision"] == "keep"
    assert plan.loc["constant_feature", "decision"] == "drop"
    assert "identical" in plan.loc["constant_feature", "reason"]
    assert plan.loc["mostly_missing", "decision"] == "drop"
    assert "missing" in plan.loc["mostly_missing", "reason"]
    assert plan.loc["ghost", "decision"] == "drop"
    assert plan.loc["noise_feature", "decision"] == "drop"
    assert "IV" in plan.loc["noise_feature", "reason"]


def test_iv_is_not_used_as_a_drop_criterion_when_positives_are_scarce():
    frame = make_frame()
    sparse = pd.concat(
        [frame.loc[frame["label"] == 1].head(5), frame.loc[frame["label"] == 0].head(500)]
    )
    plan = screen_features(sparse, ["noise_feature"], "label").set_index("feature")
    assert plan.loc["noise_feature", "decision"] == "keep"
    assert "unreliable" in plan.loc["noise_feature", "reason"]
    assert not bool(plan.loc["noise_feature", "iv_reliable"])


def test_cohort_stability_drops_a_feature_that_only_works_in_one_period():
    rng = np.random.default_rng(11)
    n = 6000
    cohort = rng.choice(["2025Q1", "2025Q2", "2025Q3"], n)
    label = rng.integers(0, 2, n)
    # Predictive in Q1 only; pure noise elsewhere.
    unstable = np.where(cohort == "2025Q1", label + rng.normal(scale=0.3, size=n),
                        rng.normal(size=n))
    stable = label + rng.normal(scale=0.6, size=n)
    frame = pd.DataFrame(
        {"cohort": cohort, "label": label, "unstable": unstable, "stable": stable}
    )

    without_stability = screen_features(frame, ["unstable", "stable"], "label").set_index(
        "feature"
    )
    assert without_stability.loc["unstable", "decision"] == "keep"

    with_stability = screen_features(
        frame, ["unstable", "stable"], "label", stability_col="cohort"
    ).set_index("feature")
    assert with_stability.loc["unstable", "decision"] == "drop"
    assert with_stability.loc["stable", "decision"] == "keep"
    assert with_stability.loc["unstable", "iv_min_cohort"] < with_stability.loc["unstable", "iv"]


def test_tier_feature_plan_screens_each_tier_separately():
    frame = make_frame()
    # Make the feature useless inside the tail tier only.
    tail = frame["tier"] == "tail"
    frame.loc[tail, "good_feature"] = 0.0

    plan = tier_feature_plan(
        frame,
        tier_col="tier",
        label_col="label",
        features_by_tier={
            "core": ["good_feature", "noise_feature"],
            "tail": ["good_feature", "noise_feature"],
        },
        min_positives_for_iv=50,
    )
    decisions = plan.set_index(["tier", "feature"])["decision"]
    assert decisions[("core", "good_feature")] == "keep"
    assert decisions[("tail", "good_feature")] == "drop"

    summary = plan_summary(plan).set_index("tier")
    assert summary.loc["core", "kept"] == 1
    assert summary.loc["tail", "dropped"] == 2

    assert selected_features(plan, "core") == ["good_feature"]
    assert selected_features(plan, "tail") == []

    comparison = compare_tiers(plan, "core", "tail")
    assert set(comparison["feature"]) == {"good_feature", "noise_feature"}
    assert "iv_core" in comparison.columns and "iv_tail" in comparison.columns


def test_tier_feature_plan_skips_empty_tiers():
    frame = make_frame()
    plan = tier_feature_plan(
        frame,
        tier_col="tier",
        label_col="label",
        features_by_tier={"core": ["good_feature"], "missing_tier": ["good_feature"]},
    )
    assert set(plan["tier"]) == {"core"}
