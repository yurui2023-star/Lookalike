"""Product profiles: label design, screening funnel and scoring-domain tiers per product.

Encodes the modelling rules agreed between BRD v2.4 (sections 2 and 5) and the MB Bank
business diagnosis v3.8 (four-step screening funnel), so that sample construction is
configuration rather than ad-hoc SQL, and so that every run reports the same funnel the
diagnosis deck shows.

The screening funnel defines *where positives are dense*, not *who may be scored*. The two
populations are kept separate:

* ``screen_candidates`` reproduces the diagnosis funnel (35.72M -> 4.72M for MB) and is used
  to build the dense training stratum.
* ``assign_scoring_tier`` labels the **whole** customer base so the customers outside the
  screened pool still receive a score (Tier B) or an explicit non-scorable reason code
  (Tier C / excluded), instead of silently disappearing.

Canonical column names are used here; both functions accept a ``column_map`` for source
systems that name the same concept differently. Conditions whose column is absent are skipped
and reported, never silently treated as passing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

# Canonical feature/flag columns used by screening rules.
COL_PRODUCT_COUNT = "product_category_count"
COL_AUM = "aum_balance_t0"
COL_ACTIVITY = "activity_level_3m"
COL_AGE = "age"
COL_TENURE_MONTHS = "tenure_months"
COL_HOLDS_TARGET = "holds_target_product"
COL_EMPLOYEE = "employee_flag"
COL_TEST_ACCOUNT = "test_account_flag"
COL_CLOSED = "closed_account_flag"
COL_DECEASED = "deceased_flag"
COL_BLACKLIST = "blacklist_flag"

_OPERATORS = {
    ">": lambda series, value: series > value,
    ">=": lambda series, value: series >= value,
    "<": lambda series, value: series < value,
    "<=": lambda series, value: series <= value,
    "==": lambda series, value: series == value,
    "!=": lambda series, value: series != value,
    "in": lambda series, value: series.isin(value),
    "not in": lambda series, value: ~series.isin(value),
}


@dataclass(frozen=True)
class Condition:
    """A single screening predicate on one canonical column."""

    column: str
    op: str
    value: Any

    def __post_init__(self) -> None:
        if self.op not in _OPERATORS:
            raise ValueError(f"Unsupported operator: {self.op}")

    def describe(self) -> str:
        return f"{self.column} {self.op} {self.value}"

    def evaluate(self, frame: pd.DataFrame, column: str) -> pd.Series:
        return _OPERATORS[self.op](frame[column], self.value)


@dataclass(frozen=True)
class ScreeningStep:
    """One funnel step: all ``conditions`` must hold, unless a ``rescue`` clause applies."""

    step_id: str
    name: str
    conditions: tuple[Condition, ...]
    rescue: tuple[Condition, ...] = ()
    rationale: str = ""


@dataclass(frozen=True)
class LabelSpec:
    """Label and observation-window design for one product."""

    primary_event: str
    outcome_window_months: int
    quality_window_months: int
    max_overdue_days: int
    min_normal_repayment_months: int
    seed_lookback_months: int
    cohort_spacing_months: int
    negative_ratio: float
    seed_age_rule: str
    notes: str = ""


@dataclass(frozen=True)
class ProductProfile:
    """Everything the modelling pipeline needs to build one product's training sample."""

    key: str
    display_name: str
    brd_reference: str
    label: LabelSpec
    steps: tuple[ScreeningStep, ...]
    primary_features: tuple[str, ...] = ()
    stratification_column: str = COL_PRODUCT_COUNT
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def step_ids(self) -> list[str]:
        return [step.step_id for step in self.steps]


_COMPLIANCE_STEP = ScreeningStep(
    step_id="S0",
    name="Compliance and eligibility exclusions",
    conditions=(
        Condition(COL_EMPLOYEE, "==", 0),
        Condition(COL_TEST_ACCOUNT, "==", 0),
        Condition(COL_CLOSED, "==", 0),
        Condition(COL_DECEASED, "==", 0),
        Condition(COL_BLACKLIST, "==", 0),
        Condition(COL_HOLDS_TARGET, "==", 0),
    ),
    rationale=(
        "BRD 2.3 universal exclusions plus removal of customers who already hold the target "
        "product; these flags are constant inside the pool and must not become features."
    ),
)


