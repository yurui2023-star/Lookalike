# 打分域实验 - Personal Unsecured Loan (Super Fast Unsecured Loan)

> **不是 MB 项目数据。** `data/Bank_Marketing_Dataset.csv` 是仓库 API 联调用的公开样例（定期存款标签）。本文件只记录对该样例重采样后的脚本输出，**不得**当作 Lookalike 训练方案或分层模型规格的证据。

产品档案 `personal_unsecured_loan`；依据 BRD v2.4 s2.1.1 / s2.2.1; diagnosis v3.8 p23。

数据集 `data/Bank_Marketing_Dataset.csv` 分层占比与各层转化率按业务诊断 v3.8 重标定，仅用于检查代码路径能否跑通。

## 1. 四步筛选漏斗（诊断口径复现）

| step_id | name | rows_in | rows_out | step_retention | positive_retention |
| --- | --- | --- | --- | --- | --- |
| S0 | Compliance and eligibility exclusions | 100000 | 100000 | 1.0000 | 1.0000 |
| S1 | Product holding >= 2 | 100000 | 92512 | 0.9251 | 0.9497 |
| S2 | AUM > 0 and active in last 3 months | 92512 | 92512 | 1.0000 | 0.9497 |
| S3 | Age 18-60 and tenure > 0 months | 92512 | 87032 | 0.9408 | 0.8987 |

因演示数据缺列而跳过的条件（6 条，不会被当作通过）：`S0:employee_flag == 0`, `S0:test_account_flag == 0`, `S0:closed_account_flag == 0`, `S0:deceased_flag == 0`, `S0:blacklist_flag == 0`, `S0:holds_target_product == 0`

## 2. 打分域分层（全量客群，不是只有种子池）

| tier | customers | share | scorable | positives | rate_per_10k |
| --- | --- | --- | --- | --- | --- |
| tier_a_core | 254076 | 0.1313 | True | 3782 | 148.8531 |
| tier_b_extended | 1681188 | 0.8687 | True | 434 | 2.5815 |
| tier_c_not_scorable | 0 | 0.0000 | False | 0 |  |
| excluded | 0 | 0.0000 | False | 0 |  |

Tier A 占比 13.2%，对应 MB 的 4.72M / 35.72M；Tier B 即需要回答的 ~31M。

## 3. 时间外验证与 60/20/20 切分

- 开发期 cohort：2025Q1, 2025Q2, 2025Q3
- OOT cohort：2025Q4

| subset | rows | positives | positive_rate | groups |
| --- | --- | --- | --- | --- |
| train | 872270 | 1926 | 0.002208 | 43047 |
| validation | 290785 | 588 | 0.002022 | 14451 |
| test | 290742 | 643 | 0.002212 | 14374 |
| oot | 481467 | 1059 | 0.002200 | 23971 |

## 4. 两种训练样本构造

**A1/A2 仅用种子池**（1:10 负样本下采样 + 先验校正）：

| positives | negatives_before | negatives_after | requested_ratio | achieved_ratio | negative_sampling_rate | training_positive_rate |
| --- | --- | --- | --- | --- | --- | --- |
| 1720 | 112066 | 17200 | 10.000000 | 10.000000 | 0.153481 | 0.090909 |

**A3 全打分域分层抽样**（每层保留全部正样本，负样本按层抽样并赋 `N_h/n_h` 权重）：

| stratum | customers | positives | negatives | negatives_sampled | negative_sampling_rate | negative_weight |
| --- | --- | --- | --- | --- | --- | --- |
| tier_a_core|2 | 29594 | 316 | 29278 | 3160 | 0.1079 | 9.2652 |
| tier_a_core|3-4 | 66155 | 1035 | 65120 | 10350 | 0.1589 | 6.2918 |
| tier_a_core|5-6 | 17281 | 348 | 16933 | 3480 | 0.2055 | 4.8658 |
| tier_a_core|7+ | 756 | 21 | 735 | 400 | 0.5442 | 1.8375 |
| tier_b_extended|0-1 | 443146 | 91 | 443055 | 910 | 0.0021 | 486.8736 |
| tier_b_extended|2 | 68371 | 21 | 68350 | 400 | 0.0059 | 170.8750 |
| tier_b_extended|3-4 | 188161 | 66 | 188095 | 660 | 0.0035 | 284.9924 |
| tier_b_extended|5-6 | 56479 | 24 | 56455 | 400 | 0.0071 | 141.1375 |
| tier_b_extended|7+ | 2327 | 4 | 2323 | 400 | 0.1722 | 5.8075 |

## 5. 分层校准：预测率 vs 实际率

