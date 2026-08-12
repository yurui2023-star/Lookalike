# 《Lookalike 定时打分服务 — 改造方案 v1.0》评估报告

> 评审对象：[`docs/DESIGN_PLAN.md`](DESIGN_PLAN.md)（v1.0，2026-08-12）。下文的 §N 一律指方案原文的章节号，本报告自身的章节写作"本报告 N"。
> 评审方式：静态审阅 + 可执行验证（`scripts/design_plan_checks.py`：13 项发现全部复现，3 项修复建议已实测通过）
> 验证环境：MySQL 系服务器（MariaDB 10.11）、LightGBM 4.7、`data/Bank_Marketing_Dataset.csv`（100,000 行）

---

## 1. 结论

**架构方向正确，但当前版本不能直接进入实现。** 方案里最重要的几个判断——用 run-once + 外部调度取代常驻服务、用 JSON 规则模板取代自由 SQL、把执行元数据落到 `t_execution_log`、按月分区管理结果表——都是对的，值得保留。问题出在这些判断的**落地细节**上：文档给出的示例代码被当作接近最终形态来呈现，而其中若干处在第一次运行时就会失败，另有几处会安静地产出错误结果。

三个层次的问题，按严重度排列：

| 层次 | 问题 | 后果 |
|------|------|------|
| **建模有效性** | 圈选规则用到的列仍然进入特征集 | 模型学到的是 WHERE 子句本身，AUC 1.0000，业务上完全无效（F-03） |
| **建模有效性** | 训练总体（规则限定）与打分总体（全量）不一致 | Top 10,000 线索里 71.2% 来自模型从未见过的分布（F-10） |
| **工程正确性** | 训练产物不完整、分区 DDL 起步即死、锁语义不符、写入非幂等 | 打分结果静默错误或作业直接失败（F-04/F-07/F-08/F-09） |
| **安全** | 列名未做白名单，"杜绝 SQL 注入"的结论不成立 | 有 `t_seed_config` 写权限即可以作业 DB 账号执行任意 SQL（F-01） |

其中 **F-03 是最关键的一条**：它不是 bug，是方案的建模范式本身缺了一个环节，而且它会以"AUC 0.99+，指标非常好"的形式出现在 `t_execution_log` 里，最容易被当成成功。

一个正面提醒：本仓库已经有针对同类问题的现成实现——`src/lookalike/adapters/leakage.py` 的泄露列黑名单与 `assert_no_leakage()` 硬失败机制，正是 F-03 需要的那类护栏，改造后应当保留并扩展，而不是随 `adapters/` 一起删掉。

### 采纳建议

下表的"章节"指方案原文，"见 …"指本报告的对应小节或发现编号。

| 方案章节 | 建议 |
|------|------|
| §2 架构总览（run-once + 外部调度） | **采纳**，但保留一个 CLI 入口（见 F-23） |
| §3 数据库设计 | **修改后采纳**，DDL 需重写（见本报告 6.1） |
| §4.2 种子圈选 | **重新设计**，规则编译器与取数方式都要改（见本报告 6.2） |
| §4.3 调度入口 | **修改后采纳**，锁与幂等性需重做（见本报告 6.4） |
| §8 训练流程 | **修改后采纳**，需补泄露排除、产物封装、目标函数（见本报告 6.3） |
| §9 监控 8090 端口 | **不采纳**，对 run-once 作业无效（见 F-20） |
| §10.1 模型缓存 | **不采纳**，方向反了（见 F-21） |

---

## 2. 如何复现本报告的证据

```bash
make install
.venv/bin/python scripts/design_plan_checks.py
```

脚本把方案 §4.2 的 `_rule_to_sql` **原样**复制过来（文件内以 `PLAN VERBATIM` 标注），因此失败的是方案本身而不是转述。输出分两段：`FINDINGS` 复现问题，`PROPOSED FIXES` 实测本报告 6.1 与 6.2 给出的替代实现确实可用——修复建议不是纸面推演。

数据库类检查需要一个可达的 MySQL/MariaDB（通过 `REVIEW_DB_*` 环境变量配置），缺失时标记 SKIP；`sqlalchemy` 缺失时绑定参数检查同样 SKIP。完整输出见 `docs/evidence/design_plan_checks.log`。

```
16 confirmed, 0 unconfirmed, 0 skipped
```

---

## 3. P0 — 阻断项

### F-01 列名是字符串拼接，"杜绝 SQL 注入"不成立

§3.1 与 §11.1 都声明规则模板化已经消除注入风险。实际上 `_rule_to_sql` 只参数化了**值**，列名走的是 f-string：

```python
parts.append(f"`{col}` {op} :{key}")
```

列名里的反引号会闭合标识符引用。把 `column` 设为 `` NetWorth` > 0 OR 1=1 --  ``，生成的片段是：

```sql
`NetWorth` > 0 OR 1=1 -- ` > :v_0
```

在真实服务器上执行（3 行样例表）：

```
the rule as written  (NetWorth > 999999999) -> 0 rows
same rule, hostile column name              -> 3 rows   过滤条件消失
subquery into an unrelated table            -> 3 rows   t_secret_salary 被读取
```

威胁模型这里需要说清楚：改造后没有 API、没有鉴权，`t_seed_config` 变成唯一的输入面。任何能 INSERT 这张表的人（配置管理员、运维脚本、被攻破的上游系统）都能以作业 DB 账号的权限执行任意 SQL。相比改造前有 JWT 保护的 API，这实际上是**扩大**了攻击面。

**修复**：列名必须来自 `information_schema.columns` 的实际列集合做白名单校验，不能靠正则；同时把作业 DB 账号收敛为特征表只读 + 结果表只写。参考实现见 §6.2。

### F-02 规则编译器在常规输入上崩溃或产出不可执行 SQL

四个独立缺陷，全部复现：

```
empty conditions              -> IndexError: list index out of range
nested group w/o 'logic' key  -> KeyError: 'column'
empty IN list                 -> `city` IN :v_0 with params {'v_0': ()}  渲染成 IN ()
sqlalchemy IN binding         -> OperationalError: near "?": syntax error
```

