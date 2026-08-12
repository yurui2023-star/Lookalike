"""Executable evidence for the findings in ``docs/DESIGN_PLAN_REVIEW.md``.

Each check reproduces one finding from the review of the *Lookalike 定时打分服务 —
改造方案* batch-scoring redesign. Where the plan ships sample code, that code is
copied verbatim below (see ``PLAN VERBATIM`` markers) so that what breaks here is
the plan itself rather than a paraphrase of it.

    .venv/bin/python scripts/design_plan_checks.py

Two groups need optional extras and report SKIP without them:

* ``sqlalchemy`` importable -> bind-parameter checks.
* a MySQL/MariaDB server reachable through the ``mariadb`` or ``mysql`` client,
  configured via ``REVIEW_DB_HOST`` / ``REVIEW_DB_PORT`` / ``REVIEW_DB_USER`` /
  ``REVIEW_DB_PASSWORD`` / ``REVIEW_DB_NAME`` -> DDL, partitioning and lock checks.

Exit code is 0 when every executed check reproduced its finding as predicted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "data" / "Bank_Marketing_Dataset.csv"

# The plan's worked example is "高净值产品A持仓客户": positives hold product A and
# have high AUM, negatives do not hold it but have the same high AUM. These are
# the closest equivalents in the bundled Bank Marketing data.
RULE_HOLDING_COL = "HasMutualFunds"
RULE_AUM_COL = "NetWorth"
RULE_AUM_THRESHOLD = 100_000.0
ID_COL = "ClientID"


# --------------------------------------------------------------------------------------
# PLAN VERBATIM — §4.2 seed selection rule compiler, copied unchanged from the plan.
# --------------------------------------------------------------------------------------

ALLOWED_OPS = {"=", "!=", ">", "<", ">=", "<=", "IN", "NOT IN", "LIKE", "IS NULL", "IS NOT NULL"}


def _rule_to_sql(rules: dict, params: dict, idx: list) -> str:
    """将 JSON 规则递归转为参数化 SQL WHERE 片段。"""
    logic = rules.get("logic", "AND")
    conditions = rules.get("conditions", [])
    parts = []
    for cond in conditions:
        if "logic" in cond:
            parts.append(_rule_to_sql(cond, params, idx))
        else:
            col = cond["column"]
            op = cond["op"].upper()
            if op not in ALLOWED_OPS:
                raise ValueError(f"不支持运算符: {op}")
            if op in ("IS NULL", "IS NOT NULL"):
                parts.append(f"`{col}` {op}")
            elif op == "IN":
                key = f"v_{idx[0]}"
                params[key] = tuple(cond["value"])
                parts.append(f"`{col}` IN :{key}")
                idx[0] += 1
            elif op == "NOT IN":
                key = f"v_{idx[0]}"
                params[key] = tuple(cond["value"])
                parts.append(f"`{col}` NOT IN :{key}")
                idx[0] += 1
            else:
                key = f"v_{idx[0]}"
                params[key] = cond["value"]
                parts.append(f"`{col}` {op} :{key}")
                idx[0] += 1
    joiner = f" {logic} "
    return f"({joiner.join(parts)})" if len(parts) > 1 else parts[0]


# --------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------


FAILED_STATUSES = frozenset({"NOT REPRODUCED", "FIX BROKEN"})


@dataclass
class Result:
    finding: str
    title: str
    status: str  # REPRODUCED | NOT REPRODUCED | VERIFIED | FIX BROKEN | SKIP
    detail: str


def _skip(finding: str, title: str, why: str) -> Result:
    return Result(finding, title, "SKIP", why)


def _verdict(finding: str, title: str, reproduced: bool, detail: str) -> Result:
    return Result(finding, title, "REPRODUCED" if reproduced else "NOT REPRODUCED", detail)


def _fix_verdict(finding: str, title: str, works: bool, detail: str) -> Result:
    return Result(finding, title, "VERIFIED" if works else "FIX BROKEN", detail)


# --------------------------------------------------------------------------------------
# Database helpers (CLI based, so no driver dependency is added to the project)
# --------------------------------------------------------------------------------------

_DB_CFG = {
    "host": os.getenv("REVIEW_DB_HOST", "127.0.0.1"),
    "port": os.getenv("REVIEW_DB_PORT", "3306"),
    "user": os.getenv("REVIEW_DB_USER", "review"),
    "password": os.getenv("REVIEW_DB_PASSWORD", "review"),
    "name": os.getenv("REVIEW_DB_NAME", "plan_review"),
}


def _db_client() -> list[str] | None:
    exe = shutil.which("mariadb") or shutil.which("mysql")
    if exe is None:
        return None
    return [
        exe,
        f"--host={_DB_CFG['host']}",
        f"--port={_DB_CFG['port']}",
        f"--user={_DB_CFG['user']}",
        "--batch",
        "--raw",
        _DB_CFG["name"],
    ]


def sql(statement: str) -> tuple[int, str]:
    """Run ``statement``; return (exit code, combined output)."""
    client = _db_client()
    if client is None:
        raise RuntimeError("no mysql/mariadb client on PATH")
    env = {**os.environ, "MYSQL_PWD": _DB_CFG["password"]}
    proc = subprocess.run(
        [*client, "-e", statement], capture_output=True, text=True, env=env, timeout=120
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def sql_message(output: str) -> str:
    """Pick the server's error line out of CLI output, else the last data row."""
    for line in output.splitlines():
        if line.startswith("ERROR"):
            return line
    rows = [line for line in output.splitlines() if line and not set(line) <= {"-"}]
    return rows[-1] if rows else ""


def _db_available() -> bool:
    if _db_client() is None:
        return False
    try:
        code, _ = sql("SELECT 1")
    except Exception:
        return False
    return code == 0


DB_UP = _db_available()


# --------------------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------------------

_FRAME: pd.DataFrame | None = None


def dataset() -> pd.DataFrame:
    global _FRAME
    if _FRAME is None:
        _FRAME = pd.read_csv(DATASET)
    return _FRAME


def seed_frame() -> pd.DataFrame:
    """Label rows exactly the way the plan's pos_rules / neg_rules would."""
    df = dataset()
    high_aum = df[RULE_AUM_COL] >= RULE_AUM_THRESHOLD
    pos = df[high_aum & (df[RULE_HOLDING_COL] == "Yes")].copy()
    neg = df[high_aum & (df[RULE_HOLDING_COL] == "No")].copy()
    pos["label"] = 1
    neg["label"] = 0
    return pd.concat([pos, neg], ignore_index=True)


