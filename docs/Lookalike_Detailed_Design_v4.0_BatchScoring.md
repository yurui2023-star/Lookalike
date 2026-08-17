# Lookalike 月度批处理打分服务 — 详细设计说明书 v4.0

| 项 | 内容 |
| --- | --- |
| 文档名称 | Lookalike 月度批处理打分服务详细设计说明书 |
| 版本 | 4.0 |
| 日期 | 2026-08-17 |
| 对齐文档 | MB Bank System Architecture Document_Lookalike V1.0 |
| 产品范围 | 白领贷（White-Collar Loan）、房屋易贷（Home Easy Loan） |
| 状态 | 草案 · 待架构与干系人评审 |

## 目录

1. 文档概述
2. 系统定位与边界
3. 逻辑架构
4. 月度批处理流程
5. 任务 API
6. 推理运行时
7. Sales CDP 结果 schema 与写入
8. 数据模型
9. 模型管理
10. 特征处理
11. 安全与审计
12. 非功能与容量
13. 测试设计
14. 待确认项与风险

## 1. 文档概述

### 1.1 目的

本文档给出生产 Lookalike 的实现级设计，覆盖任务接入、候选与特征读取、已发布模型加载、全量推理、结果写入 Sales CDP、状态管理与审计。

生产环境只执行推理。样本构建、特征筛选、模型训练、评估与审批发布在预生产完成。

### 1.2 全量打分定义

每月对 Smart Sales 按产品资格、活跃状态、黑名单与合规规则圈选并冻结的候选快照中，全部有效客户进行打分。推理过程不设相似度阈值，不做 Top-K 截断。筛选、分层与目标名单生成由 Smart Sales 在读取 CDP 分数后完成。

### 1.3 术语

| 术语 | 含义 |
| --- | --- |
| 候选快照 Candidate Snapshot | Smart Sales 按产品资格生成的冻结客户集合，是生产推理的打分总体 |
| 特征快照 Feature Snapshot | 与已发布模型 schema 对齐、以 T0 为截止的客户特征集 |
| 批处理运行 Scoring Run | 指定产品、评分月、候选快照与模型版本的一次受控推理 |
| run_batch_id | 一次运行的唯一标识，即 Lookalike process ID，对应 CDP 复合键左侧 |
| Profile ID | CDP 客户档案主键，逻辑对应 customer_id；与 HostCif / personal_cif 的物理映射待确认 |
| Lookalike score | 模型正类概率，区间 [0, 1] |
| Sales CDP | 分数存储与治理归属；物理存储为 CDP 所属 ClickHouse |
| Seed | 预生产训练正样本；生产运行不接收种子文件、不圈选种子 |

### 1.4 参考

| 文件 | 用途 |
| --- | --- |
| MB Bank System Architecture Document_Lookalike V1.0 | 系统边界、逻辑/数据/集成架构、容量与非功能需求 |
| Sales CDP 字段表 | 写入 CDP 的业务字段 |
| BRD v2.4 / BAD v3.2 | 业务目标、双产品、评估与验收 |
| Capacity Planning v2.3 | 单产品 3.0M / 4.5M / 6.0M、约 50 特征、4 小时窗口的性能测试基线 |

## 2. 系统定位与边界

### 2.1 定位

Lookalike 是独立的后端批处理推理服务，无前端。它不拥有客户、产品、候选池或 CRM 主数据。分数仅用于营销排序，不替代资格判定、反欺诈、授信审批、定价、押品估值或放款决策。

### 2.2 职责划分

| 能力 | 责任方 |
| --- | --- |
| 候选资格计算与候选快照发布 | Smart Sales |
| 月度调度、依赖检查、状态查询、受控重跑 | 调度平台 |
| 模型训练、验证、审批、发布 | 预生产 / 模型治理 |
| 加载已发布模型并对候选快照全量推理 | Lookalike |
| 客户级分数存储与治理 | CDP（ClickHouse） |
| 按分数筛选、分层、生成目标名单 | Smart Sales |
| 名单送达与 RM 跟进 | Smart Sales / CRM |

