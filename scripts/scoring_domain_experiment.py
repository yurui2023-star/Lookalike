#!/usr/bin/env python3
"""How should the customers outside the screened pool be scored?

The business diagnosis screens 35.72M customers down to a 4.72M pool where positives are
dense. That pool is the *training sample*. The remaining ~31M customers still have to receive
a score (or an explicit non-scorable reason), so this experiment compares three strategies on
a base whose tier composition and conversion rates are set to the diagnosed values:

  A1  pool-only training          - train on Tier A, score everybody, prior correction only
  A2  pool-only + tier calibration - same model, per-tier log-odds offset fitted on validation
  A3  whole-domain training       - stratified negatives across all tiers with IPW weights

Reported for each strategy: per-tier calibration (predicted vs actual rate), expected
calibration error, ranking power overall and inside each tier, and how much of the tail is
recovered into the top-20% audience.

The bundled CSV `data/Bank_Marketing_Dataset.csv` is **not** MB project data (term-deposit
label, ~30% positives). This script only smoke-tests the scoring-domain code paths.
Do not cite its AUC/Lift tables in the training scheme.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from demo_base import (
    CANONICAL,
    DIAGNOSIS,
    HELPERS,
    build_base,
    build_canonical,
    markdown_table,
    predict,
    product_band,
    train_lightgbm,
)
from sklearn.metrics import roc_auc_score

from lookalike.config import DEFAULT_DATA_FILE, LABEL_COL, OUTPUT_DIR
from lookalike.domain.product_profiles import (
    COL_PRODUCT_COUNT,
    SCORABLE_TIERS,
    TIER_A_CORE,
    TIER_B_EXTENDED,
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
from lookalike.modeling.metrics import ab_test_sample_size, ks_statistic, lift_at, segment_lift
from lookalike.modeling.sampling import (
    downsample_negatives,
    prior_correction,
    stratified_negative_sample,
)
from lookalike.modeling.splits import describe_splits, split_out_of_time, stratified_split
from lookalike.pipeline.service import load_dataframe, prepare_modeling_frame

_table = markdown_table


def evaluate(
    name: str,
    frame: pd.DataFrame,
    score_col: str,
) -> tuple[dict[str, object], pd.DataFrame, pd.DataFrame]:
    """Global metrics, per-tier calibration and per-tier lift for one strategy."""
    truth = frame[LABEL_COL]
    scores = frame[score_col]
    top20 = lift_at(truth, scores, 0.2)
    top_index = scores.nlargest(top20["selected"]).index
    tail_positives = int(frame.loc[frame["tier"] == TIER_B_EXTENDED, LABEL_COL].sum())
    tail_in_top = int(
        frame.loc[top_index].query(f"tier == '{TIER_B_EXTENDED}'")[LABEL_COL].sum()
    )
    summary = {
        "strategy": name,
        "auc": roc_auc_score(truth, scores),
        "ks": ks_statistic(truth, scores),
        "lift@20%": top20["lift"],
        "capture@20%": top20["capture_rate"],
        "ece": expected_calibration_error(truth, scores),
        "predicted_rate": float(scores.mean()),
        "actual_rate": float(truth.mean()),
        "tail_share_of_top20%": float(
            (frame.loc[top_index, "tier"] == TIER_B_EXTENDED).mean()
        ),
        "tail_positive_capture": tail_in_top / tail_positives if tail_positives else np.nan,
    }
    calibration = calibration_report(truth, scores, frame["tier"]).assign(strategy=name)
    lift_by_tier = segment_lift(truth, scores, frame["tier"], 0.2, min_positives=10).assign(
        strategy=name
    )
    return summary, calibration, lift_by_tier


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", default="personal_unsecured_loan")
    parser.add_argument("--data", default=str(DEFAULT_DATA_FILE))
    parser.add_argument("--base-rows", type=int, default=500_000)
    parser.add_argument("--report", default=str(OUTPUT_DIR / "scoring_domain_experiment.md"))
    parser.add_argument("--evidence", default="")
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()

    profile = get_product_profile(args.product)
    raw = build_canonical(load_dataframe(args.data))
    labelled = prepare_modeling_frame(raw, rename_target=True)
    for column in ("group_id", "cohort", *CANONICAL):
        labelled[column] = raw[column]

    lines: list[str] = [
        f"# 打分域实验 - {profile.display_name}",
        "",
        "> **不是 MB 项目数据。** `data/Bank_Marketing_Dataset.csv` 是仓库 API 联调用的公开样例"
        "（定期存款标签）。本文件只记录对该样例重采样后的脚本输出，**不得**当作 Lookalike "
        "训练方案或分层模型规格的证据。",
        "",
        f"产品档案 `{profile.key}`；依据 {profile.brd_reference}。",
        "",
        "数据集 `data/Bank_Marketing_Dataset.csv` 分层占比与各层转化率按业务诊断 v3.8 重标定，"
        "仅用于检查代码路径能否跑通。",
        "",
        "## 1. 四步筛选漏斗（诊断口径复现）",
        "",
    ]

    screened = screen_candidates(labelled, profile, label_col=LABEL_COL)
    lines += [
        _table(
            screened.funnel[
                ["step_id", "name", "rows_in", "rows_out", "step_retention", "positive_retention"]
            ]
        ),
        "",
        f"因演示数据缺列而跳过的条件（{len(screened.skipped_conditions)} 条，"
        "不会被当作通过）：" + ", ".join(f"`{item}`" for item in screened.skipped_conditions),
        "",
    ]

    tiers = assign_scoring_tier(labelled, profile)
    base = build_base(labelled, tiers, args.base_rows, args.random_state)
    base["stratum"] = base["tier"].astype(str) + "|" + product_band(base[COL_PRODUCT_COUNT])

    lines += [
        "## 2. 打分域分层（全量客群，不是只有种子池）",
        "",
        _table(scoring_domain_summary(base[["tier", "tier_reason"]], base[LABEL_COL]), 4),
        "",
        f"Tier A 占比 {DIAGNOSIS['pool_customers'] / DIAGNOSIS['base_customers']:.1%}，"
        f"对应 MB 的 4.72M / 35.72M；Tier B 即需要回答的 ~31M。",
        "",
    ]

    oot = split_out_of_time(base, cohort_col="cohort", oot_cohorts=1)
    splits = stratified_split(
        oot.development, label_col=LABEL_COL, group_col="group_id",
        random_state=args.random_state,
    )
    splits["oot"] = oot.out_of_time
    lines += [
        "## 3. 时间外验证与 60/20/20 切分",
        "",
        f"- 开发期 cohort：{', '.join(map(str, oot.development_cohorts))}",
        f"- OOT cohort：{', '.join(map(str, oot.out_of_time_cohorts))}",
        "",
        _table(describe_splits(splits, LABEL_COL, group_col="group_id"), 6),
        "",
    ]

    train, validation, test = splits["train"], splits["validation"], splits["test"]
    feature_cols = [
        col for col in train.columns if col not in {LABEL_COL, "ClientID", *HELPERS}
    ]

    # ---- A1 / A2: train on the screened pool only -------------------------------------
    pool_train = train.loc[train["tier"] == TIER_A_CORE]
    pool_validation = validation.loc[validation["tier"] == TIER_A_CORE]
    pool_sample = downsample_negatives(
        pool_train, label_col=LABEL_COL, ratio=profile.label.negative_ratio
    )
    pool_model, pool_cols = train_lightgbm(pool_sample.frame, pool_validation, feature_cols)

    # ---- A3: stratified sample across the whole scoring domain ------------------------
    domain_train = train.loc[train["tier"].isin(SCORABLE_TIERS)]
    domain_sample = stratified_negative_sample(
        domain_train,
        stratum_col="stratum",
        label_col=LABEL_COL,
        ratio=profile.label.negative_ratio,
        min_negatives=400,
        random_state=args.random_state,
    )
    domain_model, domain_cols = train_lightgbm(
        domain_sample.frame,
        validation.loc[validation["tier"].isin(SCORABLE_TIERS)],
        feature_cols,
        weight_col=domain_sample.weight_col,
    )

    lines += [
        "## 4. 两种训练样本构造",
        "",
        "**A1/A2 仅用种子池**（1:10 负样本下采样 + 先验校正）：",
        "",
        _table(pd.DataFrame([pool_sample.summary()]), 6),
        "",
        "**A3 全打分域分层抽样**（每层保留全部正样本，负样本按层抽样并赋 `N_h/n_h` 权重）：",
        "",
        _table(domain_sample.strata, 4),
        "",
    ]

    scorable_test = test.loc[test["tier"].isin(SCORABLE_TIERS)].copy()
    scorable_validation = validation.loc[validation["tier"].isin(SCORABLE_TIERS)].copy()

    pool_raw_test = predict(pool_model, scorable_test, feature_cols, pool_cols)
    scorable_test["a1_pool_only"] = prior_correction(
        pool_raw_test, pool_sample.negative_sampling_rate
    )
    pool_raw_validation = predict(pool_model, scorable_validation, feature_cols, pool_cols)
    calibrator = SegmentCalibrator(method="offset").fit(
        scorable_validation[LABEL_COL],
        prior_correction(pool_raw_validation, pool_sample.negative_sampling_rate),
        scorable_validation["tier"],
    )
    scorable_test["a2_pool_plus_tier_calibration"] = calibrator.transform(
        scorable_test["a1_pool_only"], scorable_test["tier"]
    ).to_numpy()
    scorable_test["a3_whole_domain_weighted"] = predict(
        domain_model, scorable_test, feature_cols, domain_cols
    )
    domain_calibrator = SegmentCalibrator(method="offset").fit(
        scorable_validation[LABEL_COL],
        predict(domain_model, scorable_validation, feature_cols, domain_cols),
        scorable_validation["tier"],
    )
    scorable_test["a4_whole_domain_calibrated"] = domain_calibrator.transform(
        scorable_test["a3_whole_domain_weighted"], scorable_test["tier"]
    ).to_numpy()

    # A5 keeps one specialised model per tier and puts both on the same probability scale.
    scorable_test["a5_two_model_calibrated"] = np.where(
        scorable_test["tier"] == TIER_A_CORE,
        scorable_test["a2_pool_plus_tier_calibration"],
        scorable_test["a4_whole_domain_calibrated"],
    )

    summaries, calibrations, tier_lifts = [], [], []
    for name, column in (
        ("A1 pool-only", "a1_pool_only"),
        ("A2 pool + tier calibration", "a2_pool_plus_tier_calibration"),
        ("A3 whole-domain weighted", "a3_whole_domain_weighted"),
        ("A4 whole-domain + calibration", "a4_whole_domain_calibrated"),
        ("A5 two models + shared scale", "a5_two_model_calibrated"),
    ):
        summary, calibration, lift_by_tier = evaluate(name, scorable_test, column)
        summaries.append(summary)
        calibrations.append(calibration)
        tier_lifts.append(lift_by_tier)

    lines += [
        "## 5. 分层校准：预测率 vs 实际率",
        "",
        _table(
            pd.concat(calibrations)[
                ["strategy", "segment", "rows", "actual_rate", "predicted_rate", "ratio"]
            ],
            6,
        ),
        "",
        "`ratio` = 预测率 / 实际率。仅用种子池训练的模型在 Tier B 上把转化率高估了一个数量级，"
        "这类分数无法与 Tier A 的分数放进同一个分数带。",
        "",
        "## 6. 排序能力与整体指标（全打分域测试集）",
        "",
        _table(pd.DataFrame(summaries), 5),
        "",
        "## 7. 层内 lift（Top 20%）",
        "",
        _table(
            pd.concat(tier_lifts)[
                ["strategy", "segment", "total", "positives", "base_rate", "lift", "capture_rate"]
            ]
        ),
        "",
    ]

    oot_frame = splits["oot"].loc[splits["oot"]["tier"].isin(SCORABLE_TIERS)].copy()
    oot_frame["a4_whole_domain_calibrated"] = domain_calibrator.transform(
        predict(domain_model, oot_frame, feature_cols, domain_cols), oot_frame["tier"]
    ).to_numpy()
    oot_summary, oot_calibration, _ = evaluate(
        "A4 whole-domain + calibration (OOT)", oot_frame, "a4_whole_domain_calibrated"
    )
    lines += [
        "## 8. OOT 稳定性（A4）",
        "",
        _table(pd.DataFrame([oot_summary]), 5),
        "",
        _table(
            oot_calibration[["segment", "rows", "actual_rate", "predicted_rate", "ratio"]], 6
        ),
        "",
    ]

    sizing = pd.DataFrame(
        [
            {"scenario": label, **ab_test_sample_size(rate, 0.2)}
            for label, rate in (
                ("PUL 池内 3 个月窗口", 143.1 / 10_000),
                ("PUL 池内 1 个月窗口", 85.8 / 10_000),
                ("Tier B 尾部 3 个月窗口", 2.5 / 10_000),
            )
        ]
    )
    lines += [
        "## 9. KPI A/B 测试样本量（>= 1.2x 目标）",
        "",
        _table(sizing[["scenario", "baseline_rate", "treatment_rate", "per_arm", "total"]], 6),
        "",
        "尾部人群单独做 A/B 需要的样本量高一个数量级，因此 Tier B 建议先做小比例探索投放"
        "（随机曝光样本）来收集无偏标签，而不是直接承诺 KPI。",
        "",
    ]

    report = "\n".join(lines) + "\n"
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    if args.evidence:
        evidence_path = Path(args.evidence)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