def _fit_auc(frame: pd.DataFrame, drop: list[str], seed: int = 42) -> tuple[float, int]:
    """One-hot encode, train LightGBM, return holdout AUC and feature count."""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    y = frame["label"]
    x = frame.drop(columns=[c for c in [*drop, "label"] if c in frame.columns])
    x = pd.get_dummies(x, columns=x.select_dtypes(include=["object"]).columns.tolist())

    x_tr, x_te, y_tr, y_te = train_test_split(
        x, y, test_size=0.2, random_state=seed, stratify=y
    )
    model = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, verbose=-1, random_state=seed)
    model.fit(x_tr, y_tr)
    auc = roc_auc_score(y_te, model.predict_proba(x_te)[:, 1])
    return float(auc), x.shape[1]


# --------------------------------------------------------------------------------------
# F-01 — column identifiers are interpolated, so the "no SQL injection" claim is false
# --------------------------------------------------------------------------------------


def check_f01_identifier_injection() -> Result:
    title = "规则编译器只参数化了值，列名仍是字符串拼接"
    hostile = {
        "logic": "AND",
        "conditions": [{"column": "NetWorth` > 0 OR 1=1 -- ", "op": ">", "value": 999999999}],
    }
    params: dict = {}
    where = _rule_to_sql(hostile, params, [0])

    exfil = {
        "logic": "AND",
        "conditions": [
            {
                "column": (
                    "NetWorth` > 0 AND "
                    "(SELECT COUNT(*) FROM t_secret_salary WHERE amount > 500000) > 0 -- "
                ),
                "op": ">",
                "value": 0,
            }
        ],
    }
    exfil_params: dict = {}
    exfil_where = _rule_to_sql(exfil, exfil_params, [0])

    lines = [
        "hostile column name:  NetWorth` > 0 OR 1=1 -- ",
        f"generated WHERE:      {where}",
        f"bound params:         {params}   <- value is parameterised, identifier is not",
    ]

    if not DB_UP:
        lines.append("db: unavailable, static evidence only")
        return _verdict("F-01", title, True, "\n".join(lines))

    sql("DROP TABLE IF EXISTS t_customer_features")
    sql("DROP TABLE IF EXISTS t_secret_salary")
    sql(
        "CREATE TABLE t_customer_features "
        "(customer_id VARCHAR(64) PRIMARY KEY, NetWorth DECIMAL(14,2))"
    )
    sql(
        "INSERT INTO t_customer_features VALUES "
        "('c1', 10.00), ('c2', 20.00), ('c3', 5000000.00)"
    )
    sql("CREATE TABLE t_secret_salary (id INT PRIMARY KEY, amount DECIMAL(14,2))")
    sql("INSERT INTO t_secret_salary VALUES (1, 900000.00)")

    # The plan binds :v_0 through SQLAlchemy; the CLI needs a literal, so inline the
    # same value the plan would have bound. The injected identifier is untouched.
    _, out_honest = sql(
        "SELECT COUNT(*) AS n FROM t_customer_features WHERE `NetWorth` > 999999999"
    )
    bypass = where.replace(":v_0", str(params["v_0"]))
    code_a, out_a = sql(f"SELECT COUNT(*) AS n FROM t_customer_features WHERE {bypass}")
    code_b, out_b = sql(
        "SELECT COUNT(*) AS n FROM t_customer_features "
        f"WHERE {exfil_where.replace(':v_0', str(exfil_params['v_0']))}"
    )

    lines += [
        "",
        "executed against a live MySQL-family server (3-row fixture table):",
        f"  the rule as written  (NetWorth > 999999999) -> {sql_message(out_honest)} rows",
        f"  same rule, hostile column name              -> {sql_message(out_a)} rows"
        f" (exit={code_a}) — the filter is gone",
        f"  subquery into an unrelated table            -> {sql_message(out_b)} rows"
        f" (exit={code_b}) — t_secret_salary was read",
        "",
        "§3.1/§11.1 claim '杜绝 SQL 注入'. Values are bound, identifiers are not, and a"
        " backtick in `column` closes the quote. Rule authors are the threat model here:"
        " anyone with INSERT on t_seed_config gets arbitrary SQL as the job's DB user.",
    ]
    reproduced = code_a == 0 and sql_message(out_a) == "3" and code_b == 0
    return _verdict("F-01", title, reproduced, "\n".join(lines))


# --------------------------------------------------------------------------------------
# F-02 — the rule compiler crashes or emits invalid SQL on ordinary inputs
# --------------------------------------------------------------------------------------


def check_f02_rule_compiler_defects() -> Result:
    title = "规则编译器在常规输入上崩溃或产出不可执行 SQL"
    lines: list[str] = []
    hits = 0

    try:
        _rule_to_sql({"logic": "AND", "conditions": []}, {}, [0])
        lines.append("empty conditions      -> no error (unexpected)")
    except IndexError as exc:
        hits += 1
        lines.append(f"empty conditions      -> IndexError: {exc}")

    nested_no_logic = {
        "logic": "AND",
        "conditions": [
            {"column": "aum", "op": ">=", "value": 1},
            {"conditions": [{"column": "city", "op": "=", "value": "BJ"}]},
        ],
    }
    try:
        _rule_to_sql(nested_no_logic, {}, [0])
        lines.append("nested group w/o logic-> no error (unexpected)")
    except KeyError as exc:
        hits += 1
        lines.append(
            f"nested group w/o 'logic' key -> KeyError: {exc} "
            "(the branch test is `if \"logic\" in cond`, not `if \"conditions\" in cond`)"
        )

    empty_in = {"logic": "AND", "conditions": [{"column": "city", "op": "IN", "value": []}]}
    p: dict = {}
    frag = _rule_to_sql(empty_in, p, [0])
    lines.append(f"empty IN list         -> {frag} with params {p} (renders `IN ()`)")
    hits += 1

    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        lines.append("sqlalchemy not installed -> IN-binding check skipped")
        return _verdict("F-02", title, hits >= 3, "\n".join(lines))

    engine = create_engine("sqlite://")
    ok_rule = {
        "logic": "AND",
        "conditions": [{"column": "city", "op": "IN", "value": ["BJ", "SH"]}],
    }
    ok_params: dict = {}
    ok_frag = _rule_to_sql(ok_rule, ok_params, [0])
    with engine.connect() as conn:
        conn.exec_driver_sql("CREATE TABLE t (city TEXT)")
        conn.exec_driver_sql("INSERT INTO t VALUES ('BJ')")
        try:
            conn.execute(text(f"SELECT COUNT(*) FROM t WHERE {ok_frag}"), ok_params).scalar()
            lines.append("sqlalchemy IN binding -> succeeded (unexpected)")
        except Exception as exc:
            hits += 1
            lines.append(
                f"sqlalchemy IN binding -> {type(exc).__name__}: {str(exc).splitlines()[0]} "
                "(needs bindparam(..., expanding=True))"
            )

    return _verdict("F-02", title, hits >= 4, "\n".join(lines))


# --------------------------------------------------------------------------------------
# F-03 — rule columns stay in the feature matrix, so the model relearns the rule
# --------------------------------------------------------------------------------------


