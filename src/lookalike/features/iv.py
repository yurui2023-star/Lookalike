from __future__ import annotations

import pandas as pd


def calculate_woe_iv(
    df: pd.DataFrame,
    feature: str,
    target: str,
    n_bins: int = 10,
    method: str = "qcut",
) -> tuple[pd.DataFrame | None, float]:
    """Numeric feature WOE/IV."""
    import numpy as np

    df_t = df[[feature, target]].dropna().copy()
    if df_t.empty:
        return None, 0.0
    try:
        if method == "qcut":
            df_t["bin"] = pd.qcut(df_t[feature], q=n_bins, duplicates="drop")
        else:
            df_t["bin"] = pd.cut(df_t[feature], bins=n_bins, duplicates="drop")
    except ValueError:
        df_t["bin"] = pd.cut(df_t[feature], bins=n_bins, duplicates="drop")

    bs = df_t.groupby("bin", observed=True)[target].agg(["count", "sum"])
    bs.columns = ["total", "positive"]
    bs["negative"] = bs["total"] - bs["positive"]
    total_positive, total_negative = df_t[target].sum(), len(df_t) - df_t[target].sum()
    if total_positive == 0 or total_negative == 0:
        return None, 0.0
    bs["positive_recall"] = bs["positive"] / total_positive
    bs["negative_recall"] = bs["negative"] / total_negative
    bs["woe"] = np.log((bs["positive_recall"] + 1e-10) / (bs["negative_recall"] + 1e-10))
    bs["iv_contrib"] = (bs["positive_recall"] - bs["negative_recall"]) * bs["woe"]
    return bs.reset_index(), float(bs["iv_contrib"].sum())


def calculate_iv_categorical(
    df: pd.DataFrame,
    feature: str,
    target: str,
) -> tuple[pd.DataFrame | None, float]:
    """Categorical feature IV."""
    import numpy as np

    df_t = df[[feature, target]].dropna().copy()
    if df_t.empty:
        return None, 0.0
    cs = df_t.groupby(feature, observed=True)[target].agg(["count", "sum"])
    cs.columns = ["total", "positive"]
    cs["negative"] = cs["total"] - cs["positive"]
    total_positive, total_negative = df_t[target].sum(), len(df_t) - df_t[target].sum()
    if total_positive == 0 or total_negative == 0:
        return None, 0.0
    cs["positive_recall"] = cs["positive"] / total_positive
    cs["negative_recall"] = cs["negative"] / total_negative
    cs["woe"] = np.log((cs["positive_recall"] + 1e-10) / (cs["negative_recall"] + 1e-10))
    cs["iv_contrib"] = (cs["positive_recall"] - cs["negative_recall"]) * cs["woe"]
    return cs.reset_index(), float(cs["iv_contrib"].sum())


def calculate_iv_table(df: pd.DataFrame, target: str) -> pd.DataFrame:
    """Calculate IV for all features and return a ranked table."""
    num_features = [
        c for c in df.select_dtypes(include=["int64", "float64"]).columns if c != target
    ]
    cat_features = [
        c for c in df.select_dtypes(include=["object", "category", "bool"]).columns if c != target
    ]
    iv_records: list[dict[str, object]] = []
    for col in num_features:
        try:
            _, iv = calculate_woe_iv(df, col, target, n_bins=10, method="qcut")
        except Exception:
            iv = 0.0
        iv_records.append({"feature": col, "feature_type": "numeric", "iv": iv})
    for col in cat_features:
        try:
            _, iv = calculate_iv_categorical(df, col, target)
        except Exception:
            iv = 0.0
        iv_records.append({"feature": col, "feature_type": "categorical", "iv": iv})
    return pd.DataFrame(iv_records).sort_values("iv", ascending=False).reset_index(drop=True)


def var_filter(
    df: pd.DataFrame,
    target: str,
    iv_limit: float = 0.02,
    missing_limit: float = 0.95,
    identical_limit: float = 0.95,
    var_rm: list[str] | None = None,
    var_kp: list[str] | None = None,
    n_bins: int = 10,
    method: str = "qcut",
    return_rm_reason: bool = False,
    verbose: bool = True,
) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    """Variable filtering (missing rate, identical rate, IV threshold)."""
    df_filtered = df.copy()
    var_rm = var_rm or []
    var_kp = var_kp or []
    all_features = [col for col in df.columns if col != target]
    rm_reasons: dict[str, str] = {}

    if verbose:
        print("=" * 70)
        print("Variable Filtering (var_filter)")
        print(f"Total features: {len(all_features)}")
        print(
            f"Filter params: iv_limit={iv_limit}, "
            f"missing_limit={missing_limit}, identical_limit={identical_limit}"
        )
        print("=" * 70)

    missing_rate = df[all_features].isnull().sum() / len(df)
    for var in missing_rate[missing_rate > missing_limit].index.tolist():
        if var not in var_kp:
            rm_reasons[var] = f"High missing rate ({missing_rate[var]:.2%} > {missing_limit})"

    for var in all_features:
        if var in rm_reasons or var in var_kp:
            continue
        value_counts = df[var].value_counts(normalize=True, dropna=False)
        max_ratio = value_counts.max() if len(value_counts) > 0 else 1.0
        if max_ratio > identical_limit:
            rm_reasons[var] = f"High identical rate ({max_ratio:.2%} > {identical_limit})"

    num_cols = df.select_dtypes(include=["int64", "float64"]).columns.tolist()
    cat_cols = df.select_dtypes(include=["object", "category", "bool"]).columns.tolist()
    if target in num_cols:
        num_cols.remove(target)
    if target in cat_cols:
        cat_cols.remove(target)

    iv_values: dict[str, float] = {}
    for col in num_cols:
        if col in var_kp or col in rm_reasons:
            continue
        try:
            _, iv = calculate_woe_iv(df, col, target, n_bins=n_bins, method=method)
            iv_values[col] = iv
        except Exception:
            iv_values[col] = 0.0

    for col in cat_cols:
        if col in var_kp or col in rm_reasons:
            continue
        try:
            _, iv = calculate_iv_categorical(df, col, target)
            iv_values[col] = iv
        except Exception:
            iv_values[col] = 0.0

    for var, iv in iv_values.items():
        if iv < iv_limit and var not in var_kp:
            rm_reasons[var] = f"Low IV ({iv:.4f} < {iv_limit})"

    for var in var_rm:
        if var == target:
            continue
        rm_reasons[var] = "Force removed (user-specified)"

    kept_features: list[str] = []
    for var in all_features:
        if var in var_kp:
            kept_features.append(var)
        elif var not in rm_reasons:
            kept_features.append(var)

    df_filtered = df_filtered[[target] + kept_features]
    if return_rm_reason:
        rm_info = pd.DataFrame(
            [{"feature": var, "reason": reason} for var, reason in rm_reasons.items()]
        )
        return df_filtered, rm_info
    return df_filtered
