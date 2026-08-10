from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    user_id: str = Field(..., examples=["user-001"])
    age: float = Field(..., ge=0, le=120, examples=[34])
    income: float = Field(..., ge=0, examples=[72000])
    engagement_score: float = Field(..., ge=0, le=1, examples=[0.82])
    purchase_frequency: float = Field(..., ge=0, examples=[6.5])


class LookalikeScoreRequest(BaseModel):
    seed_users: list[UserProfile] = Field(..., min_length=1)
    candidates: list[UserProfile] = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=100)


class ScoredCandidate(BaseModel):
    user_id: str
    similarity_score: float = Field(..., ge=0, le=1)
    rank: int = Field(..., ge=1)


class LookalikeScoreResponse(BaseModel):
    seed_count: int
    candidate_count: int
    top_matches: list[ScoredCandidate]
