import time

import pytest
from fastapi.testclient import TestClient

from lookalike.config import ID_COL, LABEL_COL, TARGET_COL
from lookalike.data.sample_dataset import generate_bank_marketing_dataset
from lookalike.domain.process_store import ProcessStore
from lookalike.main import app
from lookalike.pipeline.service import LookalikePipeline, prepare_modeling_frame

client = TestClient(app)


@pytest.fixture
def sample_frame():
    return prepare_modeling_frame(generate_bank_marketing_dataset(n_rows=400, random_state=1))


@pytest.fixture
def synthetic_csv_file(sample_frame, tmp_path) -> str:
    csv_path = tmp_path / "synthetic_sample.csv"
    raw = sample_frame.rename(columns={LABEL_COL: TARGET_COL})
    raw.insert(0, ID_COL, [f"CIF{i:04d}" for i in range(len(raw))])
    raw.to_csv(csv_path, index=False)
    return str(csv_path)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "lookalike-model"


def test_pipeline_train_and_score(sample_frame):
    pipeline = LookalikePipeline()
    train_result = pipeline.train(sample_frame, is_unbalance=False)
    assert train_result["metrics"]["auc"] > 0.5

    candidates = generate_bank_marketing_dataset(n_rows=50, random_state=2)
    score_result = pipeline.score_candidates(candidates, similarity_threshold=0.5)
    assert score_result["total_scored"] <= 50
    assert "histogram" in score_result
    assert len(score_result["histogram"]["counts"]) == 100


def test_eda_endpoint(synthetic_csv_file: str):
    with open(synthetic_csv_file, "rb") as handle:
        response = client.post(
            "/api/v1/eda?use_sample_data=false",
            files={"file": ("sample.csv", handle, "text/csv")},
        )
    assert response.status_code == 200
    assert response.json()["rows"] > 0


def test_train_and_score_api_flow(synthetic_csv_file: str):
    train_response = client.post("/api/v1/model/train", json={"data_path": synthetic_csv_file})
    assert train_response.status_code == 200
    assert train_response.json()["metrics"]["auc"] > 0.5

    with open(synthetic_csv_file, "rb") as handle:
        score_response = client.post(
            "/api/v1/lookalike/score?similarity_threshold=0.3",
            files={"file": ("candidates.csv", handle, "text/csv")},
        )
    assert score_response.status_code == 200
    payload = score_response.json()
    assert payload["total_scored"] > 0
    assert payload["histogram"] is not None


def test_analyze_features_endpoint(synthetic_csv_file: str):
    response = client.post("/api/v1/features/analyze", json={"data_path": synthetic_csv_file})
    assert response.status_code == 200
    assert len(response.json()["kept_features"]) > 0


def test_dashboard_endpoint(synthetic_csv_file: str):
    client.post("/api/v1/model/train", json={"data_path": synthetic_csv_file})
    response = client.get("/api/v1/dashboard")
    assert response.status_code == 200
    assert response.json()["model_trained"] is True


def test_process_async_generate_flow(synthetic_csv_file: str):
    # Ensure model trained
    train = client.post("/api/v1/model/train", json={"data_path": synthetic_csv_file})
    assert train.status_code == 200

    created = client.post("/api/v1/processes", json={"name": "MVP demo process"})
    assert created.status_code == 200
    process_id = created.json()["id"]

    with open(synthetic_csv_file, "rb") as handle:
        upload = client.post(
            f"/api/v1/processes/{process_id}/candidates/upload",
            files={"file": ("candidates.csv", handle, "text/csv")},
        )
    assert upload.status_code == 200

    generate = client.post(f"/api/v1/processes/{process_id}/generate", json={})
    assert generate.status_code == 200
    version_id = generate.json()["id"]
    assert generate.json()["status"] in {"pending", "running", "completed"}

    # Wait for background task (TestClient runs BackgroundTasks inline after response)
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status = client.get(f"/api/v1/versions/{version_id}").json()
        if status["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)

    assert status is not None
    assert status["status"] == "completed", status
    assert status["scored_candidates"] > 0

    dashboard = client.get(f"/api/v1/versions/{version_id}/dashboard")
    assert dashboard.status_code == 200
    body = dashboard.json()
    assert body["histogram"] is not None
    assert len(body["top_scores"]) > 0


def test_process_store_isolation():
    store = ProcessStore()
    p1 = store.create_process("a")
    p2 = store.create_process("b")
    assert p1.id != p2.id
    assert len(store.list_processes()) == 2
