"""Async generate job for process versions (FastAPI BackgroundTasks / P1-lite)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lookalike.domain.process_store import get_process_store
from lookalike.pipeline.service import get_pipeline


def run_generate_job(
    process_id: str,
    version_id: str,
    *,
    similarity_threshold: float | None = None,
) -> None:
    """Score candidates for a process version; updates store on success/failure."""
    store = get_process_store()
    pipeline = get_pipeline()
    try:
        store.update_version(version_id, status="running", progress=0.05)
        candidates = store.get_candidates(process_id)
        if candidates is None or len(candidates) == 0:
            raise RuntimeError("No candidates attached to process")

        if not pipeline.is_trained:
            raise RuntimeError("Model is not trained. Call POST /api/v1/model/train first.")

        store.update_version(version_id, progress=0.2, total_candidates=len(candidates))
        result = pipeline.score_candidates(
            candidates,
            similarity_threshold=similarity_threshold,
            exclude_cold_start=True,
        )
        store.update_version(version_id, progress=0.85)

        score_rows = result["scores"]
        avg_score = float(
            sum(row["similarity_score"] for row in score_rows) / max(len(score_rows), 1)
        )
        snapshot: dict[str, Any] = {
            "avg_score": avg_score,
            "feature_importance": pipeline.feature_importance_rows[:15],
            "kept_features": pipeline.filtered_columns,
            "train_metrics": pipeline.train_metrics,
        }
        store.update_version(
            version_id,
            status="completed",
            progress=1.0,
            scored_candidates=result["total_scored"],
            valid_candidates=result["valid_candidates"],
            cold_start_excluded=result["cold_start_excluded"],
            similarity_threshold=similarity_threshold,
            scores=score_rows,
            histogram=result["histogram"],
            snapshot=snapshot,
            completed_at=datetime.now(UTC),
        )
    except Exception as exc:  # noqa: BLE001 — persist failure onto version
        store.update_version(
            version_id,
            status="failed",
            progress=1.0,
            error_message=str(exc),
        )