### 2.3 架构决策

| ID | 决策 |
| --- | --- |
| AD-01 | 无前端的后端批处理服务，与 Smart Sales 分离 |
| AD-02 | 一期仅白领贷、房屋易贷，模型独立，不做跨产品融合模型 |
| AD-03 | 银行提供种子；训练、验证、发布在预生产完成；生产只推理 |
| AD-04 | Smart Sales 计算并提供符合资格的候选池 |
| AD-05 | 调度平台按月发起产品级批处理 |
| AD-06 | 对全部有效候选打分，推理阶段无阈值、无 Top-K |
| AD-07 | 完整结果写入 CDP ClickHouse，按评分月与 run batch 隔离 |
| AD-08 | Smart Sales 选客后送 CRM；Lookalike 不调用 CRM |
| AD-09 | CRM 承载 RM 跟进；反馈字段与 KPI 归因待确认 |

### 2.4 端到端流程

```
Smart Sales 发布候选快照
        ↓
调度平台发起月度产品级运行
        ↓
Lookalike 校验请求、加载已发布模型、读取候选 ∩ 特征
        ↓
全量推理（无阈值 / 无 Top-K）
        ↓
幂等写入 CDP ClickHouse（staging → 对账后 published）
        ↓
Smart Sales 读取 published 批次，筛选分层后送 CRM
```

## 3. 逻辑架构

### 3.1 分层

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 外部编排 | 调度平台、Smart Sales | 触发运行；提供候选；消费分数 |
| 接入层 | Batch Task API | 任务接入、认证、参数校验、标准错误 |
| 任务控制层 | Run Orchestrator、Idempotency & Checkpoint、Task Metadata Store | 状态机、幂等、分片进度、失败恢复 |
| 模型运行时 | Model Resolver、Input Assembler、Scoring Worker | 加载产物、组装输入、产品级全量推理 |
| 数据适配层 | Candidate Reader、Feature Reader、Score Writer | 读取快照；幂等写入 CDP |
| 外部平台 | Model Registry、CDP ClickHouse、特征数据源 | 已发布模型；分数存储；特征快照 |

白领贷与房屋易贷使用独立 Worker、独立模型、独立批次与独立结果标识。

### 3.2 部署单元

| 单元 | 职责 | 技术基线 |
| --- | --- | --- |
| Batch Task Service | 任务接入、校验、状态、取消、重跑 | Python 3.12 + FastAPI + Uvicorn |
| Scoring Worker | 特征变换、模型加载、分片推理、checkpoint | 同运行时；按产品隔离；4 vCPU / 16 GiB 为性能测试单元 |
| Task Metadata Store | 运行状态、幂等键、checkpoint、重试关系 | 禁止仅存进程内存；组件待确认 |
| Model Cache | 已发布产物只读缓存 | 按产品 + 版本隔离；checksum 校验 |
| CDP ClickHouse | 客户级分数存储与查询 | CDP 负责集群、权限、分区、TTL |

### 3.3 生产镜像范围

生产镜像包含任务接入、批推理、数据访问与可观测性依赖。训练、EDA、报表、前端不进入生产镜像。

### 3.4 模块依赖

```
api/            → orchestrator, auth, schemas
orchestrator/   → metadata, workers, adapters
workers/        → model_runtime, feature_align, leakage_validate
adapters/       → candidate reader, feature reader, cdp writer
model_runtime   → artifact repo (read-only)
```

禁止反向依赖。生产代码路径不得导入训练或 EDA 模块。

## 4. 月度批处理流程

### 4.1 步骤与责任

