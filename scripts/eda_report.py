#!/usr/bin/env python3
"""CLI entrypoint mirroring uploaded eda_report.py."""


from lookalike.config import DEFAULT_DATA_FILE, ID_COL, OUTPUT_DIR, TARGET_COL
from lookalike.data.sample_dataset import ensure_sample_dataset
from lookalike.eda.report import eda_report
from lookalike.pipeline.service import load_dataframe


def main() -> None:
    data_file = ensure_sample_dataset(DEFAULT_DATA_FILE)
    output_file = OUTPUT_DIR / "eda_report.xlsx"

    print(f"Loading data: {data_file}")
    bank_df = load_dataframe(data_file)
    if ID_COL in bank_df.columns:
        bank_df = bank_df.drop(columns=[ID_COL])

    eda_report(bank_df, target_col=TARGET_COL, output_excel=output_file)


if __name__ == "__main__":
    main()
