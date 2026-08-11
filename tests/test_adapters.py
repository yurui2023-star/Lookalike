import pandas as pd
import pytest

from lookalike.adapters.bank_marketing import BankMarketingCsvAdapter, get_adapter
from lookalike.adapters.leakage import (
    LEAKAGE_DENYLIST,
    assert_no_leakage,
    find_leakage_columns,
)
from lookalike.config import ID_COL, LABEL_COL, TARGET_COL


def _raw_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            ID_COL: ["CIF1", "CIF2", "CIF3"],
            "Age": [30, 40, 50],
            "AnnualIncome": [50000, 80000, 20000],
            "TotalTransactions": [10, 0, 5],
            "NumOnlineTransactions": [2, 0, 1],
            "NumMobileAppLogins": [3, 0, 0],
            "BranchVisitFrequency": [1, 0, 2],
            "MarketingScore": [0.8, 0.2, 0.5],
            "ResponsePropensity": [0.9, 0.1, 0.4],
            TARGET_COL: [1, 0, 1],
        }
    )


def test_find_leakage_columns_detects_response_propensity():
    hits = find_leakage_columns(["Age", "ResponsePropensity", "MarketingScore"])
    assert hits == ["ResponsePropensity"]


def test_assert_no_leakage_raises():
    with pytest.raises(ValueError, match="denylist"):
        assert_no_leakage(["Age", "ResponsePropensity"])


def test_assert_no_leakage_passes_clean_columns():
    assert_no_leakage(["Age", "MarketingScore", LABEL_COL])


def test_adapter_strips_leakage_and_renames_target():
    adapter = BankMarketingCsvAdapter()
    frame = adapter.to_model_frame(_raw_frame())
    assert ID_COL not in frame.columns
    assert "ResponsePropensity" not in frame.columns
    assert LABEL_COL in frame.columns
    assert TARGET_COL not in frame.columns
    assert_no_leakage(frame.columns.tolist())


def test_adapter_for_scoring_drops_label():
    adapter = get_adapter("mvp")
    frame = adapter.to_model_frame(_raw_frame(), for_scoring=True)
    assert LABEL_COL not in frame.columns
    assert "ResponsePropensity" not in frame.columns


def test_adapter_rejects_unknown_product():
    with pytest.raises(ValueError, match="Unknown product"):
        get_adapter("mortgage_not_ready")


def test_cold_start_mask():
    adapter = BankMarketingCsvAdapter()
    mask = adapter.cold_start_mask(_raw_frame())
    assert mask.tolist() == [False, True, False]


def test_denylist_includes_required_entries():
    for required in ("ResponsePropensity", "ClientID", "credit_decision_score"):
        assert required in LEAKAGE_DENYLIST