逐条说明：

- `parts[0]` 在 `conditions` 为空时越界。空规则是配置错误，但应该给出可读报错而不是 `IndexError`。
- 嵌套分组的判定条件写成了 `if "logic" in cond`。而 `logic` 在 `_rule_to_sql` 内部是 `rules.get("logic", "AND")`，即**可省略**。一个省略了 `logic` 的嵌套组会被当成叶子条件，`cond["column"]` 抛 `KeyError`。判定应该用 `"conditions" in cond`。
- 空 `IN` 列表渲染成 `IN ()`，MySQL 语法错误。
- 最要紧的一条：`IN :key` 这种写法在 SQLAlchemy 里**不工作**。`text()` 的绑定参数是标量占位符，传 tuple 需要显式声明 `bindparam(key, expanding=True)`。方案里所有 `IN` / `NOT IN` 规则——也就是 §3.1 示例中的 `{"column":"city","op":"IN","value":["北京","上海","深圳"]}`——运行时都会直接报错。

### F-03 圈选规则用到的列仍然入模，模型学到的是规则本身

这是全篇最重要的一条。

方案的正负样本由规则定义：正样本 `product_a_holding > 0`，负样本 `product_a_holding = 0`。而 `feature_columns` 为空时"取特征表全部列"，`product_a_holding` 因此同时是**标签的定义**和**模型的输入特征**。

用 `data/Bank_Marketing_Dataset.csv` 复刻这个配置（`HasMutualFunds` 对应产品持仓，`NetWorth` 对应 AUM）：

```
seeds: 4,318 positive / 12,210 negative
pos rule: HasMutualFunds='Yes' AND NetWorth>=100,000
neg rule: HasMutualFunds='No'  AND NetWorth>=100,000

方案行为（特征表全部列入模，101 个编码特征）:  AUC = 1.0000
排除规则列后（98 个编码特征）:                AUC = 0.7235
```

AUC 1.0000 不是模型好，是模型把 WHERE 子句背下来了。后果链条：

1. `t_execution_log.auc` 写入 0.99+，看起来是本月最成功的一次运行。
2. 打分时，所有 `product_a_holding = 0` 的候选人（即全部潜在新客）得分趋近 0。
3. `t_score_result` 的 Top-N 全是**已经持有产品 A 的存量客户**——恰好是这个 lookalike 模型唯一不该推荐的人群。
4. 没有任何一处会报错。

**修复**：规则编译器返回它引用到的列集合，这些列强制从特征集中剔除；同时对每一次训练做 AUC 上限告警（例如 `auc > 0.98` 视为疑似泄露而非成功）。本仓库 `src/lookalike/adapters/leakage.py` 的 `assert_no_leakage()` 已经是这个模式，把"规则引用列"并入它的黑名单即可。

### F-04 训练产物不完整，打分端无法复现训练变换

`train_lgb()` 返回：

```
model, auc, ks, cv_mean_ks, best_params, iv_table, feature_importance
```

`_score_all_candidates(feature_conn, train_result, seed_ids)` 需要：训练集上拟合的填充值与 P99 截断阈值（`clean_params`）、IV 筛选后的入模列清单**及其顺序**、类别特征的水平集合。**一个都没有返回，也没有持久化。** §8 的流程图里写了输出 `preprocessor`，但 §8 的代码没有。

两种失败模式都测了，都是静默的：

```
(1) 训练期填充未在打分端复现:
    12.0% 的行得分改变, mean |delta| = 0.0112, max |delta| = 0.5490
    Top 10,000 线索名单只保留了 8,821 人 (88.2%) —— 1,179 个线索被换掉

(2) 列顺序未固定（上游加列、或 SELECT * 顺序变化）:
    LightGBM 静默接受重排后的 DataFrame, mean |delta| = 0.2120
    Top 10,000 名单重合度仅 24.2%
```

第二条尤其危险：LightGBM 的 sklearn 接口不校验列名，只校验列**数量**。列数不变而顺序变化时它不报错，直接返回一份 76% 内容错误的线索名单，作业照常写库、照常记 `status=1`。

**修复**：把预处理参数和模型封成单个可序列化产物，打分端只允许通过它的 `transform()` 入口构造特征帧。参考实现见 §6.3。

### F-05 `neg_ratio` 以 `max_seeds` 为基数，正负比例失控

```python
neg_limit = int(max_seeds * neg_ratio)
```

`max_seeds` 是种子数量**上限**，不是实际正样本数。只要正样本规则匹配数少于 `max_seeds`（种子人群的常态），`neg_ratio` 就完全不起作用：

```
config: max_seeds=100,000, neg_ratio=2.0 （即请求 1:2）

方案 neg_limit = int(max_seeds * neg_ratio)    -> 4,318 : 12,210  = 1:2.8
修复 neg_limit = int(len(pos_ids) * neg_ratio) -> 4,318 :  8,636  = 1:2.0

生产规模下差距扩大：负样本池 3,000,000 行时
方案取 200,000 负样本对 4,318 正样本 = 1:46
```

1:46 的不平衡会让 LightGBM 在默认阈值下几乎不预测正类，也让 KS 目标函数的方差显著变大。

**修复**：`neg_limit = int(len(pos_ids) * neg_ratio)`，并把 `max_seeds` 的语义明确为"正样本上限"或"总量上限"二选一，写进列注释。

### F-06 holdout 同时用于 early stopping 和指标上报

§8.1 的原话是"`val_holdout` 在整个调参+训练过程中完全不可见"、"X_val 完全不可见"。但 §8 Step 6 的代码是：

```python
best_model.fit(
    X_train_final, y_train,
    eval_set=[(X_val_final, y_val)],
    callbacks=[lgb.early_stopping(LGB_EARLY_STOPPING), ...],
)
```

`X_val` 决定了最终的树数量，Step 7 又在同一批 `X_val` 上算 AUC/KS 写进 `t_execution_log`。这是标准的选择偏差。