def check_f03_rule_column_leakage() -> Result:
    title = "圈选规则用到的列仍然入模 → 模型学到的是规则本身"
    frame = seed_frame()
    leaky_auc, leaky_n = _fit_auc(frame, drop=[ID_COL, "ResponsePropensity"])
    clean_auc, clean_n = _fit_auc(
        frame, drop=[ID_COL, "ResponsePropensity", RULE_HOLDING_COL, RULE_AUM_COL]
    )
    detail = "\n".join(
        [
            f"seeds: {int((frame['label'] == 1).sum()):,} positive / "
            f"{int((frame['label'] == 0).sum()):,} negative",
            f"pos rule: {RULE_HOLDING_COL}='Yes' AND {RULE_AUM_COL}>={RULE_AUM_THRESHOLD:,.0f}",
            f"neg rule: {RULE_HOLDING_COL}='No'  AND {RULE_AUM_COL}>={RULE_AUM_THRESHOLD:,.0f}",
            "",
            f"plan behaviour (all feature-table columns in scope, {leaky_n} encoded features):"
            f"  AUC = {leaky_auc:.4f}",
            f"rule columns excluded ({clean_n} encoded features):"
            f"                       AUC = {clean_auc:.4f}",
            "",
            "AUC ~1.0 is the model rediscovering the WHERE clause, not lookalike similarity;"
            " every candidate with the holding flag off scores ~0 by construction.",
        ]
    )
    return _verdict("F-03", title, leaky_auc > 0.99 and clean_auc < 0.90, detail)


# --------------------------------------------------------------------------------------
# F-06 — the holdout drives early stopping and is then reported as an unbiased metric
# --------------------------------------------------------------------------------------


def check_f06_holdout_double_use() -> Result:
    title = "holdout 既用于 early stopping 又用于上报指标，AUC/KS 偏乐观"
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    full = dataset()
    encoded = full.drop(columns=[ID_COL, "ResponsePropensity", "TermDepositSubscribed"])
    encoded = pd.get_dummies(
        encoded, columns=encoded.select_dtypes(include=["object"]).columns.tolist()
    )
    target = full["TermDepositSubscribed"]

    # A large, never-used pool stands in for "how the model really performs".
    pool_idx = full.sample(n=25_000, random_state=999).index
    x_pool, y_pool = encoded.loc[pool_idx], target.loc[pool_idx]
    remaining = full.index.difference(pool_idx)

    rows: list[str] = []
    plan_bias_by_size: dict[int, float] = {}
    clean_bias_by_size: dict[int, float] = {}

    for n_seeds in (400, 1_000, 4_000):
        plan_bias, clean_bias = [], []
        for rep in range(40):
            seed_idx = pd.Index(remaining).to_series().sample(n=n_seeds, random_state=rep).index
            x_seed, y_seed = encoded.loc[seed_idx], target.loc[seed_idx]
            x_tr, x_val, y_tr, y_val = train_test_split(
                x_seed, y_seed, test_size=0.2, random_state=42, stratify=y_seed
            )

            # Plan: early stopping listens to X_val, then X_val is reported as the metric.
            m_plan = lgb.LGBMClassifier(n_estimators=600, learning_rate=0.05, verbose=-1)
            m_plan.fit(
                x_tr,
                y_tr,
                eval_set=[(x_val, y_val)],
                eval_metric="auc",
                callbacks=[lgb.early_stopping(50, verbose=False)],
            )
            plan_bias.append(
                roc_auc_score(y_val, m_plan.predict_proba(x_val)[:, 1])
                - roc_auc_score(y_pool, m_plan.predict_proba(x_pool)[:, 1])
            )

            # Fixed: early stopping listens to an inner split; X_val stays untouched.
            x_in, x_es, y_in, y_es = train_test_split(
                x_tr, y_tr, test_size=0.2, random_state=42, stratify=y_tr
            )
            m_ok = lgb.LGBMClassifier(n_estimators=600, learning_rate=0.05, verbose=-1)
            m_ok.fit(
                x_in,
                y_in,
                eval_set=[(x_es, y_es)],
                eval_metric="auc",
                callbacks=[lgb.early_stopping(50, verbose=False)],
            )
            clean_bias.append(
                roc_auc_score(y_val, m_ok.predict_proba(x_val)[:, 1])
                - roc_auc_score(y_pool, m_ok.predict_proba(x_pool)[:, 1])
            )

        plan_bias_by_size[n_seeds] = float(np.mean(plan_bias))
        clean_bias_by_size[n_seeds] = float(np.mean(clean_bias))
        rows.append(
            f"  seeds={n_seeds:>5,} (holdout {int(n_seeds * 0.2):>3} rows):"
            f"  plan {np.mean(plan_bias):+.4f} ± {np.std(plan_bias) / np.sqrt(40):.4f}"
            f"   fixed {np.mean(clean_bias):+.4f} ± {np.std(clean_bias) / np.sqrt(40):.4f}"
        )

    detail = "\n".join(
        [
            "optimism = AUC the job would report on its holdout, minus that same model's AUC"
            " on a 25,000-row pool it never touched. 40 resamples per size.",
            *rows,
            "",
            "SEED_MIN_COUNT defaults to 100, so 400-seed configs are inside the supported"
            " range, and that is where the reported AUC is most inflated.",
            "",
            "§8.1 states 'X_val 在整个调参+训练过程中完全不可见'. It is visible:"
            " best_model.fit(..., eval_set=[(X_val_final, y_val)],"
            " callbacks=[lgb.early_stopping(...)]) picks the tree count on it, and the same"
            " rows then produce the AUC/KS written to t_execution_log.",
        ]
    )
    reproduced = plan_bias_by_size[400] > clean_bias_by_size[400]
    return _verdict("F-06", title, reproduced, detail)


# --------------------------------------------------------------------------------------
# F-05 — negative sample count is derived from max_seeds instead of the positive count
# --------------------------------------------------------------------------------------


