from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def handle_missing_and_outliers(
    df: pd.DataFrame,
    num_fill_strategy: str | dict[str, float] = "median",
    num_outlier_method: str = "p99",
    skew_threshold: float = 5,
    cat_fill_strategy: str | dict[str, str] = "mode",
    constant_value: str = "Unknown",
    custom_num_fill: dict[str, float] | None = None,
    custom_cat_fill: dict[str, str] | None = None,
    verbose: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Missing value imputation and outlier treatment for numeric/categorical columns."""
    df_clean = df.copy()
    report: dict[str, Any] = {"numerical": {}, "categorical": {}}

    num_cols = df_clean.select_dtypes(include=["int64", "float64"]).columns.tolist()
    cat_cols = df_clean.select_dtypes(include=["object", "category", "bool"]).columns.tolist()

    if verbose:
        print("=" * 60)
        print("Starting missing value imputation & outlier treatment")
        print(f"Numeric cols ({len(num_cols)}): {num_cols}")
        print(f"Categorical cols ({len(cat_cols)}): {cat_cols}")
        print(f"Skewness threshold for P99 clipping: skew|> {skew_threshold}")
        print("=" * 60)

    if num_cols:
        if verbose:
            print("\n[Numeric Variable Processing]")

        if num_outlier_method == "p99":
            for col in num_cols:
                if df_clean[col].notna().sum() == 0:
                    report["numerical"][col] = {
                        "outliers_replaced": 0,
                        "skew": None,
                        "clipped": False,
                        "reason": "all NaN",
                    }
                    continue

                col_skew = df_clean[col].skew()
                if pd.isna(col_skew) or col_skew <= skew_threshold:
                    report["numerical"][col] = {
                        "outliers_replaced": 0,
                        "skew": float(col_skew) if pd.notna(col_skew) else None,
                        "clipped": False,
                        "reason": f"|skew| <= {skew_threshold}",
                    }
                    continue

                p99 = df_clean[col].quantile(0.99)
                outliers_mask = df_clean[col] > p99
                outlier_count = int(outliers_mask.sum())
                if outlier_count > 0:
                    df_clean.loc[outliers_mask, col] = p99
                report["numerical"][col] = {
                    "outliers_replaced": outlier_count,
                    "p99_value": float(p99),
                    "skew": float(col_skew),
                    "clipped": True,
                }
        elif verbose:
            print("  No outlier treatment applied (num_outlier_method='none')")

        num_fill_map: dict[str, Any] = {}
        if isinstance(num_fill_strategy, dict):
            num_fill_map.update(num_fill_strategy)
        else:
            for col in num_cols:
                if custom_num_fill and col in custom_num_fill:
                    fill_val = custom_num_fill[col]
                elif num_fill_strategy == "median":
                    fill_val = df_clean[col].median()
                elif num_fill_strategy == "mean":
                    fill_val = df_clean[col].mean()
                elif num_fill_strategy == "mode":
                    mode_vals = df_clean[col].mode()
                    fill_val = mode_vals[0] if len(mode_vals) > 0 else np.nan
                else:
                    raise ValueError(f"Unsupported num_fill_strategy: {num_fill_strategy}")
                num_fill_map[col] = fill_val

        for col in num_cols:
            if col not in num_fill_map:
                continue
            fill_val = num_fill_map[col]
            missing_mask = df_clean[col].isnull()
            missing_count = int(missing_mask.sum())
            if missing_count > 0:
                df_clean.loc[missing_mask, col] = fill_val
                report["numerical"].setdefault(col, {})
                report["numerical"][col]["missing_filled"] = missing_count
                report["numerical"][col]["fill_value"] = fill_val
            else:
                report["numerical"].setdefault(col, {})
                report["numerical"][col]["missing_filled"] = 0

    if cat_cols:
        if verbose:
            print("\n[Categorical Variable Processing]")

        cat_fill_map: dict[str, Any] = {}
        if isinstance(cat_fill_strategy, dict):
            cat_fill_map.update(cat_fill_strategy)
        else:
            for col in cat_cols:
                if custom_cat_fill and col in custom_cat_fill:
                    fill_val = custom_cat_fill[col]
                elif cat_fill_strategy == "mode":
                    mode_vals = df_clean[col].mode()
                    fill_val = mode_vals[0] if len(mode_vals) > 0 else constant_value
                elif cat_fill_strategy == "constant":
                    fill_val = constant_value
                else:
                    raise ValueError(f"Unsupported cat_fill_strategy: {cat_fill_strategy}")
                cat_fill_map[col] = fill_val

        for col in cat_cols:
            if col not in cat_fill_map:
                continue
            fill_val = cat_fill_map[col]
            missing_mask = df_clean[col].isnull()
            missing_count = int(missing_mask.sum())
            if missing_count > 0:
                df_clean.loc[missing_mask, col] = fill_val
                report["categorical"][col] = {
                    "missing_filled": missing_count,
                    "fill_value": fill_val,
                }
            else:
                report["categorical"][col] = {"missing_filled": 0}

    if verbose:
        print("\nProcessing completed!")
        print("=" * 60)

    return df_clean, report