实测（optimism = 作业上报的 holdout AUC 减去同一模型在 25,000 行从未接触过的样本上的 AUC，每档 40 次重采样）：

```
seeds=  400 (holdout  80 行):  方案 +0.0314 ± 0.0101   修复后 -0.0004 ± 0.0105
seeds=1,000 (holdout 200 行):  方案 +0.0123 ± 0.0074   修复后 -0.0001 ± 0.0073
seeds=4,000 (holdout 800 行):  方案 -0.0003 ± 0.0037   修复后 -0.0049 ± 0.0034
```

偏差随 holdout 缩小而单调放大，修复后的接线在三档上都归零——这正是选择偏差的特征。`SEED_MIN_COUNT` 默认 100，400 种子的配置完全在支持范围内，而那正是虚高最严重的区间。

**修复**：early stopping 用 `X_train` 内部再切出的一小块，或直接采用 Optuna CV 阶段得到的 `best_iteration` 均值；`X_val` 只用于最终上报。

顺带一提，§8 同时让 Optuna 搜索 `n_estimators` 又开 early stopping，两者互相抵消——early stopping 一旦生效，`n_estimators` 只剩上限的作用，30 次 trial 里有相当一部分算力浪费在一个无效维度上。建议固定 `n_estimators=3000`，让 `learning_rate` 和 early stopping 去决定实际树数，并把 `best_iteration_` 记入日志。

### F-07 结果表以单个 MAXVALUE 分区起步，`_ensure_partition()` 必然失败

§3.2 的 DDL 只定义了一个 `p_default VALUES LESS THAN MAXVALUE`。§11.5 说 `_ensure_partition()` 会"在执行前创建下月分区（幂等）"。用方案原文的 DDL 实测：

```
CREATE TABLE (完全按 §3.2)              -> exit=0
ALTER TABLE ... ADD PARTITION           -> exit=1
  ERROR 1481 (HY000): MAXVALUE can only be used in last partition definition
ALTER TABLE ... REORGANIZE PARTITION    -> exit=0   （这才是正确做法）
```

MAXVALUE 必须是最后一个区间，所以一旦它是唯一分区，就再也追加不了任何分区。首次运行即失败。

**修复**：预建若干月度分区 + 尾部 `p_max`，每月用 `REORGANIZE PARTITION p_max INTO (...)` 切出新月份并重建 `p_max`；执行前查 `information_schema.PARTITIONS` 保证幂等。

### F-08 `GET_LOCK` 是会话级锁，与连接池和 7200 秒超时的用法都不符

实测：

```
会话 A 持锁，会话 B（另一个池化连接）:
  从其它会话 RELEASE_LOCK      -> 0     （0 = 非持有者，静默无操作）
  锁依然被持有                  -> 47    （持有者的 connection id）
  GET_LOCK(name, 3) 阻塞了       3.0s 后才返回 0
会话 A 的连接关闭后 IS_USED_LOCK -> NULL  （随连接自动释放）
```

对应到方案的三个问题：

1. `acquire_lock(mysql_conn)` 和 `release_lock(mysql_conn)` 如果拿到的是连接池里**不同**的物理连接，释放是静默无操作。
2. 锁随连接消失。作业跑 1~3 小时，期间连接池的 `pool_recycle` 或网络中断都会让锁悄悄失效，防重入形同虚设。
3. `LOCK_TIMEOUT_SECONDS` 默认 7200。代码在拿不到锁时打的日志是"上一批次仍在执行中，本次跳过"，但它会先**阻塞两个小时**再打这行日志。意图与实现完全相反。

**修复**：锁用一条专用的、不进池的连接，整个作业期间持有；超时改为 `GET_LOCK(name, 0)` 立即返回；锁名按配置区分（`lookalike_scoring_{config_id}`）而不是全局一把；同时在 K8s 侧加 `concurrencyPolicy: Forbid` 作为更廉价的第一道防线。

另外 `execute_scoring()` 内部调用 `sys.exit(0)` —— 库函数里终止进程会跳过 `finally`、无法单元测试、也让调用方拿不到状态。应当返回状态码由 `__main__` 决定退出。

### F-09 结果写入非幂等，重试会产生重复批次

`batch_id = datetime.now().strftime("B%Y%m%d%H%M%S")` 在每次进程启动时生成。配合 `backoffLimit: 1`：第一次运行写了 60% 的 `t_score_result` 然后失败，重试用**新的 batch_id** 再写一遍，两批数据同时留在表里，下游无法判断哪一批是权威的。`t_execution_log` 也没有 `(batch_id, config_id)` 唯一键。

三个必要条件：

- **batch_id 确定化**：按业务归属月生成（`YYYYMM`），重试得到同一个 id。
- **写入幂等**：按 `(config_id, batch_date)` 先删后写，或用覆盖 PK 的 upsert。
- **完成标记**：`t_execution_log` 增加唯一键与完成状态，下游只读 `status=1` 的批次；建议再提供一个只暴露已完成批次的视图。

顺带：`_fail_log(mysql_conn, log_id, str(e))` 只存了异常消息。§11.4 承诺"失败原因 + 堆栈写入 `t_execution_log.fail_reason`"，实际堆栈丢失了，应改为 `traceback.format_exc()`，日志调用改用 `logger.exception`。

---

## 4. P1 — 重要项

### F-10 训练总体与打分总体不一致

示例配置的正负规则都带 `aum >= 1000000`。这意味着两件事同时成立：AUM 在训练集内部**没有区分度**（两侧都被钉死），而打分时模型却要面对大量 `aum < 1000000` 的客户。

```
训练总体: NetWorth >= 100,000  (100,000 人中的 16,528 人)
打分总体: 全部客户            (100,000 人)
训练总体之外的客户: 83,472 人 (83.5%)

Top 10,000 线索中，71.2% 来自训练总体之外
```

也就是说，最终线索名单里七成的人，模型从未在任何一个标签下见过他们所处的特征区间，得分完全来自树模型的外推。

**修复**：二选一，并写进设计文档而不是留给实现者判断。