def check_f05_neg_ratio() -> Result:
    title = "neg_ratio 以 max_seeds 为基数，正负比例失控"
    df = dataset()
    high_aum = df[RULE_AUM_COL] >= RULE_AUM_THRESHOLD
    pos_matches = int((high_aum & (df[RULE_HOLDING_COL] == "Yes")).sum())
    neg_matches = int((high_aum & (df[RULE_HOLDING_COL] == "No")).sum())

    max_seeds, neg_ratio = 100_000, 2.0
    plan_pos = min(pos_matches, max_seeds)
    plan_neg = min(neg_matches, int(max_seeds * neg_ratio))  # plan: max_seeds * neg_ratio
    fixed_neg = min(neg_matches, int(plan_pos * neg_ratio))  # fix: len(pos_ids) * neg_ratio

    # A real bank's negative rule ("holds no product A") matches most of the base.
    bank_neg_pool = 3_000_000
    bank_plan_neg = min(bank_neg_pool, int(max_seeds * neg_ratio))

    detail = "\n".join(
        [
            f"rule matches in the bundled data: {pos_matches:,} positive / "
            f"{neg_matches:,} negative",
            f"config: max_seeds={max_seeds:,}, neg_ratio={neg_ratio} (i.e. 1:2 requested)",
            "",
            f"plan  `neg_limit = int(max_seeds * neg_ratio)`    -> {plan_pos:,} pos :"
            f" {plan_neg:,} neg  = 1:{plan_neg / plan_pos:.1f}",
            f"fixed `neg_limit = int(len(pos_ids) * neg_ratio)` -> {plan_pos:,} pos :"
            f" {fixed_neg:,} neg  = 1:{fixed_neg / plan_pos:.1f}",
            "",
            f"at production scale the gap widens: with a {bank_neg_pool:,}-row negative pool"
            f" the plan takes {bank_plan_neg:,} negatives against {plan_pos:,} positives"
            f" = 1:{bank_plan_neg / plan_pos:.0f}",
            "",
            "neg_ratio only takes effect when the positive rule matches at least max_seeds"
            " rows, which is the opposite of a seed audience's usual shape.",
        ]
    )
    reproduced = abs(plan_neg / plan_pos - neg_ratio) > 0.5
    return _verdict("F-05", title, reproduced, detail)


# --------------------------------------------------------------------------------------
# F-12 — nothing stops a customer from matching both rules
# --------------------------------------------------------------------------------------


def check_f12_overlap() -> Result:
    title = "正负样本规则可重叠，同一 customer 会同时拿到 1 和 0"
    df = dataset()
    pos_mask = df[RULE_AUM_COL] >= RULE_AUM_THRESHOLD
    neg_mask = df["AnnualIncome"] >= 40_000  # a plausible, independently written negative rule
    overlap = int((pos_mask & neg_mask).sum())

    pos_ids = df.loc[pos_mask, ID_COL]
    neg_ids = df.loc[neg_mask, ID_COL]
    seed_df = pd.concat(
        [
            pd.DataFrame({"customer_id": pos_ids, "label": 1}),
            pd.DataFrame({"customer_id": neg_ids, "label": 0}),
        ],
        ignore_index=True,
    )
    dup = int(seed_df["customer_id"].duplicated().sum())
    conflicting = int(seed_df.groupby("customer_id")["label"].nunique().gt(1).sum())

    detail = "\n".join(
        [
            "pos rule: NetWorth >= 100,000     neg rule: AnnualIncome >= 40,000",
            f"rows matching both rules: {overlap:,}",
            f"seed_df after the plan's pd.concat: {len(seed_df):,} rows, "
            f"{dup:,} duplicated customer_id, {conflicting:,} customers with both labels",
            "",
            "The plan concatenates without a set difference or an assertion, so the same"
            " customer trains the model in both directions and also lands in both CV folds.",
        ]
    )
    return _verdict("F-12", title, conflicting > 0, detail)


# --------------------------------------------------------------------------------------
# F-10 — the training universe is narrower than the population that gets scored
# --------------------------------------------------------------------------------------


def check_f10_covariate_shift() -> Result:
    title = "候选池远大于训练总体，模型在未见过的分布上外推"
    import lightgbm as lgb
    from sklearn.model_selection import train_test_split

    frame = seed_frame()
    y = frame["label"]
    x = frame.drop(columns=[ID_COL, "ResponsePropensity", RULE_HOLDING_COL, "label"])
    x = pd.get_dummies(x, columns=x.select_dtypes(include=["object"]).columns.tolist())
    x_tr, _, y_tr, _ = train_test_split(x, y, test_size=0.2, random_state=42, stratify=y)

    model = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, verbose=-1, random_state=42)
    model.fit(x_tr, y_tr)

    # Scoring runs over the whole customer base, per the plan's "全量打分".
    pool = dataset()
    pool_x = pool.drop(columns=[ID_COL, "ResponsePropensity", RULE_HOLDING_COL])
    pool_x = pd.get_dummies(
        pool_x, columns=pool_x.select_dtypes(include=["object"]).columns.tolist()
    )
    pool_x = pool_x.reindex(columns=x.columns, fill_value=0)
    scores = model.predict_proba(pool_x)[:, 1]

    out_of_universe = pool[RULE_AUM_COL] < RULE_AUM_THRESHOLD
    top_n = 10_000
    top_idx = np.argsort(-scores)[:top_n]
    share = float(out_of_universe.to_numpy()[top_idx].mean())

    detail = "\n".join(
        [
            f"training universe: {RULE_AUM_COL} >= {RULE_AUM_THRESHOLD:,.0f} "
            f"({int((~out_of_universe).sum()):,} of {len(pool):,} customers)",
            f"scoring pool:      every customer ({len(pool):,})",
            f"customers outside the training universe: {int(out_of_universe.sum()):,} "
            f"({out_of_universe.mean():.1%})",
            "",
            f"of the top {top_n:,} leads by score, {share:.1%} come from outside the training"
            " universe — rows whose feature range the model never saw with either label.",
            "",
            "Both rules pin the same AUM condition, so AUM carries no in-sample signal, yet"
            " the plan applies the model to the customers that condition excluded.",
        ]
    )
    return _verdict("F-10", title, share > 0.05, detail)


# --------------------------------------------------------------------------------------
# F-15 — categorical handling has no dtype contract between training and scoring
# --------------------------------------------------------------------------------------


def check_f15_categorical_contract() -> Result:
    title = "categorical_feature 缺少 dtype 契约，训练或打分必然报错"
    import lightgbm as lgb

    df = dataset().sample(n=4000, random_state=3).reset_index(drop=True)
    y = df["TermDepositSubscribed"]
    cat_col = "Region"
    x_obj = df[[cat_col, "Age", "AnnualIncome"]].copy()

    lines: list[str] = []
    hits = 0

    # (a) _detect_categorical_columns() returns object-dtype columns, and LightGBM
    #     refuses to train on them however they are declared.
    try:
        lgb.LGBMClassifier(n_estimators=10, verbose=-1).fit(
            x_obj, y, categorical_feature=[cat_col]
        )
        lines.append("fit on object dtype + categorical_feature -> accepted (unexpected)")
    except Exception as exc:
        hits += 1
        lines.append(
            f"fit on object dtype + categorical_feature -> {type(exc).__name__}: "
            f"{str(exc).splitlines()[0][:120]}"
        )

    # (b) Train on category dtype, then score a frame read straight from the DB (object).
    x_train = x_obj.copy()
    x_train[cat_col] = x_train[cat_col].astype("category")
    model = lgb.LGBMClassifier(n_estimators=60, verbose=-1, random_state=1)
    model.fit(x_train, y, categorical_feature=[cat_col])

    batch_obj = x_obj.head(1500).copy()  # what a candidate-pool SELECT returns
    try:
        model.predict_proba(batch_obj)
        lines.append("predict on object dtype -> accepted (unexpected)")
    except Exception as exc:
        hits += 1
        lines.append(
            f"predict on object dtype after training on category dtype -> "
            f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
        )

    # (c) A category that did not exist at training time is scored without complaint.
    unseen = batch_obj.head(50).copy()
    unseen[cat_col] = pd.Categorical(["Antarctica"] * 50, categories=["Antarctica"])
    unseen_scores = model.predict_proba(unseen)[:, 1]

    lines += [
        "",
        f"a category absent from training ('Antarctica') scores "
        f"{unseen_scores.min():.3f}-{unseen_scores.max():.3f} with no warning",
        "",
        "For the record, LightGBM does remap pandas category *labels* correctly, so"
        " re-running astype('category') per batch is not itself a defect — the defect is"
        " that the plan never fixes the dtype or the level set anywhere, and never carries"
        " them from training to scoring.",
    ]
    return _verdict("F-15", title, hits >= 2, "\n".join(lines))


