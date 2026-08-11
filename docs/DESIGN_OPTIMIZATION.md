# Lookalike Detailed Design — Optimization Notes (v2.0 → v2.1)

完整优化文档（HTML，含图示）：[`Lookalike_Detailed_Design_v2.1_Optimized.html`](./Lookalike_Detailed_Design_v2.1_Optimized.html)

原文归档：[`Lookalike_Detailed_Design_v2.html`](./Lookalike_Detailed_Design_v2.html)

## 核心结论

v2.0 **业务对齐好**（双产品、71+24 特征、LR+LightGBM、阈值后置、全量打分），但 **一期过重**、与当前仓库脱节、存在技术口径不一致。v2.1 改为 **MVP → P1 → P2** 可演进方案。

## 12 项关键优化

| # | 问题 | 优化 |
|---|------|------|
| 1 | 一期全上 React/PG/Redis/Celery/MinIO | 分三阶段；MVP 沿用现有 FastAPI |
| 2 | 未对接现有代码 | 明确复用 `pipeline/` `features/` `modeling/` |
| 3 | XGBoost vs LR+LGB 矛盾 | 统一 LightGBM 主模型 + LR 基线 |
| 4 | 训练与打分耦合 | Offline Training / Online Scoring 分离 |
| 5 | 71 特征与 45 列 CSV 无映射 | Feature Adapter 桥接 |
| 6 | API FR 编号混乱 | 对齐 BRD FR-01~09，分阶段暴露接口 |
| 7 | Event Importance/RFM 过浅 | 补齐 R/F/M 定义 |
| 8 | scoring_result 存全量 JSONB | 默认只存 key+score；明细按需 |
| 9 | AI Insights 无约束 | 一期规则模板，二期再 LLM |
| 10 | 自建 JWT 不适配银行 | P1 企业 SSO/OIDC |
| 11 | Cold Start 无细则 | 无行为/无映射计数与过滤 |
| 12 | 缺业务验收包 | Lift@Top20% + A/B 清单 |

## 建议下一步

1. 仓库内增加 `adapters/`（CSV → 模型特征）与泄漏 denylist 单测  
2. P1：`process` / `process_version` + 异步打分  
3. P2：React 8 屏 + CDP/PMS + Recurring + Conversion  
