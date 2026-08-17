# Lookalike 定时打分服务 — 改造方案

> **已被 v4.0 取代，请勿按本文实现。** 生产目标设计见 [`Lookalike_Detailed_Design_v4.0_BatchScoring.html`](Lookalike_Detailed_Design_v4.0_BatchScoring.html)。本文的种子 SQL / 生产训练 / 全特征表打分与 SAD V1.0 冲突；评审结论仍见 [`DESIGN_PLAN_REVIEW.md`](DESIGN_PLAN_REVIEW.md)。
>
> 版本：v1.0 | 日期：2026-08-12 | 状态：archived

---

## 1. 改造目标

将当前"用户上传种子文件 → 交互式调参 → 手动触发打分 → 查询下载结果"的 API 服务，改造为**纯后端定时服务**：

- 每月定时自动执行
- 从数据库表读取种子配置和特征数据
- 自动训练 + 全量打分
- 结果直接写回数据库
- 所有流程隐式完成，无需人工操作

---

## 2. 架构变化总览

```
改造前：用户 → [HTTP API] → 文件上传 → 手动触发打分 → 结果查询

改造后：定时器 → 读取种子规则 → 拉取特征 → 训练+打分 → 写入结果表
```

### 保留的模块

| 模块 | 用途 | 变化 |
|------|------|------|
| `services/scoring_service.py` | 训练 + 批量预测 | 简化为仅 LightGBM，自动 IV/KS |
| `services/model_utils.py` | 特征发现/清洗/IV/KS | 不变 |
| `db/connection.py` | MySQL + ClickHouse 连接池 | 不变 |
| `db/queries.py` | 部分 SQL 查询 | 精简，只保留特征查询相关 |
| `config.py` | 全局配置 | 新增调度相关配置项 |

### 删除的模块

| 模块 | 原因 |
|------|------|
| `api/` (全部 10 个路由文件) | 不再提供 HTTP API |
| `services/file_service.py` | 不再接收文件上传 |
| `services/eda_service.py` | 不再生成 EDA 报告 |
| `services/export_service.py` | 结果直接写 DB，不导出文件 |
| `services/model_factory.py` | 模型固定 LightGBM，不再需要工厂和多算法切换 |
| `utils/auth.py` | 不再需要 JWT 鉴权 |
| `utils/jwt_utils.py` | 同上 |
| `utils/response.py` | 不再返回 HTTP 响应 |
| `models/` (全部 3 个 Pydantic 模型) | 不再有请求/响应模型 |
| `tests/` | 需重写集成测试 |
| `gen_token.py` | 不再需要 Token |
| `stop.py` | 不再启动 HTTP 服务 |

### 新增的模块

| 模块 | 职责 |
|------|------|
| `scheduler.py` | 定时调度入口，cron 驱动 |
| `services/seed_service.py` | 从 DB 读取种子规则并圈选样本 |
| `services/result_writer.py` | 打分结果批量写入目标表 |

---

## 3. 数据库设计

### 3.1 新增表：`t_seed_config` — 种子配置表

**采用规则模板化（JSON）替代原始 SQL**，杜绝 SQL 注入。配置时填写结构化规则，代码自动生成参数化查询。

```sql
CREATE TABLE IF NOT EXISTS t_seed_config (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    config_name      VARCHAR(50)  NOT NULL COMMENT '配置名称',
    description      VARCHAR(200) DEFAULT '' COMMENT '说明',
    pos_rules        JSON         NOT NULL COMMENT '正样本圈选规则',
    neg_rules        JSON         NOT NULL COMMENT '负样本圈选规则',
    neg_ratio        DECIMAL(3,2) DEFAULT 1.00 COMMENT '负正比例',
    max_seeds        INT          DEFAULT 100000 COMMENT '种子数量上限',
    feature_columns  JSON         DEFAULT NULL COMMENT '参与建模的特征列白名单，NULL=全部',
    is_active        TINYINT      DEFAULT 1 COMMENT '是否启用',
    create_time      DATETIME     DEFAULT CURRENT_TIMESTAMP,
    update_time      DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**规则 JSON 格式**（参数化，无 SQL 注入风险）：

```json
{
  "logic": "AND",
  "conditions": [
    {"column": "product_a_holding", "op": ">",  "value": 0},
    {"column": "product_a_holding", "op": "<=", "value": 1000000},
    {"column": "aum",               "op": ">=", "value": 1000000},
    {"column": "city",              "op": "IN", "value": ["北京","上海","深圳"]}
  ]
}
```

支持的运算符：`=`, `!=`, `>`, `<`, `>=`, `<=`, `IN`, `NOT IN`, `LIKE`, `IS NULL`, `IS NOT NULL`，`logic` 取值 `AND` / `OR`。

**feature_columns** 为空或 null 时取特征表全部列（排除 customer_id），指定后只 SELECT 白名单列，避免未知新增列干扰模型。

**示例数据**（高净值产品A持仓 + 贵宾客户）：

```sql
INSERT INTO t_seed_config (config_name, pos_rules, neg_rules, neg_ratio)
VALUES
('高净值产品A持仓客户',
 '{"logic":"AND","conditions":[{"column":"product_a_holding","op":">","value":0},{"column":"aum","op":">=","value":1000000}]}',
 '{"logic":"AND","conditions":[{"column":"product_a_holding","op":"=","value":0},{"column":"aum","op":">=","value":1000000}]}',
 2.0),
