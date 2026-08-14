"""Tests for the model training scheme: tiering, sampling, splitting, metrics, calibration."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lookalike.domain.product_profiles import (
    COL_ACTIVITY,
    COL_AGE,
    COL_AUM,
    COL_BLACKLIST,
    COL_HOLDS_TARGET,
    COL_PRODUCT_COUNT,
    COL_TENURE_MONTHS,
    MORTGAGE_LOAN,
    PERSONAL_UNSECURED_LOAN,
    TIER_A_CORE,
    TIER_B_EXTENDED,
    TIER_C_NOT_SCORABLE,
    TIER_EXCLUDED,
    assign_scoring_tier,
    get_product_profile,
    scoring_domain_summary,
    screen_candidates,
)
from lookalike.modeling.calibration import (
    SegmentCalibrator,
    calibration_report,
    expected_calibration_error,
)
from lookalike.modeling.metrics import (
    ab_test_sample_size,
    ks_statistic,
    ks_table,
    lift_at,
    lift_table,
    psi,
    psi_by_feature,
    segment_lift,
)
from lookalike.modeling.sampling import (
    downsample_negatives,
    prior_correction,
    score_band_summary,
    stratified_negative_sample,
)
from lookalike.modeling.splits import (
    describe_splits,
    split_out_of_time,
    stratified_split,
)


def make_customers(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            COL_PRODUCT_COUNT: rng.integers(0, 8, n),
            COL_AUM: rng.integers(0, 5, n) * 1_000_000,
            COL_ACTIVITY: rng.integers(0, 6, n),
            COL_AGE: rng.integers(16, 70, n),
            COL_TENURE_MONTHS: rng.integers(0, 90, n),
            COL_HOLDS_TARGET: 0,
            COL_BLACKLIST: 0,
        }
    )
    frame["label"] = (rng.random(n) < 0.1 + 0.05 * frame[COL_PRODUCT_COUNT]).astype(int)
    return frame


# --------------------------------------------------------------------------- metrics


def test_ks_statistic_is_one_for_perfect_separation():
    truth = [0, 0, 0, 1, 1, 1]
    score = [0.1, 0.2, 0.3, 0.8, 0.9, 0.95]
    assert ks_statistic(truth, score) == pytest.approx(1.0)


def test_ks_statistic_is_zero_for_constant_score():
    truth = [0, 1] * 50
    assert ks_statistic(truth, [0.5] * 100) == pytest.approx(0.0, abs=1e-9)


def test_ks_table_columns_follow_brd_naming_and_recalls_reach_one():
    rng = np.random.default_rng(1)
    truth = rng.integers(0, 2, 500)
    score = rng.random(500)
    table = ks_table(truth, score, bins=10)
    for column in (
        "positive_recall",
        "negative_recall",
        "cum_positive_recall",
        "cum_negative_recall",
        "ks",
    ):
        assert column in table.columns
    assert table["cum_positive_recall"].iloc[-1] == pytest.approx(1.0)
    assert table["cum_negative_recall"].iloc[-1] == pytest.approx(1.0)
    assert table["ks"].max() == pytest.approx(ks_statistic(truth, score), abs=0.05)


def test_lift_at_matches_hand_computed_values():
    truth = [1] * 10 + [0] * 90
    score = list(range(100, 0, -1))
    result = lift_at(truth, score, top_fraction=0.1)
    assert result["selected"] == 10
    assert result["response_rate"] == pytest.approx(1.0)
    assert result["lift"] == pytest.approx(10.0)
    assert result["capture_rate"] == pytest.approx(1.0)


def test_lift_table_cumulative_capture_reaches_one():
    rng = np.random.default_rng(3)
    truth = rng.integers(0, 2, 300)
    table = lift_table(truth, rng.random(300), bins=10)
    assert table["capture_rate"].iloc[-1] == pytest.approx(1.0)
    assert table["cum_lift"].iloc[-1] == pytest.approx(1.0)


def test_segment_lift_flags_segments_without_enough_positives():
    truth = [1, 1, 1, 1, 0, 0, 0, 0] + [0] * 8
    score = [0.9, 0.8, 0.7, 0.6, 0.1, 0.2, 0.3, 0.4] + [0.5] * 8
    segment = ["A"] * 8 + ["B"] * 8
    table = segment_lift(truth, score, segment, top_fraction=0.5, min_positives=2)
    row_a = table.loc[table["segment"] == "A"].iloc[0]
    row_b = table.loc[table["segment"] == "B"].iloc[0]
    assert bool(row_a["evaluable"]) is True
    assert row_a["lift"] == pytest.approx(2.0)
    assert bool(row_b["evaluable"]) is False


def test_psi_is_zero_for_identical_and_positive_for_shifted():
    rng = np.random.default_rng(5)
    reference = rng.normal(size=5000)
    assert psi(reference, reference) == pytest.approx(0.0, abs=1e-9)
    assert psi(reference, reference + 1.5) > 0.25


def test_psi_by_feature_ranks_the_shifted_feature_first():
    rng = np.random.default_rng(11)
    expected = pd.DataFrame({"stable": rng.normal(size=2000), "drifting": rng.normal(size=2000)})
    actual = expected.copy()
    actual["drifting"] = actual["drifting"] + 2.0
    table = psi_by_feature(expected, actual)
    assert table.iloc[0]["feature"] == "drifting"
    assert table.iloc[0]["psi"] > table.iloc[1]["psi"]


def test_ab_test_sample_size_grows_as_the_baseline_rate_falls():
    dense = ab_test_sample_size(0.01431, 0.2)
    sparse = ab_test_sample_size(0.00025, 0.2)
    assert dense["treatment_rate"] == pytest.approx(0.01431 * 1.2)
    assert sparse["per_arm"] > dense["per_arm"] * 10
    assert dense["total"] == dense["per_arm"] * 2


# --------------------------------------------------------------------------- sampling


def test_downsample_negatives_hits_the_requested_ratio():
    frame = pd.DataFrame({"label": [1] * 100 + [0] * 5000, "x": range(5100)})
    result = downsample_negatives(frame, ratio=10.0, random_state=1)
    assert result.positives == 100
    assert result.negatives_after == 1000
    assert result.achieved_ratio == pytest.approx(10.0)
    assert result.negative_sampling_rate == pytest.approx(0.2)
    assert result.frame["label"].sum() == 100


def test_prior_correction_recovers_the_population_rate():
    population_rate = 0.0143
    sampling_rate = 0.1
    population_odds = population_rate / (1 - population_rate)
    sample_odds = population_odds / sampling_rate
    sample_probability = sample_odds / (1 + sample_odds)
    corrected = prior_correction([sample_probability], sampling_rate)
    assert corrected.iloc[0] == pytest.approx(population_rate, rel=1e-6)


def test_prior_correction_is_monotone_and_shrinks_scores():
    scores = pd.Series([0.1, 0.4, 0.9])
    corrected = prior_correction(scores, 0.1)
    assert corrected.is_monotonic_increasing
    assert (corrected < scores).all()


def test_stratified_negative_sample_keeps_positives_and_restores_weight_mass():
    rng = np.random.default_rng(2)
    frame = pd.DataFrame(
        {
            "stratum": ["core"] * 2000 + ["tail"] * 8000,
            "label": [1] * 100 + [0] * 1900 + [1] * 10 + [0] * 7990,
            "x": rng.normal(size=10000),
        }
    )
    result = stratified_negative_sample(
        frame, stratum_col="stratum", ratio=10.0, min_negatives=100, random_state=3
    )
    assert result.frame["label"].sum() == 110
    for _, row in result.strata.iterrows():
        assert row["negatives_sampled"] * row["negative_weight"] == pytest.approx(
            row["negatives"]
        )
    negatives = result.frame.loc[result.frame["label"] == 0]
    assert negatives["sample_weight"].sum() == pytest.approx(9890)


def test_score_band_summary_counts_each_band():
    scores = pd.Series([0.01, 0.05, 0.2, 0.6, 0.9])
    table = score_band_summary(scores, {"Cool": (0.0, 0.1), "Warm": (0.1, 0.5), "Hot": (0.5, 1.1)})
    assert table.set_index("band")["customers"].to_dict() == {"Cool": 2, "Warm": 1, "Hot": 2}
    assert table["share"].sum() == pytest.approx(1.0)


# --------------------------------------------------------------------------- splits


def test_split_out_of_time_reserves_the_latest_cohort():
    frame = pd.DataFrame(
        {"cohort": ["2025Q1"] * 10 + ["2025Q2"] * 10 + ["2025Q3"] * 10, "label": [0, 1] * 15}
    )
    split = split_out_of_time(frame, "cohort", oot_cohorts=1)
    assert split.out_of_time_cohorts == ["2025Q3"]
    assert set(split.development["cohort"]) == {"2025Q1", "2025Q2"}
    assert len(split.development) + len(split.out_of_time) == len(frame)


def test_split_out_of_time_rejects_selecting_every_cohort():
    frame = pd.DataFrame({"cohort": ["a", "b"], "label": [0, 1]})
    with pytest.raises(ValueError):
        split_out_of_time(frame, "cohort", oot_cohorts=2)


def test_stratified_split_preserves_positive_rate():
    frame = pd.DataFrame({"label": [1] * 200 + [0] * 800, "x": range(1000)})
    splits = stratified_split(frame, ratios=(0.6, 0.2, 0.2))
    assert len(splits["train"]) == 600
    for subset in splits.values():
        assert subset["label"].mean() == pytest.approx(0.2, abs=0.02)


def test_grouped_split_keeps_every_customer_in_one_subset():
    frame = pd.DataFrame(
        {
            "group_id": [f"cif-{index // 3}" for index in range(900)],
            "label": [1 if index % 7 == 0 else 0 for index in range(900)],
        }
    )
    splits = stratified_split(frame, group_col="group_id", ratios=(0.6, 0.2, 0.2))
    seen: set[str] = set()
    for subset in splits.values():
        groups = set(subset["group_id"])
        assert not (groups & seen)
        seen |= groups
    assert sum(len(subset) for subset in splits.values()) == 900
    description = describe_splits(splits, group_col="group_id")
    assert set(description["subset"]) == {"train", "validation", "test"}
    assert description.loc[description["subset"] == "train", "rows"].iloc[0] > 400


# --------------------------------------------------------------------------- tiers


def test_screening_funnel_reports_retention_and_skipped_conditions():
    frame = make_customers()
    result = screen_candidates(frame, PERSONAL_UNSECURED_LOAN, label_col="label")
    assert list(result.funnel["step_id"]) == ["S0", "S1", "S2", "S3"]
    assert result.retained < len(frame)
    assert (result.frame[COL_PRODUCT_COUNT] >= 2).all()
    assert (result.frame[COL_ACTIVITY] >= 1).all()
    # employee/test/closed/deceased columns are absent, so those conditions are reported.
    assert any("employee_flag" in item for item in result.skipped_conditions)


def test_screening_is_monotonically_decreasing():
    frame = make_customers()
    funnel = screen_candidates(frame, PERSONAL_UNSECURED_LOAN, label_col="label").funnel
    assert funnel["rows_out"].is_monotonic_decreasing
    assert funnel["cumulative_retention"].iloc[-1] <= 1.0


def test_mortgage_rescue_clause_recovers_high_aum_customers():
    frame = pd.DataFrame(
        {
            COL_PRODUCT_COUNT: [1, 1, 3],
            COL_AUM: [500_000_000, 1_000_000, 1_000_000],
            COL_ACTIVITY: [3, 3, 3],
            COL_AGE: [40, 40, 40],
            COL_TENURE_MONTHS: [40, 40, 40],
            COL_HOLDS_TARGET: [0, 0, 0],
        }
    )
    kept = screen_candidates(frame, MORTGAGE_LOAN).frame
    assert len(kept) == 2
    assert set(kept[COL_AUM]) == {500_000_000, 1_000_000}
    # The same customer is dropped by the unsecured profile, which has no rescue clause.
    assert len(screen_candidates(frame, PERSONAL_UNSECURED_LOAN).frame) == 1


def test_scoring_tiers_cover_the_whole_base():
    frame = pd.DataFrame(
        {
            COL_PRODUCT_COUNT: [3, 1, 0, 3],
            COL_AUM: [1_000_000, 2_000_000, 0, 5_000_000],
            COL_ACTIVITY: [2, 2, 0, 2],
            COL_AGE: [35, 35, 35, 35],
            COL_TENURE_MONTHS: [30, 30, 30, 30],
            COL_HOLDS_TARGET: [0, 0, 0, 1],
        }
    )
    tiers = assign_scoring_tier(frame, PERSONAL_UNSECURED_LOAN)
    assert list(tiers["tier"]) == [
        TIER_A_CORE,
        TIER_B_EXTENDED,
        TIER_C_NOT_SCORABLE,
        TIER_EXCLUDED,
    ]
    assert tiers["tier_reason"].iloc[1] == "failed:S1"
    assert tiers["tier_reason"].iloc[2] == "cold_start_no_feature_footprint"

    summary = scoring_domain_summary(tiers, pd.Series([1, 0, 0, 0]))
    assert summary["customers"].sum() == 4
    assert summary.loc[summary["tier"] == TIER_A_CORE, "scorable"].iloc[0]
    assert not summary.loc[summary["tier"] == TIER_C_NOT_SCORABLE, "scorable"].iloc[0]


def test_tier_b_records_every_failed_step():
    frame = pd.DataFrame(
        {
            COL_PRODUCT_COUNT: [1],
            COL_AUM: [1_000_000],
            COL_ACTIVITY: [2],
            COL_AGE: [70],
            COL_TENURE_MONTHS: [30],
            COL_HOLDS_TARGET: [0],
        }
    )
    tiers = assign_scoring_tier(frame, PERSONAL_UNSECURED_LOAN)
    assert tiers["tier"].iloc[0] == TIER_B_EXTENDED
    assert tiers["tier_reason"].iloc[0] == "failed:S1,S3"


def test_screened_pool_equals_tier_a():
    frame = make_customers()
    pool = screen_candidates(frame, PERSONAL_UNSECURED_LOAN).retained
    tiers = assign_scoring_tier(frame, PERSONAL_UNSECURED_LOAN)
    assert int((tiers["tier"] == TIER_A_CORE).sum()) == pool


def test_profile_lookup_rejects_unknown_products():
    assert get_product_profile("mortgage_loan") is MORTGAGE_LOAN
    with pytest.raises(ValueError):
        get_product_profile("credit_card")


def test_profiles_carry_the_brd_label_windows():
    assert PERSONAL_UNSECURED_LOAN.label.max_overdue_days == 30
    assert PERSONAL_UNSECURED_LOAN.label.seed_lookback_months == 12
    assert MORTGAGE_LOAN.label.max_overdue_days == 10
    assert MORTGAGE_LOAN.label.seed_lookback_months == 24
    assert MORTGAGE_LOAN.label.negative_ratio >= PERSONAL_UNSECURED_LOAN.label.negative_ratio


# --------------------------------------------------------------------------- calibration


def test_calibration_report_exposes_the_tier_gap():
    truth = pd.Series([1] * 10 + [0] * 90 + [0] * 100)
    score = pd.Series([0.1] * 100 + [0.1] * 100)
    segment = pd.Series(["core"] * 100 + ["tail"] * 100)
    table = calibration_report(truth, score, segment).set_index("segment")
    assert table.loc["core", "actual_rate"] == pytest.approx(0.1)
    assert table.loc["core", "ratio"] == pytest.approx(1.0)
    assert table.loc["tail", "actual_rate"] == pytest.approx(0.0)
    assert table.loc["ALL", "ratio"] == pytest.approx(2.0)


def test_segment_calibrator_aligns_predicted_rate_per_segment():
    rng = np.random.default_rng(9)
    core = pd.DataFrame(
        {
            "segment": "core",
            "score": rng.uniform(0.05, 0.3, 1000),
            "label": rng.random(1000) < 0.15,
        }
    )
    tail = pd.DataFrame(
        {
            "segment": "tail",
            "score": rng.uniform(0.05, 0.3, 4000),
            "label": rng.random(4000) < 0.005,
        }
    )
    frame = pd.concat([core, tail]).reset_index(drop=True)
    frame["label"] = frame["label"].astype(int)

    calibrated = SegmentCalibrator(method="offset").fit_transform(
        frame["label"], frame["score"], frame["segment"]
    )
    report = calibration_report(frame["label"], calibrated, frame["segment"]).set_index(
        "segment"
    )
    for tier in ("core", "tail"):
        assert report.loc[tier, "ratio"] == pytest.approx(1.0, abs=0.02)

    before = expected_calibration_error(frame["label"], frame["score"])
    after = expected_calibration_error(frame["label"], calibrated)
    assert after < before


def test_segment_calibrator_preserves_ranking_inside_a_segment():
    scores = pd.Series([0.1, 0.2, 0.3, 0.4])
    segment = pd.Series(["a", "a", "a", "a"])
    labels = pd.Series([0, 0, 1, 1])
    calibrated = SegmentCalibrator().fit_transform(labels, scores, segment)
    assert calibrated.is_monotonic_increasing


def test_unknown_segment_falls_back_to_the_global_offset():
    labels = pd.Series([1, 0, 0, 0])
    scores = pd.Series([0.5, 0.5, 0.5, 0.5])
    segment = pd.Series(["a", "a", "b", "b"])
    calibrator = SegmentCalibrator().fit(labels, scores, segment)
    transformed = calibrator.transform(pd.Series([0.5]), pd.Series(["unseen"]))
    assert transformed.iloc[0] == pytest.approx(0.25, abs=1e-6)