| 步 | 责任方 | 处理 | 输出 |
| --- | --- | --- | --- |
| 1 | Smart Sales | 按白领贷 / 房屋易贷资格、活跃、排除名单、合规规则圈选 | 产品级候选快照 |
| 2 | 调度平台 | 校验上游依赖，发起产品级运行 | run_batch_id、评分月、产品、快照引用 |
| 3 | Lookalike | 校验请求、幂等键、快照、已发布模型 | 可执行运行记录 |
| 4 | Lookalike / 数据提供方 | 分批读取有效候选及模型所需特征 | 对齐后的打分输入 |
| 5 | Lookalike | 加载对应产品已发布模型，对全部有效候选推理 | 客户级完整分数 |
| 6 | Lookalike / CDP | 写入分数与追溯字段；对账后发布 | 按月 / 批次隔离的 CDP 表 |
| 7 | Smart Sales | 读取 published 分数，按业务规则选客 | 目标客户名单 |
| 8 | Smart Sales / CRM | 发送名单，记录 RM 跟进 | CRM 名单与跟进记录 |

### 4.2 运行状态

| 状态 | 含义 |
| --- | --- |
| PENDING | 已受理，尚未开始校验 |
| VALIDATING | 正在校验快照、模型与幂等 |
| RUNNING | 推理或写入进行中 |
| RETRYING | 瞬态错误自动重试（最多 3 次） |
| SUCCEEDED | 对账通过，批次已 published |
| FAILED | 不可自动恢复的失败 |
| CANCELLED | 受控取消 |

写入 ClickHouse 期间，内部区分为 staging（未发布）与 published（可被 Smart Sales 消费）。对调度平台，发布成功映射为 SUCCEEDED。

规则：

- 同一业务幂等键不得产生不受控的重复运行。
- 受控重跑使用新的 `run_batch_id`，并关联原批次，不得覆盖已发布历史。
- 对账失败不得将批次标记为可消费。
- 进程重启后根据 checkpoint 从已提交分片续跑，或安全失败并等待受控重跑。

## 5. 任务 API

接口仅供调度平台与运维系统身份调用。协议、认证、时区、回调方式待确认（P-01）。

### 5.1 约定

| 项 | 约定 |
| --- | --- |
| Base path | `/api/v1/lookalike` |
| 认证 | 系统间身份（mTLS 或平台颁发的服务凭证） |
| 字段 | JSON camelCase；元数据存储 snake_case |
| 幂等 | 请求头 `Idempotency-Key` 或 body `idempotencyKey` |
| 追踪 | 每个请求携带 `traceId` |

### 5.2 端点

| # | 方法 | 路径 | 功能 |
| --- | --- | --- | --- |
| 1 | GET | `/health` | 存活 / 就绪 |
| 2 | POST | `/api/v1/lookalike/runs` | 创建月度产品级打分运行 |
| 3 | GET | `/api/v1/lookalike/runs/{runBatchId}` | 查询状态、计数、错误、重试 |
| 4 | POST | `/api/v1/lookalike/runs/{runBatchId}/cancel` | 受控取消 |
| 5 | POST | `/api/v1/lookalike/runs/{runBatchId}/rerun` | 受控重跑（新批次，关联原批次） |

### 5.3 创建运行

`POST /api/v1/lookalike/runs`

```json
{
  "runBatchId": "WCL-202608-0001",
  "scoringMonth": "2026-08",
  "productCode": "WCL",
  "candidateSnapshotId": "SNAP-WCL-202608-01",
  "featureSnapshotId": "FEAT-202608-T0",
  "modelVersion": "wcl-lgb-3.2.0",
  "triggerTime": "2026-08-01T02:00:00+07:00",
  "traceId": "tr-8f3a...",
  "idempotencyKey": "WCL|2026-08|SNAP-WCL-202608-01|wcl-lgb-3.2.0"
}
```

`modelVersion` 可省略，此时解析该产品当前已审批且已发布的生效版本。响应返回 `state`、`resolvedModelId`、`resolvedModelVersion`。

### 5.4 状态查询

```json
{
  "runBatchId": "WCL-202608-0001",
  "state": "RUNNING",
  "productCode": "WCL",
  "modelId": "M-WCL-LGB",
  "modelVersion": "wcl-lgb-3.2.0",
  "candidateCount": 4500000,
  "validInputCount": 4488120,
  "scoredCount": 2100400,
  "writtenCount": 2100400,
  "published": false,
  "retryCount": 0,
  "checkpoint": { "shardId": "14", "offset": 50000 },
  "errorCode": null,
  "traceId": "tr-8f3a..."
}
```

