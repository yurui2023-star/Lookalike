#!/usr/bin/env python3
"""CLI entrypoint mirroring uploaded full_process.py."""


from lookalike.config import DEFAULT_DATA_FILE, LABEL_COL, OUTPUT_DIR
from lookalike.data.sample_dataset import ensure_sample_dataset
from lookalike.features.iv import calculate_iv_table, var_filter
from lookalike.modeling.lightgbm_model import build_model, feature_importance
from lookalike.pipeline.service import load_dataframe, prepare_modeling_frame
from lookalike.preprocessing.missing_outliers import handle_missing_and_outliers


def main() -> None:
    data_file = ensure_sample_dataset(DEFAULT_DATA_FILE)
    print("=" * 70)
    print("Loading Bank Marketing Dataset")
    print("=" * 70)
    print(f"Data file: {data_file}")
    raw_df = load_dataframe(data_file)
    print(f"Raw data shape: {raw_df.shape}")

    raw_df = prepare_modeling_frame(raw_df)
    target = LABEL_COL

    print("=" * 70)
    print("Step 1: Missing value imputation & outlier treatment")
    print("=" * 70)
    df_cleaned, _ = handle_missing_and_outliers(raw_df, verbose=True)

    print("\n" + "=" * 70)
    print("Step 2: Calculate IV values for all variables")
    print("=" * 70)
    iv_df = calculate_iv_table(df_cleaned, target)
    print(iv_df.to_string(index=False))

    print("\n" + "=" * 70)
    print("Step 3: Variable filtering")
    print("=" * 70)
    df_filtered, rm_info = var_filter(
        df_cleaned,
        target=target,
        iv_limit=0.02,
        missing_limit=0.95,
        identical_limit=0.95,
        return_rm_reason=True,
        verbose=True,
    )
    print("\nFiltered data shape:", df_filtered.shape)
    if rm_info is not None and len(rm_info) > 0:
        print(rm_info.to_string(index=False))

    print("\n" + "=" * 70)
    print("Step 4: Modeling & evaluation (LightGBM)")
    print("=" * 70)
    model, metrics = build_model(
        df_filtered,
        target_col=target,
        test_size=0.2,
        random_state=42,
        is_unbalance=False,
        verbose=True,
    )
    print(f"AUC: {metrics['auc']:.4f}")
    print(f"AP: {metrics['average_precision']:.4f}")
    print("Top features:")
    for row in feature_importance(model, top_n=10):
        print(f"  {row['feature']}: {row['importance']:.2f}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nFull pipeline completed. Sample data: {data_file}")


if __name__ == "__main__":
    main()
