"""Leakage denylist and cold-start rules (Design v2.1 §5)."""

from __future__ import annotations

# Fields that must never enter training/scoring feature matrices.
# Presence of any of these after adapter preparation fails the run hard.
LEAKAGE_DENYLIST: frozenset[str] = frozenset(
    {
        # Derived / other-model scores
        "ResponsePropensity",
        "response_propensity",
        "credit_decision_score",
        "CreditDecisionScore",
        # Post-event / disbursement-only
        "current_loan_balance",
        "CurrentLoanBalance",
        "recent_disbursement_date",
        "RecentDisbursementDate",
        "marketing_touch_outcome",
        "MarketingTouchOutcome",
        # Pure identifiers (no predictive value as model features)
        "ClientID",
        "personal_cif",
        "enterprise_cif",
        "host_cif",
        "HostCif",
        "device_id",
        "DeviceID",
        "id_number",
        "IDNumber",
    }
)

_LEAKAGE_DENYLIST_LOWER: frozenset[str] = frozenset(name.lower() for name in LEAKAGE_DENYLIST)

# Columns dropped from modeling frames by default (subset of denylist + known proxies).
DEFAULT_DROP_FROM_MODELING: frozenset[str] = frozenset(
    {
        "ResponsePropensity",
        "response_propensity",
        "ClientID",
    }
)

# Behaviour columns used to detect cold-start (no activity in observation window).
COLD_START_ACTIVITY_COLUMNS: tuple[str, ...] = (
    "TotalTransactions",
    "NumOnlineTransactions",
    "NumMobileAppLogins",
    "BranchVisitFrequency",
)


def find_leakage_columns(columns: list[str] | set[str]) -> list[str]:
    """Return denylist columns present in ``columns`` (case-insensitive match)."""
    hits: list[str] = []
    for col in columns:
        if col.lower() in _LEAKAGE_DENYLIST_LOWER:
            hits.append(col)
    return sorted(set(hits))


def assert_no_leakage(columns: list[str] | set[str], *, context: str = "features") -> None:
    """Raise ValueError if any leakage denylist column remains."""
    hits = find_leakage_columns(columns)
    if hits:
        raise ValueError(
            f"Temporal/leakage audit failed for {context}: denylist columns present: {hits}"
        )


def drop_denylist_columns(columns: list[str]) -> list[str]:
    """Return columns with denylist names removed (case-insensitive)."""
    return [col for col in columns if col.lower() not in _LEAKAGE_DENYLIST_LOWER]