- **限定打分域**：候选池施加与负样本规则相同的总体条件，模型只在它训练过的人群里排序。
- **随机负采样**（lookalike 的标准做法）：正样本 = 种子，负样本 = 从**真实候选池**里随机抽样的非种子客户。这样训练分布与打分分布天然一致，同时也顺带解决了 F-03——负样本不再由某个业务列的取反来定义。

推荐后者。它把 `neg_rules` 的角色从"定义负样本"降级为"限定候选池范围"，语义更清晰。

### F-11 `LIMIT` 无 `ORDER BY`，抽样有偏且不可复现

```sql
SELECT customer_id FROM `{table}` WHERE {pos_where} LIMIT {max_seeds}
```

没有 `ORDER BY` 的 `LIMIT` 返回的是存储引擎恰好先扫到的行，通常与主键顺序（开户时间、客户号段）强相关。当匹配数超过 `max_seeds` 时，拿到的是"最老的 N 个客户"而不是随机样本，且两次运行结果可能不同。

**修复**：按加盐哈希排序取数，既无偏、又恰好取到 N 条、还可复现：

```sql
SELECT customer_id FROM `{table}` WHERE {where}
 ORDER BY CRC32(CONCAT(customer_id, :salt))
 LIMIT :max_seeds
```

实测同一 salt 两次运行取到同一批客户，换 salt 得到另一批。`salt` 按 batch 固定并写入 `t_execution_log`，任何一次历史运行都能精确重放。若匹配集过大不宜排序，可退化为按比例过滤 `CRC32(CONCAT(customer_id, :salt)) % 1000 < :permille`，代价是拿不到精确的 N。

### F-12 正负样本规则可以重叠

两条规则各自独立编写，没有任何机制阻止它们相交。方案直接 `pd.concat`：

```
pos rule: NetWorth >= 100,000     neg rule: AnnualIncome >= 40,000
同时匹配两条规则的行: 14,421
concat 后: 67,862 行，14,421 个重复 customer_id，14,421 个客户同时带 1 和 0
```

同一个客户以两个相反标签参与训练，还会同时落进不同的 CV 折，污染验证集。

**修复**：`neg_ids = neg_ids - pos_ids`（正样本优先），并在种子数不足或重叠比例超阈值时让该配置失败而不是继续。

### F-13 三次查询 + 巨型 `IN` 列表

`select_seeds()` 扫三遍特征表：查正样本 id、查负样本 id、再用 `customer_id IN (...)` 回捞特征。第三步的 `IN` 列表可能有几十万个元素。

除了拼串本身的开销，MySQL 8 的 range optimizer 有 `range_optimizer_max_mem_size`（默认 8MB）限制，`IN` 列表超限时优化器会**放弃索引、退化为全表扫描**并只发一条 warning。在千万级特征表上这是分钟级到小时级的差别。

**修复**：一次扫描搞定，顺带解决 F-12 的重叠：

```sql
SELECT f.*, 1 AS label FROM `t_customer_features` f WHERE {pos_where}
UNION ALL
SELECT f.*, 0 AS label FROM `t_customer_features` f
 WHERE {neg_where} AND NOT COALESCE(({pos_where}), 0)
```

`COALESCE` 不能省。SQL 的三值逻辑下，正样本规则引用的列若为 NULL，`{pos_where}` 求值为 NULL，`NOT NULL` 仍是 NULL，该行会被**静默排除在负样本之外**。实测三行样例（`holding` 取 5 / 0 / NULL）：`NOT (holding > 0)` 只返回 `c2`，`NOT COALESCE((holding > 0), 0)` 返回 `c2, c3`。特征表里 NULL 很常见，这一字之差会让负样本系统性地缺失一整类客户。

种子量大时改为把种子 id 物化进临时表再 JOIN。

### F-14 `feature_columns` 为空时拼出的 SQL 是语法错误

```python
cols = ", ".join(...) if feature_cols else "*"
... text(f"SELECT customer_id, {cols} FROM `{table}`")
```

`feature_columns` 为 NULL 时展开成 `SELECT customer_id, * FROM ...`：

```
SELECT customer_id, * FROM `t_feat`          -> exit=1
  ERROR 1064 (42000): You have an error in your SQL syntax ... near '* FROM `t_feat`'
SELECT customer_id, `t_feat`.* FROM `t_feat` -> exit=0
```

而 §3.1 明确把"为空或 null 时取特征表全部列"列为受支持的默认行为。也就是说默认路径从来跑不通。

**修复**：用 `` `{table}`.* ``，或者（更好）总是显式解析列清单——反正 F-01 的白名单校验和 F-04 的列顺序固定都需要这份清单。

### F-15 类别特征缺少 dtype 契约

`_detect_categorical_columns()` 返回 `object` dtype 的列并交给 `categorical_feature`，但两端都会报错：

```
object dtype 上 fit + categorical_feature
  -> ValueError: pandas dtypes must be int, float or bool.
先用 category dtype 训练，再对 object dtype 的打分帧 predict
  -> ValueError: train and valid dataset categorical_feature do not match.
训练时不存在的类别值('Antarctica')得分 0.139-0.654，没有任何告警
```

需要澄清一点以免过度修改：LightGBM **会**按 pandas category 的**标签**正确重映射，所以逐批 `astype('category')` 本身不是缺陷。缺陷在于方案从头到尾没有固定过 dtype 和水平集合，也没有把它们从训练带到打分。

**修复**：水平集合进 F-04 的产物包，`transform()` 里统一用 `pd.Categorical(col, categories=levels)`；未见类别映射为 NaN 并统计其占比，占比超阈值时告警（这通常意味着上游枚举变更）。

### F-16 没有模型质量闸门，也没有漂移监控

一个无人值守的月度作业，AUC 掉到 0.52 时会发生什么？按现在的流程：照常全量打分、照常覆盖写入全量结果、`t_execution_log.status` 记 1（成功）、下游照常按这份名单去做营销触达。没有任何一个环节会拦住它。

对无人值守作业来说这是**最重要的一项运维补充**。建议：

