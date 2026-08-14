# 分层模型细化 - Personal Unsecured Loan (Super Fast Unsecured Loan)

输入：`MB_Bank_Lookalike_Feature_List_v1.2.xlsx`（69 核心 + 30 可选现金流）。
A 部分来自真实特征清单；B/C 部分在模拟客群上验证分层筛选与建模机制，特征名为演示数据集的列名。

## A. 特征目录（v1.2）

- 核心特征 69，可选现金流特征 30，合计 99
- 类别型特征 8，设了单调约束的特征 28
- **数据源为空、需 MB 确认的特征 11 个**：`investment_product_flag`, `loan_flag`, `casa_flag`, `term_deposit_flag`, `certificate_of_deposit_cds_flag`, `vietqr_customer_flag`, `vietqr_merchant_customer_flag`, `mbal_insurance_policy_flag`, `credit_card_flag`, `total_product_count`, `banking_service_count`

### A.1 交付依赖与分层计划特征数

| delivery | features | source_confirmed | tier_a_planned | tier_b_planned |
| --- | --- | --- | --- | --- |
| D1_mb_confirmed | 11 | 11 | 11 | 6 |
| D2_customer_master | 7 | 7 | 7 | 6 |
| D3_product_holding_source_tbd | 11 | 0 | 11 | 6 |
| D4_internal_assets_and_transactions | 24 | 24 | 24 | 4 |
| D5_app_events | 8 | 8 | 8 | 4 |
| D6_cic_external | 8 | 8 | 8 | 8 |
| D7_transaction_detail_optional | 30 | 30 | 30 | 0 |

### A.2 Tier B 预期覆盖矩阵（按特征组）

| group | high | medium | low | total | tier_b_planned |
| --- | --- | --- | --- | --- | --- |
| A. Demographics | 5 | 0 | 1 | 6 | 5 |
| B. Banking Relationship Depth | 2 | 8 | 8 | 18 | 10 |
| B. Banking Relationship Depth - MB Additions | 1 | 2 | 2 | 5 | 3 |
| C. Credit Behaviour Depth | 0 | 8 | 0 | 8 | 8 |
| C. Credit Behaviour Depth - MB Additions | 1 | 1 | 0 | 2 | 2 |
| D. Income & Financial Health | 0 | 0 | 8 | 8 | 0 |
| E. Transaction Behaviour Patterns | 0 | 1 | 7 | 8 | 1 |
| E. Transaction Behaviour Patterns - MB Additions | 0 | 0 | 2 | 2 | 0 |
| F. App Behaviour Depth | 0 | 4 | 4 | 8 | 4 |
| F. App Behaviour Depth - MB Additions | 1 | 0 | 1 | 2 | 1 |
| G. Consumer Credit Preferences | 0 | 0 | 2 | 2 | 0 |

### A.3 分层特征集规模

| 模型 | 全量交付后 | V1（仅 MB 已确认源 + 主数据 + 产品持有） | CIC 缺失时 |
| --- | --- | --- | --- |
| tier_a_core | 69 | 29 | 61 |
| tier_b_extended | 34 | 18 | 26 |

Tier B 相比 Tier A 少 35 个特征，删的全部是在单产品/休眠客群里预期覆盖过低的交易、消费、App 深度类字段。

## B. 分层特征筛选（在各层内部分别计算缺失率 / 单值率 / IV）

| tier | evaluated | kept | dropped | dropped_missing | dropped_identical | dropped_low_iv | dropped_absent | missing_indicators |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tier_a_core | 42 | 13 | 29 | 0 | 0 | 29 | 0 | 0 |
| tier_b_extended | 42 | 31 | 11 | 0 | 0 | 11 | 0 | 0 |

Tier A 保留 13 个、Tier B 保留 31 个；两层筛选结果不同的特征有 18 个。

### B.1 两层间 IV 差异最大的特征

| feature | iv_tier_a | iv_tier_b | tier_a | tier_b |
| --- | --- | --- | --- | --- |
| PreviousYearDeposit | 0.0080 | 0.1048 | drop | keep |
| AvgTransactionValue | 0.0341 | 0.1299 | keep | keep |
| AccountLengthYears | 0.0018 | 0.0958 | drop | keep |
| TenureWithBank | 0.0049 | 0.0933 | drop | keep |
| DaysSinceLastContact | 0.0013 | 0.0878 | drop | keep |
| CallResponseScore | 0.0258 | 0.1016 | keep | keep |
| Age | 0.0096 | 0.0771 | keep | keep |
| BranchVisitFrequency | 0.0029 | 0.0699 | drop | keep |
| InvestmentPortfolioValue | 0.0832 | 0.1473 | keep | keep |
| NumOnlineTransactions | 0.0041 | 0.0672 | drop | keep |

同一个特征在两层的 IV 可以相差一个数量级，这就是必须分层筛选、分层建模的原因。

## C. 两个分层模型的构建对照

参数差异：Tier A 用 `num_leaves=31 / min_data_in_leaf=40`（MB 量级建议 63 / 100）；Tier B 用 `num_leaves=15 / min_data_in_leaf=200 / min_sum_hessian_in_leaf=50 / lambda_l2=10`——尾部样本带 IPW 权重，必须用加权口径的 hessian 下限约束叶子，否则少数高权重样本会单独成叶。

M1 在 test 上评估；M2-M4 因尾部正样本稀少，在 test + OOT 合并集上评估，结论只用于机制验证，不代表 MB 真实水平。

| build | tier | features | rows | positives | auc | ks | lift@20% | capture@20% |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| M1 tier_a_core | tier_a_core | 13 | 37770 | 581 | 0.60577 | 0.18779 | 1.44578 | 0.28916 |
| M2 tier_b 全域加权 + 分层特征 | tier_b_extended | 31 | 670469 | 169 | 0.56802 | 0.12568 | 1.30177 | 0.26036 |
| M3 tier_b 仅尾部样本 + 分层特征 | tier_b_extended | 31 | 670469 | 169 | 0.57424 | 0.12914 | 1.62722 | 0.32544 |
| M4 tier_b 全域加权 + 全特征（对照） | tier_b_extended | 42 | 670469 | 169 | 0.56868 | 0.15108 | 1.15384 | 0.23077 |

## D. 组合打分（各层用自己的模型，校准到同一刻度）

尾部模型选用 **M3 tier_b 仅尾部样本 + 分层特征**（层内 lift 最高）。

| segment | rows | actual_rate | predicted_rate | ratio |
| --- | --- | --- | --- | --- |
| tier_a_core | 37770 | 0.015383 | 0.013693 | 0.890140 |
| tier_b_extended | 252972 | 0.000245 | 0.000231 | 0.942312 |
| ALL | 290742 | 0.002212 | 0.001980 | 0.895170 |

| 指标 | 取值 |
| --- | --- |
| 全域 AUC | 0.90842 |
| 全域 KS | 0.77564 |
| Lift@20% | 4.60337 |
| Capture@20% | 0.92068 |

