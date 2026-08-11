from datetime import datetime

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class TrainRequest(BaseModel):
    data_path: str | None = Field(
        default=None,
        description="Optional CSV path; defaults to bundled Bank Marketing sample dataset.",
    )
    iv_limit: float = Field(default=0.02, ge=0)
    missing_limit: float = Field(default=0.95, ge=0, le=1)
    identical_limit: float = Field(default=0.95, ge=0, le=1)
    test_size: float = Field(default=0.2, gt=0, lt=1)
    random_state: int = 42
    is_unbalance: bool = False
    product: str = "bank_marketing_term_deposit"


class MetricsSummary(BaseModel):
    auc: float
    average_precision: float
    train_size: int
    test_size: int


class FeatureIvRecord(BaseModel):
    feature: str
    feature_type: str
    iv: float


class RemovedFeature(BaseModel):
    feature: str
    reason: str


class FeatureImportanceItem(BaseModel):
    feature: str
    importance: float


class TrainResponse(BaseModel):
    kept_features: list[str]
    removed_features: list[RemovedFeature]
    iv_ranking: list[FeatureIvRecord]
    metrics: MetricsSummary
    feature_importance: list[FeatureImportanceItem]
    shape_after_filter: list[int]


class AnalyzeResponse(BaseModel):
    kept_features: list[str]
    removed_features: list[RemovedFeature]
    iv_ranking: list[FeatureIvRecord]
    shape_after_filter: list[int]


class EdaResponse(BaseModel):
    rows: int
    columns: int
    numeric_variables: int
    categorical_variables: int
    output_excel: str
    sheets: dict[str, int]


class ScoredCustomer(BaseModel):
    client_id: str
    similarity_score: float = Field(..., ge=0, le=1)
    rank: int = Field(..., ge=1)


class LookalikeScoreResponse(BaseModel):
    total_scored: int
    similarity_threshold: float | None
    count_above_threshold: int
    scores: list[ScoredCustomer]
    matches: list[ScoredCustomer]
    cold_start_excluded: int = 0
    histogram: dict | None = None


class DashboardSummary(BaseModel):
    model_trained: bool
    train_metrics: MetricsSummary | None = None
    feature_importance: list[FeatureImportanceItem] = Field(default_factory=list)
    kept_features: list[str] = Field(default_factory=list)


class CreateProcessRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    product: str = "bank_marketing_term_deposit"


class ProcessSummary(BaseModel):
    id: str
    name: str
    product: str
    candidate_source: str
    status: str
    created_at: datetime
    updated_at: datetime
    latest_version_id: str | None = None
    error_message: str | None = None
    candidate_path: str | None = None
    has_candidates: bool = False


class GenerateRequest(BaseModel):
    # Threshold is NOT a create-process input; optional here only for version snapshot filter.
    similarity_threshold: float | None = Field(default=None, ge=0, le=1)


class VersionSummary(BaseModel):
    id: str
    process_id: str
    status: str
    progress: float
    triggered_by: str
    created_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None
    total_candidates: int = 0
    valid_candidates: int = 0
    scored_candidates: int = 0
    cold_start_excluded: int = 0
    similarity_threshold: float | None = None


class VersionDashboard(BaseModel):
    version: VersionSummary
    histogram: dict | None = None
    snapshot: dict | None = None
    top_scores: list[ScoredCustomer] = Field(default_factory=list)
