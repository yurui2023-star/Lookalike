from __future__ import annotations

from typing import Any

import lightgbm as lgb
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    classification_report,
    confusion_matrix,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split


def encode_features(df: pd.DataFrame) -> pd.DataFrame:
    """One-hot encode categorical columns."""
    features = df.copy()
    cat_cols = features.select_dtypes(include=["object", "category"]).columns.tolist()
    if cat_cols:
        features = pd.get_dummies(features, columns=cat_cols, drop_first=False)
    return features


def build_model(
    df: pd.DataFrame,
    target_col: str,
    test_size: float = 0.2,
    random_state: int = 42,
    is_unbalance: bool = True,
    num_boost_round: int = 200,
    early_stopping_rounds: int = 20,
    verbose: bool = False,
) -> tuple[lgb.Booster, dict[str, Any]]:
    """Train a LightGBM binary classifier and return model + metadata."""
    x_raw = df.drop(columns=[target_col])
    y = df[target_col]
    x = encode_features(x_raw)

    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=test_size, random_state=random_state, stratify=y
    )

    params: dict[str, Any] = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "verbose": -1,
        "random_state": random_state,
    }
    if is_unbalance:
        params["is_unbalance"] = True

    lgb_train = lgb.Dataset(x_train, y_train)
    lgb_eval = lgb.Dataset(x_test, y_test, reference=lgb_train)
    callbacks = [lgb.early_stopping(stopping_rounds=early_stopping_rounds)]
    if verbose:
        callbacks.append(lgb.log_evaluation(50))

    model = lgb.train(
        params,
        lgb_train,
        valid_sets=[lgb_eval],
        num_boost_round=num_boost_round,
        callbacks=callbacks,
    )

    y_pred_proba = model.predict(x_test)
    y_pred = (y_pred_proba > 0.5).astype(int)
    metrics = {
        "auc": float(roc_auc_score(y_test, y_pred_proba)),
        "average_precision": float(average_precision_score(y_test, y_pred_proba)),
        "classification_report": classification_report(
            y_test, y_pred, target_names=["Negative(0)", "Positive(1)"], output_dict=True
        ),
        "confusion_matrix": confusion_matrix(y_test, y_pred).tolist(),
        "train_size": int(x_train.shape[0]),
        "test_size": int(x_test.shape[0]),
        "feature_columns": x.columns.tolist(),
    }
    return model, metrics


def evaluate_model(model: lgb.Booster, df: pd.DataFrame, target_col: str) -> dict[str, Any]:
    """Evaluate a trained model on a labeled dataframe."""
    x = encode_features(df.drop(columns=[target_col]))
    x = x.reindex(columns=model.feature_name(), fill_value=0)
    y = df[target_col]
    y_pred_proba = model.predict(x)
    y_pred = (y_pred_proba > 0.5).astype(int)
    return {
        "auc": float(roc_auc_score(y, y_pred_proba)),
        "average_precision": float(average_precision_score(y, y_pred_proba)),
        "classification_report": classification_report(
            y, y_pred, target_names=["Negative(0)", "Positive(1)"], output_dict=True
        ),
        "confusion_matrix": confusion_matrix(y, y_pred).tolist(),
    }


def feature_importance(model: lgb.Booster, top_n: int = 15) -> list[dict[str, float | str]]:
    """Return ranked feature importance (gain)."""
    importance = model.feature_importance(importance_type="gain")
    names = model.feature_name()
    rows = [{"feature": name, "importance": float(score)} for name, score in zip(names, importance)]
    rows.sort(key=lambda row: row["importance"], reverse=True)
    return rows[:top_n]


def predict_similarity_scores(model: lgb.Booster, df: pd.DataFrame) -> pd.Series:
    """Predict probability scores (0-1) used as Similarity Score per BRD."""
    x = encode_features(df)
    x = x.reindex(columns=model.feature_name(), fill_value=0)
    return pd.Series(model.predict(x), index=df.index, name="similarity_score")
