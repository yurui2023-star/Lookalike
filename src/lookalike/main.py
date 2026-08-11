from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from lookalike import __version__
from lookalike.config import ID_COL, OUTPUT_DIR, TARGET_COL
from lookalike.data.sample_dataset import ensure_sample_dataset
from lookalike.domain.process_store import get_process_store
from lookalike.pipeline.service import get_pipeline, load_dataframe, prepare_modeling_frame
from lookalike.schemas import (
    AnalyzeResponse,
    CreateProcessRequest,
    DashboardSummary,
    EdaResponse,
    FeatureImportanceItem,
    FeatureIvRecord,
    GenerateRequest,
    HealthResponse,
    LookalikeScoreResponse,
    MetricsSummary,
    ProcessSummary,
    RemovedFeature,
    ScoredCustomer,
    TrainRequest,
    TrainResponse,
    VersionDashboard,
    VersionSummary,
)
from lookalike.workers.generate import run_generate_job

app = FastAPI(
    title="Lookalike Audience & Lead Generation API",
    description=(
        "Lookalike scoring platform (Design v2.1 MVP+P1-lite): Feature Adapter, "
        "leakage denylist, process versions, and async generate."
    ),
    version=__version__,
)


def _process_summary(record) -> ProcessSummary:
    return ProcessSummary(
        id=record.id,
        name=record.name,
        product=record.product,
        candidate_source=record.candidate_source,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        latest_version_id=record.latest_version_id,
        error_message=record.error_message,
        candidate_path=record.candidate_path,
        has_candidates=bool(record.candidate_path),
    )


def _version_summary(record) -> VersionSummary:
    return VersionSummary(
        id=record.id,
        process_id=record.process_id,
        status=record.status,
        progress=record.progress,
        triggered_by=record.triggered_by,
        created_at=record.created_at,
        completed_at=record.completed_at,
        error_message=record.error_message,
        total_candidates=record.total_candidates,
        valid_candidates=record.valid_candidates,
        scored_candidates=record.scored_candidates,
        cold_start_excluded=record.cold_start_excluded,
        similarity_threshold=record.similarity_threshold,
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
            "processes": "GET|POST /api/v1/processes",
            "generate": "POST /api/v1/processes/{id}/generate",
            "version_dashboard": "GET /api/v1/versions/{vid}/dashboard",
        },
    }


@app.post("/api/v1/eda", response_model=EdaResponse)
async def run_eda(
    file: UploadFile | None = File(default=None),
    use_sample_data: bool = Query(default=True),
) -> EdaResponse:
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


def _load_training_frame(
    data_path: str | None,
    product: str = "bank_marketing_term_deposit",
) -> pd.DataFrame:
    path = Path(data_path) if data_path else ensure_sample_dataset()
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Training data not found: {path}")
    raw_df = load_dataframe(path)
    return prepare_modeling_frame(raw_df, product=product)


@app.post("/api/v1/features/analyze", response_model=AnalyzeResponse)
async def analyze_features(
    request: TrainRequest | None = None,
    file: UploadFile | None = File(default=None),
) -> AnalyzeResponse:
    request = request or TrainRequest()
    if file is not None:
        frame = prepare_modeling_frame(load_dataframe(await file.read()), product=request.product)
    else:
        frame = _load_training_frame(request.data_path, product=request.product)

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
    request = request or TrainRequest()
    if file is not None:
        frame = prepare_modeling_frame(load_dataframe(await file.read()), product=request.product)
    else:
        frame = _load_training_frame(request.data_path, product=request.product)

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
        cold_start_excluded=result["cold_start_excluded"],
        histogram=result["histogram"],
    )


@app.get("/api/v1/dashboard", response_model=DashboardSummary)
def dashboard_summary() -> DashboardSummary:
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


# ----- Process lifecycle (Design v2.1 P1-lite) -----


@app.get("/api/v1/processes", response_model=list[ProcessSummary])
def list_processes() -> list[ProcessSummary]:
    return [_process_summary(p) for p in get_process_store().list_processes()]


@app.post("/api/v1/processes", response_model=ProcessSummary)
def create_process(request: CreateProcessRequest) -> ProcessSummary:
    record = get_process_store().create_process(name=request.name, product=request.product)
    return _process_summary(record)


@app.get("/api/v1/processes/{process_id}", response_model=ProcessSummary)
def get_process(process_id: str) -> ProcessSummary:
    record = get_process_store().get_process(process_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Process not found")
    return _process_summary(record)


@app.post("/api/v1/processes/{process_id}/candidates/upload", response_model=ProcessSummary)
async def upload_candidates(process_id: str, file: UploadFile = File(...)) -> ProcessSummary:
    store = get_process_store()
    if store.get_process(process_id) is None:
        raise HTTPException(status_code=404, detail="Process not found")
    frame = load_dataframe(await file.read())
    record = store.set_candidates(process_id, frame, source="file")
    return _process_summary(record)


@app.post("/api/v1/processes/{process_id}/generate", response_model=VersionSummary)
def generate_lookalike(
    process_id: str,
    background_tasks: BackgroundTasks,
    request: GenerateRequest | None = None,
) -> VersionSummary:
    """FR-06: async generate — scores 100% of attached candidates."""
    request = request or GenerateRequest()
    store = get_process_store()
    process = store.get_process(process_id)
    if process is None:
        raise HTTPException(status_code=404, detail="Process not found")
    if store.get_candidates(process_id) is None:
        raise HTTPException(status_code=400, detail="Upload candidates before generate")
    if not get_pipeline().is_trained:
        raise HTTPException(
            status_code=400,
            detail="Train model first via POST /api/v1/model/train",
        )

    version = store.create_version(process_id, triggered_by="manual")
    background_tasks.add_task(
        run_generate_job,
        process_id,
        version.id,
        similarity_threshold=request.similarity_threshold,
    )
    return _version_summary(version)


@app.get("/api/v1/processes/{process_id}/versions", response_model=list[VersionSummary])
def list_versions(process_id: str) -> list[VersionSummary]:
    if get_process_store().get_process(process_id) is None:
        raise HTTPException(status_code=404, detail="Process not found")
    return [_version_summary(v) for v in get_process_store().list_versions(process_id)]


@app.get("/api/v1/versions/{version_id}", response_model=VersionSummary)
def get_version(version_id: str) -> VersionSummary:
    version = get_process_store().get_version(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return _version_summary(version)


@app.get("/api/v1/versions/{version_id}/dashboard", response_model=VersionDashboard)
def version_dashboard(
    version_id: str,
    top_n: int = Query(default=20, ge=1, le=500),
) -> VersionDashboard:
    version = get_process_store().get_version(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    top_scores = [ScoredCustomer(**row) for row in version.scores[:top_n]]
    return VersionDashboard(
        version=_version_summary(version),
        histogram=version.histogram,
        snapshot=version.snapshot,
        top_scores=top_scores,
    )


def run() -> None:
    import uvicorn

    ensure_sample_dataset()
    uvicorn.run("lookalike.main:app", host="0.0.0.0", port=8000, reload=True)