### 5.5 错误码

| code | 含义 | 自动重试 |
| --- | --- | --- |
| 0 | 成功 | — |
| 1001 | 参数缺失或非法 | 否 |
| 2001 | 调用方身份无效 | 否 |
| 2002 | 无权调用该接口 | 否 |
| 3002 | 同一幂等键运行冲突，返回已有运行 | 否 |
| 4002 | 候选快照未就绪或不匹配 | 否 |
| 4003 | 特征快照或 schema 不匹配 | 否 |
| 5003 | 模型未审批或未发布 | 否 |
| 5004 | 产物 checksum 或签名失败 | 否 |
| 5101 | 瞬态读 / 写超时 | 是（≤3） |
| 5102 | 出入库对账失败 | 否 |
| 9999 | 未分类内部错误 | 按错误类型 |

## 6. 推理运行时

### 6.1 Worker 步骤

1. 按产品与版本从 Registry 加载已审批发布产物（模型、预处理、有序 feature_list、填补与截断阈值、schema 版本），校验 checksum / 签名。
2. 运行开始后冻结候选快照、特征快照与模型版本。
3. 候选 ID 与特征快照按统一客户标识 JOIN；校验必填字段、缺失策略与 schema；记录 valid / invalid。
4. 对入模列执行泄漏 denylist；命中则失败。
5. 按 50,000 行分片推理；`predict_proba` 正类概率为 Lookalike score，截断到 [0, 1]。
6. 每个有效候选都必须有分，不按分数过滤。
7. 分片幂等写入 CDP，更新 checkpoint。
8. `candidateCount`、`validInputCount`、`scoredCount`、`writtenCount` 对账通过后置 `published=true`。允许已文档化的 invalid 差额。

### 6.2 推理约束

- 不训练、不重算 IV、不动态重选特征。
- 不应用 similarity threshold，不截 Top-K，不生成营销名单。
- 打分总体是候选快照中的有效客户。
- 已发布模型规定 Observation Window 内无行为的客户记为 invalid，计入 `invalidInputCount` 并写原因码。

### 6.3 幂等与重跑

| 场景 | 行为 |
| --- | --- |
| 调度重复投递同一幂等键 | 返回已有 runBatchId，不新建 |
| Worker 崩溃后重启 | 从最近成功 checkpoint 续跑 |
| 受控 rerun | 新 runBatchId，rerunOf 指向原批次；原 published 行保留 |
| ClickHouse 短暂不可用 | 停止加压；退避重试 ≤3；超限 FAILED 并告警 |

### 6.4 产品隔离

白领贷与房屋易贷使用独立模型、特征定义、批次与结果标识。容量基线默认串行执行两个产品；并行须重算 Worker 配额。路由按 `productCode` 进入对应 Worker 池。

## 7. Sales CDP 结果 schema 与写入

Lookalike 为生产者，CDP 负责存储治理。物理表名、分区表达式、排序键、去重引擎、TTL 由 CDP 确认（P-04）。

### 7.1 业务字段

| 业务字段名 | 物理列 | 类型 | 来源 | 说明 |
| --- | --- | --- | --- | --- |
| Lookalike Model ID x Profile ID | lookalike_key | String | 拼接 | 复合唯一标识：process ID（run_batch_id）与 Profile ID 拼接 |
| Profile ID | profile_id | String | 候选快照客户主键 | 客户在 CDP 的档案 ID |
| Lookalike score | lookalike_score | Decimal(8,4) | 模型 predict_proba | 相似度分，[0, 1] |
| Model ID | model_id | String | 已发布模型元数据 | 模型标识 |
| Lookalike model | lookalike_model | String | 已发布模型元数据 | 模型名称 |
| product ID | product_id | String | 运行请求 productCode | 本次使用的产品 |

### 7.2 复合键

```
lookalike_key = "{run_batch_id}x{profile_id}"

示例：
run_batch_id = WCL-202608-0001
profile_id   = 000123456789
lookalike_key = WCL-202608-0001x000123456789
```

