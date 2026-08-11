"""Feature adapters: map raw source schemas onto modeling frames (Design v2.1 §4)."""

from __future__ import annotations

from typing import Protocol

import pandas as pd

from lookalike.adapters.leakage import (
    COLD_START_ACTIVITY_COLUMNS,
    DEFAULT_DROP_FROM_MODELING,
    assert_no_leakage,
    drop_denylist_columns,
    find_leakage_columns,
)
from lookalike.config import ID_COL, LABEL_COL, TARGET_COL


class FeatureAdapter(Protocol):
    """Bridge raw tabular data to a product-specific modeling frame."""

    product: str

    def to_model_frame(
        self,
        raw: pd.DataFrame,
        *,
        drop_id: bool = True,
        rename_target: bool = True,
        for_scoring: bool = False,
    ) -> pd.DataFrame: ...

    def feature_list(self) -> list[str]: ...

    def leakage_denylist(self) -> list[str]: ...

    def extract_ids(self, raw: pd.DataFrame) -> list[str]: ...

    def cold_start_mask(self, raw: pd.DataFrame) -> pd.Series: ...


class BankMarketingCsvAdapter:
    """
    Adapter for data/Bank_Marketing_Dataset.csv (CSV → model features).

    Maps ClientID / TermDepositSubscribed conventions and strips leakage fields.
    """

    product = "bank_marketing_term_deposit"

    def __init__(self, id_col: str = ID_COL, target_col: str = TARGET_COL) -> None:
        self.id_col = id_col
        self.target_col = target_col

    def leakage_denylist(self) -> list[str]:
        from lookalike.adapters.leakage import LEAKAGE_DENYLIST

        return sorted(LEAKAGE_DENYLIST)

    def feature_list(self) -> list[str]:
        """Documented semantic features excluding id/target/denylist."""
        return [
            "Age",
            "Gender",
            "MaritalStatus",
            "EducationLevel",
            "EmploymentStatus",
            "JobTitle",
            "Region",
            "SalaryCategory",
            "CustomerSegment",
            "AnnualIncome",
            "NetWorth",
            "CreditScore",
            "CreditLimit",
            "RiskRating",
            "AccountLengthYears",
            "TenureWithBank",
            "AccountBalance",
            "NumBankProducts",
            "HasCreditCard",
            "HasMortgage",
            "HasPersonalLoan",
            "HasLifeInsurance",
            "HasMutualFunds",
            "InvestmentPortfolioValue",
            "TotalTransactions",
            "AvgTransactionValue",
            "NumOnlineTransactions",
            "NumMobileAppLogins",
            "BranchVisitFrequency",
            "ChannelPreference",
            "WebsiteActivityScore",
            "LastContactChannel",
            "LastContactMonth",
            "LastContactDay",
            "LastContactDuration",
            "NumContactsInCampaign",
            "NumPrevCampaignContacts",
            "PrevCampaignOutcome",
            "CallResponseScore",
            "DaysSinceLastContact",
            "PreviousYearDeposit",
            "MarketingScore",
        ]

    def extract_ids(self, raw: pd.DataFrame) -> list[str]:
        if self.id_col in raw.columns:
            return raw[self.id_col].astype(str).tolist()
        return [f"row-{index}" for index in range(len(raw))]

    def cold_start_mask(self, raw: pd.DataFrame) -> pd.Series:
        """True where customer has no measurable activity (Design v2.1 cold-start)."""
        activity_cols = [c for c in COLD_START_ACTIVITY_COLUMNS if c in raw.columns]
        if not activity_cols:
            return pd.Series(False, index=raw.index)
        totals = raw[activity_cols].fillna(0).sum(axis=1)
        return totals <= 0

    def validate_raw_columns(self, raw: pd.DataFrame) -> list[str]:
        """Return leakage columns present in raw input (informational before drop)."""
        return find_leakage_columns(raw.columns.tolist())

    def to_model_frame(
        self,
        raw: pd.DataFrame,
        *,
        drop_id: bool = True,
        rename_target: bool = True,
        for_scoring: bool = False,
    ) -> pd.DataFrame:
        frame = raw.copy()

        # Drop all denylist columns first (case-insensitive), then ensure id drop policy.
        keep = drop_denylist_columns(frame.columns.tolist())
        # If drop_id is False, restore id_col when it was only removed via denylist.
        if not drop_id and self.id_col in raw.columns and self.id_col not in keep:
            keep = [self.id_col, *keep]
        # Also drop any DEFAULT_DROP extras still present under exact names.
        for col in DEFAULT_DROP_FROM_MODELING:
            if col in keep and (drop_id or col != self.id_col):
                keep = [c for c in keep if c != col]
        frame = frame.loc[:, [c for c in keep if c in frame.columns]]

        if rename_target and self.target_col in frame.columns:
            frame = frame.rename(columns={self.target_col: LABEL_COL})

        if for_scoring and LABEL_COL in frame.columns:
            frame = frame.drop(columns=[LABEL_COL])

        # Hard fail if anything denylisted still remains (e.g. unexpected casing left in).
        assert_no_leakage(frame.columns.tolist(), context=f"{self.product} model frame")
        return frame


def get_adapter(product: str = "bank_marketing_term_deposit") -> BankMarketingCsvAdapter:
    if product in {"bank_marketing_term_deposit", "default", "mvp"}:
        return BankMarketingCsvAdapter()
    raise ValueError(f"Unknown product adapter: {product}")