| strategy | segment | rows | actual_rate | predicted_rate | ratio |
| --- | --- | --- | --- | --- | --- |
| A1 pool-only | tier_a_core | 37770 | 0.015383 | 0.015319 | 0.995870 |
| A1 pool-only | tier_b_extended | 252972 | 0.000245 | 0.012654 | 51.629193 |
| A1 pool-only | ALL | 290742 | 0.002212 | 0.013000 | 5.878087 |
| A2 pool + tier calibration | tier_a_core | 37770 | 0.015383 | 0.013732 | 0.892681 |
| A2 pool + tier calibration | tier_b_extended | 252972 | 0.000245 | 0.000228 | 0.931968 |
| A2 pool + tier calibration | ALL | 290742 | 0.002212 | 0.001983 | 0.896469 |
| A3 whole-domain weighted | tier_a_core | 37770 | 0.015383 | 0.018873 | 1.226924 |
| A3 whole-domain weighted | tier_b_extended | 252972 | 0.000245 | 0.000421 | 1.719706 |
| A3 whole-domain weighted | ALL | 290742 | 0.002212 | 0.002819 | 1.274440 |
| A4 whole-domain + calibration | tier_a_core | 37770 | 0.015383 | 0.013645 | 0.887016 |
| A4 whole-domain + calibration | tier_b_extended | 252972 | 0.000245 | 0.000220 | 0.897421 |
| A4 whole-domain + calibration | ALL | 290742 | 0.002212 | 0.001964 | 0.888019 |
| A5 two models + shared scale | tier_a_core | 37770 | 0.015383 | 0.013732 | 0.892681 |
| A5 two models + shared scale | tier_b_extended | 252972 | 0.000245 | 0.000220 | 0.897421 |
| A5 two models + shared scale | ALL | 290742 | 0.002212 | 0.001975 | 0.893138 |

`ratio` = 预测率 / 实际率。仅用种子池训练的模型在 Tier B 上把转化率高估了一个数量级，这类分数无法与 Tier A 的分数放进同一个分数带。

## 6. 排序能力与整体指标（全打分域测试集）

| strategy | auc | ks | lift@20% | capture@20% | ece | predicted_rate | actual_rate | tail_share_of_top20% | tail_positive_capture |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A1 pool-only | 0.70499 | 0.32051 | 2.43388 | 0.48678 | 0.01079 | 0.01300 | 0.00221 | 0.77215 | 0.25806 |
| A2 pool + tier calibration | 0.90891 | 0.77560 | 4.60337 | 0.92068 | 0.00026 | 0.00198 | 0.00221 | 0.35046 | 0.17742 |
| A3 whole-domain weighted | 0.90239 | 0.77472 | 4.58005 | 0.91602 | 0.00068 | 0.00282 | 0.00221 | 0.35046 | 0.12903 |
| A4 whole-domain + calibration | 0.90251 | 0.77611 | 4.58005 | 0.91602 | 0.00025 | 0.00196 | 0.00221 | 0.35046 | 0.12903 |
| A5 two models + shared scale | 0.90681 | 0.77665 | 4.58005 | 0.91602 | 0.00027 | 0.00198 | 0.00221 | 0.35046 | 0.12903 |

## 7. 层内 lift（Top 20%）

| strategy | segment | total | positives | base_rate | lift | capture_rate |
| --- | --- | --- | --- | --- | --- | --- |
| A1 pool-only | tier_a_core | 37770 | 581.0000 | 0.0154 | 1.5232 | 0.3046 |
| A1 pool-only | tier_b_extended | 252972 | 62.0000 | 0.0002 | 1.4516 | 0.2903 |
| A2 pool + tier calibration | tier_a_core | 37770 | 581.0000 | 0.0154 | 1.5232 | 0.3046 |
| A2 pool + tier calibration | tier_b_extended | 252972 | 62.0000 | 0.0002 | 1.4516 | 0.2903 |
| A3 whole-domain weighted | tier_a_core | 37770 | 581.0000 | 0.0154 | 1.3425 | 0.2685 |
| A3 whole-domain weighted | tier_b_extended | 252972 | 62.0000 | 0.0002 | 1.5322 | 0.3065 |
| A4 whole-domain + calibration | tier_a_core | 37770 | 581.0000 | 0.0154 | 1.3425 | 0.2685 |
| A4 whole-domain + calibration | tier_b_extended | 252972 | 62.0000 | 0.0002 | 1.5322 | 0.3065 |
| A5 two models + shared scale | tier_a_core | 37770 | 581.0000 | 0.0154 | 1.5232 | 0.3046 |
| A5 two models + shared scale | tier_b_extended | 252972 | 62.0000 | 0.0002 | 1.5322 | 0.3065 |

## 8. OOT 稳定性（A4）

| strategy | auc | ks | lift@20% | capture@20% | ece | predicted_rate | actual_rate | tail_share_of_top20% | tail_positive_capture |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A4 whole-domain + calibration (OOT) | 0.90039 | 0.76763 | 4.57504 | 0.91501 | 0.00032 | 0.00192 | 0.00220 | 0.33568 | 0.15888 |

| segment | rows | actual_rate | predicted_rate | ratio |
| --- | --- | --- | --- | --- |
| tier_a_core | 63970 | 0.014882 | 0.013001 | 0.873624 |
| tier_b_extended | 417497 | 0.000256 | 0.000218 | 0.850478 |
| ALL | 481467 | 0.002200 | 0.001916 | 0.871286 |

## 9. KPI A/B 测试样本量（>= 1.2x 目标）

| scenario | baseline_rate | treatment_rate | per_arm | total |
| --- | --- | --- | --- | --- |
| PUL 池内 3 个月窗口 | 0.014310 | 0.017172 | 29689 | 59378 |
| PUL 池内 1 个月窗口 | 0.008580 | 0.010296 | 49835 | 99670 |
| Tier B 尾部 3 个月窗口 | 0.000250 | 0.000300 | 1726275 | 3452550 |

尾部人群单独做 A/B 需要的样本量高一个数量级，因此 Tier B 建议先做小比例探索投放（随机曝光样本）来收集无偏标签，而不是直接承诺 KPI。