# --------------------------------------------------------------------------------------
# F-04 — train_lgb() returns no preprocessing artifacts, so scoring cannot reproduce it
# --------------------------------------------------------------------------------------


def check_f04_missing_artifacts() -> Result:
    title = "train_lgb() 不返回清洗参数与入模特征清单 → 打分无法复现训练变换"
    import lightgbm as lgb

    num_cols = [
        "Age",
        "AnnualIncome",
        "NetWorth",
        "CreditScore",
        "AccountBalance",
        "InvestmentPortfolioValue",
        "AvgTransactionValue",
        "TotalTransactions",
    ]
    full = dataset()
    train = full.sample(n=20_000, random_state=1).copy()
    for col in ("AnnualIncome", "CreditScore"):
        train.loc[train.sample(frac=0.12, random_state=2).index, col] = np.nan

    fill = train[num_cols].median()  # the plan's clean_params, fitted on the training set
    model = lgb.LGBMClassifier(n_estimators=150, verbose=-1, random_state=1)
    model.fit(train[num_cols].fillna(fill), train["TermDepositSubscribed"])

    pool = full[num_cols].copy()
    for col in ("AnnualIncome", "CreditScore"):
        pool.loc[pool.sample(frac=0.12, random_state=5).index, col] = np.nan

    scored_right = model.predict_proba(pool.fillna(fill))[:, 1]
    scored_wrong = model.predict_proba(pool)[:, 1]  # clean_params unavailable at scoring
    delta = np.abs(scored_right - scored_wrong)

    top_n = 10_000
    kept = len(
        set(np.argsort(-scored_right)[:top_n]) & set(np.argsort(-scored_wrong)[:top_n])
    )

    reordered = model.predict_proba(pool[num_cols[::-1]].fillna(fill))[:, 1]
    reorder_kept = len(
        set(np.argsort(-scored_right)[:top_n]) & set(np.argsort(-reordered)[:top_n])
    )
    reorder_delta = float(np.abs(scored_right - reordered).mean())

    detail = "\n".join(
        [
            "returned by train_lgb(): model, auc, ks, cv_mean_ks, best_params, iv_table,"
            " feature_importance",
            "needed by _score_all_candidates(): the fill/clip values, the IV-selected column"
            " list and its order, the category levels — none are returned or persisted",
            "",
            "(1) training-time imputation not reapplied at scoring:",
            f"    {(delta > 1e-9).mean():.1%} of rows change, mean |delta| = {delta.mean():.4f},"
            f" max |delta| = {delta.max():.4f}",
            f"    the top {top_n:,} lead list keeps only {kept:,} of {top_n:,} customers"
            f" ({kept / top_n:.1%}) — {top_n - kept:,} leads swap in or out",
            "",
            "(2) column order not pinned (a new column upstream, or SELECT * ordering):",
            f"    LightGBM accepts the reordered frame silently, mean |delta| ="
            f" {reorder_delta:.4f}, top {top_n:,} overlap {reorder_kept / top_n:.1%}",
            "",
            "Both failures are silent: the batch job writes a full result set and logs"
            " status=1 success.",
        ]
    )
    reproduced = kept < top_n and reorder_kept < top_n
    return _verdict("F-04", title, reproduced, detail)


# --------------------------------------------------------------------------------------
# F-07 — the partitioned result table cannot accept the monthly partition it needs
# --------------------------------------------------------------------------------------


def check_f07_partition_ddl() -> Result:
    title = "结果表以单个 MAXVALUE 分区起步，_ensure_partition() 必然失败"
    if not DB_UP:
        return _skip("F-07", title, "no MySQL/MariaDB server available")

    sql("DROP TABLE IF EXISTS t_score_result")
    code_create, out_create = sql(
        """
        CREATE TABLE t_score_result (
            id                BIGINT AUTO_INCREMENT,
            batch_id          VARCHAR(32)  NOT NULL,
            config_id         INT          NOT NULL,
            customer_id       VARCHAR(64)  NOT NULL,
            similarity_score  DECIMAL(8,4) NOT NULL,
            `rank`            INT          NOT NULL,
            create_time       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id, create_time),
            INDEX idx_batch (batch_id),
            INDEX idx_config (config_id),
            INDEX idx_customer (customer_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        PARTITION BY RANGE (TO_DAYS(create_time)) (
            PARTITION p_default VALUES LESS THAN MAXVALUE
        )
        """
    )
    code_add, out_add = sql(
        "ALTER TABLE t_score_result ADD PARTITION "
        "(PARTITION p202609 VALUES LESS THAN (TO_DAYS('2026-10-01')))"
    )
    code_fix, out_fix = sql(
        "ALTER TABLE t_score_result REORGANIZE PARTITION p_default INTO ("
        " PARTITION p202609 VALUES LESS THAN (TO_DAYS('2026-10-01')),"
        " PARTITION p_max VALUES LESS THAN MAXVALUE)"
    )

    detail = "\n".join(
        [
            f"CREATE TABLE (exactly as specified in §3.2) -> exit={code_create}"
            f" {sql_message(out_create)}",
            f"ALTER TABLE ... ADD PARTITION               -> exit={code_add}",
            f"  {sql_message(out_add)}",
            f"ALTER TABLE ... REORGANIZE PARTITION        -> exit={code_fix} (this is the fix)",
            "",
            "MAXVALUE has to be the last range, so once it is the only partition no partition"
            " can be appended. §11.5 describes _ensure_partition() as idempotent; it would"
            " fail on its first run.",
        ]
    )
    return _verdict("F-07", title, code_create == 0 and code_add != 0 and code_fix == 0, detail)


# --------------------------------------------------------------------------------------
# F-14 — the feature query the plan builds is not valid SQL when feature_columns is NULL
# --------------------------------------------------------------------------------------