PERSONAL_UNSECURED_LOAN = ProductProfile(
    key="personal_unsecured_loan",
    display_name="Personal Unsecured Loan (Super Fast Unsecured Loan)",
    brd_reference="BRD v2.4 s2.1.1 / s2.2.1; diagnosis v3.8 p23",
    label=LabelSpec(
        primary_event="disbursed",
        outcome_window_months=3,
        quality_window_months=3,
        max_overdue_days=30,
        min_normal_repayment_months=3,
        seed_lookback_months=12,
        cohort_spacing_months=3,
        negative_ratio=10.0,
        seed_age_rule="22-55 (female) / 22-60 (male) at seed definition",
        notes=(
            "Primary label: disbursed within T0+1..T0+3. Quality filter (no overdue > 30 days "
            "in the following 3 months) removes first-payment-default positives."
        ),
    ),
    steps=(
        _COMPLIANCE_STEP,
        ScreeningStep(
            step_id="S1",
            name="Product holding >= 2",
            conditions=(Condition(COL_PRODUCT_COUNT, ">=", 2),),
            rationale=(
                "Diagnosis: 18.6% of the base holds 2+ products but contributes 93.6% of "
                "3-month applicants (5.0x concentration)."
            ),
        ),
        ScreeningStep(
            step_id="S2",
            name="AUM > 0 and active in last 3 months",
            conditions=(
                Condition(COL_AUM, ">", 0),
                Condition(COL_ACTIVITY, ">=", 1),
            ),
            rationale=(
                "Inactive customers (21.6M) apply at 0.45 per 10K versus 37.65 for activity "
                "level 1; zero-AUM customers apply at 1.34 per 10K."
            ),
        ),
        ScreeningStep(
            step_id="S3",
            name="Age 18-60 and tenure > 0 months",
            conditions=(
                Condition(COL_AGE, ">=", 18),
                Condition(COL_AGE, "<=", 60),
                Condition(COL_TENURE_MONTHS, ">", 0),
            ),
            rationale="Age band 25-50 carries 89.3% of applicants; <18 and 60+ are near zero.",
        ),
    ),
    primary_features=(COL_PRODUCT_COUNT, COL_ACTIVITY, COL_AUM, COL_TENURE_MONTHS, COL_AGE),
    metadata={
        "diagnosis_pool_size": 4_720_000,
        "diagnosis_pool_applicants_3m": 69_130,
        "diagnosis_pool_rate_per_10k": 143.1,
        "diagnosis_overall_rate_per_10k": 21.52,
        "diagnosis_applicant_retention": 0.900,
    },
)


MORTGAGE_LOAN = ProductProfile(
    key="mortgage_loan",
    display_name="Mortgage Loan (Home Easy Loan)",
    brd_reference="BRD v2.4 s2.1.2 / s2.2.2; diagnosis v3.8 p24",
    label=LabelSpec(
        primary_event="applied",
        outcome_window_months=3,
        quality_window_months=6,
        max_overdue_days=10,
        min_normal_repayment_months=3,
        seed_lookback_months=24,
        cohort_spacing_months=3,
        negative_ratio=15.0,
        seed_age_rule="18-60 at feature observation date, per product policy",
        notes=(
            "Seed product is Home Easy Loan. Application-to-disbursement takes longer and "
            "drops ~30%, so the primary label stays at application and a second-stage model "
            "estimates P(disbursed|applied)."
        ),
    ),
    steps=(
        _COMPLIANCE_STEP,
        ScreeningStep(
            step_id="S1",
            name="Product holding >= 2, rescued by high AUM",
            conditions=(Condition(COL_PRODUCT_COUNT, ">=", 2),),
            rescue=(Condition(COL_AUM, ">=", 100_000_000),),
            rationale=(
                "Product holding >= 2 keeps only 80.6% of mortgage applicants; AUM is the "
                "second dimension (5B+ applies at 12.0%), so high-AUM low-product customers "
                "are recovered instead of dropped."
            ),
        ),
        ScreeningStep(
            step_id="S2",
            name="AUM > 0 and active in last 3 months",
            conditions=(
                Condition(COL_AUM, ">", 0),
                Condition(COL_ACTIVITY, ">=", 1),
            ),
            rationale="Activity lift 2.2x; inactive customers apply at 2.60 per 10K.",
        ),
        ScreeningStep(
            step_id="S3",
            name="Age 18-60 and tenure >= 13 months",
            conditions=(
                Condition(COL_AGE, ">=", 18),
                Condition(COL_AGE, "<=", 60),
                Condition(COL_TENURE_MONTHS, ">=", 13),
            ),
            rationale=(
                "Tenure is nearly flat for mortgage (1.0x); 13+ months mainly guarantees that "
                "the 6- and 12-month feature windows are populated."
            ),
        ),
    ),
    primary_features=(COL_PRODUCT_COUNT, COL_AUM, COL_ACTIVITY, COL_TENURE_MONTHS, COL_AGE),
    metadata={
        "diagnosis_pool_size": 4_720_000,
        "diagnosis_pool_applicants_3m": 28_912,
        "diagnosis_pool_rate_per_10k": 61.3,
        "diagnosis_overall_rate_per_10k": 11.70,
        "diagnosis_applicant_retention": 0.692,
        "seed_product": "Home Easy Loan",
    },
)