- **绝对闸门**：`auc < MIN_AUC`（如 0.60）或 `ks < MIN_KS` 时该配置判失败，**保留上月结果**，不覆盖。
- **相对闸门**：与上一批次相比 AUC 跌幅超过阈值（如 0.05）时告警，因为绝对值达标不代表没有退化。
- **上限闸门**：`auc > 0.98` 视为疑似泄露（见 F-03）而不是成功。
- **特征漂移**：对入模特征计算与上月的 PSI，超阈值时在日志中标注并告警。
- **样本量漂移**：种子数、候选池量相对上月的变化率——上游 ETL 挂掉最常见的表现就是数据量突降。

### F-17 全量 rank 需要全局排序，与分批打分和内存约束冲突

§4.1 写的是"排序 + rank → 批量写入"，§11.3 说"分批评分(50k/批)控制内存"。这两件事互相矛盾：rank 是全局量，必须等所有批次打完才能确定，也就意味着全量分数要先攒在内存里。千万级客户下这是 GB 级的常驻占用，而 `_score_all_candidates` 的返回值 `all_scores` 正是这么用的。

**修复**：分批打分直接落 staging 表，全部完成后在库内用窗口函数一次算 rank 和分位：

```sql
INSERT INTO t_score_result (config_id, batch_date, customer_id, similarity_score,
                            score_rank, score_pct)
SELECT :config_id, :batch_date, customer_id, similarity_score,
       RANK()       OVER (ORDER BY similarity_score DESC),
       PERCENT_RANK() OVER (ORDER BY similarity_score DESC)
  FROM t_score_staging WHERE run_id = :run_id;
```

Python 侧内存与候选池规模解耦，同时 `seed_ids` 的排除也从巨型 `NOT IN` 变成 staging 表的 anti-join。

### F-18 KS 作为调参目标与 lookalike 的业务目标不匹配

`objective()` 最大化 CV-KS。KS 衡量的是整个分数域上正负累积分布的最大间距，而 lookalike 的实际用法是**取排名最靠前的 N 个人去触达**——只有头部那一段重要。KS 的最优点常常落在分布中部，与头部精度不是一回事，而且 KS 作为一个 max 统计量比 AUC 噪声更大，在小种子集上尤其不稳。

**修复**：目标函数改为业务对齐的指标，优先级依次为 `lift@top5%`、PR-AUC（average precision）、AUC。KS 仍然可以算、可以记进 `t_execution_log`，只是不适合当优化目标。

### F-19 字段类型容量不足

```
similarity_score DECIMAL(8,4) 在 100,000 个分数上:
  100,000 个不同取值坍缩为 7,616 个
  -> 92,384 行 (92.4%) 与其它行同分，`rank` 不再确定

43 个特征的分箱级 IV 表 = 74,720 字节 (~1,738 字节/特征)
  -> TEXT (65,535 字节) 在约 37 个特征处溢出

INSERT neg_ratio=20.0 into DECIMAL(3,2) -> ERROR 1264: Out of range value
INSERT 70,000 bytes into TEXT           -> ERROR 1406: Data too long
```

- `similarity_score DECIMAL(8,4)`：对 0~1 概率而言整数位是浪费的，小数位又不够。92.4% 的行同分意味着 `rank` 在两次运行间可以完全不同。改 `DECIMAL(9,8)`。
- `iv_table TEXT` / `fail_reason TEXT`：分箱级 IV 表和完整堆栈都会超 64KB。非严格模式下会**静默截断**，严格模式下报错。改 `MEDIUMTEXT` 或 `JSON`。
- `neg_ratio DECIMAL(3,2)`：上限 9.99，写 20 会报错。改 `DECIMAL(6,2)`。
- `` `rank` `` 在 MySQL 8.0 是保留字（MariaDB 不是）。DDL 里加了反引号，但后续任何手写 SQL 或 ORM 映射都会踩坑。改名 `score_rank`。

---

## 5. P2 — 建议项

### F-20 8090 监控端口对 run-once 作业无效

§9 让 `main.py` 起一个 daemon 线程暴露 `/health` 和 `/metrics`。但 §7 明确说程序是 run-once-and-exit，一个月只运行 1~3 小时。Prometheus 在其余 99.6% 的时间抓到的是 `up=0`，而抓不到的那些时间点恰好没有任何业务含义。批作业的正确做法是 **Pushgateway**（作业结束时推一次），或者干脆不要额外端口——`t_execution_log` 已经有全部指标，对"当月没有 status=1 的记录"这一条件告警就够了，还更可靠。

同一节还有两处：`MetricsHandler.do_GET` 里 `status = 200 if _metrics["last_run_status"] != "running" else 200` 两个分支都是 200，是段死代码；`metrics.py` 的注释写"独立进程"，实际以线程启动。

另外 §6 的目录说明写 `main.py # 程序入口 + 启动 metrics 线程`，§7 又说"`main.py` 简化为仅负责日志初始化的入口"，两处矛盾，且 `main.py` 与 `scheduler.py` 都自称入口。建议只保留一个入口。

### F-21 模型缓存方向反了

§10.1 的缓存 key 包含全量种子 ID 的 MD5。月度作业跑在活数据上，种子集合每月都变，**缓存永远不会命中**。而它唯一会命中的场景——上游 ETL 挂了、数据与上月完全一致——恰恰是最应该重新训练并告警的时候。同时 CronJob 的 Pod 文件系统是临时的，缓存存哪里也没有定义。

真正的需求不是"跳过训练"，是**可审计、可复现**。建议删掉缓存，改为按 `(config_id, batch_id)` 持久化训练产物（F-04 的产物包 + 规则快照 + 特征清单 + 库版本）到对象存储，用于事后追溯与线上问题复盘。

至于省时间：真正值得省的是 Optuna。30 trials × 5 折 = 每个配置每月 150 次拟合，而月度稳定 schema 下超参基本不动。建议默认复用 `t_execution_log` 里上一批次的 `best_params`（用 `study.enqueue_trial()` 作为热启动），只在季度或检测到漂移时做完整搜索。

### F-22 CronJob 清单缺关键字段