def check_f14_select_star() -> Result:
    title = "feature_columns 为空时拼出 `SELECT customer_id, *`，语法错误"
    if not DB_UP:
        return _skip("F-14", title, "no MySQL/MariaDB server available")

    sql("DROP TABLE IF EXISTS t_feat")
    sql("CREATE TABLE t_feat (customer_id VARCHAR(64) PRIMARY KEY, aum DECIMAL(14,2))")
    sql("INSERT INTO t_feat VALUES ('c1', 1.00)")

    cols = "*"  # what the plan assigns when feature_columns is NULL
    code_bad, out_bad = sql(f"SELECT customer_id, {cols} FROM `t_feat`")
    code_ok, _ = sql("SELECT customer_id, `t_feat`.* FROM `t_feat`")

    detail = "\n".join(
        [
            "plan: cols = ', '.join(...) if feature_cols else '*'",
            '      SELECT customer_id, {cols} FROM `{table}`',
            "",
            f"SELECT customer_id, * FROM `t_feat`          -> exit={code_bad}",
            f"  {sql_message(out_bad)}",
            f"SELECT customer_id, `t_feat`.* FROM `t_feat` -> exit={code_ok} (this is the fix)",
            "",
            "The default path (feature_columns NULL = 'take every column', which §3.1"
            " documents as supported) never runs.",
        ]
    )
    return _verdict("F-14", title, code_bad != 0 and code_ok == 0, detail)


# --------------------------------------------------------------------------------------
# F-19 — column types are too tight for the values the plan writes into them
# --------------------------------------------------------------------------------------


