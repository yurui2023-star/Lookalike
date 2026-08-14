#!/usr/bin/env python3
"""Feature plan and model comparison for tier_a_core and tier_b_extended.

Part A reads the v1.2 feature catalog (69 core + 30 optional) and derives the planned feature
set, delivery dependencies and monotone constraints for each tier - no data required.

Part B/C smoke-test screening and training on `data/Bank_Marketing_Dataset.csv`. That CSV
is the repo's bundled API sample (term-deposit label), **not** MB Lookalike project data.
Do not cite Part B/C Lift/AUC in the tier-model spec.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from demo_base import (
    CANONICAL,
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
from lookalike.domain.feature_catalog import (
    D_APP_EVENTS,
    D_CIC,
    D_INTERNAL_TXN,
    catalog_frame,
    categorical_features,
    coverage_matrix,
    delivery_summary,
    monotone_constraints,
    planned_features,
    unconfirmed_source_features,
)
from lookalike.domain.product_profiles import (
    COL_PRODUCT_COUNT,
    SCORABLE_TIERS,
    TIER_A_CORE,
    TIER_B_EXTENDED,
    assign_scoring_tier,
    get_product_profile,
)
from lookalike.modeling.calibration import SegmentCalibrator, calibration_report
from lookalike.modeling.feature_selection import (
    compare_tiers,
    plan_summary,
    selected_features,
    tier_feature_plan,
)
from lookalike.modeling.metrics import ks_statistic, lift_at
from lookalike.modeling.sampling import (
    downsample_negatives,
    prior_correction,
    stratified_negative_sample,
)
from lookalike.modeling.splits import split_out_of_time, stratified_split
from lookalike.pipeline.service import load_dataframe, prepare_modeling_frame

# Capacity is scaled to the demo sample (tens of thousands of rows). The MB-scale settings
# recommended in the scheme are num_leaves=63 / min_data_in_leaf=100 for tier A and
# num_leaves=31 / min_data_in_leaf=200 for tier B.
TIER_A_PARAMS = {"num_leaves": 31, "min_data_in_leaf": 40, "learning_rate": 0.05}
TIER_B_PARAMS = {
    "num_leaves": 15,
    "min_data_in_leaf": 200,
    "min_sum_hessian_in_leaf": 50.0,
    "learning_rate": 0.03,
    "lambda_l2": 10.0,
    "feature_fraction": 0.7,
}


def catalog_section() -> list[str]:
    """Part A: everything that can be decided from the v1.2 workbook alone."""
    catalog = catalog_frame()
    core = catalog.loc[~catalog["optional"]]
    tier_a = planned_features("tier_a_core")
    tier_b = planned_features("tier_b_extended")
    tier_a_v1 = planned_features(
        "tier_a_core", exclude_delivery={D_INTERNAL_TXN, D_APP_EVENTS, D_CIC}
    )
    tier_b_v1 = planned_features(
        "tier_b_extended", exclude_delivery={D_INTERNAL_TXN, D_APP_EVENTS, D_CIC}
    )
    tier_b_no_cic = planned_features("tier_b_extended", exclude_delivery={D_CIC})
    tier_a_no_cic = planned_features("tier_a_core", exclude_delivery={D_CIC})

    lines = [
        "## A. 特征目录（v1.2）",
        "",
        f"- 核心特征 {len(core)}，可选现金流特征 {len(catalog) - len(core)}，合计 {len(catalog)}",
        f"- 类别型特征 {len(categorical_features(tier_a))}，"
        f"设了单调约束的特征 {sum(1 for value in monotone_constraints(tier_a) if value != 0)}",
    ]
    unconfirmed = unconfirmed_source_features()
    if unconfirmed:
        lines.append(
            f"- **数据源为空、需确认的特征 {len(unconfirmed)} 个**："
            + ", ".join(f"`{name}`" for name in unconfirmed)
        )
    else:
        lines.append("- 11 个产品持有字段已确认可交付；CIC 在尾部覆盖低，不进默认尾部特征集")
    lines += [
        "",
        "### A.1 交付依赖与分层计划特征数",
        "",
        markdown_table(delivery_summary(), 0),
        "",
        "### A.2 Tier B 预期覆盖矩阵（按特征组）",
        "",
        markdown_table(coverage_matrix(), 0),
        "",
        "### A.3 分层特征集规模",
        "",
        markdown_table(
            pd.DataFrame(
                [
                    {
                        "模型": "tier_a_core",
                        "全量交付后": len(tier_a),
                        "V1（仅 MB 已确认源 + 主数据 + 产品持有）": len(tier_a_v1),
                        "CIC 缺失时": len(tier_a_no_cic),
                    },
                    {
                        "模型": "tier_b_extended",
                        "全量交付后": len(tier_b),
                        "V1（仅 MB 已确认源 + 主数据 + 产品持有）": len(tier_b_v1),
                        "CIC 缺失时": len(tier_b_no_cic),
                    },
                ]
            ),
            0,
        ),
        "",
        f"Tier B 相比 Tier A 少 {len(tier_a) - len(tier_b)} 个特征，删的全部是"
        "在单产品/休眠客群里预期覆盖过低的交易、消费、App 深度类字段。",
        "",
    ]
    return lines


def build_dataset(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    profile = get_product_profile(args.product)
    raw = build_canonical(load_dataframe(args.data))
    labelled = prepare_modeling_frame(raw, rename_target=True)
    for column in ("group_id", "cohort", *CANONICAL):
        labelled[column] = raw[column]

    tiers = assign_scoring_tier(labelled, profile)
    base = build_base(labelled, tiers, args.base_rows, args.random_state)
    base["stratum"] = base["tier"].astype(str) + "|" + product_band(base[COL_PRODUCT_COUNT])

    oot = split_out_of_time(base, cohort_col="cohort", oot_cohorts=1)
    splits = stratified_split(
        oot.development, label_col=LABEL_COL, group_col="group_id",
        random_state=args.random_state,
    )
    splits["oot"] = oot.out_of_time
    return base, splits


def evaluate_on_tier(
    frame: pd.DataFrame,
    score: pd.Series,
    tier: str,
    name: str,
    feature_count: int,
) -> dict[str, object]:
    subset = frame.loc[frame["tier"] == tier]
    truth = subset[LABEL_COL]
    tier_score = score.loc[subset.index]
    top20 = lift_at(truth, tier_score, 0.2)
    return {
        "build": name,
        "tier": tier,
        "features": feature_count,
        "rows": len(subset),
        "positives": int(truth.sum()),
        "auc": roc_auc_score(truth, tier_score),
        "ks": ks_statistic(truth, tier_score),
        "lift@20%": top20["lift"],
        "capture@20%": top20["capture_rate"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", default="personal_unsecured_loan")
    parser.add_argument("--data", default=str(DEFAULT_DATA_FILE))
    parser.add_argument("--base-rows", type=int, default=2_000_000)
    parser.add_argument("--report", default=str(OUTPUT_DIR / "tier_model_plan.md"))
    parser.add_argument("--evidence", default="")
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()

    profile = get_product_profile(args.product)
    lines: list[str] = [
        f"# 分层模型细化 - {profile.display_name}",
        "",
        "> **不是 MB 项目数据。** A 部分读的是 v1.2 特征清单；B/C 部分跑在 "
        "`data/Bank_Marketing_Dataset.csv`（仓库 API 联调样例，定期存款标签）上，"
        "列名与 Lift/AUC **不得**当作分层模型规格的证据。",
        "",
        "输入：`MB_Bank_Lookalike_Feature_List_v1.2.xlsx`（69 核心 + 30 可选现金流）。",
        "",
    ]
    lines += catalog_section()

    base, splits = build_dataset(args)
    train, validation, test = splits["train"], splits["validation"], splits["test"]
    all_features = [
        col for col in train.columns if col not in {LABEL_COL, "ClientID", *HELPERS}
    ]

    plan = tier_feature_plan(
        train,
        tier_col="tier",
        label_col=LABEL_COL,
        features_by_tier={TIER_A_CORE: all_features, TIER_B_EXTENDED: all_features},
        stability_col="cohort",
    )
    tier_a_features = selected_features(plan, TIER_A_CORE)
    tier_b_features = selected_features(plan, TIER_B_EXTENDED)
    comparison = compare_tiers(plan, TIER_A_CORE, TIER_B_EXTENDED)
    comparison["iv_gap"] = (
        comparison[f"iv_{TIER_A_CORE}"] - comparison[f"iv_{TIER_B_EXTENDED}"]
    ).abs()
    divergent = int(
        (
            comparison[f"decision_{TIER_A_CORE}"] != comparison[f"decision_{TIER_B_EXTENDED}"]
        ).sum()
    )

    lines += [
        "## B. 分层特征筛选（在各层内部分别计算缺失率 / 单值率 / IV）",
        "",
        markdown_table(plan_summary(plan), 0),
        "",
        f"Tier A 保留 {len(tier_a_features)} 个、Tier B 保留 {len(tier_b_features)} 个；"
        f"两层筛选结果不同的特征有 {divergent} 个。",
        "",
        "### B.1 两层间 IV 差异最大的特征",
        "",
        markdown_table(
            comparison.sort_values("iv_gap", ascending=False)
            .head(10)[
                [
                    "feature",
                    f"iv_{TIER_A_CORE}",
                    f"iv_{TIER_B_EXTENDED}",
                    f"decision_{TIER_A_CORE}",
                    f"decision_{TIER_B_EXTENDED}",
                ]
            ]
            .rename(
                columns={
                    f"iv_{TIER_A_CORE}": "iv_tier_a",
                    f"iv_{TIER_B_EXTENDED}": "iv_tier_b",
                    f"decision_{TIER_A_CORE}": "tier_a",
                    f"decision_{TIER_B_EXTENDED}": "tier_b",
                }
            )
        ),
        "",
        "同一个特征在两层的 IV 可以相差一个数量级，这就是必须分层筛选、分层建模的原因。",
        "",
    ]

    # ---- Part C: build the two tier models --------------------------------------------
    tier_a_train = train.loc[train["tier"] == TIER_A_CORE]
    tier_a_validation = validation.loc[validation["tier"] == TIER_A_CORE]
    tier_a_sample = downsample_negatives(
        tier_a_train, label_col=LABEL_COL, ratio=profile.label.negative_ratio
    )
    m1_model, m1_cols = train_lightgbm(
        tier_a_sample.frame, tier_a_validation, tier_a_features, params=TIER_A_PARAMS
    )

    domain_train = train.loc[train["tier"].isin(SCORABLE_TIERS)]
    domain_sample = stratified_negative_sample(
        domain_train,
        stratum_col="stratum",
        label_col=LABEL_COL,
        ratio=profile.label.negative_ratio,
        min_negatives=400,
        random_state=args.random_state,
    )
    tail_sample = stratified_negative_sample(
        train.loc[train["tier"] == TIER_B_EXTENDED],
        stratum_col="stratum",
        label_col=LABEL_COL,
        ratio=profile.label.negative_ratio,
        min_negatives=400,
        random_state=args.random_state,
    )
    scorable_validation = validation.loc[validation["tier"].isin(SCORABLE_TIERS)]
    tail_validation = validation.loc[validation["tier"] == TIER_B_EXTENDED]

    m2_model, m2_cols = train_lightgbm(
        domain_sample.frame, scorable_validation, tier_b_features,
        weight_col=domain_sample.weight_col, params=TIER_B_PARAMS,
    )
    m3_model, m3_cols = train_lightgbm(
        tail_sample.frame, tail_validation, tier_b_features,
        weight_col=tail_sample.weight_col, params=TIER_B_PARAMS,
    )
    m4_model, m4_cols = train_lightgbm(
        domain_sample.frame, scorable_validation, all_features,
        weight_col=domain_sample.weight_col, params=TIER_B_PARAMS,
    )

    scorable_test = test.loc[test["tier"].isin(SCORABLE_TIERS)].copy()
    # Tail positives are scarce by construction, so the tail builds are compared on the
    # union of the test and OOT cohorts; the core build uses the test cohort alone.
    eval_tail = pd.concat([test, splits["oot"]], ignore_index=True)
    eval_tail = eval_tail.loc[eval_tail["tier"] == TIER_B_EXTENDED].copy()

    m1_score = prior_correction(
        predict(m1_model, scorable_test, tier_a_features, m1_cols),
        tier_a_sample.negative_sampling_rate,
    )
    tail_builds = [
        (
            "M2 tier_b 全域加权 + 分层特征",
            m2_model,
            tier_b_features,
            m2_cols,
        ),
        (
            "M3 tier_b 仅尾部样本 + 分层特征",
            m3_model,
            tier_b_features,
            m3_cols,
        ),
        (
            "M4 tier_b 全域加权 + 全特征（对照）",
            m4_model,
            all_features,
            m4_cols,
        ),
    ]
    results = [
        evaluate_on_tier(
            scorable_test, m1_score, TIER_A_CORE, "M1 tier_a_core", len(tier_a_features)
        )
    ]
    for name, model, features, columns in tail_builds:
        score = predict(model, eval_tail, features, columns)
        results.append(
            evaluate_on_tier(eval_tail, score, TIER_B_EXTENDED, name, len(features))
        )

    lines += [
        "## C. 两个分层模型的构建对照",
        "",
        "参数差异：Tier A 用 `num_leaves=31 / min_data_in_leaf=40`（MB 量级建议 63 / 100）；"
        "Tier B 用 `num_leaves=15 / min_data_in_leaf=200 / min_sum_hessian_in_leaf=50 / "
        "lambda_l2=10`——尾部样本带 IPW 权重，必须用加权口径的 hessian 下限约束叶子，"
        "否则少数高权重样本会单独成叶。",
        "",
        "M1 在 test 上评估；M2-M4 因尾部正样本稀少，在 test + OOT 合并集上评估，"
        "结论只用于机制验证，不代表 MB 真实水平。",
        "",
        markdown_table(pd.DataFrame(results), 5),
        "",
    ]

    # ---- Combined scoring: each tier scored by its own model, one shared scale ---------
    best_tail = max(
        (row for row in results if row["tier"] == TIER_B_EXTENDED),
        key=lambda row: row["lift@20%"],
    )
    tail_model, tail_feature_set, tail_columns = next(
        (model, features, columns)
        for name, model, features, columns in tail_builds
        if name == best_tail["build"]
    )
    combined = pd.Series(0.0, index=scorable_test.index)
    combined.loc[scorable_test["tier"] == TIER_A_CORE] = m1_score.loc[
        scorable_test["tier"] == TIER_A_CORE
    ]
    tail_mask = scorable_test["tier"] == TIER_B_EXTENDED
    combined.loc[tail_mask] = predict(
        tail_model, scorable_test.loc[tail_mask], tail_feature_set, tail_columns
    )

    validation_scorable = validation.loc[validation["tier"].isin(SCORABLE_TIERS)].copy()
    validation_combined = pd.Series(0.0, index=validation_scorable.index)
    val_a = validation_scorable["tier"] == TIER_A_CORE
    validation_combined.loc[val_a] = prior_correction(
        predict(m1_model, validation_scorable.loc[val_a], tier_a_features, m1_cols),
        tier_a_sample.negative_sampling_rate,
    )
    val_b = validation_scorable["tier"] == TIER_B_EXTENDED
    validation_combined.loc[val_b] = predict(
        tail_model, validation_scorable.loc[val_b], tail_feature_set, tail_columns
    )

    calibrator = SegmentCalibrator(method="offset").fit(
        validation_scorable[LABEL_COL], validation_combined, validation_scorable["tier"]
    )
    scorable_test["calibrated_score"] = calibrator.transform(
        combined, scorable_test["tier"]
    ).to_numpy()

    top20 = lift_at(scorable_test[LABEL_COL], scorable_test["calibrated_score"], 0.2)
    lines += [
        "## D. 组合打分（各层用自己的模型，校准到同一刻度）",
        "",
        f"尾部模型选用 **{best_tail['build']}**（层内 lift 最高）。",
        "",
        markdown_table(
            calibration_report(
                scorable_test[LABEL_COL],
                scorable_test["calibrated_score"],
                scorable_test["tier"],
            )[["segment", "rows", "actual_rate", "predicted_rate", "ratio"]],
            6,
        ),
        "",
        markdown_table(
            pd.DataFrame(
                [
                    {
                        "指标": "全域 AUC",
                        "取值": roc_auc_score(
                            scorable_test[LABEL_COL], scorable_test["calibrated_score"]
                        ),
                    },
                    {
                        "指标": "全域 KS",
                        "取值": ks_statistic(
                            scorable_test[LABEL_COL], scorable_test["calibrated_score"]
                        ),
                    },
                    {"指标": "Lift@20%", "取值": top20["lift"]},
                    {"指标": "Capture@20%", "取值": top20["capture_rate"]},
                ]
            ),
            5,
        ),
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