PRODUCT_PROFILES: dict[str, ProductProfile] = {
    PERSONAL_UNSECURED_LOAN.key: PERSONAL_UNSECURED_LOAN,
    MORTGAGE_LOAN.key: MORTGAGE_LOAN,
}


def get_product_profile(key: str) -> ProductProfile:
    try:
        return PRODUCT_PROFILES[key]
    except KeyError as exc:
        available = ", ".join(sorted(PRODUCT_PROFILES))
        raise ValueError(f"Unknown product profile '{key}'. Available: {available}") from exc


@dataclass(frozen=True)
class ScreeningResult:
    """Screened candidate pool plus the step-by-step funnel used for reporting."""

    frame: pd.DataFrame
    funnel: pd.DataFrame
    skipped_conditions: list[str]

    @property
    def retained(self) -> int:
        return len(self.frame)


def _evaluate_step(
    frame: pd.DataFrame,
    step: ScreeningStep,
    mapping: Mapping[str, str],
) -> tuple[pd.Series, list[str], list[str]]:
    """Return (keep mask, applied condition descriptions, skipped condition descriptions)."""
    keep = pd.Series(True, index=frame.index)
    applied: list[str] = []
    skipped: list[str] = []

    for condition in step.conditions:
        column = mapping.get(condition.column, condition.column)
        if column not in frame.columns:
            skipped.append(f"{step.step_id}:{condition.describe()}")
            continue
        keep &= condition.evaluate(frame, column)
        applied.append(condition.describe())

    if step.rescue:
        rescue_mask = pd.Series(False, index=frame.index)
        rescued_any = False
        for condition in step.rescue:
            column = mapping.get(condition.column, condition.column)
            if column not in frame.columns:
                skipped.append(f"{step.step_id}:rescue:{condition.describe()}")
                continue
            rescue_mask |= condition.evaluate(frame, column)
            applied.append(f"OR {condition.describe()}")
            rescued_any = True
        if rescued_any:
            keep |= rescue_mask

    return keep, applied, skipped


def screen_candidates(
    frame: pd.DataFrame,
    profile: ProductProfile,
    *,
    column_map: Mapping[str, str] | None = None,
    label_col: str | None = None,
    steps: Sequence[str] | None = None,
) -> ScreeningResult:
    """Apply a product's screening funnel and report retention at every step."""
    mapping = dict(column_map or {})
    working = frame
    selected_steps = [
        step for step in profile.steps if steps is None or step.step_id in set(steps)
    ]

    initial_rows = len(frame)
    initial_positives = int(frame[label_col].sum()) if label_col else 0
    funnel_rows: list[dict[str, object]] = []
    skipped: list[str] = []

    for step in selected_steps:
        rows_in = len(working)
        positives_in = int(working[label_col].sum()) if label_col else 0
        keep, applied, step_skipped = _evaluate_step(working, step, mapping)
        skipped.extend(step_skipped)

        working = working.loc[keep]
        rows_out = len(working)
        positives_out = int(working[label_col].sum()) if label_col else 0
        funnel_rows.append(
            {
                "step_id": step.step_id,
                "name": step.name,
                "conditions_applied": "; ".join(applied) if applied else "(none available)",
                "rows_in": rows_in,
                "rows_out": rows_out,
                "removed": rows_in - rows_out,
                "step_retention": rows_out / rows_in if rows_in else float("nan"),
                "cumulative_retention": (
                    rows_out / initial_rows if initial_rows else float("nan")
                ),
                "positives_in": positives_in,
                "positives_out": positives_out,
                "positive_retention": (
                    positives_out / initial_positives if initial_positives else float("nan")
                ),
            }
        )

    return ScreeningResult(
        frame=working.reset_index(drop=True),
        funnel=pd.DataFrame(funnel_rows),
        skipped_conditions=skipped,
    )


