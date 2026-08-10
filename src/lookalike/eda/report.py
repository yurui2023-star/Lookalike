from __future__ import annotations

from pathlib import Path

import pandas as pd


def eda_report(
    df: pd.DataFrame,
    target_col: str | None = None,
    output_excel: str | Path = "eda_report.xlsx",
) -> dict[str, object]:
    """
    Exploratory Data Analysis report written to Excel (multiple sheets), no graphics.

    Sheets: Overview, Missing, Numeric, Categorical, Target_Cross (optional).
    Returns summary dict for API responses.
    """
    output_excel = Path(output_excel)
    print("=" * 80)
    print("Exploratory Data Analysis (EDA) -> Excel")
    print("=" * 80)
    print(f"Dataset shape: {df.shape[0]} rows x {df.shape[1]} cols")
    print(f"Output file : {output_excel}")
    print("=" * 80)

    num_cols = df.select_dtypes(include=["int64", "float64"]).columns.tolist()
    cat_cols = df.select_dtypes(include=["object", "category", "bool"]).columns.tolist()

    overview_rows = [
        ("Rows", df.shape[0]),
        ("Columns", df.shape[1]),
        ("Numeric variables", len(num_cols)),
        ("Categorical variables", len(cat_cols)),
        ("Total missing cells", int(df.isnull().sum().sum())),
        ("Total cells", int(df.shape[0] * df.shape[1])),
        (
            "Overall missing rate (%)",
            round(df.isnull().sum().sum() / (df.shape[0] * df.shape[1]) * 100, 4),
        ),
    ]
    if target_col and target_col in df.columns:
        overview_rows.append(("Target variable", target_col))
        vc = df[target_col].value_counts(dropna=False)
        for key, value in vc.items():
            overview_rows.append((f"  target={key} count", int(value)))
            overview_rows.append((f"  target={key} rate (%)", round(value / len(df) * 100, 4)))
    overview_df = pd.DataFrame(overview_rows, columns=["Metric", "Value"])

    missing_cnt = df.isnull().sum()
    missing_pct = (missing_cnt / len(df)) * 100
    missing_df = pd.DataFrame(
        {
            "column": missing_cnt.index,
            "dtype": [str(df[c].dtype) for c in missing_cnt.index],
            "missing_count": missing_cnt.values,
            "missing_rate (%)": missing_pct.round(4).values,
            "non_null_count": (len(df) - missing_cnt).values,
        }
    ).sort_values("missing_count", ascending=False).reset_index(drop=True)

    if num_cols:
        percentiles = [1, 5, 10, 25, 50, 75, 90, 95, 99]
        rows = []
        for col in num_cols:
            series = df[col]
            row: dict[str, object] = {
                "column": col,
                "missing_count": int(series.isnull().sum()),
                "missing_rate (%)": round(series.isnull().sum() / len(df) * 100, 4),
                "non_null_count": int(series.notnull().sum()),
                "unique_count": int(series.nunique(dropna=True)),
                "min": series.min(),
                "max": series.max(),
                "mean": series.mean(),
                "std": series.std(),
                "skewness": series.skew(),
                "kurtosis": series.kurt(),
            }
            for percentile in percentiles:
                row[f"P{percentile}"] = series.quantile(percentile / 100)
            rows.append(row)
        numeric_df = pd.DataFrame(rows)
    else:
        numeric_df = pd.DataFrame({"note": ["No numeric variables"]})

    if cat_cols:
        cat_rows = []
        for col in cat_cols:
            vc = df[col].value_counts(dropna=False)
            pct = df[col].value_counts(normalize=True, dropna=False) * 100
            for cat_value, count in vc.items():
                cat_rows.append(
                    {
                        "column": col,
                        "category": "Missing(NaN)" if pd.isna(cat_value) else str(cat_value),
                        "frequency": int(count),
                        "percentage (%)": round(float(pct.loc[cat_value]), 4),
                    }
                )
        categorical_df = pd.DataFrame(cat_rows)
    else:
        categorical_df = pd.DataFrame({"note": ["No categorical variables"]})

    target_cross_df = None
    if target_col and target_col in df.columns and cat_cols:
        cross_rows = []
        target_vals = sorted(df[target_col].dropna().unique().tolist())
        for col in cat_cols:
            if col == target_col:
                continue
            grp = df.groupby(col, dropna=False)[target_col]
            total = grp.count()
            for cat_value, sample_count in total.items():
                row: dict[str, object] = {
                    "categorical_col": col,
                    "category": "Missing(NaN)" if pd.isna(cat_value) else str(cat_value),
                    "total_samples": int(sample_count),
                }
                sub = df[
                    df[col].fillna("__NaN__") == ("__NaN__" if pd.isna(cat_value) else cat_value)
                ]
                for target_value in target_vals:
                    count = int((sub[target_col] == target_value).sum())
                    row[f"target={target_value}_count"] = count
                    row[f"target={target_value}_rate (%)"] = (
                        round(count / sample_count * 100, 4) if sample_count > 0 else 0.0
                    )
                cross_rows.append(row)
        target_cross_df = pd.DataFrame(cross_rows)

    output_excel.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_excel, engine="openpyxl") as writer:
        overview_df.to_excel(writer, sheet_name="1.Overview", index=False)
        missing_df.to_excel(writer, sheet_name="2.Missing", index=False)
        numeric_df.to_excel(writer, sheet_name="3.Numeric", index=False)
        categorical_df.to_excel(writer, sheet_name="4.Categorical", index=False)
        if target_cross_df is not None and len(target_cross_df) > 0:
            target_cross_df.to_excel(writer, sheet_name="5.Target_Cross", index=False)

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for column_cells in ws.columns:
                length = max(
                    (len(str(cell.value)) if cell.value is not None else 0) for cell in column_cells
                )
                ws.column_dimensions[column_cells[0].column_letter].width = min(
                    max(length + 2, 10), 40
                )

    summary: dict[str, object] = {
        "rows": df.shape[0],
        "columns": df.shape[1],
        "numeric_variables": len(num_cols),
        "categorical_variables": len(cat_cols),
        "output_excel": str(output_excel.resolve()),
        "sheets": {
            "overview_rows": len(overview_df),
            "missing_rows": len(missing_df),
            "numeric_rows": len(numeric_df),
            "categorical_rows": len(categorical_df),
        },
    }
    if target_cross_df is not None:
        summary["target_cross_rows"] = len(target_cross_df)

    print("\n" + "=" * 80)
    print(f"EDA report saved to: {output_excel.resolve()}")
    print("=" * 80)
    return summary