左侧使用 `run_batch_id`，不使用单独的 `model_id`。同一模型每月对同一客户再次打分，按运行批次隔离，避免跨月冲突，且重跑不覆盖已发布历史。

### 7.3 追溯列

与六字段写在同一张结果表：

| 列 | 说明 |
| --- | --- |
| run_batch_id | 与复合键左侧一致 |
| scoring_month | YYYY-MM |
| model_version | 已发布版本 |
| candidate_snapshot_id | 候选快照引用 |
| feature_snapshot_id | 特征快照引用 |
| scored_at | 打分时间 |
| trace_id | 追踪标识 |
| publish_status | staging / published / superseded |
| rerun_of | 受控重跑时指向原 run_batch_id |

逻辑唯一键：`product_id + run_batch_id + profile_id + model_version`。结果不得包含证件号、完整账号或明文特征宽表。

### 7.4 写入与发布

1. 按批次大小（建议 5,000）INSERT；同一 `lookalike_key` 幂等。
2. 写入时 `publish_status=staging`。
3. 全批对账成功后更新为 `published`。
4. 对账失败保持 staging，运行状态 FAILED / 5102，不向下游开放。

### 7.5 消费约定

- Smart Sales 只读 `publish_status='published'` 的批次。
- 阈值、分层、Top-N、排除已持仓等规则在 Smart Sales 执行。
- 向 CRM 发送目标名单不经过 Lookalike。

## 8. 数据模型

### 8.1 对象与权限

| 对象 | 责任域 | Lookalike 权限 |
| --- | --- | --- |
| 候选快照 | Smart Sales（物理位置待确认） | 只读 |
| 特征快照 | 数据域（待确认） | 只读 |
| 模型产物 | 模型治理 / Registry | 只读 |
| 运行元数据 / checkpoint | Lookalike | 读写 |
| 客户级分数 | CDP 存储治理；Lookalike 生产 | 授权写入 |
| 目标名单 / CRM 跟进 | Smart Sales / CRM | 无 |

### 8.2 运行元数据 scoring_run

| 列 | 说明 |
| --- | --- |
| run_batch_id | 主键，即 CDP process ID |
| scoring_month / product_code | 运行维度 |
| candidate_snapshot_id / feature_snapshot_id | 冻结输入 |
| model_id / model_version / artifact_checksum | 已发布模型 |
| state / retry_count / rerun_of | 状态机 |
| candidate_count / valid_input_count / scored_count / written_count | 对账计数 |
| checkpoint_json | 分片进度 |
| idempotency_key | 唯一 |
| error_code / error_message / trace_id | 失败与追踪 |
| started_at / completed_at | 时间 |

Lookalike 不以自建结果表作为 Smart Sales 消费源。Worker 本地暂存仅用于失败恢复，TTL 短，不对外查询。

### 8.3 ClickHouse 逻辑表

表名待 CDP 确认。

```sql
CREATE TABLE lookalike_customer_score
(
    lookalike_key            String,
    profile_id               String,
    lookalike_score          Decimal(8, 4),
    model_id                 String,
    lookalike_model          String,
    product_id               String,
    run_batch_id             String,
    scoring_month            String,
    model_version            String,
    candidate_snapshot_id    String,
    feature_snapshot_id      String,
    scored_at                DateTime,
    trace_id                 String,
    publish_status           LowCardinality(String),
    rerun_of                 String
)
ENGINE = /* 去重引擎待 CDP 确认 */
PARTITION BY scoring_month
ORDER BY (product_id, run_batch_id, profile_id);
```

## 9. 模型管理

### 9.1 预生产

1. 按产品定义种子、标签、候选范围、T0 与验收指标。
2. 构建训练、验证、OOT 数据集。
3. 完成 EDA、IV、缺失 / 同值筛选、P99 截断、泄漏审计，固化 Feature Spec 与 Model Recipe。
4. 训练 LightGBM（主模型）与可选 LR+WOE（解释基线），完成 OOT 与稳定性评估。
5. 治理审批：适用产品、版本、schema、checksum / 签名、生效时间、回滚版本。