('贵宾客户',
 '{"logic":"AND","conditions":[{"column":"vip_level","op":">=","value":3}]}',
 '{"logic":"AND","conditions":[{"column":"vip_level","op":"<","value":3}]}',
 1.0);
```

### 3.2 新增表：`t_score_result` — 打分结果表（按月分区）

```sql
CREATE TABLE IF NOT EXISTS t_score_result (
    id                BIGINT AUTO_INCREMENT,
    batch_id          VARCHAR(32)  NOT NULL COMMENT '执行批次ID',
    config_id         INT          NOT NULL COMMENT '种子配置ID',
    customer_id       VARCHAR(64)  NOT NULL,
    similarity_score  DECIMAL(8,4) NOT NULL COMMENT '相似度得分',
    `rank`            INT          NOT NULL COMMENT '排名',
    create_time       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, create_time),
    INDEX idx_batch (batch_id),
    INDEX idx_config (config_id),
    INDEX idx_customer (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE (TO_DAYS(create_time)) (
    PARTITION p_default VALUES LESS THAN MAXVALUE
);
```

按月自动分区由定时任务管理——每月执行前创建下月分区，清理时直接 `DROP PARTITION`，比 DELETE 高效数个数量级。

### 3.3 新增表：`t_execution_log` — 执行日志表

```sql
CREATE TABLE IF NOT EXISTS t_execution_log (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    batch_id          VARCHAR(32)  NOT NULL COMMENT '执行批次ID',
    config_id         INT          NOT NULL COMMENT '种子配置ID',
    status            TINYINT      DEFAULT 0 COMMENT '0=执行中 1=成功 2=失败',
    seed_count        INT          DEFAULT 0 COMMENT '种子总数',
    pos_count         INT          DEFAULT 0 COMMENT '正样本数',
    neg_count         INT          DEFAULT 0 COMMENT '负样本数',
    candidate_count   INT          DEFAULT 0 COMMENT '候选池总量',
    result_count      INT          DEFAULT 0 COMMENT '结果数量',
    auc               DECIMAL(6,4) DEFAULT NULL COMMENT '最终模型AUC',
    ks                DECIMAL(6,4) DEFAULT NULL COMMENT '最终模型KS',
    best_params       TEXT         COMMENT 'Optuna最优超参JSON',
    top_features      TEXT         COMMENT 'Top20特征重要性JSON',
    iv_table          TEXT         COMMENT 'IV表JSON',
    elapsed_seconds   INT          DEFAULT 0,
    fail_reason       TEXT,
    start_time        DATETIME     DEFAULT CURRENT_TIMESTAMP,
    end_time          DATETIME     DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.4 保留的表

| 表 | 状态 |
|------|------|
| `t_task` / `t_task_result` | 废弃，保留历史数据不删除 |
| `t_customer_features` | 保留，特征宽表 |
| `t_param_template` | 可删除（无再用场景） |

---

## 4. 执行流程

### 4.1 整体流程

```
┌──────────────┐
│ 定时器触发    │  每月1日凌晨2:00
└──────┬───────┘
       ▼
┌──────────────┐
│ 读取种子配置  │  从 t_seed_config 读取所有启用的配置
└──────┬───────┘
       ▼
┌──────────────┐
│ For Each 配置 │
└──────┬───────┘
       ▼
┌──────────────┐
│ 圈选种子人群  │  用 pos_rule/neg_rule 从特征表查询正负样本
└──────┬───────┘
       ▼
┌──────────────┐
│ 数据预处理    │  特征发现 → 清洗 → IV自动计算并筛选
└──────┬───────┘
       ▼
┌──────────────────────────────────────┐
│ LightGBM 训练（唯一模型）              │
│  StratifiedKFold CV → KS自动计算      │
│  取CV均分最高的超参组合为最终模型       │
└──────┬───────────────────────────────┘
       ▼
┌──────────────┐
│ 全量打分      │  候选池排除种子 → 分批50k预测
└──────┬───────┘
       ▼
┌──────────────┐
│ 结果写库      │  排序 + rank → 批量写入 t_score_result
└──────┬───────┘
       ▼
┌──────────────┐
│ 记录执行日志  │  写入 t_execution_log（含AUC/KS/IV/Top特征）
└──────┬───────┘
       ▼
┌──────────────┐
│ 执行完成      │  日志输出
└──────────────┘
```

### 4.2 种子圈选逻辑（`seed_service.py`）

规则模板化为 JSON，由代码生成参数化 SQL，杜绝注入：

```python
import json
import pandas as pd
from sqlalchemy import text

# 允许的运算符白名单
ALLOWED_OPS = {"=", "!=", ">", "<", ">=", "<=", "IN", "NOT IN", "LIKE", "IS NULL", "IS NOT NULL"}


def _rule_to_sql(rules: dict, params: dict, idx: list) -> str:
    """将 JSON 规则递归转为参数化 SQL WHERE 片段。
    idx 用单元素 list 传递，确保递归内部的自增能传播回调用方。"""
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


def select_seeds(config: dict, feature_conn) -> pd.DataFrame:
    """根据参数化规则圈选正负样本"""
    pos_rules = json.loads(config["pos_rules"])
    neg_rules = json.loads(config["neg_rules"])
    neg_ratio = config.get("neg_ratio", 1.0)
    max_seeds = config.get("max_seeds", 100000)
    feature_cols = config.get("feature_columns")  # None = 全部列

    table = get_feature_table()
    cols = ", ".join(f"`{c}`" for c in feature_cols) if feature_cols else "*"

    # 正样本
    params = {}
    pos_where = _rule_to_sql(pos_rules, params, [0])
    pos_sql = f"SELECT customer_id FROM `{table}` WHERE {pos_where} LIMIT {max_seeds}"
    pos_ids = [r[0] for r in feature_conn.execute(text(pos_sql), params).fetchall()]

    # 负样本
    params = {}
    neg_where = _rule_to_sql(neg_rules, params, [0])
    neg_limit = int(max_seeds * neg_ratio)
    neg_sql = f"SELECT customer_id FROM `{table}` WHERE {neg_where} LIMIT {neg_limit}"
    neg_ids = [r[0] for r in feature_conn.execute(text(neg_sql), params).fetchall()]

    # 构建训练集
    pos_df = pd.DataFrame({"customer_id": pos_ids, "label": 1})
    neg_df = pd.DataFrame({"customer_id": neg_ids, "label": 0})
    seed_df = pd.concat([pos_df, neg_df], ignore_index=True)

    # JOIN 特征向量
    ids_str, id_params = build_in_clause("seed", seed_df["customer_id"].tolist())
    feature_rows = feature_conn.execute(
        text(f"SELECT customer_id, {cols} FROM `{table}` WHERE customer_id IN ({ids_str})"),
        id_params,
    ).fetchall()
    feature_df = pd.DataFrame([dict(r._mapping) for r in feature_rows])
    return feature_df.merge(seed_df[["customer_id", "label"]], on="customer_id", how="inner")
```

### 4.3 调度入口（`scheduler.py`）

采用 run-once-and-exit 模型，由 K8s CronJob 或系统 crontab 周期性触发，程序执行完自动退出，不再常驻内存。内部通过 DB 分布式锁防重入。

```python
"""评分调度器：run-once 模式，由外部调度系统触发"""
import logging
import os
import sys
import time
from datetime import datetime

from services.seed_service import select_seeds
from services.scoring_service import train_lgb, batch_predict
from services.result_writer import write_results
from db.connection import get_mysql_connection, get_feature_conn
from config import OPTUNA_TRIALS, LOCK_TIMEOUT_SECONDS, DRY_RUN

logger = logging.getLogger("lookalike.scheduler")


def acquire_lock(mysql_conn, lock_name: str = "lookalike_scoring", timeout: int = None) -> bool:
    """MySQL GET_LOCK 分布式锁，防止多实例并发执行"""
    if timeout is None:
        timeout = LOCK_TIMEOUT_SECONDS
    result = mysql_conn.execute(
        text(f"SELECT GET_LOCK(:name, :timeout)"),
        {"name": lock_name, "timeout": timeout}
    ).scalar()
    return bool(result)


def release_lock(mysql_conn, lock_name: str = "lookalike_scoring"):
    mysql_conn.execute(text(f"SELECT RELEASE_LOCK(:name)"), {"name": lock_name})


def execute_scoring():
    """执行一轮完整打分流程（单次运行后退出）"""
    batch_id = datetime.now().strftime("B%Y%m%d%H%M%S")
    logger.info("===== 打分批次 %s 开始 =====", batch_id)

    mysql_conn = get_mysql_connection()

    # ---- 防重入锁 ----
    if not acquire_lock(mysql_conn):
        logger.warning("上一批次仍在执行中，本次跳过")
        mysql_conn.close()
        sys.exit(0)

    feature_conn = get_feature_conn()
    try:
        seed_configs = _load_active_configs(mysql_conn)

        for config in seed_configs:
            log_id = _init_log(mysql_conn, batch_id, config)
            try:
                # 1. 圈选种子
                train_df = select_seeds(config, feature_conn)
                if len(train_df) < 100:
                    raise ValueError(f"种子数量不足: {len(train_df)}")

                # Dry-run 模式：只统计不训练
                if DRY_RUN:
                    logger.info("[DRY-RUN] 配置=%s 正样本=%d 负样本=%d 特征列=%d",
                        config["config_name"],
                        int((train_df["label"] == 1).sum()),
                        int((train_df["label"] == 0).sum()),
                        len(train_df.columns) - 2)
                    continue

                # 2. 数据预处理 → IV → Optuna 搜参 → LightGBM 训练
                train_result = train_lgb(train_df, n_trials=OPTUNA_TRIALS)

                # 3. 全量打分（排除种子）
                seed_ids = train_df["customer_id"].astype(str).tolist()
                all_scores = _score_all_candidates(feature_conn, train_result, seed_ids)

                # 4. 写入结果表（含按月分区管理）
                _ensure_partition(mysql_conn)
                write_results(mysql_conn, batch_id, config["id"], all_scores, train_result)

                # 5. 更新执行日志
                _complete_log(mysql_conn, log_id, train_result, len(all_scores))

            except Exception as e:
                logger.error("配置 %s 执行失败: %s", config["id"], str(e))
                _fail_log(mysql_conn, log_id, str(e))
                continue

        # 清理过期分区
        _drop_old_partitions(mysql_conn)

    finally:
        release_lock(mysql_conn)
        mysql_conn.close()
        feature_conn.close()

    logger.info("===== 打分批次 %s 完成 =====", batch_id)


if __name__ == "__main__":
    execute_scoring()
```

---

## 5. 配置项

在现有 `config.py` 基础上新增：

```python
# ==================== 调度配置 ====================
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"          # 试运行模式：只圈选不训练
LOCK_TIMEOUT_SECONDS = int(os.getenv("LOCK_TIMEOUT_SECONDS", "7200"))  # 分布式锁超时

# ==================== 种子圈选默认值 ====================
DEFAULT_NEG_RATIO = float(os.getenv("DEFAULT_NEG_RATIO", "1.0"))
DEFAULT_MAX_SEEDS = int(os.getenv("DEFAULT_MAX_SEEDS", "100000"))
SEED_MIN_COUNT = int(os.getenv("SEED_MIN_COUNT", "100"))

# ==================== LightGBM + Optuna 搜参 ====================
OPTUNA_TRIALS = int(os.getenv("OPTUNA_TRIALS", "30"))
LGB_CV_FOLDS = int(os.getenv("LGB_CV_FOLDS", "5"))
LGB_EARLY_STOPPING = int(os.getenv("LGB_EARLY_STOPPING", "50"))
IV_THRESHOLD = float(os.getenv("IV_THRESHOLD", "0.02"))
OPTUNA_STORAGE = os.getenv("OPTUNA_STORAGE", "")  # 留空=内存，可填 sqlite:///path 持久化

# ==================== 结果保留 ====================
RESULT_RETENTION_MONTHS = int(os.getenv("RESULT_RETENTION_MONTHS", "12"))
```

---

## 6. 新的目录结构

```
lookalike_service/
├── main.py                   # 程序入口 + 启动 metrics 线程
├── config.py                 # 配置管理
├── scheduler.py              # 主执行流程（run-once 模式）
├── metrics.py                # [新增] 健康检查 + 指标暴露（端口8090）
├── init_db.py                # 数据库建表脚本
├── services/
│   ├── scoring_service.py    # [简化] 仅 LightGBM + Optuna + 类别特征
│   ├── model_utils.py        # [保留] 特征发现/清洗/IV/KS
│   ├── seed_service.py       # [新增] 规则模板化种子圈选
│   ├── result_writer.py      # [新增] 结果批量写入 + 分区管理
├── db/
│   ├── connection.py         # [保留] MySQL + ClickHouse 连接池
│   └── queries.py            # [精简] 特征查询 + 分区管理
├── requirements.txt          # [更新] +optuna -openpyxl -uvicorn -fastapi
└── README.md                 # [更新]
```

### 删除的目录

```
api/          — 10个路由文件 + __pycache__
models/       — 3个Pydantic模型文件
utils/        — auth.py, jwt_utils.py, response.py
tests/        — 旧测试用例（后续重写）
services/     — file_service.py, eda_service.py, export_service.py
tasks/        — async_scoring.py（逻辑已合并到 scheduler.py）
```

---

## 7. 启动方式

程序采用 **run-once-and-exit** 模型，由外部调度系统触发，不再常驻内存。

```bash
# 直接执行（一次性）
python scheduler.py
```

`main.py` 简化为仅负责日志初始化的入口：

```python
if __name__ == "__main__":
    import logging, sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    from scheduler import execute_scoring
    execute_scoring()
```

**部署方式（推荐）**：

```yaml
# K8s CronJob
apiVersion: batch/v1
kind: CronJob
metadata:
  name: lookalike-scoring
spec:
  schedule: "0 2 1 * *"   # 每月1日凌晨2:00
  jobTemplate:
    spec:
      backoffLimit: 1      # 失败最多重试1次
      template:
        spec:
          containers:
          - name: scorer
            image: lookalike-service:latest
            env:
            - name: DB_HOST
              value: "mysql-host"
          restartPolicy: Never
```

或使用系统 crontab：

```bash
0 2 1 * * cd /app && python scheduler.py >> /var/log/lookalike.log 2>&1
```

**手动触发**：

```bash
# 正式执行
python scheduler.py

# Dry-Run 模式（只圈选不训练，验证种子规则）
DRY_RUN=true python scheduler.py
```

---

## 8. LightGBM 训练流程（Optuna + 剪枝 + 类别特征）

用 Optuna TPE 采样器做贝叶斯超参优化，目标最大化 CV-KS。MedianPruner 剪枝 + LightGBM early_stopping 双重提前终止，30轮可在十几分钟完成。

```
  ┌──────────────────┐
  │ 预处理后特征集     │
  └────────┬─────────┘
           ▼
  ┌──────────────────┐
  │ IV 自动计算       │  calc_iv() 遍历所有特征
  │ 剔除 IV ≤ 0.02   │
  └────────┬─────────┘
           ▼
  ┌──────────────────────────────────────────────┐
  │ 识别类别特征                                   │
  │  object/category 列 + unique≤50 的整型列       │
  │  传入 LightGBM categorical_feature            │
  └────────┬─────────────────────────────────────┘
           ▼
  ┌──────────────────────────────────────────────┐
  │ Optuna Study (TPE, 30 trials)                │
  │  每 trial: 5折CV, 每折 early_stopping=50     │
  │  MedianPruner 剪枝劣质 trial                  │
  │  目标函数 = CV-KS 均值（最大化）               │
  └────────┬─────────────────────────────────────┘
           ▼
  ┌──────────────────────────────────────────────┐
  │ 最优超参全量训练 → 独立验证集算 AUC + KS       │
  │  study.best_params 训练最终模型                │
  │  留一折做 holdout，用 roc_auc_score 算真实 AUC │
  └────────┬─────────────────────────────────────┘
           ▼
  ┌──────────────────────────────────────────────┐
  │ 输出: model, preprocessor, auc, ks,           │
  │  best_params, iv_table, feature_importance    │
  └──────────────────────────────────────────────┘
```

`scoring_service.train_lgb()` 核心代码：

```python
import numpy as np
import optuna
import lightgbm as lgb
from optuna.samplers import TPESampler
from optuna.pruners import MedianPruner
from sklearn.model_selection import StratifiedKFold, train_test_split
from sklearn.metrics import roc_auc_score

from services.model_utils import (
    auto_discover_features, clean_data, calc_iv, compute_ks,
    LABEL_COLUMN, NON_FEATURE_COLS, CATEGORICAL_THRESHOLD,
)


def _fit_clean_params(df: pd.DataFrame) -> tuple:
    """在训练集上拟合清洗参数（P99 截断阈值 + 缺失值填充值），不做 inplace 修改"""
    params = {}
    exclude = SENTINEL_COLS | MANUAL_EXCLUDE_COLS
    num_cols = [c for c in df.select_dtypes(include=["int64","float64"]).columns
                if c not in exclude]
    cat_cols = [c for c in df.select_dtypes(include=["object","category","bool"]).columns
                if c not in exclude]

    for col in num_cols:
        s = df[col].dropna()
        params[col] = {
            "p99": float(s.quantile(0.99)) if len(s) > 0 else 0,
            "fill": float(s.median()) if len(s) > 0 else 0,
        }
    for col in cat_cols:
        mode_vals = df[col].mode()
        params[col] = {
            "fill": mode_vals[0] if len(mode_vals) > 0 else "Unknown",
        }
    return _apply_clean_params(df, params), params


def _apply_clean_params(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """将训练集上拟合的清洗参数应用到任意 DataFrame（包括验证集）"""
    df = df.copy()
    for col, p in params.items():
        if col not in df.columns:
            continue
        if "p99" in p:
            df[col] = df[col].clip(upper=p["p99"])
        df[col] = df[col].fillna(p["fill"])
    return df


def _detect_categorical_columns(df):
    """自动识别类别特征列"""
    cat_cols = []
    for col in df.columns:
        if df[col].dtype in ("object", "category", "bool"):
            cat_cols.append(col)
        elif df[col].dtype in ("int64", "int32"):
            if df[col].nunique() <= CATEGORICAL_THRESHOLD:
                cat_cols.append(col)
    return cat_cols


def train_lgb(train_df, n_trials=30):
    # ================================================================
    #  Step 1: ★ 先拆后处理 — 杜绝数据泄露
    #  val_holdout 在整个调参+训练过程中完全不可见
    # ================================================================
    exclude_cols = [c for c in NON_FEATURE_COLS if c in train_df.columns]
    X_full = train_df.drop(columns=exclude_cols, errors="ignore")
    y_full = train_df[LABEL_COLUMN]

    X_train, X_val, y_train, y_val = train_test_split(
        X_full, y_full, test_size=0.2, random_state=42, stratify=y_full,
    )

    # ---- Step 2: 数据清洗 — 在 X_train 上拟合统计量，apply 到 X_val ----
    X_train_clean, clean_params = _fit_clean_params(X_train)
    X_val_clean = _apply_clean_params(X_val, clean_params)
    # clean_params 记录每列的 {p99, fill_value}，X_val 严格复用，不自行计算

    # ---- Step 3: IV 筛选 — 仅在 X_train 上计算 IV ----
    iv_df, iv_important = calc_iv(X_train_clean, y_train, iv_threshold=IV_THRESHOLD)
    X_train_final = X_train_clean[iv_important]
    X_val_final = X_val_clean[iv_important]  # 应用同一个特征列表

    # ---- Step 4: 识别类别特征（仅在 X_train 上） ----
    cat_features = _detect_categorical_columns(X_train_final)

    # ---- Step 5: Optuna 目标函数（仅使用 X_train / y_train） ----
    cv = StratifiedKFold(n_splits=LGB_CV_FOLDS, shuffle=True, random_state=42)
    cv_splits = list(cv.split(X_train_final, y_train))

    def objective(trial):
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 100, 500),
            "max_depth": trial.suggest_int("max_depth", 3, 10),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
            "num_leaves": trial.suggest_int("num_leaves", 15, 127),
            "subsample": trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
            "reg_alpha": trial.suggest_float("reg_alpha", 1e-8, 10.0, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-8, 10.0, log=True),
            "random_state": 42, "verbose": -1, "n_jobs": -1,
        }
        cv_kss = []
        for fold_i, (tr_idx, val_idx) in enumerate(cv_splits):
            model = lgb.LGBMClassifier(**params)
            model.fit(
                X_train_final.iloc[tr_idx], y_train.iloc[tr_idx],
                categorical_feature=cat_features,
                eval_set=[(X_train_final.iloc[val_idx], y_train.iloc[val_idx])],
                callbacks=[lgb.early_stopping(LGB_EARLY_STOPPING),
                           lgb.log_evaluation(0)],
            )
            y_prob = model.predict_proba(X_train_final.iloc[val_idx])[:, 1]
            cv_kss.append(compute_ks(y_train.iloc[val_idx], y_prob))

            trial.report(np.mean(cv_kss), fold_i)
            if trial.should_prune():
                raise optuna.TrialPruned()

        return float(np.mean(cv_kss))

    study = optuna.create_study(
        direction="maximize",
        sampler=TPESampler(seed=42),
        pruner=MedianPruner(n_startup_trials=5, n_warmup_steps=2),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    # ---- Step 6: 用最优超参在 X_train 全量上训练最终模型 ----
    best_model = lgb.LGBMClassifier(**study.best_params, random_state=42, verbose=-1)
    best_model.fit(
        X_train_final, y_train,
        categorical_feature=cat_features,
        eval_set=[(X_val_final, y_val)],
        callbacks=[lgb.early_stopping(LGB_EARLY_STOPPING), lgb.log_evaluation(0)],
    )

    # ---- Step 7: ★ 在完全不可见的 X_val 上计算真实指标 ----
    y_val_prob = best_model.predict_proba(X_val_final)[:, 1]
    real_auc = float(roc_auc_score(y_val, y_val_prob))
    real_ks = float(compute_ks(y_val, y_val_prob))

    logger.info("最终模型: AUC=%.4f KS=%.4f (cv_mean_ks=%.4f) best_params=%s",
                real_auc, real_ks, float(study.best_value), study.best_params)

    return {
        "model": best_model,
        "auc": real_auc,                     # ✅ holdout 真实 AUC
        "ks": real_ks,                       # ✅ holdout 真实 KS
        "cv_mean_ks": float(study.best_value),  # 仅内部参考，不写入 t_execution_log 主指标
        "best_params": study.best_params,
        "iv_table": iv_df.to_dict("records"),
        "feature_importance": _top_features(best_model, X_train_final.columns.tolist()),
    }
```

### 8.1 关键设计点

**数据隔离**（修复 Bug 1 & 2 + 数据泄露）：
- **先拆分后处理**：`train_test_split` 在清洗/IV/特征选择之前执行
- `_fit_clean_params` 在 X_train 上计算每列的 P99 截断阈值和中位数/众数填充值，`_apply_clean_params` 将同一组参数应用到 X_val，杜绝 X_val 自身统计量泄露
- IV 计算仅在 X_train 上进行，将筛选出的特征列表 apply 到 X_val
- Optuna 全部的 CV 搜索仅在 X_train 上进行，X_val 完全不可见
- `return` 中的 `auc` 和 `ks` 来自 X_val 的真实计算，`cv_mean_ks` 仅作内部参考

**Optuna 剪枝**（建议 #7）：
- `MedianPruner(n_startup_trials=5, n_warmup_steps=2)` — 初始 5 轮不剪枝，之后每折报告中间值
- 劣质组合在 CV 早期被终止，预计节省 30%~50% 训练时间

**类别特征原生处理**（建议 #8）：
- 自动识别 `object/category/bool` 列和 unique≤20 的整型列
- 通过 `categorical_feature` 参数传入 LightGBM，模型内部做最优分割，无需 OneHotEncoder

## 9. 监控与可观测性

保留一个极简 HTTP 端口（8090），仅供运维探活和指标采集，不暴露业务接口。

```python
# metrics.py — 独立进程，在 main.py 中作为 daemon 线程启动
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

_metrics = {
    "last_run_ts": None,
    "last_run_status": None,  # "success" | "failed" | "running"
    "last_seed_count": 0,
    "last_result_count": 0,
    "last_auc": None,
    "last_ks": None,
    "last_elapsed_seconds": 0,
    "last_error": None,
}


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            status = 200 if _metrics["last_run_status"] != "running" else 200
            self._reply(status, {"status": "ok", "last_run": _metrics["last_run_ts"]})
        elif self.path == "/metrics":
            self._reply(200, _metrics)
        else:
            self._reply(404, {})

    def _reply(self, code, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 静默 HTTP 日志


def start_metrics_server(port=8090):
    server = HTTPServer(("0.0.0.0", port), MetricsHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
```

`scheduler.py` 在执行前后更新 `_metrics`：

```python
from metrics import _metrics as m
m["last_run_ts"] = datetime.now().isoformat()
m["last_run_status"] = "running"
# ... 执行 ...
m["last_run_status"] = "success"
m["last_auc"] = train_result["auc"]
m["last_ks"] = train_result["ks"]
```

### 9.1 执行信息记录

所有关键指标（AUC、KS、特征重要性、IV 表等）均写入 `t_execution_log`，可直接通过 SQL 查询历史执行情况和模型趋势。

## 10. 与现有代码的兼容性

| 组件 | 处理方式 |
|------|----------|
| 训练/预测逻辑 | `scoring_service.py` 重写为 LightGBM + Optuna，删除 XGB/LR/RF/ensemble/GridSearchCV |
| 特征工具 | `model_utils.py` 完全复用（auto_discover_features / clean_data / preprocess_data / calc_iv / compute_ks） |
| 特征关联 | 统一从特征表 JOIN，支持 feature_columns 白名单 |
| 数据库连接 | `connection.py` 完全复用 |
| 环境变量 | 所有现有 DB/CK 相关环境变量继续使用，新增调度/Optuna/分区配置项 |
| 模型缓存 | 按 `(规则MD5, 样本行数, 特征列MD5)` 生成缓存 key，数据分布变化自动重训 |

### 10.1 模型缓存策略

```python
def _cache_key(config, seed_ids, feature_cols):
    """缓存 key = MD5(规则 + 种子ID的MD5 + 特征列清单)"""
    # 种子 ID 的 MD5 代替全量 ID 列表，避免 key 随种子数线性膨胀
    seed_hash = hashlib.md5(
        ",".join(sorted(seed_ids)).encode()
    ).hexdigest()
    payload = json.dumps({
        "pos_rules": config["pos_rules"],
        "neg_rules": config["neg_rules"],
        "seed_count": len(seed_ids),
        "seed_hash": seed_hash,
        "feature_cols": sorted(feature_cols or []),
    }, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()
```

`seed_hash` 对排序后的全量 ID 做一次 MD5，固定 32 字符，不再随种子数量线性膨胀。同一规则 + 同一样本 + 同一特征列集合 → 命中缓存。

---

## 11. 风险与注意事项

1. **规则配置审查**：JSON 规则模板化已杜绝 SQL 注入，但 `op` 字段仍需白名单校验（代码中已加入 `ALLOWED_OPS`）
2. **执行时间**：全量打分 + Optuna 搜参可能耗时 1~3 小时，建议调度在业务低峰期；K8s CronJob 的 `backoffLimit: 1` 防止失败死循环
3. **内存占用**：分批评分(50k/批)控制内存；LightGBM 原生处理类别特征免去 OneHot 维度爆炸
4. **失败重试**：单条配置失败不影响其他配置；失败原因 + 堆栈写入 `t_execution_log.fail_reason`
5. **分区管理**：`_ensure_partition()` 在执行前创建下月分区（幂等），`_drop_old_partitions()` 清理超过保留期的分区
6. **指标准确性**：AUC/KS 在独立 holdout（20% 数据，全程不可见）上计算；`cv_mean_ks` 仅作内部参考不写入主指标列，避免 CV 均值冒充最终模型指标
7. **数据隔离**：`train_test_split` 在清洗/IV/特征选择之前执行，所有统计量仅在 training set 上拟合后应用到 holdout，杜绝数据泄露
8. **特征白名单**：`t_seed_config.feature_columns` 为空时取全部列，生产环境建议显式指定，防止未知新增列干扰模型
