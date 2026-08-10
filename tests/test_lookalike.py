from fastapi.testclient import TestClient

from lookalike.main import app
from lookalike.model import score_lookalikes
from lookalike.schemas import UserProfile

client = TestClient(app)

SEED = [
    UserProfile(
        user_id="seed-1",
        age=32,
        income=85000,
        engagement_score=0.9,
        purchase_frequency=8,
    ),
    UserProfile(
        user_id="seed-2",
        age=35,
        income=92000,
        engagement_score=0.85,
        purchase_frequency=7,
    ),
]

CANDIDATES = [
    UserProfile(
        user_id="candidate-close",
        age=33,
        income=88000,
        engagement_score=0.88,
        purchase_frequency=7.5,
    ),
    UserProfile(
        user_id="candidate-far",
        age=19,
        income=25000,
        engagement_score=0.05,
        purchase_frequency=0.5,
    ),
]


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "lookalike-model"


def test_score_endpoint_returns_ranked_matches():
    response = client.post(
        "/api/v1/lookalike/score",
        json={
            "seed_users": [user.model_dump() for user in SEED],
            "candidates": [user.model_dump() for user in CANDIDATES],
            "top_k": 2,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["seed_count"] == 2
    assert payload["candidate_count"] == 2
    assert payload["top_matches"][0]["user_id"] == "candidate-close"
    assert payload["top_matches"][0]["rank"] == 1
    top_score = payload["top_matches"][0]["similarity_score"]
    second_score = payload["top_matches"][1]["similarity_score"]
    assert top_score > second_score


def test_score_lookalikes_unit():
    matches = score_lookalikes(SEED, CANDIDATES, top_k=1)
    assert len(matches) == 1
    assert matches[0].user_id == "candidate-close"
    assert 0 <= matches[0].similarity_score <= 1
