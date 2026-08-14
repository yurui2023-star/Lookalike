"""Shared scaffolding for the modelling demos.

The Bank Marketing CSV stands in for MB's feature mart. These helpers map it onto the five
diagnosis dimensions and re-weight tier shares and conversion rates to the values in business
diagnosis v3.8, so the demos exercise MB-like mechanics while the underlying feature/label
association stays real. They are demo utilities, not part of the library.
"""

from __future__ import annotations

import lightgbm as lgb
import pandas as pd

from lookalike.config import LABEL_COL
from lookalike.domain.product_profiles import (
    COL_ACTIVITY,
    COL_AGE,
    COL_AUM,
    COL_PRODUCT_COUNT,
    COL_TENURE_MONTHS,
    TIER_A_CORE,
    TIER_B_EXTENDED,
)
from lookalike.modeling.lightgbm_model import encode_features

CANONICAL = (COL_PRODUCT_COUNT, COL_AUM, COL_ACTIVITY, COL_AGE, COL_TENURE_MONTHS)
HELPERS = ("cohort", "group_id", "tier", "tier_reason", "stratum", *CANONICAL)
QUARTER_BY_MONTH = {
    "Jan": "2025Q1", "Feb": "2025Q1", "Mar": "2025Q1",
    "Apr": "2025Q2", "May": "2025Q2", "Jun": "2025Q2",
    "Jul": "2025Q3", "Aug": "2025Q3", "Sep": "2025Q3",
    "Oct": "2025Q4", "Nov": "2025Q4", "Dec": "2025Q4",
}
# MB diagnosis v3.8, Personal Unsecured Loan, 3-month outcome window.
DIAGNOSIS = {
    "base_customers": 35_720_000,
    "pool_customers": 4_720_000,
    "pool_rate_per_10k": 143.1,
    "tail_rate_per_10k": 2.5,  # 7,723 applicants outside the pool over ~31M customers
}


def build_canonical(raw: pd.DataFrame) -> pd.DataFrame:
    """Derive the five diagnosis dimensions from the demo CSV (proxy mapping)."""
    frame = raw.copy()
    frame[COL_PRODUCT_COUNT] = frame["NumBankProducts"]
    frame[COL_AUM] = frame["AccountBalance"]
    frame[COL_AGE] = frame["Age"]
    frame[COL_TENURE_MONTHS] = frame["TenureWithBank"] * 12
    frame[COL_ACTIVITY] = sum(
        signal.astype(int)
        for signal in (
            frame["TotalTransactions"] > 0,
            frame["NumOnlineTransactions"] > 0,
            frame["NumMobileAppLogins"] > 0,
            frame["BranchVisitFrequency"] > 0,
            frame["WebsiteActivityScore"] > 50,
        )
    )
    frame["cohort"] = frame["LastContactMonth"].map(QUARTER_BY_MONTH)
    frame["group_id"] = frame["ClientID"].astype(str)
    return frame


def build_base(
    frame: pd.DataFrame,
    tiers: pd.DataFrame,
    base_rows: int,
    random_state: int = 42,
) -> pd.DataFrame:
    """Resample so tier shares and per-tier conversion rates match the diagnosis."""
    pool_share = DIAGNOSIS["pool_customers"] / DIAGNOSIS["base_customers"]
    targets = {
        TIER_A_CORE: int(base_rows * pool_share),
        TIER_B_EXTENDED: base_rows - int(base_rows * pool_share),
    }
    rates = {
        TIER_A_CORE: DIAGNOSIS["pool_rate_per_10k"] / 10_000,
        TIER_B_EXTENDED: DIAGNOSIS["tail_rate_per_10k"] / 10_000,
    }

    labelled = frame.assign(tier=tiers["tier"], tier_reason=tiers["tier_reason"])
    parts = []
    for tier, target in targets.items():
        subset = labelled.loc[labelled["tier"] == tier]
        if subset.empty:
            continue
        resampled = subset.sample(
            n=target, replace=target > len(subset), random_state=random_state
        )
        parts.append(_thin_positives(resampled, rates[tier], random_state))
    return pd.concat(parts).sample(frac=1.0, random_state=random_state).reset_index(drop=True)


def _thin_positives(frame: pd.DataFrame, target_rate: float, random_state: int) -> pd.DataFrame:
    """Randomly demote positives until the frame hits ``target_rate``.

    Thinning is random, so P(label=1 | features) keeps its original shape inside the tier and
    only the base rate changes.
    """
    positives = frame.loc[frame[LABEL_COL] == 1]
    negatives = frame.loc[frame[LABEL_COL] == 0]
    keep = int(round(len(frame) * target_rate))
    keep = max(1, min(keep, len(positives)))
    kept = positives.sample(n=keep, random_state=random_state)
    demoted = positives.drop(kept.index).assign(**{LABEL_COL: 0})
    return pd.concat([kept, demoted, negatives])


def product_band(count: pd.Series) -> pd.Series:
    return pd.cut(
        count, bins=[-1, 1, 2, 4, 6, 100], labels=["0-1", "2", "3-4", "5-6", "7+"]
    ).astype(str)


def train_lightgbm(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    feature_cols: list[str],
    weight_col: str | None = None,
    params: dict[str, object] | None = None,
) -> tuple[lgb.Booster, list[str]]:
    x_train = encode_features(train[feature_cols])
    x_validation = encode_features(validation[feature_cols]).reindex(
        columns=x_train.columns, fill_value=0
    )
    merged: dict[str, object] = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 40,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "seed": 42,
    }
    merged.update(params or {})
    booster = lgb.train(
        merged,
        lgb.Dataset(
            x_train,
            train[LABEL_COL],
            weight=train[weight_col] if weight_col else None,
        ),
        valid_sets=[lgb.Dataset(x_validation, validation[LABEL_COL])],
        num_boost_round=400,
        callbacks=[lgb.early_stopping(40, verbose=False)],
    )
    return booster, x_train.columns.tolist()


def predict(
    booster: lgb.Booster,
    frame: pd.DataFrame,
    feature_cols: list[str],
    encoded_cols: list[str],
) -> pd.Series:
    matrix = encode_features(frame[feature_cols]).reindex(columns=encoded_cols, fill_value=0)
    return pd.Series(booster.predict(matrix), index=frame.index, name="score")


def markdown_table(frame: pd.DataFrame, decimals: int = 4) -> str:
    """Render a markdown table without pulling in a formatting dependency."""
    display = frame.copy()
    for column in display.columns:
        if pd.api.types.is_float_dtype(display[column]):
            display[column] = display[column].map(
                lambda value: "" if pd.isna(value) else f"{value:.{decimals}f}"
            )
        else:
            display[column] = display[column].astype(str)
    header = "| " + " | ".join(display.columns) + " |"
    divider = "| " + " | ".join("---" for _ in display.columns) + " |"
    rows = ["| " + " | ".join(row) + " |" for row in display.to_numpy().tolist()]
    return "\n".join([header, divider, *rows])