```yaml
schedule: "0 2 1 * *"   # 每月1日凌晨2:00
```

K8s CronJob 默认按 **UTC** 解释。注释写的"凌晨 2:00"如果指北京时间，需要 `spec.timeZone: "Asia/Shanghai"`（K8s 1.27+ GA），否则实际是北京时间上午 10 点——业务高峰期，正好与 §11.2"建议调度在业务低峰期"相反。同理，`datetime.now()` 生成 batch_id 时容器 TZ 通常是 UTC，批次号会和业务日差 8 小时。

其它缺失项：

- `concurrencyPolicy: Forbid` — 比 DB 锁更廉价的第一道防线。
- `activeDeadlineSeconds` — 否则挂死的作业会一直占资源。
- `startingDeadlineSeconds`、`ttlSecondsAfterFinished`、`successfulJobsHistoryLimit`。
- `resources.requests/limits` — 这是个数 GB 内存的作业，没有 request 会被随意调度和 OOMKill。
- `env` 里直接写 DB 连接信息应改用 `secretKeyRef`。
- LightGBM 的 `n_jobs=-1` 读的是宿主机核数而不是 cgroup 配额，在受限 Pod 里会超额起线程导致颠簸。应显式设为 CPU limit。

另外，§11.2 估计单次 1~3 小时，而 §4.1 是**串行遍历所有配置**。5 个配置就是 5~15 小时，超过 `LOCK_TIMEOUT_SECONDS` 也超过任何合理的 `activeDeadlineSeconds`。建议**一个配置一个 CronJob**：天然并行、故障隔离、可独立重试、资源可按配置规模分别设定。

### F-23 删光 API 和 tests 的代价

删掉 HTTP 层的理由成立，但要意识到失去了什么：单独重跑某个配置、上线前校验一条规则、查某批次为什么失败、导出某个人群包。方案自己也需要这些能力——`DRY_RUN` 环境变量和"手动触发"其实就是 CLI 的雏形。

建议把它做成真正的 CLI 而不是环境变量开关，成本极低：

```bash
python -m lookalike_service run --config-id 3
python -m lookalike_service run --config-id 3 --dry-run
python -m lookalike_service validate-rule --config-id 3   # 只编译规则并 EXPLAIN，不执行
python -m lookalike_service backfill --config-id 3 --batch 202607
```

`tests/` 更不该整体删除。方案里 bug 最密集的地方——规则编译器、负样本采样、预处理的 fit/apply 配对——全都是**纯函数**，是最容易写单元测试的部分。规则编译器尤其应该有一组 golden test（含 F-01 的恶意列名用例）。删了测试再重写，中间的空窗期正好是这个月度作业最容易静默出错的时候。

### F-24 分数跨批次不可比

模型每月重训，`similarity_score` 是新模型的输出概率。同一个客户行为没变，得分也可能从 0.30 跳到 0.70。下游如果按"score > 0.8"取人，每月拿到的人数会剧烈波动。

方案存了 `rank`，这是对的。建议再补一列 `score_pct`（同批分位），并在文档里明确写清楚：**原始分数不具备跨批次可比性，下游必须按 rank 或分位取数**。若确实需要可比的绝对分，则要引入固定参照集上的等距/保序校准。

### F-25 规则变更没有版本快照

`t_seed_config` 可以随时被 UPDATE，而 `t_score_result.config_id` 是外键式引用。规则一改，历史批次的含义就静默变了，事后无法解释"上个季度那批线索到底是怎么圈出来的"——在银行的模型治理语境下这是个合规问题。

建议 `t_execution_log` 增加 `rules_snapshot JSON` 和 `rules_hash CHAR(32)`，把当次实际使用的规则原文落库；`t_seed_config` 加 `version` 字段，UPDATE 改为插入新版本。

### F-26 代码卫生

文档中的代码以接近最终形态呈现，这些就值得一并指出：

- `scheduler.py` 用了 `text()` 但没导入；`os`、`time` 导入未使用；`select_seeds` 用了 `build_in_clause`、`get_feature_table` 但未导入或定义。
- `_fit_clean_params` 引用 `SENTINEL_COLS`、`MANUAL_EXCLUDE_COLS`、`pd`，都不在 §8 的导入列表里。
- `_load_active_configs`、`_init_log`、`_complete_log`、`_fail_log`、`_score_all_candidates`、`_ensure_partition`、`_drop_old_partitions`、`_top_features` 均被调用但未给出。
- `SEED_MIN_COUNT` 定义了却硬编码成 `if len(train_df) < 100`；`OPTUNA_STORAGE`、`DEFAULT_NEG_RATIO`、`DEFAULT_MAX_SEEDS`、`RESULT_RETENTION_MONTHS` 定义后未被引用。
- `len(train_df) < 100` 判的是总行数。`train_test_split(stratify=)` 要求每类至少 2 个，`StratifiedKFold(5)` 要求每类至少 5 个。应该判**少数类**样本数。
- `_detect_categorical_columns` 的阈值，§8 流程图写"unique≤50"，§8.1 说明写"unique≤20"，两处不一致。
- `feature_df.merge(seed_df[["customer_id","label"]], ...)`：特征表若自带 `label` 列会产生 `label_x`/`label_y`。
- `t_seed_config.config_name` 无唯一约束；`t_execution_log` 无索引也无 `(batch_id, config_id)` 唯一键。
- `t_score_result` 上三个二级索引（`idx_batch`/`idx_config`/`idx_customer`）对一张"每月写一次、按批次读"的表来说过重，写放大明显。`idx_customer` 尤其可疑：分区表上的索引是本地索引，只按 `customer_id` 查会扫遍所有分区。除非确有单客户点查场景，否则应去掉。

### 未决问题（需要方案作者确认）