质量门禁基线（最终以模型治理为准）：

- 缺失率 >95% 剔除
- 同值率 >95% 剔除
- IV <0.02 剔除
- 显著偏度按批准的 P99 截断
- 禁止使用 T0 之后才出现的结果、审批、放款或营销反馈字段

筛选与填补规则冻结进产物。生产只校验并执行，不重新筛选。

### 9.2 生产加载契约

```
PublishedArtifact
  model_id, model_name, product_code, model_version
  feature_schema_version
  feature_list[]          # 有序
  preprocessor            # 填补值、P99 阈值、编码水平
  estimator
  leakage_denylist[]
  checksum / signature
  approval_status == published
  effective_from / rollback_version
```

加载失败（未发布、产品不匹配、schema 不一致、checksum 失败）时任务 FAILED（5003 或 5004）。不得用缓存旧版本静默顶替，除非请求显式指定仍为 published 的回滚版本。

### 9.3 PSI 监控

上线后按月监控分数 / 特征 PSI。0.25 为当前参考基线，正式阈值待治理批准。超阈产生告警工单。生产不自动训练、不自动切换流量。新版本经预生产训练、OOT、审批发布后，由下一次月度调度加载。

## 10. 特征处理

生产使用与产物绑定的 Feature Spec 做 schema 对齐。

- 入模列命中泄漏 denylist（如 ResponsePropensity、放款后字段、证件号、CIF）则运行失败。
- 标签定义列不得进入特征矩阵。
- 候选资格字段若在快照内为常量，不得进入特征矩阵。
- 缺失填补与 P99 截断只执行产物内冻结规则。
- 逻辑主键 `customer_id` 写入 CDP 时映射为 `profile_id`。与 `personal_cif` / `HostCif` 的正式映射待确认（P-03）。

## 11. 安全与审计

- 无业务用户、无页面会话；调度平台、ClickHouse、Registry、运维平台使用分离的最小权限系统身份。
- 密钥不写入代码、镜像或依赖清单，使用银行批准的 Secret 机制。
- 传输使用批准的 TLS；静态加密由 CDP 与产物平台负责。
- 日志不得记录证件号、完整账号、明文凭证、模型 Secret 或不必要的特征值。
- 生产数据不得未授权复制到开发或外部。
- 审计事件覆盖触发、校验、模型加载、读写、状态变更、重试、失败；字段含 `run_batch_id`、`trace_id`、系统身份、结果、错误码。

## 12. 非功能与容量

### 12.1 容量基线

Capacity Planning v2.3 Lite，非正式生产配额。正式 SLA 以 3.0M 与 6.0M 等价测试为准。

| 场景 | 单产品候选 | Worker | 配额 |
| --- | --- | --- | --- |
| 低 | 3.0M | 2 × 4 vCPU / 16 GiB | 8 vCPU / 32 GiB |
| 期望 | 4.5M | 3 | 12 vCPU / 48 GiB |
| 高 | 6.0M | 4 | 16 vCPU / 64 GiB |

- 规划吞吐：500K 候选·模型 / 小时 / Worker（含读、组装、推理、回写），含 25% 余量。
- 单产品端到端窗口规划目标 ≤ 4 小时。
- 任务服务规划 2 vCPU / 4 GiB。
- 最终特征约 50 列。
- 存储逻辑估算（期望 / 高）：150 GB / 200 GB；分数约 0.25 KB / 客户 / 模型 / 月，保留 12 个月，待 CDP 确认。

### 12.2 可靠性

| 属性 | 目标 |
| --- | --- |
| 月度执行窗口可用性 | 参考 ≥ 99.5%（口径待运维确认） |
| 自动重试 | 仅瞬态错误，最多 3 次，退避 + 抖动 |
| 在途数据 | 已提交分片故障后不丢 |
| 安全 | 无高危；中危关闭 |

### 12.3 监控

