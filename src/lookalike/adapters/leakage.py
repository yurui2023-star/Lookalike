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
        # Post-event / disbursement-only
        "current_loan_balance",
        "recent_disbursement_date",
        "marketing_touch_outcome",
        # Pure identifiers (no predictive value as model features)
        "ClientID",
        "personal_cif",
        "enterprise_cif",
        "host_cif",
        "device_id",
        "id_number",
    }
)

# Columns dropped from modeling frames by default (subset of denylist + known proxies).
DEFAULT_DROP_FROM_MODELING: frozenset[str] = frozenset(
    {
        "ResponsePropensity",
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
    """Return denylist columns present in ``columns`` (case-sensitive)."""
    present = set(columns)
    return sorted(col for col in LEAKAGE_DENYLIST if col in present)


def assert_no_leakage(columns: list[str] | set[str], *, context: str = "features") -> None:
    """Raise ValueError if any leakage denylist column remains."""
    hits = find_leakage_columns(columns)
    if hits:
        raise ValueError(
            f"Temporal/leakage audit failed for {context}: denylist columns present: {hits}"
        )
