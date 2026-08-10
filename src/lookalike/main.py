from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from lookalike import __version__
from lookalike.config import ID_COL, OUTPUT_DIR, TARGET_COL
from lookalike.data.sample_dataset import ensure_sample_dataset
from lookalike.pipeline.service import get_pipeline, load_dataframe, prepare_modeling_frame
from lookalike.schemas import (
    AnalyzeResponse,
    DashboardSummary,
    EdaResponse,
    FeatureImportanceItem,
    FeatureIvRecord,
    HealthResponse,
    LookalikeScoreResponse,
    MetricsSummary,
    RemovedFeature,
    ScoredCustomer,
    TrainRequest,
    TrainResponse,
)

app = FastAPI(
    title="Lookalike Audience & Lead Generation API",
    description=(
        "Lookalike scoring platform aligned with BRD: EDA, feature importance (IV), "
        "LightGBM training, and similarity scoring for 100% of candidate records."
    ),
    version=__version__,
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="lookalike-model", version=__version__)


@app.get("/")
def root() -> dict[str, object]:
    return {
        "message": "Lookalike Audience & Lead Generation API",
        "docs": "/docs",
        "endpoints": {
            "eda": "POST /api/v1/eda",
            "analyze_features": "POST /api/v1/features/analyze",
            "train_model": "POST /api/v1/model/train",
            "score_candidates": "POST /api/v1/lookalike/score",
            "dashboard": "GET /api/v1/dashboard",
        },
    }


@app.post("/api/v1/eda", response_model=EdaResponse)
async def run_eda(
    file: UploadFile | None = File(default=None),
    use_sample_data: bool = Query(default=True),
) -> EdaResponse:
    """FR-05 support: generate Excel EDA report (Overview/Missing/Numeric/Categorical/Target)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "eda_report.xlsx"

    if file is not None:
        raw_df = load_dataframe(await file.read())
    elif use_sample_data:
        raw_df = load_dataframe(ensure_sample_dataset())
    else:
        raise HTTPException(status_code=400, detail="Upload a CSV file or set use_sample_data=true")

    pipeline = get_pipeline()
    summary = pipeline.run_eda(raw_df, output_path, target_col=TARGET_COL)
    return EdaResponse(**summary)


@app.get("/api/v1/eda/download")
def download_eda_report() -> FileResponse:
    report_path = OUTPUT_DIR / "eda_report.xlsx"
    if not report_path.exists():
        raise HTTPException(
            status_code=404,
            detail="EDA report not found. Run POST /api/v1/eda first.",
        )
    return FileResponse(
        path=report_path,
        filename="eda_report.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _load_training_frame(data_path: str | None) -> pd.DataFrame:
    path = Path(data_path) if data_path else ensure_sample_dataset()
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Training data not found: {path}")
    raw_df = load_dataframe(path)
    return prepare_modeling_frame(raw_df)


@app.post("/api/v1/features/analyze", response_model=AnalyzeResponse)
async def analyze_features(
    request: TrainRequest | None = None,
    file: UploadFile | None = File(default=None),
) -> AnalyzeResponse:
    """FR-05: IV ranking and variable filtering for sample/seed data."""
    request = request or TrainRequest()
    if file is not None:
        frame = prepare_modeling_frame(load_dataframe(await file.read()))
    else:
        frame = _load_training_frame(request.data_path)

    pipeline = get_pipeline()
    result = pipeline.analyze_features(
        frame,
        iv_limit=request.iv_limit,
        missing_limit=request.missing_limit,
        identical_limit=request.identical_limit,
    )
    return AnalyzeResponse(
        kept_features=result["kept_features"],
        removed_features=[RemovedFeature(**row) for row in result["removed_features"]],
        iv_ranking=[FeatureIvRecord(**row) for row in result["iv_ranking"]],
        shape_after_filter=result["shape_after_filter"],
    )


@app.post("/api/v1/model/train", response_model=TrainResponse)
async def train_model(
    request: TrainRequest | None = None,
    file: UploadFile | None = File(default=None),
) -> TrainResponse:
    """Train LightGBM on sample data after cleaning, IV filtering (FR-05/FR-06)."""
    request = request or TrainRequest()
    if file is not None:
        frame = prepare_modeling_frame(load_dataframe(await file.read()))
    else:
        frame = _load_training_frame(request.data_path)

    pipeline = get_pipeline()
    result = pipeline.train(
        frame,
        iv_limit=request.iv_limit,
        missing_limit=request.missing_limit,
        identical_limit=request.identical_limit,
        test_size=request.test_size,
        random_state=request.random_state,
        is_unbalance=request.is_unbalance,
    )
    return TrainResponse(
        kept_features=result["kept_features"],
        removed_features=[RemovedFeature(**row) for row in result["removed_features"]],
        iv_ranking=[FeatureIvRecord(**row) for row in result["iv_ranking"]],
        metrics=MetricsSummary(**result["metrics"]),
        feature_importance=[FeatureImportanceItem(**row) for row in result["feature_importance"]],
        shape_after_filter=result["shape_after_filter"],
    )


@app.post("/api/v1/lookalike/score", response_model=LookalikeScoreResponse)
async def score_lookalike_candidates(
    file: UploadFile | None = File(default=None),
    use_sample_data: bool = Query(default=True),
    similarity_threshold: float | None = Query(
        default=None,
        ge=0,
        le=1,
        description="Optional post-scoring filter (BRD dashboard threshold).",
    ),
) -> LookalikeScoreResponse:
    """FR-06: score 100% of candidate records; threshold filters results only."""
    pipeline = get_pipeline()
    if not pipeline.is_trained:
        frame = _load_training_frame(None)
        pipeline.train(frame)

    if file is not None:
        candidates = load_dataframe(await file.read())
    elif use_sample_data:
        sample_path = ensure_sample_dataset()
        candidates = load_dataframe(sample_path).head(200)
    else:
        raise HTTPException(
            status_code=400,
            detail="Upload candidate CSV or set use_sample_data=true",
        )

    result = pipeline.score_candidates(
        candidates,
        similarity_threshold=similarity_threshold,
        id_col=ID_COL,
    )
    return LookalikeScoreResponse(
        total_scored=result["total_scored"],
        similarity_threshold=result["similarity_threshold"],
        count_above_threshold=result["count_above_threshold"],
        scores=[ScoredCustomer(**row) for row in result["scores"]],
        matches=[ScoredCustomer(**row) for row in result["matches"]],
    )


@app.get("/api/v1/dashboard", response_model=DashboardSummary)
def dashboard_summary() -> DashboardSummary:
    """FR-07: model quality metrics and feature importance snapshot."""
    pipeline = get_pipeline()
    metrics = None
    if pipeline.train_metrics:
        metrics = MetricsSummary(
            auc=pipeline.train_metrics["auc"],
            average_precision=pipeline.train_metrics["average_precision"],
            train_size=pipeline.train_metrics["train_size"],
            test_size=pipeline.train_metrics["test_size"],
        )
    return DashboardSummary(
        model_trained=pipeline.is_trained,
        train_metrics=metrics,
        feature_importance=[
            FeatureImportanceItem(**row) for row in pipeline.feature_importance_rows
        ],
        kept_features=pipeline.filtered_columns,
    )


def run() -> None:
    import uvicorn

    ensure_sample_dataset()
    uvicorn.run("lookalike.main:app", host="0.0.0.0", port=8000, reload=True)
