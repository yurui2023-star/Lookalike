# Lookalike Detailed Design — Optimization Notes

## Current target: v4.0 (monthly batch scoring + Sales CDP)

Preferred documents:

| Language | File |
|----------|------|
| 中文 | [`Lookalike_Detailed_Design_v4.0_BatchScoring.html`](./Lookalike_Detailed_Design_v4.0_BatchScoring.html) |
| English | [`Lookalike_Detailed_Design_v4.0_BatchScoring_EN.html`](./Lookalike_Detailed_Design_v4.0_BatchScoring_EN.html) |

Architecture source of truth: **MB Bank System Architecture Document_Lookalike V1.0**.

v2.1 Complete HTML remains archived for the former interactive Process/upload design:

- [`Lookalike_Detailed_Design_v2.1_Complete_ZH.html`](./Lookalike_Detailed_Design_v2.1_Complete_ZH.html)
- [`Lookalike_Detailed_Design_v2.1_Complete_EN.html`](./Lookalike_Detailed_Design_v2.1_Complete_EN.html)

The current FastAPI upload/Process implementation is **not** the production contract. See v4.0 §15 for the landing sequence.

---

## v2.1 → v4.0 (customer + SAD)

客户确认：取消 Lookalike 前端；模型预生产建好并发布后，每月定时对合格候选客群全量打分；结果按附件 schema 同步 Sales CDP。上传文件与基于 Segment 打分取消。

| # | 原设计 | v4.0 |
|---|--------|------|
| 1 | React 8 屏 / 业务 Dashboard | 无前端；仅调度任务 API |
| 2 | CSV 上传种子/候选 | Smart Sales 发布候选快照 |
| 3 | Segment 打分 | 资格圈选上收 Smart Sales |
| 4 | Create Process + Generate | `POST /api/v1/lookalike/runs` |
| 5 | 阈值/Top-K/导出在 Lookalike | 筛选与名单在 Smart Sales |
| 6 | 生产可训练 / PSI 自动重训 | 生产只加载已发布产物；PSI 只告警 |
| 7 | CDP 作为 P2 只读画像 | 主路径：幂等写入 CDP ClickHouse |
| 8 | 结果主键不清晰 | `lookalike_key = {run_batch_id}x{profile_id}` |
| 9 | 50M 全特征表 / 进程内线程池 | 3.0M–6.0M/产品；Task Service + Scoring Worker |
| 10 | Lookalike 可选推 CRM | 不直连 CRM |

「全量打分」= 当月候选快照中全部有效客户，不是全行客户主数据。

---

## v2.0 → v2.1 (historical)

完整优化文档（HTML，含图示）：[`Lookalike_Detailed_Design_v2.1_Optimized.html`](./Lookalike_Detailed_Design_v2.1_Optimized.html)

原文归档：[`Lookalike_Detailed_Design_v2.html`](./Lookalike_Detailed_Design_v2.html)

v2.0 业务对齐好但一期过重。v2.1 曾改为 MVP → P1 → P2。**P2 前端与交互式 Process 已被 v4.0 取消**，不再作为目标态。
