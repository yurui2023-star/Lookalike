from __future__ import annotations

import numpy as np

from lookalike.schemas import ScoredCandidate, UserProfile

FEATURE_NAMES = ("age", "income", "engagement_score", "purchase_frequency")


def profiles_to_matrix(profiles: list[UserProfile]) -> np.ndarray:
    return np.array(
        [[getattr(profile, feature) for feature in FEATURE_NAMES] for profile in profiles],
        dtype=float,
    )


def normalize_against_seed(
    matrix: np.ndarray,
    seed_matrix: np.ndarray,
) -> np.ndarray:
    min_vals = seed_matrix.min(axis=0)
    max_vals = seed_matrix.max(axis=0)
    span = np.where(max_vals - min_vals == 0, 1.0, max_vals - min_vals)
    return (matrix - min_vals) / span


def score_lookalikes(
    seed_users: list[UserProfile],
    candidates: list[UserProfile],
    top_k: int,
) -> list[ScoredCandidate]:
    seed_matrix = profiles_to_matrix(seed_users)
    candidate_matrix = profiles_to_matrix(candidates)

    normalized_seed = normalize_against_seed(seed_matrix, seed_matrix)
    normalized_candidates = normalize_against_seed(candidate_matrix, seed_matrix)

    seed_centroid = normalized_seed.mean(axis=0, keepdims=True)
    distances = np.linalg.norm(normalized_candidates - seed_centroid, axis=1)
    similarities = 1.0 / (1.0 + distances)

    ranked_indices = np.argsort(similarities)[::-1][:top_k]
    return [
        ScoredCandidate(
            user_id=candidates[index].user_id,
            similarity_score=round(float(similarities[index]), 4),
            rank=rank,
        )
        for rank, index in enumerate(ranked_indices, start=1)
    ]