1. **`t_customer_features` 到底在 MySQL 还是 ClickHouse？** §2 说 `db/connection.py`（MySQL + ClickHouse 连接池）保持不变，但 §4.2 的取数 SQL 用的是 MySQL 语法，§4.3 的锁是 MySQL 的 `GET_LOCK`。如果特征表在 ClickHouse，取数、`IN` 绑定、抽样函数全都要改写；如果打分结果有千万级，逐行写 MySQL 也是错误的选择。这个问题会影响 §4 的大部分实现。
2. **`NON_FEATURE_COLS` 是否包含 `LABEL_COLUMN`？** §8 Step 1 是 `X_full = train_df.drop(columns=exclude_cols)`，其中 `exclude_cols` 只来自 `NON_FEATURE_COLS`。如果标签列不在其中，标签会直接留在特征矩阵里，AUC 恒为 1.0。需要确认并显式 drop。
3. **候选池的定义是什么？** §4.1 只写了"候选池排除种子"。是全行库客户、还是某个营销可触达子集？这个定义直接决定 F-10 的修复方式。
4. **月度重训的评估口径。** 随机切分的 holdout 衡量的是同一份快照内的可分性，不是"这个月训的模型下个月表现如何"。是否要保留上一批次 Top 分位的实际转化回流，做真正的时间外验证？这也决定了 §3.4 中 `t_task_result` 是否真的可以废弃。

---

## 6. 修订建议

### 6.1 修订后的结果表 DDL

```sql
CREATE TABLE t_score_result (
    config_id         INT           NOT NULL,
    batch_date        DATE          NOT NULL COMMENT '批次归属日（业务时区），分区键',
    customer_id       VARCHAR(64)   NOT NULL,
    similarity_score  DECIMAL(9,8)  NOT NULL COMMENT '0~1，不具备跨批次可比性',
    score_rank        INT UNSIGNED  NOT NULL COMMENT '同批降序排名，从 1 开始',
    score_pct         DECIMAL(7,6)  NOT NULL COMMENT '同批分位，跨批次可比，下游按此取数',
    PRIMARY KEY (config_id, batch_date, customer_id),
    KEY idx_rank (config_id, batch_date, score_rank)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
PARTITION BY RANGE COLUMNS(batch_date) (
    PARTITION p202608 VALUES LESS THAN ('2026-09-01'),
    PARTITION p202609 VALUES LESS THAN ('2026-10-01'),
    PARTITION p_max   VALUES LESS THAN (MAXVALUE)
);
```

要点：主键即幂等键，重跑同一批次是覆盖而不是追加（F-09）；主键包含分区列，满足 MySQL 分区约束；去掉自增 `id` 省一个二级结构；`RANGE COLUMNS(batch_date)` 比 `TO_DAYS()` 更直观且同样支持分区裁剪；尾部保留 `p_max` 让 `REORGANIZE` 可行（F-07）。

这段 DDL、下面的分区维护语句、F-17 提到的窗口函数写入，以及"跑两遍不产生重复行"，都在真实服务器上验证过（脚本中的 `FIX-2`）。

月度维护（幂等）：

```sql
-- 建下月分区：先查 information_schema.PARTITIONS 确认不存在，再切分 p_max
ALTER TABLE t_score_result REORGANIZE PARTITION p_max INTO (
    PARTITION p202610 VALUES LESS THAN ('2026-11-01'),
    PARTITION p_max   VALUES LESS THAN (MAXVALUE)
);
-- 清理过期：DROP PARTITION 而不是 DELETE
ALTER TABLE t_score_result DROP PARTITION p202508;
```

`t_execution_log` 的对应调整：

```sql
ALTER TABLE t_execution_log
    ADD COLUMN rules_snapshot JSON        NOT NULL COMMENT '本次实际使用的规则原文',
    ADD COLUMN rules_hash     CHAR(32)    NOT NULL,
    ADD COLUMN feature_list   JSON        NOT NULL COMMENT '入模特征及顺序',
    ADD COLUMN sample_salt    VARCHAR(32) NOT NULL COMMENT '抽样盐值，用于重放',
    ADD COLUMN best_iteration INT         NULL,
    ADD COLUMN psi_max        DECIMAL(8,6) NULL COMMENT '与上批次的最大特征 PSI',
    MODIFY COLUMN iv_table    MEDIUMTEXT,
    MODIFY COLUMN fail_reason MEDIUMTEXT,
    ADD UNIQUE KEY uk_batch_config (batch_id, config_id);
```

### 6.2 修订后的规则编译器

下面这版在脚本中以 `FIX-1` 实测：恶意列名、未知列、空 `conditions`、空 `IN` 列表四种输入全部被 `RuleError` 拦下；省略 `logic` 的嵌套组正确编译；`IN` 规则带 expanding bindparam 后可以真正执行。

```python
import re
from sqlalchemy import bindparam, text

ALLOWED_OPS = {"=", "!=", ">", "<", ">=", "<=", "IN", "NOT IN", "LIKE",
               "IS NULL", "IS NOT NULL"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class RuleError(ValueError):
    """配置错误，应导致该配置失败并写入 fail_reason。"""


def compile_rule(node, allowed_columns, state):
    """把 JSON 规则编译成参数化 WHERE 片段。

    allowed_columns 来自 information_schema.columns，是唯一可信的列名来源。
    state 累积 {"params": {}, "expanding": [], "columns": set(), "i": 0}。
    """
    logic = node.get("logic", "AND")
    if logic not in ("AND", "OR"):
        raise RuleError(f"不支持的 logic: {logic}")
    conditions = node.get("conditions") or []
    if not conditions:
        raise RuleError("conditions 不能为空")

    parts = []
    for cond in conditions:
        if "conditions" in cond:                      # 嵌套组，不依赖 logic 是否显式给出
            parts.append(compile_rule(cond, allowed_columns, state))
            continue

        col = cond.get("column")
        if not isinstance(col, str) or not _IDENT.match(col):
            raise RuleError(f"非法列名: {col!r}")
        if col not in allowed_columns:                # 白名单，F-01 的关键一步
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
            state["expanding"].append(key)            # 没有它 SQLAlchemy 会报错
            parts.append(f"`{col}` {op} :{key}")
        else:
            state["params"][key] = value
            parts.append(f"`{col}` {op} :{key}")

    return "(" + f" {logic} ".join(parts) + ")"


def build_where(rules, allowed_columns):
    """返回 (where 片段, 绑定参数, expanding 键, 规则引用到的列集合)。"""
    state = {"params": {}, "expanding": [], "columns": set(), "i": 0}
    where = compile_rule(rules, allowed_columns, state)
    return where, state["params"], state["expanding"], state["columns"]


def as_text(sql_string, expanding_keys):
    stmt = text(sql_string)
    if expanding_keys:
        stmt = stmt.bindparams(*(bindparam(k, expanding=True) for k in expanding_keys))
    return stmt
```