# ---------------------------------------------------------------------------
# Scoring domain: what happens to the customers the funnel removed
# ---------------------------------------------------------------------------

TIER_EXCLUDED = "excluded"
TIER_A_CORE = "tier_a_core"
TIER_B_EXTENDED = "tier_b_extended"
TIER_C_NOT_SCORABLE = "tier_c_not_scorable"

TIER_ORDER = (TIER_A_CORE, TIER_B_EXTENDED, TIER_C_NOT_SCORABLE, TIER_EXCLUDED)
SCORABLE_TIERS = (TIER_A_CORE, TIER_B_EXTENDED)


def assign_scoring_tier(
    frame: pd.DataFrame,
    profile: ProductProfile,
    *,
    column_map: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Classify every customer of the base into a scoring tier with a reason code.

    * ``excluded``            - compliance exclusion or already holds the target product.
    * ``tier_c_not_scorable`` - no feature footprint at all (cold start, BRD 1.4): the model
      must not invent a score; a segment prior is reported instead.
    * ``tier_a_core``         - passes the full screening funnel; dense positives.
    * ``tier_b_extended``     - scorable but fails one or more soft screening rules; scored by
      the same model, with the failed rules recorded so the score can be interpreted.
    """
    mapping = dict(column_map or {})
    compliance = next((step for step in profile.steps if step.step_id == "S0"), None)
    soft_steps = [step for step in profile.steps if step.step_id != "S0"]

    excluded = pd.Series(False, index=frame.index)
    if compliance is not None:
        keep, applied, _ = _evaluate_step(frame, compliance, mapping)
        if applied:
            excluded = ~keep

    # Encode which soft steps failed as a bitmask so reason strings stay vectorised.
    failure_code = pd.Series(0, index=frame.index, dtype="int64")
    evaluated_steps: list[str] = []
    for position, step in enumerate(soft_steps):
        keep, applied, _ = _evaluate_step(frame, step, mapping)
        if not applied:
            continue
        evaluated_steps.append(step.step_id)
        failure_code += (~keep).astype("int64") * (1 << position)

    code_to_reason = {0: "core_pool"}
    for code in range(1, 1 << len(soft_steps)):
        failed = [
            step.step_id
            for position, step in enumerate(soft_steps)
            if code & (1 << position) and step.step_id in evaluated_steps
        ]
        code_to_reason[code] = "failed:" + ",".join(failed) if failed else "core_pool"

    cold = pd.Series(True, index=frame.index)
    footprint_columns = 0
    for canonical in (COL_PRODUCT_COUNT, COL_AUM, COL_ACTIVITY):
        column = mapping.get(canonical, canonical)
        if column in frame.columns:
            cold &= frame[column].fillna(0) <= 0
            footprint_columns += 1
    if footprint_columns == 0:
        cold = pd.Series(False, index=frame.index)

    tier = pd.Series(TIER_A_CORE, index=frame.index, dtype="object")
    reason = failure_code.map(code_to_reason).astype("object")
    tier.loc[failure_code > 0] = TIER_B_EXTENDED
    tier.loc[cold] = TIER_C_NOT_SCORABLE
    reason.loc[cold] = "cold_start_no_feature_footprint"
    tier.loc[excluded] = TIER_EXCLUDED
    reason.loc[excluded] = "compliance_or_holds_target_product"

    return pd.DataFrame({"tier": tier, "tier_reason": reason})


def scoring_domain_summary(
    tiers: pd.DataFrame,
    label_col: pd.Series | None = None,
) -> pd.DataFrame:
    """Row counts, share and (optionally) conversion rate per scoring tier."""
    frame = tiers.copy()
    if label_col is not None:
        frame = frame.assign(label=pd.Series(label_col).to_numpy())

    total = len(frame)
    rows = []
    for tier in TIER_ORDER:
        subset = frame.loc[frame["tier"] == tier]
        row: dict[str, object] = {
            "tier": tier,
            "customers": len(subset),
            "share": len(subset) / total if total else float("nan"),
            "scorable": tier in SCORABLE_TIERS,
        }
        if label_col is not None:
            row["positives"] = int(subset["label"].sum()) if len(subset) else 0
            row["rate_per_10k"] = (
                float(subset["label"].mean()) * 10_000 if len(subset) else float("nan")
            )
        rows.append(row)
    return pd.DataFrame(rows)
