import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lookalike.config import ID_COL, LABEL_COL, TARGET_COL
from lookalike.data.sample_dataset import generate_bank_marketing_dataset
from lookalike.main import app
from lookalike.pipeline.service import LookalikePipeline, prepare_modeling_frame

client = TestClient(app)


@pytest.fixture
def sample_frame() -> pd.DataFrame:
    return prepare_modeling_frame(generate_bank_marketing_dataset(n_rows=400, random_state=1))


@pytest.fixture
def synthetic_csv_file(sample_frame: pd.DataFrame, tmp_path) -> str:
    """Small synthetic CSV for fast API tests (does not use the 100k production file)."""
    csv_path = tmp_path / "synthetic_sample.csv"
    raw = sample_frame.rename(columns={LABEL_COL: TARGET_COL})
    raw.insert(0, ID_COL, [f"CIF{i:04d}" for i in range(len(raw))])
    raw.to_csv(csv_path, index=False)
    return str(csv_path)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "lookalike-model"


def test_pipeline_train_and_score(sample_frame: pd.DataFrame):
    pipeline = LookalikePipeline()
    train_result = pipeline.train(sample_frame, is_unbalance=False)
    assert train_result["metrics"]["auc"] > 0.5
    assert len(train_result["feature_importance"]) > 0

    candidates = generate_bank_marketing_dataset(n_rows=50, random_state=2)
    score_result = pipeline.score_candidates(candidates, similarity_threshold=0.5)
    assert score_result["total_scored"] == 50
    assert len(score_result["scores"]) == 50
    assert all(0 <= row["similarity_score"] <= 1 for row in score_result["scores"])


def test_eda_endpoint(synthetic_csv_file: str):
    with open(synthetic_csv_file, "rb") as handle:
        response = client.post(
            "/api/v1/eda?use_sample_data=false",
            files={"file": ("sample.csv", handle, "text/csv")},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["rows"] > 0
    assert payload["output_excel"].endswith("eda_report.xlsx")


def test_train_and_score_api_flow(synthetic_csv_file: str):
    train_response = client.post(
        "/api/v1/model/train",
        json={"data_path": synthetic_csv_file},
    )
    assert train_response.status_code == 200
    train_payload = train_response.json()
    assert train_payload["metrics"]["auc"] > 0.5
    assert len(train_payload["feature_importance"]) > 0

    with open(synthetic_csv_file, "rb") as handle:
        score_response = client.post(
            "/api/v1/lookalike/score?similarity_threshold=0.3",
            files={"file": ("candidates.csv", handle, "text/csv")},
        )
    assert score_response.status_code == 200
    score_payload = score_response.json()
    assert score_payload["total_scored"] > 0
    assert score_payload["similarity_threshold"] == 0.3
    assert score_payload["count_above_threshold"] <= score_payload["total_scored"]


def test_analyze_features_endpoint(synthetic_csv_file: str):
    response = client.post(
        "/api/v1/features/analyze",
        json={"data_path": synthetic_csv_file},
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["kept_features"]) > 0
    assert len(payload["iv_ranking"]) > 0


def test_dashboard_endpoint(synthetic_csv_file: str):
    client.post("/api/v1/model/train", json={"data_path": synthetic_csv_file})
    response = client.get("/api/v1/dashboard")
    assert response.status_code == 200
    payload = response.json()
    assert payload["model_trained"] is True
    assert payload["train_metrics"] is not None


@pytest.mark.slow
def test_real_dataset_smoke():
    """Smoke test against the bundled 100k-row production CSV (optional: pytest -m slow)."""
    from lookalike.config import DEFAULT_DATA_FILE
    from lookalike.pipeline.service import load_dataframe, prepare_modeling_frame

    frame = prepare_modeling_frame(load_dataframe(DEFAULT_DATA_FILE))
    assert len(frame) == 100_000
    pipeline = LookalikePipeline()
    result = pipeline.train(frame.sample(2000, random_state=0), is_unbalance=False)
    assert result["metrics"]["auc"] > 0.5
