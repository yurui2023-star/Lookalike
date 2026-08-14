"""Feature catalog from MB_Bank_Lookalike_Feature_List_v1.2.xlsx.

69 shared core features (Personal Unsecured Loan and Mortgage Loan use the same list) plus
30 optional settlement-account cash-flow features that are only considered when the core
model underperforms.

Beyond the workbook columns, every feature carries three planning attributes that the tier
models need:

* ``delivery``  - which data-delivery workstream must land before the feature exists;
* ``tier_b_coverage`` - expected coverage inside the extended tier (single-product / dormant
  customers). It is a *planning prior* only: the binding decision is the empirical per-tier
  screening in ``lookalike.modeling.feature_selection``;
* ``monotone`` - sign constraint to apply where the business direction is certain.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Delivery workstreams (drive the phased feature rollout).
D_MB_CONFIRMED = "D1_mb_confirmed"  # RPT_CST_PD_HLD_DLY / BPM snapshots, source confirmed
D_CUSTOMER_MASTER = "D2_customer_master"
D_PRODUCT_HOLDING = "D3_product_holding_source_tbd"
D_INTERNAL_TXN = "D4_internal_assets_and_transactions"
D_APP_EVENTS = "D5_app_events"
D_CIC = "D6_cic_external"
D_CASHFLOW = "D7_transaction_detail_optional"

# Expected coverage inside tier_b_extended.
COVERAGE_HIGH = "high"
COVERAGE_MEDIUM = "medium"
COVERAGE_LOW = "low"

# Diagnosis dimensions (business diagnosis v3.8 five-dimension framework).
DIM_PRODUCT = "product_holding"
DIM_AUM = "aum"
DIM_ACTIVITY = "activity"
DIM_TENURE = "tenure"
DIM_AGE = "age"

SOURCE_CONFIRMED = "confirmed"
SOURCE_TO_CONFIRM = "to_confirm"


@dataclass(frozen=True)
class FeatureSpec:
    """One row of the v1.2 feature list plus modelling metadata."""

    name: str
    group: str
    dtype: str
    source: str
    source_status: str
    expected_iv: int | None
    delivery: str
    tier_b_coverage: str
    dimension: str | None = None
    monotone: int = 0
    optional: bool = False

    @property
    def is_categorical(self) -> bool:
        return self.dtype == "CAT"


def _spec(
    name: str,
    group: str,
    dtype: str,
    source: str,
    iv: int | None,
    delivery: str,
    coverage: str,
    dimension: str | None = None,
    monotone: int = 0,
    optional: bool = False,
) -> FeatureSpec:
    return FeatureSpec(
        name=name,
        group=group,
        dtype=dtype,
        source=source,
        source_status=SOURCE_TO_CONFIRM if not source else SOURCE_CONFIRMED,
        expected_iv=iv,
        delivery=delivery,
        tier_b_coverage=coverage,
        dimension=dimension,
        monotone=monotone,
        optional=optional,
    )


GROUP_A = "A. Demographics"
GROUP_B = "B. Banking Relationship Depth"
GROUP_C = "C. Credit Behaviour Depth"
GROUP_D = "D. Income & Financial Health"
GROUP_E = "E. Transaction Behaviour Patterns"
GROUP_F = "F. App Behaviour Depth"
GROUP_G = "G. Consumer Credit Preferences"
GROUP_B_MB = "B. Banking Relationship Depth - MB Additions"
GROUP_C_MB = "C. Credit Behaviour Depth - MB Additions"
GROUP_E_MB = "E. Transaction Behaviour Patterns - MB Additions"
GROUP_F_MB = "F. App Behaviour Depth - MB Additions"

CORE_FEATURES: tuple[FeatureSpec, ...] = (
    # A. Demographics (6) - v1.2 dropped the three region-derived source features and added region.
    _spec("age", GROUP_A, "NUM", "Individual Customer Master", 3,
          D_CUSTOMER_MASTER, COVERAGE_HIGH, DIM_AGE),
    _spec("marital_status", GROUP_A, "CAT", "Individual Customer Master", 3,
          D_CUSTOMER_MASTER, COVERAGE_HIGH),
    _spec("education_level", GROUP_A, "CAT", "Individual Customer Master", 3,
          D_CUSTOMER_MASTER, COVERAGE_HIGH),
    _spec("occupation_type", GROUP_A, "CAT", "Individual Customer Master", 4,
          D_CUSTOMER_MASTER, COVERAGE_HIGH),
    _spec("employment_years", GROUP_A, "NUM", "Customer form + HR data", 3,
          D_CUSTOMER_MASTER, COVERAGE_LOW, None, 1),
    _spec("region", GROUP_A, "CAT", "Customer Master / Address Mapping", None,
          D_CUSTOMER_MASTER, COVERAGE_HIGH),
    # B. Banking Relationship Depth (18)
    _spec("banking_relationship_months", GROUP_B, "NUM", "Individual Customer Master", 3,
          D_CUSTOMER_MASTER, COVERAGE_HIGH, DIM_TENURE, 1),
    _spec("avg_balance_6m", GROUP_B, "NUM", "Internal Asset Table", 4,
          D_INTERNAL_TXN, COVERAGE_MEDIUM, DIM_AUM, 1),
    _spec("balance_trend_6m", GROUP_B, "CAT", "Internal Asset Table", 3,
          D_INTERNAL_TXN, COVERAGE_MEDIUM, DIM_AUM),
    _spec("avg_monthly_flow_6m", GROUP_B, "NUM", "Transaction Records", 4,
          D_INTERNAL_TXN, COVERAGE_MEDIUM, DIM_ACTIVITY, 1),
    _spec("investment_product_flag", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_LOW, DIM_PRODUCT),
    _spec("loan_flag", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_MEDIUM, DIM_PRODUCT),
    _spec("casa_flag", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_MEDIUM, DIM_PRODUCT),
    _spec("term_deposit_flag", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_LOW, DIM_PRODUCT),
    _spec("certificate_of_deposit_cds_flag", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_LOW, DIM_PRODUCT),
    _spec("vietqr_customer_flag", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_MEDIUM, DIM_PRODUCT),
    _spec("vietqr_merchant_customer_flag", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_LOW, DIM_PRODUCT),
    _spec("mbal_insurance_policy_flag", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_LOW, DIM_PRODUCT),
    _spec("credit_card_flag", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_MEDIUM, DIM_PRODUCT),
    _spec("total_product_count", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_HIGH, DIM_PRODUCT, 1),
    _spec("banking_service_count", GROUP_B, "NUM", "", None,
          D_PRODUCT_HOLDING, COVERAGE_MEDIUM, DIM_PRODUCT, 1),
    _spec("total_product_balance", GROUP_B, "NUM", "Internal Product Holdings", 4,
          D_INTERNAL_TXN, COVERAGE_LOW, DIM_AUM, 1),
    _spec("wealth_management_user", GROUP_B, "BIN", "Internal Product Holdings", 2,
          D_INTERNAL_TXN, COVERAGE_LOW, DIM_PRODUCT),
    _spec("credit_card_user", GROUP_B, "BIN", "Internal Product Holdings", 2,
          D_INTERNAL_TXN, COVERAGE_LOW, DIM_PRODUCT),
    # C. Credit Behaviour Depth (8) - CIC based, the only signal independent of the MB relationship.
    _spec("total_credit_accounts", GROUP_C, "NUM", "CIC + Internal Credit", 3,
          D_CIC, COVERAGE_MEDIUM),
    _spec("credit_card_utilization", GROUP_C, "NUM", "CIC", 4, D_CIC, COVERAGE_MEDIUM),
    _spec("max_overdue_days_12m", GROUP_C, "NUM", "CIC + Internal Credit", 4,
          D_CIC, COVERAGE_MEDIUM, None, -1),
    _spec("overdue_count_12m", GROUP_C, "NUM", "CIC + Internal Credit", 4,
          D_CIC, COVERAGE_MEDIUM, None, -1),
    _spec("inquiry_concentration", GROUP_C, "NUM", "CIC API", 3, D_CIC, COVERAGE_MEDIUM),
    _spec("loan_inquiry_ratio_6m", GROUP_C, "NUM", "CIC API", 3, D_CIC, COVERAGE_MEDIUM),
    _spec("other_bank_debt_to_income", GROUP_C, "NUM", "CIC + Income Data", 4,
          D_CIC, COVERAGE_MEDIUM, None, -1),
    _spec("cic_score_trend", GROUP_C, "CAT", "CIC API", 3, D_CIC, COVERAGE_MEDIUM),
    # D. Income & Financial Health (8) - needs salary/transaction history.
    _spec("income_cv_12m", GROUP_D, "NUM", "Transaction Records", 4,
          D_INTERNAL_TXN, COVERAGE_LOW, None, -1),
    _spec("salary_credit_months_6m", GROUP_D, "NUM", "Transaction Records", 4,
          D_INTERNAL_TXN, COVERAGE_LOW, None, 1),
    _spec("avg_salary_amount_6m", GROUP_D, "NUM", "Transaction Records", 4,
          D_INTERNAL_TXN, COVERAGE_LOW, None, 1),
    _spec("salary_trend_6m", GROUP_D, "CAT", "Transaction Records", 3,
          D_INTERNAL_TXN, COVERAGE_LOW),
    _spec("income_source_count", GROUP_D, "NUM", "Transaction Records", 2,
          D_INTERNAL_TXN, COVERAGE_LOW),
    _spec("disposable_income_3m", GROUP_D, "NUM", "Transaction Records", 4,
          D_INTERNAL_TXN, COVERAGE_LOW, None, 1),
    _spec("savings_rate_12m", GROUP_D, "NUM", "Transaction Records", 3,
          D_INTERNAL_TXN, COVERAGE_LOW),
    _spec("liquidity_coverage_months", GROUP_D, "NUM", "Internal Assets + Expenditure", 4,
          D_INTERNAL_TXN, COVERAGE_LOW, None, 1),
    # E. Transaction Behaviour Patterns (8)
    _spec("balance_volatility_6m", GROUP_E, "NUM", "Internal Asset Table", 3,
          D_INTERNAL_TXN, COVERAGE_MEDIUM, DIM_AUM),
    _spec("avg_spending_3m", GROUP_E, "NUM", "Transaction Records", 3,
          D_INTERNAL_TXN, COVERAGE_LOW, DIM_ACTIVITY),
    _spec("spending_category_count_3m", GROUP_E, "NUM", "Transaction Records + MCC", 2,
          D_INTERNAL_TXN, COVERAGE_LOW, DIM_ACTIVITY),
    _spec("premium_spending_ratio_3m", GROUP_E, "NUM", "Transaction Records + MCC", 2,
          D_INTERNAL_TXN, COVERAGE_LOW),
    _spec("night_spending_ratio_6m", GROUP_E, "NUM", "Transaction Records", 2,
          D_INTERNAL_TXN, COVERAGE_LOW),
    _spec("online_spending_ratio_6m", GROUP_E, "NUM", "Transaction Records", 2,
          D_INTERNAL_TXN, COVERAGE_LOW),
    _spec("cash_withdrawal_freq_6m", GROUP_E, "NUM", "ATM/Counter Records", 2,
          D_INTERNAL_TXN, COVERAGE_LOW),
    _spec("cross_border_txn_count_12m", GROUP_E, "NUM", "Transaction Records", 2,
          D_INTERNAL_TXN, COVERAGE_LOW),
    # F. App Behaviour Depth (8) - intent signals, strongest for the core tier.
    _spec("login_freq_30d", GROUP_F, "NUM", "App Events", 3,
          D_APP_EVENTS, COVERAGE_MEDIUM, DIM_ACTIVITY, 1),
    _spec("active_days_90d", GROUP_F, "NUM", "App Events", 3,
          D_APP_EVENTS, COVERAGE_MEDIUM, DIM_ACTIVITY, 1),
    _spec("avg_session_duration_30d", GROUP_F, "NUM", "App Events", 2,
          D_APP_EVENTS, COVERAGE_LOW, DIM_ACTIVITY),
    _spec("credit_page_views_30d", GROUP_F, "NUM", "App Events", 4,
          D_APP_EVENTS, COVERAGE_LOW, None, 1),
    _spec("loan_calculator_usage_30d", GROUP_F, "NUM", "App Events", 4,
          D_APP_EVENTS, COVERAGE_LOW, None, 1),
    _spec("loan_feature_used_ever", GROUP_F, "BIN", "App Events", 3,
          D_APP_EVENTS, COVERAGE_MEDIUM, None, 1),
    _spec("app_usage_time_preference", GROUP_F, "CAT", "App Events", 2,
          D_APP_EVENTS, COVERAGE_LOW),
    _spec("days_since_last_login", GROUP_F, "NUM", "App Events", 3,
          D_APP_EVENTS, COVERAGE_MEDIUM, DIM_ACTIVITY, -1),
    # G. Consumer Credit Preferences (2)
    _spec("has_instalment_3m", GROUP_G, "BIN", "Transaction Records", 2,
          D_INTERNAL_TXN, COVERAGE_LOW),
    _spec("has_insurance_12m", GROUP_G, "BIN", "Internal Product Holdings", 2,
          D_INTERNAL_TXN, COVERAGE_LOW, DIM_PRODUCT),
    # MB Available Additions (11) - the only group with confirmed physical sources.
    _spec("cash_deposit_balance_t0", GROUP_B_MB, "NUM", "RPT_CST_PD_HLD_DLY.CASA_AMT", 4,
          D_MB_CONFIRMED, COVERAGE_MEDIUM, DIM_AUM, 1),
    _spec("term_deposit_balance_t0", GROUP_B_MB, "NUM", "RPT_CST_PD_HLD_DLY.TGTK_AMT", 3,
          D_MB_CONFIRMED, COVERAGE_LOW, DIM_AUM, 1),
    _spec("liquid_asset_balance_t0", GROUP_B_MB, "NUM",
          "RPT_CST_PD_HLD_DLY.CASA_AMT/TGTK_AMT/CDS_AMT", 4,
          D_MB_CONFIRMED, COVERAGE_MEDIUM, DIM_AUM, 1),
    _spec("deposit_stability_ratio_t0", GROUP_B_MB, "NUM",
          "RPT_CST_PD_HLD_DLY.TGTK_AMT/CDS_AMT/CASA_AMT", 3,
          D_MB_CONFIRMED, COVERAGE_LOW, DIM_AUM),
    _spec("core_product_category_count", GROUP_B_MB, "NUM",
          "RPT_CST_PD_HLD_DLY product flags", 4,
          D_MB_CONFIRMED, COVERAGE_HIGH, DIM_PRODUCT, 1),
    _spec("card_spending_amount_6m", GROUP_E_MB, "NUM",
          "RPT_CST_PD_HLD_DLY.CARD_CR_DB_AMT_TRAN_6M", 3,
          D_MB_CONFIRMED, COVERAGE_LOW, DIM_ACTIVITY),
    _spec("card_spending_recent_intensity", GROUP_E_MB, "NUM",
          "RPT_CST_PD_HLD_DLY.CARD_CR_DB_AMT_TRAN_6M/CARD_CR_DB_AMT_TRAN_12M", 3,
          D_MB_CONFIRMED, COVERAGE_LOW, DIM_ACTIVITY),
    _spec("mb_app_active_flag", GROUP_F_MB, "BIN", "RPT_CST_PD_HLD_DLY.FLAG_APP_ACTIVE", 3,
          D_MB_CONFIRMED, COVERAGE_HIGH, DIM_ACTIVITY, 1),
    _spec("mini_app_count", GROUP_F_MB, "NUM", "RPT_CST_PD_HLD_DLY.SL_MINI_APP", 2,
          D_MB_CONFIRMED, COVERAGE_LOW, DIM_ACTIVITY),
    _spec("internal_npl_flag", GROUP_C_MB, "BIN",
          "BPM_CS_LOAN_OPPORTUNITY_DATA_SNPST.NON_PERFORMING_LOAN", 4,
          D_MB_CONFIRMED, COVERAGE_HIGH, None, -1),
    _spec("dti_latest", GROUP_C_MB, "NUM", "BPM_CS_LOAN_OPPORTUNITY_DATA_SNPST.DTI", 4,
          D_MB_CONFIRMED, COVERAGE_MEDIUM, None, -1),
)

GROUP_D2 = "D2. Settlement Account Cash Flow (Optional)"
GROUP_D3 = "D3. Counterparty and Concentration (Optional)"
GROUP_D4 = "D4. Cash Flow Volatility and Seasonality (Optional)"

_OPTIONAL_NAMES: tuple[tuple[str, str], ...] = (
    ("settlement_inflow_amount_6m", GROUP_D2),
    ("settlement_inflow_count_6m", GROUP_D2),
    ("settlement_outflow_amount_6m", GROUP_D2),
    ("settlement_outflow_count_6m", GROUP_D2),
    ("net_cashflow_amount_6m", GROUP_D2),
    ("internal_inflow_amount_6m", GROUP_D2),
    ("internal_inflow_count_6m", GROUP_D2),
    ("internal_outflow_amount_6m", GROUP_D2),
    ("internal_outflow_count_6m", GROUP_D2),
    ("avg_inflow_ticket_6m", GROUP_D2),
    ("avg_outflow_ticket_6m", GROUP_D2),
    ("same_name_interbank_inflow_amount_6m", GROUP_D3),
    ("same_name_interbank_inflow_count_6m", GROUP_D3),
    ("same_name_interbank_outflow_amount_6m", GROUP_D3),
    ("same_name_interbank_outflow_count_6m", GROUP_D3),
    ("diff_name_interbank_inflow_amount_6m", GROUP_D3),
    ("diff_name_interbank_inflow_count_6m", GROUP_D3),
    ("diff_name_interbank_outflow_amount_6m", GROUP_D3),
    ("diff_name_interbank_outflow_count_6m", GROUP_D3),
    ("distinct_inflow_counterparty_count_6m", GROUP_D3),
    ("distinct_outflow_counterparty_count_6m", GROUP_D3),
    ("top1_inflow_counterparty_concentration_6m", GROUP_D3),
    ("top3_inflow_counterparty_concentration_6m", GROUP_D3),
    ("top1_outflow_counterparty_concentration_6m", GROUP_D3),
    ("monthly_inflow_cv_12m", GROUP_D4),
    ("monthly_outflow_cv_12m", GROUP_D4),
    ("positive_cashflow_months_12m", GROUP_D4),
    ("seasonality_strength_12m", GROUP_D4),
    ("recent_inflow_momentum_3m_vs_12m", GROUP_D4),
    ("recent_outflow_momentum_3m_vs_12m", GROUP_D4),
)

OPTIONAL_FEATURES: tuple[FeatureSpec, ...] = tuple(
    _spec(
        name,
        group,
        "NUM",
        "Transaction detail tables",
        4,
        D_CASHFLOW,
        COVERAGE_LOW,
        DIM_ACTIVITY,
        0,
        optional=True,
    )
    for name, group in _OPTIONAL_NAMES
)

ALL_FEATURES: tuple[FeatureSpec, ...] = CORE_FEATURES + OPTIONAL_FEATURES
_BY_NAME: dict[str, FeatureSpec] = {spec.name: spec for spec in ALL_FEATURES}


def get_feature(name: str) -> FeatureSpec:
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise ValueError(f"Unknown feature '{name}'") from exc


def core_features() -> tuple[FeatureSpec, ...]:
    return CORE_FEATURES


def optional_features() -> tuple[FeatureSpec, ...]:
    return OPTIONAL_FEATURES


def catalog_frame(include_optional: bool = True) -> pd.DataFrame:
    """The catalog as a DataFrame, for reports and joins against real data profiling."""
    specs = ALL_FEATURES if include_optional else CORE_FEATURES
    return pd.DataFrame(
        [
            {
                "feature": spec.name,
                "group": spec.group,
                "type": spec.dtype,
                "source": spec.source or "(blank in v1.2)",
                "source_status": spec.source_status,
                "expected_iv": spec.expected_iv,
                "delivery": spec.delivery,
                "tier_b_coverage": spec.tier_b_coverage,
                "dimension": spec.dimension,
                "monotone": spec.monotone,
                "optional": spec.optional,
            }
            for spec in specs
        ]
    )


def planned_features(
    tier: str,
    *,
    include_optional: bool = False,
    exclude_delivery: set[str] | None = None,
) -> list[str]:
    """Catalog-level feature plan for a tier, before empirical screening.

    ``tier_a_core`` gets everything that has landed; ``tier_b_extended`` drops the features
    whose expected coverage in that tier is low, because a column that is empty or constant
    for single-product and dormant customers only adds noise and maintenance cost.
    """
    excluded = exclude_delivery or set()
    specs = CORE_FEATURES + (OPTIONAL_FEATURES if include_optional else ())
    if tier == "tier_a_core":
        return [spec.name for spec in specs if spec.delivery not in excluded]
    if tier == "tier_b_extended":
        return [
            spec.name
            for spec in specs
            if spec.delivery not in excluded
            and spec.tier_b_coverage in {COVERAGE_HIGH, COVERAGE_MEDIUM}
            and not spec.optional
        ]
    raise ValueError(f"Unknown tier '{tier}'")


def monotone_constraints(features: list[str]) -> list[int]:
    """LightGBM ``monotone_constraints`` aligned with the given feature order."""
    return [get_feature(name).monotone for name in features]


def categorical_features(features: list[str]) -> list[str]:
    return [name for name in features if get_feature(name).is_categorical]


def unconfirmed_source_features() -> list[str]:
    """Features whose data source is still blank in v1.2 (delivery risk)."""
    return [spec.name for spec in CORE_FEATURES if spec.source_status == SOURCE_TO_CONFIRM]


def delivery_summary(include_optional: bool = True) -> pd.DataFrame:
    """Feature counts per delivery workstream and per tier plan."""
    frame = catalog_frame(include_optional)
    tier_a = set(planned_features("tier_a_core", include_optional=include_optional))
    tier_b = set(planned_features("tier_b_extended"))
    summary = (
        frame.groupby("delivery")
        .agg(
            features=("feature", "size"),
            source_confirmed=("source_status", lambda values: int((values == "confirmed").sum())),
        )
        .reset_index()
    )
    summary["tier_a_planned"] = summary["delivery"].map(
        lambda delivery: int(
            frame.loc[frame["delivery"] == delivery, "feature"].isin(tier_a).sum()
        )
    )
    summary["tier_b_planned"] = summary["delivery"].map(
        lambda delivery: int(
            frame.loc[frame["delivery"] == delivery, "feature"].isin(tier_b).sum()
        )
    )
    return summary.sort_values("delivery").reset_index(drop=True)


def coverage_matrix() -> pd.DataFrame:
    """Feature-group by expected tier-B coverage, the input to the tier-B feature plan."""
    frame = catalog_frame(include_optional=False)
    matrix = (
        frame.pivot_table(
            index="group", columns="tier_b_coverage", values="feature", aggfunc="count"
        )
        .fillna(0)
        .astype(int)
    )
    for column in (COVERAGE_HIGH, COVERAGE_MEDIUM, COVERAGE_LOW):
        if column not in matrix.columns:
            matrix[column] = 0
    matrix = matrix[[COVERAGE_HIGH, COVERAGE_MEDIUM, COVERAGE_LOW]]
    matrix["total"] = matrix.sum(axis=1)
    matrix["tier_b_planned"] = matrix[COVERAGE_HIGH] + matrix[COVERAGE_MEDIUM]
    return matrix.reset_index()
