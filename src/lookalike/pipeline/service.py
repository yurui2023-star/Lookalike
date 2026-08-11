from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from lookalike.adapters.bank_marketing import BankMarketingCsvAdapter, get_adapter
from lookalike.config import (
    DEFAULT_IDENTICAL_LIMIT,
    DEFAULT_IV_LIMIT,
    DEFAULT_MISSING_LIMIT,
    ID_COL,
    LABEL_COL,
    TARGET_COL,
)
from lookalike.eda.report import eda_report
from lookalike.features.iv import calculate_iv_table, var_filter
from lookalike.modeling.lightgbm_model import (
    build_model,
    feature_importance,
    predict_similarity_scores,
)
from lookalike.preprocessing.missing_outliers import handle_missing_and_outliers


def load_dataframe(source: str | Path | BytesIO | bytes) -> pd.DataFrame:
    """Load CSV from path, bytes, or uploaded file buffer."""
    if isinstance(source, bytes):
        return pd.read_csv(BytesIO(source))
    if isinstance(source, BytesIO):
        return pd.read_csv(source)
    return pd.read_csv(source)


def prepare_modeling_frame(
    df: pd.DataFrame,
    *,
    drop_id: bool = True,
    drop_cols: list[str] | None = None,
    rename_target: bool = True,
    product: str = "bank_marketing_term_deposit",
    for_scoring: bool = False,
) -> pd.DataFrame:
    """Prepare modeling frame via Feature Adapter (Design v2.1)."""
    adapter = get_adapter(product)
    # drop_cols retained for backward compatibility but adapter owns denylist.
    _ = drop_cols
    return adapter.to_model_frame(
        df,
        drop_id=drop_id,
        rename_target=rename_target,
        for_scoring=for_scoring,
    )


def compute_score_histogram(scores: np.ndarray, bins: int = 100) -> dict[str, Any]:
    """Pre-compute 0.01-resolution histogram for threshold slider (no re-score)."""
    counts, edges = np.histogram(scores, bins=bins, range=(0.0, 1.0))
    return {
        "bins": edges.round(4).tolist(),
        "counts": counts.astype(int).tolist(),
        "resolution": round(1.0 / bins, 4),
    }