def check_f19_column_types() -> Result:
    title = "字段类型容量不足：DECIMAL(3,2) / DECIMAL(8,4) / TEXT"
    lines: list[str] = []
    hits = 0

    # Score precision: DECIMAL(8,4) keeps 4 decimals, which collapses ranks.
    rng = np.random.default_rng(0)
    scores = rng.beta(2, 5, size=100_000)
    distinct_full = len(np.unique(scores))
    distinct_4dp = len(np.unique(np.round(scores, 4)))
    tied = int(len(scores) - distinct_4dp)
    lines += [
        f"similarity_score DECIMAL(8,4) on 100,000 scores: {distinct_full:,} distinct values"
        f" collapse to {distinct_4dp:,}",
        f"  -> {tied:,} rows ({tied / len(scores):.1%}) share a score with another row, so"
        " `rank` is not deterministic across reruns",
    ]
    if tied > 0:
        hits += 1

    # IV payload size against the TEXT limit.
    per_bin_rows = []
    for feature_i in range(43):
        for bin_i in range(10):
            per_bin_rows.append(
                {
                    "feature": f"feature_name_number_{feature_i}",
                    "bin": f"(bin_lower_{bin_i}, bin_upper_{bin_i}]",
                    "total": 1234,
                    "positive": 234,
                    "negative": 1000,
                    "woe": -0.123456789,
                    "iv_contrib": 0.001234567,
                }
            )
    payload = json.dumps(per_bin_rows, ensure_ascii=False)
    per_feature = len(payload.encode()) / 43
    overflow_at = int(65_535 // per_feature)
    lines += [
        "",
        f"per-bin IV table for 43 features = {len(payload.encode()):,} bytes"
        f" (~{per_feature:,.0f} bytes/feature)",
        f"  -> TEXT (65,535 bytes) overflows at about {overflow_at} features;"
        " a wide bank feature table has several hundred",
    ]
    if overflow_at < 400:
        hits += 1

    if DB_UP:
        sql("SET SESSION sql_mode='STRICT_ALL_TABLES'")
        sql("DROP TABLE IF EXISTS t_types")
        sql("CREATE TABLE t_types (neg_ratio DECIMAL(3,2), iv_table TEXT)")
        code_ratio, out_ratio = sql(
            "SET SESSION sql_mode='STRICT_ALL_TABLES';"
            " INSERT INTO t_types (neg_ratio) VALUES (20.0)"
        )
        big = "x" * 70_000
        code_text, out_text = sql(
            "SET SESSION sql_mode='STRICT_ALL_TABLES';"
            f" INSERT INTO t_types (iv_table) VALUES ('{big}')"
        )
        lines += [
            "",
            f"INSERT neg_ratio=20.0 into DECIMAL(3,2) -> exit={code_ratio}",
            f"  {sql_message(out_ratio)}",
            f"INSERT 70,000 bytes into TEXT           -> exit={code_text}",
            f"  {sql_message(out_text)}",
        ]
        if code_ratio != 0 and code_text != 0:
            hits += 1
    else:
        lines.append("\n(db checks skipped: no server)")

    return _verdict("F-19", title, hits >= 2, "\n".join(lines))


# --------------------------------------------------------------------------------------
# F-08 — GET_LOCK is session scoped, so pooling and the 7200s timeout both misbehave
# --------------------------------------------------------------------------------------


def check_f08_get_lock() -> Result:
    title = "GET_LOCK 是会话级锁，与连接池和 7200s 超时的用法不符"
    if not DB_UP:
        return _skip("F-08", title, "no MySQL/MariaDB server available")

    client = _db_client()
    assert client is not None
    env = {**os.environ, "MYSQL_PWD": _DB_CFG["password"]}

    holder = subprocess.Popen(
        [*client, "-e", "SELECT GET_LOCK('lookalike_scoring', 0); SELECT SLEEP(9);"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    time.sleep(2)

    _, out_other = sql(
        "SELECT IS_USED_LOCK('lookalike_scoring') AS held_by_conn,"
        " RELEASE_LOCK('lookalike_scoring') AS release_result,"
        " IS_USED_LOCK('lookalike_scoring') AS still_held"
    )

    start = time.time()
    _, out_wait = sql("SELECT GET_LOCK('lookalike_scoring', 3) AS acquired")
    waited = time.time() - start

    holder.wait(timeout=30)
    _, out_after = sql("SELECT IS_USED_LOCK('lookalike_scoring') AS held_after_close")

    other_row = out_other.splitlines()[-1].split("\t")
    released_from_other = other_row[1] if len(other_row) > 1 else "?"
    still_held = other_row[2] if len(other_row) > 2 else "?"
    after = out_after.splitlines()[-1]

    detail = "\n".join(
        [
            "session A holds the lock; session B (a different pooled connection) then:",
            f"  RELEASE_LOCK from another session -> {released_from_other} "
            "(0 = not the owner, silently a no-op)",
            f"  lock still held afterwards        -> {still_held}",
            f"  GET_LOCK(name, 3) blocked for      {waited:.1f}s before returning"
            f" {out_wait.splitlines()[-1]}",
            f"after session A's connection closes, IS_USED_LOCK -> {after} (NULL = auto-released)",
            "",
            "Consequences for the plan: release_lock() run on a different pooled connection is"
            " a no-op; the lock disappears if the pool recycles the connection mid-run; and"
            " GET_LOCK(name, 7200) blocks for two hours before logging '本次跳过' instead of"
            " skipping. A CronJob wants GET_LOCK(name, 0) on a dedicated connection.",
        ]
    )
    reproduced = released_from_other == "0" and waited > 2.5
    return _verdict("F-08", title, reproduced, detail)


# --------------------------------------------------------------------------------------
# Proposed fixes — the review recommends these, so they are exercised too
# --------------------------------------------------------------------------------------

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class RuleError(ValueError):
    """Configuration error: the affected config should fail and log fail_reason."""


def compile_rule(node: dict, allowed_columns: set[str], state: dict) -> str:
    """Hardened replacement for the plan's _rule_to_sql (review §6.2)."""
    logic = node.get("logic", "AND")
    if logic not in ("AND", "OR"):
        raise RuleError(f"不支持的 logic: {logic}")
    conditions = node.get("conditions") or []
    if not conditions:
        raise RuleError("conditions 不能为空")

    parts = []
    for cond in conditions:
        if "conditions" in cond:
            parts.append(compile_rule(cond, allowed_columns, state))
            continue

        col = cond.get("column")
        if not isinstance(col, str) or not _IDENT.match(col):
            raise RuleError(f"非法列名: {col!r}")
        if col not in allowed_columns:
            raise RuleError(f"列不存在于特征表: {col}")
        state["columns"].add(col)

        op = str(cond.get("op", "")).upper()
        if op not in ALLOWED_OPS:
            raise RuleError(f"不支持运算符: {op}")
        if op in ("IS NULL", "IS NOT NULL"):
            parts.append(f"`{col}` {op}")
            continue

        key = f"v_{state['i']}"
        state["i"] += 1
        value = cond.get("value")
        if op in ("IN", "NOT IN"):
            if not isinstance(value, (list, tuple)) or len(value) == 0:
                raise RuleError(f"{op} 的 value 必须是非空列表")
            state["params"][key] = list(value)
            state["expanding"].append(key)
        else:
            state["params"][key] = value
        parts.append(f"`{col}` {op} :{key}")

    return "(" + f" {logic} ".join(parts) + ")"


def build_where(rules: dict, allowed_columns: set[str]) -> tuple[str, dict, list, set]:
    state: dict = {"params": {}, "expanding": [], "columns": set(), "i": 0}
    where = compile_rule(rules, allowed_columns, state)
    return where, state["params"], state["expanding"], state["columns"]


def check_fix_rule_compiler() -> Result:
    title = "§6.2 加固后的规则编译器"
    allowed = {"NetWorth", "aum", "city", "product_a_holding"}
    lines: list[str] = []
    passed = 0
    expected = 6

    for label, rule in (
        ("hostile column name", {"conditions": [{"column": "NetWorth` OR 1=1 -- ", "op": ">",
                                                 "value": 0}]}),
        ("unknown column", {"conditions": [{"column": "not_a_column", "op": ">", "value": 0}]}),
        ("empty conditions", {"logic": "AND", "conditions": []}),
        ("empty IN list", {"conditions": [{"column": "city", "op": "IN", "value": []}]}),
    ):
        try:
            build_where(rule, allowed)
            lines.append(f"{label:<22} -> accepted (REGRESSION)")
        except RuleError as exc:
            passed += 1
            lines.append(f"{label:<22} -> RuleError: {exc}")

    nested = {
        "logic": "OR",
        "conditions": [
            {"column": "aum", "op": ">=", "value": 1_000_000},
            {"conditions": [  # no explicit "logic", which broke the plan's version
                {"column": "product_a_holding", "op": ">", "value": 0},
                {"column": "city", "op": "IN", "value": ["北京", "上海"]},
            ]},
        ],
    }
    where, params, expanding, columns = build_where(nested, allowed)
    passed += 1
    lines += [
        "",
        f"nested group without 'logic' -> {where}",
        f"  params={params} expanding={expanding}",
        f"  rule columns for leakage exclusion -> {sorted(columns)}",
    ]

    try:
        from sqlalchemy import bindparam, create_engine, text
    except ImportError:
        lines.append("\nsqlalchemy not installed -> execution check skipped")
        return _fix_verdict("FIX-1", title, passed >= expected - 1, "\n".join(lines))

    engine = create_engine("sqlite://")
    stmt = text(f"SELECT COUNT(*) FROM t WHERE {where}".replace("`", '"'))
    stmt = stmt.bindparams(*(bindparam(k, expanding=True) for k in expanding))
    with engine.connect() as conn:
        conn.exec_driver_sql(
            'CREATE TABLE t (aum INT, product_a_holding INT, city TEXT, "NetWorth" INT)'
        )
        conn.exec_driver_sql("INSERT INTO t VALUES (10, 5, '北京', 1)")
        count = conn.execute(stmt, params).scalar()
    passed += 1
    lines.append(f"\nexecuted with expanding bindparams -> {count} row(s) matched")

    return _fix_verdict("FIX-1", title, passed >= expected, "\n".join(lines))


def check_fix_result_schema() -> Result:
    title = "§6.1 修订后的结果表 DDL 与窗口函数写入路径"
    if not DB_UP:
        return _skip("FIX-2", title, "no MySQL/MariaDB server available")

    steps: list[tuple[str, int, str]] = []
    sql("DROP TABLE IF EXISTS t_score_result_fixed")
    steps.append(
        (
            "CREATE TABLE (RANGE COLUMNS on batch_date, PK = idempotency key)",
            *sql(
                """
                CREATE TABLE t_score_result_fixed (
                    config_id         INT           NOT NULL,
                    batch_date        DATE          NOT NULL,
                    customer_id       VARCHAR(64)   NOT NULL,
                    similarity_score  DECIMAL(9,8)  NOT NULL,
                    score_rank        INT UNSIGNED  NOT NULL,
                    score_pct         DECIMAL(7,6)  NOT NULL,
                    PRIMARY KEY (config_id, batch_date, customer_id),
                    KEY idx_rank (config_id, batch_date, score_rank)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                PARTITION BY RANGE COLUMNS(batch_date) (
                    PARTITION p202609 VALUES LESS THAN ('2026-10-01'),
                    PARTITION p_max   VALUES LESS THAN (MAXVALUE)
                )
                """
            ),
        )
    )
    steps.append(
        (
            "REORGANIZE p_max to append next month (the F-07 fix)",
            *sql(
                "ALTER TABLE t_score_result_fixed REORGANIZE PARTITION p_max INTO ("
                " PARTITION p202610 VALUES LESS THAN ('2026-11-01'),"
                " PARTITION p_max VALUES LESS THAN (MAXVALUE))"
            ),
        )
    )
    sql("DROP TABLE IF EXISTS t_score_staging")
    sql(
        "CREATE TABLE t_score_staging (run_id VARCHAR(32), customer_id VARCHAR(64),"
        " similarity_score DECIMAL(9,8))"
    )
    sql(
        "INSERT INTO t_score_staging VALUES ('r1','c1',0.9),('r1','c2',0.5),('r1','c3',0.7)"
    )
    steps.append(
        (
            "rank + percentile computed in-database (the F-17 fix)",
            *sql(
                "INSERT INTO t_score_result_fixed (config_id, batch_date, customer_id,"
                " similarity_score, score_rank, score_pct)"
                " SELECT 1, '2026-09-15', customer_id, similarity_score,"
                " RANK() OVER (ORDER BY similarity_score DESC),"
                " PERCENT_RANK() OVER (ORDER BY similarity_score DESC)"
                " FROM t_score_staging WHERE run_id='r1'"
            ),
        )
    )
    steps.append(
        (
            "retry writes the same rows again (the F-09 fix)",
            *sql(
                "DELETE FROM t_score_result_fixed WHERE config_id=1 AND batch_date='2026-09-15';"
                " INSERT INTO t_score_result_fixed (config_id, batch_date, customer_id,"
                " similarity_score, score_rank, score_pct)"
                " SELECT 1, '2026-09-15', customer_id, similarity_score,"
                " RANK() OVER (ORDER BY similarity_score DESC),"
                " PERCENT_RANK() OVER (ORDER BY similarity_score DESC)"
                " FROM t_score_staging WHERE run_id='r1'"
            ),
        )
    )
    _, out_rows = sql("SELECT COUNT(*) AS n FROM t_score_result_fixed")
    steps.append(("DROP PARTITION for retention (the F-07 fix)", *sql(
        "ALTER TABLE t_score_result_fixed DROP PARTITION p202610"
    )))

    lines = [f"{label:<52} -> exit={code} {sql_message(out)}" for label, code, out in steps]
    lines += [
        "",
        f"rows after running the batch twice: {sql_message(out_rows)} (3 staged rows,"
        " no duplicates)",
    ]
    return _fix_verdict("FIX-2", title, all(code == 0 for _, code, _ in steps), "\n".join(lines))


def check_fix_seed_sql() -> Result:
    title = "§6.2 种子圈选 SQL 的 NULL 语义与 F-11 的确定性抽样"
    if not DB_UP:
        return _skip("FIX-3", title, "no MySQL/MariaDB server available")

    sql("DROP TABLE IF EXISTS t_null_demo")
    sql("CREATE TABLE t_null_demo (customer_id VARCHAR(8), holding INT)")
    sql("INSERT INTO t_null_demo VALUES ('c1',5),('c2',0),('c3',NULL)")

    def ids(where_or_query: str, *, raw: bool = False) -> str:
        query = (
            where_or_query
            if raw
            else f"SELECT GROUP_CONCAT(customer_id) FROM t_null_demo WHERE {where_or_query}"
        )
        return sql_message(sql(query)[1])

    positives = ids("(holding > 0)")
    naive_neg = ids("NOT (holding > 0)")
    safe_neg = ids("NOT COALESCE((holding > 0), 0)")

    sample_a = ids(
        "SELECT GROUP_CONCAT(customer_id) FROM (SELECT customer_id FROM t_null_demo"
        " ORDER BY CRC32(CONCAT(customer_id, 'salt2026')) LIMIT 2) t",
        raw=True,
    )
    sample_b = ids(
        "SELECT GROUP_CONCAT(customer_id) FROM (SELECT customer_id FROM t_null_demo"
        " ORDER BY CRC32(CONCAT(customer_id, 'salt2026')) LIMIT 2) t",
        raw=True,
    )
    sample_c = ids(
        "SELECT GROUP_CONCAT(customer_id) FROM (SELECT customer_id FROM t_null_demo"
        " ORDER BY CRC32(CONCAT(customer_id, 'other')) LIMIT 2) t",
        raw=True,
    )

    detail = "\n".join(
        [
            "fixture: c1 holding=5, c2 holding=0, c3 holding=NULL",
            f"  positives  (holding > 0)                    -> {positives}",
            f"  negatives  NOT (holding > 0)                -> {naive_neg}"
            "   c3 silently dropped by three-valued logic",
            f"  negatives  NOT COALESCE((holding > 0), 0)   -> {safe_neg}   correct",
            "",
            "deterministic salted sampling (the F-11 fix):",
            f"  ORDER BY CRC32(CONCAT(id,'salt2026')) LIMIT 2 -> {sample_a}",
            f"  same salt, run again                         -> {sample_b}  reproducible",
            f"  different salt                               -> {sample_c}  different draw",
        ]
    )
    works = (
        positives == "c1"
        and naive_neg == "c2"
        and safe_neg == "c2,c3"
        and sample_a == sample_b
        and sample_a != sample_c
    )
    return _fix_verdict("FIX-3", title, works, detail)


# --------------------------------------------------------------------------------------


CHECKS: list[Callable[[], Result]] = [
    check_f01_identifier_injection,
    check_f02_rule_compiler_defects,
    check_f03_rule_column_leakage,
    check_f04_missing_artifacts,
    check_f05_neg_ratio,
    check_f06_holdout_double_use,
    check_f07_partition_ddl,
    check_f08_get_lock,
    check_f10_covariate_shift,
    check_f12_overlap,
    check_f14_select_star,
    check_f15_categorical_contract,
    check_f19_column_types,
]

FIX_CHECKS: list[Callable[[], Result]] = [
    check_fix_rule_compiler,
    check_fix_result_schema,
    check_fix_seed_sql,
]


def main() -> int:
    if not DATASET.exists():
        print(f"missing dataset: {DATASET}", file=sys.stderr)
        return 2

    print("=" * 96)
    print("DESIGN PLAN REVIEW — executable evidence")
    print(f"dataset : {DATASET.relative_to(REPO_ROOT)} ({len(dataset()):,} rows)")
    print(f"database: {'connected' if DB_UP else 'unavailable (db checks will skip)'}")
    print("=" * 96)

    results: list[Result] = []
    for section, checks in (("FINDINGS", CHECKS), ("PROPOSED FIXES", FIX_CHECKS)):
        print("\n" + "-" * 96)
        print(section)
        print("-" * 96)
        for check in checks:
            result = check()
            results.append(result)
            print(f"\n[{result.finding}] {result.title}")
            print(f"  status: {result.status}")
            for line in result.detail.splitlines():
                print(f"  {line}" if line else "")

    print("\n" + "=" * 96)
    print("SUMMARY")
    print("=" * 96)
    for result in results:
        print(f"  {result.status:<15} {result.finding}  {result.title}")

    failed = [r for r in results if r.status in FAILED_STATUSES]
    skipped = [r for r in results if r.status == "SKIP"]
    print(
        f"\n{len(results) - len(failed) - len(skipped)} confirmed, "
        f"{len(failed)} unconfirmed, {len(skipped)} skipped"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
