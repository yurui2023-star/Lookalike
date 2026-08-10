from fastapi import FastAPI

from lookalike import __version__
from lookalike.model import score_lookalikes
from lookalike.schemas import LookalikeScoreRequest, LookalikeScoreResponse

app = FastAPI(
    title="Lookalike Model API",
    description="Score candidate users by similarity to a seed audience.",
    version=__version__,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "lookalike-model", "version": __version__}


@app.get("/")
def root() -> dict[str, str]:
    return {
        "message": "Lookalike Model API",
        "docs": "/docs",
        "score_endpoint": "POST /api/v1/lookalike/score",
    }


@app.post("/api/v1/lookalike/score", response_model=LookalikeScoreResponse)
def score(request: LookalikeScoreRequest) -> LookalikeScoreResponse:
    top_matches = score_lookalikes(request.seed_users, request.candidates, request.top_k)
    return LookalikeScoreResponse(
        seed_count=len(request.seed_users),
        candidate_count=len(request.candidates),
        top_matches=top_matches,
    )


def run() -> None:
    import uvicorn

    uvicorn.run("lookalike.main:app", host="0.0.0.0", port=8000, reload=True)