class LookalikePipeline:
    """End-to-end Lookalike pipeline aligned with BRD FR-05/FR-06."""

    def __init__(self, product: str = "bank_marketing_term_deposit") -> None:
        self.product = product
        self.adapter: BankMarketingCsvAdapter = get_adapter(product)
        self.model = None
        self.target_col = LABEL_COL
        self.clean_report: dict[str, Any] | None = None
        self.iv_table: pd.DataFrame | None = None
        self.removal_info: pd.DataFrame | None = None
        self.filtered_columns: list[str] = []
        self.train_metrics: dict[str, Any] | None = None
        self.feature_importance_rows: list[dict[str, float | str]] = []

    @property
    def is_trained(self) -> bool:
        return self.model is not None

    def clean_data(self, df: pd.DataFrame, verbose: bool = False) -> pd.DataFrame:
        cleaned, report = handle_missing_and_outliers(
            df,
            num_fill_strategy="median",
            num_outlier_method="p99",
            cat_fill_strategy="mode",
            verbose=verbose,
        )
        self.clean_report = report
        return cleaned

    def analyze_features(
        self,
        df: pd.DataFrame,
        *,
        iv_limit: float = DEFAULT_IV_LIMIT,
        missing_limit: float = DEFAULT_MISSING_LIMIT,
        identical_limit: float = DEFAULT_IDENTICAL_LIMIT,
        verbose: bool = False,
    ) -> dict[str, Any]:
        if self.target_col not in df.columns:
            raise ValueError(f"Target column '{self.target_col}' not found")

        cleaned = self.clean_data(df, verbose=verbose)
        self.iv_table = calculate_iv_table(cleaned, self.target_col)
        filtered, rm_info = var_filter(
            cleaned,
            target=self.target_col,
            iv_limit=iv_limit,
            missing_limit=missing_limit,
            identical_limit=identical_limit,
            return_rm_reason=True,
            verbose=verbose,
        )
        self.removal_info = rm_info
        self.filtered_columns = [c for c in filtered.columns if c != self.target_col]
        return {
            "iv_ranking": self.iv_table.to_dict(orient="records"),
            "kept_features": self.filtered_columns,
            "removed_features": rm_info.to_dict(orient="records") if len(rm_info) else [],
            "shape_after_filter": list(filtered.shape),
        }

    def train(
        self,
        df: pd.DataFrame,
        *,
        iv_limit: float = DEFAULT_IV_LIMIT,
        missing_limit: float = DEFAULT_MISSING_LIMIT,
        identical_limit: float = DEFAULT_IDENTICAL_LIMIT,
        test_size: float = 0.2,
        random_state: int = 42,
        is_unbalance: bool = False,
        verbose: bool = False,
    ) -> dict[str, Any]:
        analysis = self.analyze_features(
            df,
            iv_limit=iv_limit,
            missing_limit=missing_limit,
            identical_limit=identical_limit,
            verbose=verbose,
        )
        cleaned = self.clean_data(df, verbose=False)
        filtered, _ = var_filter(
            cleaned,
            target=self.target_col,
            iv_limit=iv_limit,
            missing_limit=missing_limit,
            identical_limit=identical_limit,
            return_rm_reason=True,
            verbose=False,
        )
        self.model, self.train_metrics = build_model(
            filtered,
            target_col=self.target_col,
            test_size=test_size,
            random_state=random_state,
            is_unbalance=is_unbalance,
            verbose=verbose,
        )
        self.feature_importance_rows = feature_importance(self.model)
        return {
            **analysis,
            "metrics": {
                "auc": self.train_metrics["auc"],
                "average_precision": self.train_metrics["average_precision"],
                "train_size": self.train_metrics["train_size"],
                "test_size": self.train_metrics["test_size"],
            },
            "feature_importance": self.feature_importance_rows,
        }

    def score_candidates(
        self,
        candidates: pd.DataFrame,
        *,
        similarity_threshold: float | None = None,
        id_col: str = ID_COL,
        exclude_cold_start: bool = True,
    ) -> dict[str, Any]:
        if not self.is_trained:
            raise RuntimeError("Model is not trained. Call train() first.")

        candidate_frame = candidates.copy()
        cold_mask = self.adapter.cold_start_mask(candidate_frame)
        cold_start_excluded = int(cold_mask.sum()) if exclude_cold_start else 0
        if exclude_cold_start and cold_mask.any():
            candidate_frame = candidate_frame.loc[~cold_mask].reset_index(drop=True)

        ids = self.adapter.extract_ids(candidate_frame)
        feature_frame = self.adapter.to_model_frame(
            candidate_frame, drop_id=True, rename_target=False, for_scoring=True
        )
        for col in self.filtered_columns:
            if col not in feature_frame.columns:
                feature_frame[col] = 0
        feature_frame = feature_frame[self.filtered_columns]
        feature_frame = self.clean_data(feature_frame, verbose=False)

        scores = predict_similarity_scores(self.model, feature_frame)
        results = pd.DataFrame(
            {
                "client_id": ids,
                "similarity_score": scores.round(4).values,
            }
        ).sort_values("similarity_score", ascending=False)
        results["rank"] = range(1, len(results) + 1)

        above_threshold = results
        if similarity_threshold is not None:
            above_threshold = results[results["similarity_score"] >= similarity_threshold]

        histogram = compute_score_histogram(results["similarity_score"].to_numpy())
        return {
            "total_scored": len(results),
            "valid_candidates": len(results),
            "cold_start_excluded": cold_start_excluded,
            "similarity_threshold": similarity_threshold,
            "count_above_threshold": len(above_threshold),
            "scores": results.to_dict(orient="records"),
            "matches": above_threshold.to_dict(orient="records"),
            "histogram": histogram,
        }

    def run_eda(
        self,
        df: pd.DataFrame,
        output_path: Path,
        target_col: str = TARGET_COL,
    ) -> dict[str, object]:
        frame = df.copy()
        if ID_COL in frame.columns:
            frame = frame.drop(columns=[ID_COL])
        return eda_report(frame, target_col=target_col, output_excel=output_path)


_pipeline = LookalikePipeline()


def get_pipeline() -> LookalikePipeline:
    return _pipeline