`build_where` 返回的第四个值——规则引用到的列集合——直接喂给 F-03 的排除逻辑：

```python
pos_where, pos_params, pos_exp, pos_cols = build_where(pos_rules, allowed_columns)
neg_where, neg_params, neg_exp, neg_cols = build_where(neg_rules, allowed_columns)

rule_columns = pos_cols | neg_cols
feature_columns = [c for c in candidate_columns
                   if c not in rule_columns
                   and c.lower() not in LEAKAGE_DENYLIST_LOWER]   # 复用现有黑名单
```

单次扫描完成圈选，同时消除重叠（F-12/F-13）。注意 `COALESCE` 不能省，理由见 F-13：

```sql
SELECT f.*, 1 AS label FROM `t_customer_features` f WHERE {pos_where}
UNION ALL
SELECT f.*, 0 AS label FROM `t_customer_features` f
 WHERE {neg_where} AND NOT COALESCE(({pos_where}), 0)
```

### 6.3 训练产物封装

```python
from dataclasses import dataclass, field
import pandas as pd


@dataclass(frozen=True)
class ScoringBundle:
    """训练与打分之间唯一的契约。打分端不得绕过 transform() 自行构帧。"""

    model: object
    feature_order: list[str]                          # IV 筛选后的入模列及顺序
    numeric_fill: dict[str, float]
    categorical_fill: dict[str, str]
    clip_upper: dict[str, float]
    category_levels: dict[str, list[str]]
    rule_columns: list[str] = field(default_factory=list)   # 已排除的规则列，留档
    best_iteration: int | None = None

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=df.index)
        for col in self.feature_order:                # 顺序由产物决定，不由 SELECT 决定
            if col not in df.columns:
                raise ValueError(f"打分帧缺少入模特征: {col}")
            series = df[col]
            if col in self.clip_upper:
                series = series.clip(upper=self.clip_upper[col])
            if col in self.numeric_fill:
                series = series.fillna(self.numeric_fill[col])
            elif col in self.categorical_fill:
                series = series.fillna(self.categorical_fill[col])
            if col in self.category_levels:
                series = pd.Categorical(series, categories=self.category_levels[col])
            out[col] = series
        return out

    def predict(self, df: pd.DataFrame):
        return self.model.predict_proba(self.transform(df))[:, 1]
```

`train_lgb()` 返回 `(ScoringBundle, metrics)`，`_score_all_candidates()` 只接受 `ScoringBundle`。产物用 joblib 按 `(config_id, batch_id)` 持久化（替代 §10.1 的缓存，见 F-21）。

### 6.4 修订后的执行流程

```
外部调度（每配置一个 CronJob，concurrencyPolicy: Forbid，spec.timeZone: Asia/Shanghai）
  │
  ├─ 取专用连接 + GET_LOCK('lookalike_{config_id}', 0)   拿不到立即退出，不阻塞
  ├─ 读配置 → 编译规则（列白名单）→ 快照规则原文与 hash 入库
  ├─ 单次扫描圈选种子（UNION ALL 去重叠 + CRC32 确定性抽样）
  │    └─ 少数类样本数不足 → 该配置失败，保留上月结果
  ├─ 排除规则引用列 + 泄露黑名单 → 切分 holdout → 在 train 上拟合清洗参数 / IV 筛选
  ├─ Optuna（热启动上月 best_params，目标 lift@top5%）；early stopping 用 train 内层切分
  ├─ 在从未参与训练与调参的 holdout 上评估 → 质量闸门
  │    └─ AUC < 下限 / 跌幅超阈值 / AUC > 0.98（疑似泄露）→ 失败，保留上月结果
  ├─ 分批打分写 staging（内存与候选池规模解耦）
  ├─ 库内窗口函数算 rank / score_pct → 按 (config_id, batch_date) 先删后写
  ├─ 写 t_execution_log（指标 + 规则快照 + 特征清单 + 抽样盐值 + PSI）
  └─ 释放锁（同一条连接）→ 返回状态码，由 __main__ 决定退出码
```

### 6.5 落地顺序

按依赖关系而非工期排列：

1. **先回答未决问题 1 与 3**（特征表存储引擎、候选池定义）。它们会改变第 2、3 步的实现方式，先做其它工作有返工风险。
2. **建模范式定稿**：F-03 的规则列排除与 F-10 的负样本策略是同一个决定的两面，一起定。这一步只改设计文档，不写代码，但它决定了整个方案是否成立。
3. **规则编译器 + 种子圈选**：F-01/F-02/F-05/F-11/F-12/F-13/F-14。全是纯函数或单条 SQL，先写 golden test 再写实现，恶意列名要作为测试用例固化下来。
4. **训练产物封装**：F-04/F-06/F-15/F-18。改动集中在 `scoring_service.py`，边界清晰。
5. **数据库层**：F-07/F-09/F-17/F-19/F-25 的 DDL 与写入路径。依赖第 4 步定下的产物结构（`feature_list` 要落库）。
6. **运维闸门与可观测性**：F-16/F-20/F-22/F-24。第一次真实运行前必须就位——否则第一次静默失败无人知晓。
7. **CLI 与回归测试**：F-23。可以与 3~6 并行推进。

风险集中在第 2 步：如果选择随机负采样（推荐），`t_seed_config.neg_rules` 的语义会从"负样本定义"变成"候选池限定"，§3.1 的表结构与注释、§4.2 的取数逻辑都要跟着调整。这个改动越早做越便宜。
