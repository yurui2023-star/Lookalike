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


class DashboardSummary(BaseModel):
    model_trained: bool
    train_metrics: MetricsSummary | None = None
    feature_importance: list[FeatureImportanceItem] = Field(default_factory=list)
    kept_features: list[str] = Field(default_factory=list)