| 域 | 指标 | 告警示例 |
| --- | --- | --- |
| 任务 | 排队、时长、状态、重试、成功率 | 月任务未启动、超时、反复重试 |
| 数据 | 候选数、有效数、缺失率、出入库差 | 候选异常、关键特征缺失、条数不一致 |
| 模型 | 版本、加载、分数分布、PSI | 未发布版本、加载失败、分布突变 |
| ClickHouse | 写入吞吐、失败率、分区大小 | 超时、拒绝、分区异常增长 |

结构化日志须可按 `run_batch_id`、`product_code`、`model_version`、`trace_id` 检索。

### 12.4 业务验收基线

最终测量定义待业务与模型治理确认。

| 产品 | 指标 | 目标 |
| --- | --- | --- |
| 白领贷 | Top 20% 名单响应率 | ≥ 历史同类 1.2 倍 |
| 房屋易贷 | Top 20% 合格线索率、申请转化率 | ≥ 历史同类 1.2 倍 |
| 两产品 | Lift@Top20% | 参考 ≥ 1.2 |
| 两产品 | AUC / KS | 在验证与 OOT 透明报告，不设硬门禁 |

## 13. 测试设计

| 层 | 重点 |
| --- | --- |
| 单测 | 复合键、幂等键、denylist、预处理与训练一致性、状态机 |
| 契约测试 | 调度必填字段；CDP 六字段 + 追溯列；未发布批次不可消费 |
| 集成 | 候选 ∩ 特征 JOIN、checkpoint 续跑、对账失败不发布、rerun 新批次 |
| 性能 | 3.0M 与 6.0M 端到端；记录 CPU、内存、读写、恢复时间、幂等 |
| 安全 | 依赖与镜像扫描、渗透；无高危 |

负例：模型未发布、schema 不一致、denylist 残留、重复投递、候选未就绪、ClickHouse 部分写入失败、用 `model_id` 构造复合键。

## 14. 待确认项与风险

### 14.1 待确认

| ID | 项 | 待确认内容 |
| --- | --- | --- |
| P-01 | 调度接口 | 协议、认证、执行日、时区、依赖、超时、回调、取消、重试、重跑 |
| P-02 | 候选交接 | 快照位置、完成标志、客户主键、产品代码、获取方式 |
| P-03 | 客户主键 | customer_id 与 personal_cif / HostCif 的映射 |
| P-04 | ClickHouse | 库表、类型、PARTITION BY、排序键、去重引擎、TTL、账号 |
| P-05 | 模型产物 | 平台、格式、审批状态、签名、缓存、回滚 |
| P-06 | 容量与 SLA | 实测吞吐、Worker 配额、正式批次窗口 |
| P-07 | 部署平台 | 环境、容器平台、实例、HA、Python 补丁版本 |
| P-08 | 网络与安全 | 分区、地址、端口、TLS、系统身份、Secret |
| P-09 | 监控告警 | 平台、阈值、告警组、响应 SLA |
| P-10 | 备份容灾 | 范围、频率、RPO / RTO、演练 |
| P-11 | Smart Sales–CRM | 名单对象、字段、幂等、失败处理 |
| P-12 | 业务反馈 | CRM / 渠道 / LOS 事件、归因窗口 |
| P-13 | 治理阈值 | 缺失率、同值率、IV、截断与 PSI 正式值 |

未确认项不得写成已实现能力。

### 14.2 风险

| ID | 风险 | 缓解 |
| --- | --- | --- |
| R-01 | 候选或特征快照未在窗口内就绪 | 调度依赖检查、就绪标志、受控重跑 |
| R-02 | 候选 / 特征 / 模型版本错配 | 冻结引用、schema 与 checksum 校验 |
| R-03 | 候选量超出 4 小时窗口 | 3.0M / 6.0M 性能测试，校准分片与并行策略 |
| R-04 | 重试产生重复分 | 复合键幂等、发布标志、出入库对账 |
| R-05 | 未发布模型进入生产 | 审批状态 + 签名；禁止静默回退 |
| R-07 | 分布漂移 | 月度 PSI 告警；治理后发布新版本 |
