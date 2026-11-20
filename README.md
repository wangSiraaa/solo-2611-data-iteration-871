# 受限 YAML 配置目录服务与配置消费分析仓

Python 3.11 标准库实现（无第三方依赖），严格按题面合同控制类型、锚点/别名与
merge 语义，并把“有效配置 + 每键来源证据 + 关系表血缘”持久化到真实 SQLite。
另含**额度池身份目录 + 断网离线额度单账本**（`quota` / `demand` /
`quota_ledger`），回答“断网接入站领走的处理额度究竟算哪个资源池”。

## 目录结构

```
confcat/
  errors.py         # 合同级错误类型（解析期/消费期分离，整次任务可回滚）
  model.py          # 原始共享图 RawNode 与物化有效树 MatNode、来源证据模型
  yaml_parser.py    # 受限 YAML 解析器（词法 + 递归下降，游标不变量统一）
  materialize.py    # 别名解引用/循环检测 + merge 合同物化 + 来源证据生成
  codec.py          # 物化树 JSON 编解码（保留 int/str 双键区分）与取值辅助
  catalog.py        # 配置产品目录：版本化发布、路径查询、哈希校验（SQLite）
  csv_pipeline.py   # CSV 读取与按配置归一化（列映射/缺失值/分区/列血缘）
  ingest.py         # 消费仓：类型化取参、整次拒绝、任务结果持久化（SQLite）
  quota.py          # 额度池身份目录：pool_id/period、别名同池、独立池不合并、重命名对账
  demand.py         # 按 conversion 换算约定从真实 CSV 明细算批次工时/托位需求
  quota_ledger.py   # 中心额度单账本 + 分站离线日志：原子承诺/幂等结算/回执/周结三笔量
  cli.py            # 人工检查命令行
samples/            # 多文档同名锚点、merge 序列冲突、显式键前后、额度池身份等 YAML 样例
data/               # 合成 CSV 与运行期 SQLite
demo/run_demo.py    # 端到端演示（含断言）
demo/run_quota_demo.py  # 额度池/离线额度单端到端演示（含断言）
tests/              # unittest：真值表、逐行关系表、隔离、重开、规模、深度、额度池合同
```

至少两个实质模块的分工：`yaml_parser`/`materialize`/`codec` 负责 YAML 目录与
来源，`csv_pipeline`/`ingest` 负责 CSV 数据转换与消费。

## 合同实现要点

### 类型与标量
- 仅双/单引号字符串、十进制整数（`0` 或负号可选的非零首位，正则 `^(0|-?[1-9][0-9]*)$`）、
  `true`/`false`/`null`；其余未加引号标量（`yes`、`001`、日期、`1.5`、`-0`）一律字符串。
- 映射键仅允许 `str`/`int`；`1` 与 `"1"` 是两个键，编解码显式带 `kt` 类型标签持久化。
- 显式拒绝：自定义 tag（`!`）、flow 集合（`[`/`{`）、块标量（`|`/`>`）、
  Tab 缩进、重复显式键、bool/null 键。

### 锚点与别名
- 锚点作用域为**当前文档**：多文档同名锚点互不影响；前向/未知别名、重名锚点拒绝。
- 自引用可通过解析期（pending 锚点），循环在物化期经 active 集合确定性抛
  `CircularReferenceError`，别名链逐跳记录（别名 -> 锚点位置）。
- 原始图保留共享子结构（别名指向同一锚点节点）；物化时每个插入点生成**独立子树**，
  从根上保证消费方修改不可能经别名影响其他任务。

### merge 键 `<<`
- 裸 `<<` 走内部哨兵识别；引号 `"<<"` 是普通字符串键，不触发 merge。
- merge 值为映射或映射序列，否则 `MergeTypeError`。
- 序列从左到右：第一个提供的键胜出，后者只补缺；当前映射**显式键恒覆盖** merge，
  与 `<<` 文本位置无关（前/后均有测试）。
- 只复制“键 -> 值引用”，**不递归合并**同名子映射（整体占位）。
- 每个有效键的 `Provenance` 记录：值/键位置、是否显式、别名链、每个 merge 候选的
  `source_index/won/overridden_by` 覆盖证据。

### 目录与消费仓
- 目录可发布多个命名产品，每次发布产生不可变新版本；任务绑定产品+版本快照，
  其他产品或新版本发布不影响在途/已完成任务。
- 任务按固定路径类型化取得 `source_uri`(str)、`column_mapping`(str->str)、
  `missing_value`(可选)、`partition_by`(可选)。
- 路径缺失、错误类型、原 CSV 列缺失、列映射冲突、分区键缺失 -> **整次任务拒绝**，
  单事务回滚，任务与结果均不落库。
- 归一化表逐行写入 SQLite，每列保留目标列、原 CSV 列、表头行号与配置来源 Provenance。
- 重开目录/任务仓可回查全部结果与配置链；发布快照含源文本 SHA-256，`verify` 校验。

## 设计取舍：物化独立树 vs 保留共享图按需解引用

本实现**两者都保留**：解析期保留共享锚点图（表达共享事实、支撑同一性检查与别名链），
发布时物化为带完整来源证据的独立树并落库。代价是大范围别名复用时物化有额外内存/时间
（已用 100 配置 × 万节点、200 路别名复用测试覆盖）；收益是消费隔离由数据结构天然保证、
来源证据随树自包含、重开无需重放解析。

## 额度池身份目录与断网离线额度单（quota / demand / quota_ledger）

### “领走的额度算哪个池”——身份不由限额数字决定

两个相等的限额可能来自两座仓，也可能只是同一池子的两种读法。`quota.build_registry`
在**发布期**把每个 CSV 接入站声明的工时（hours）/托位（pallets）解析成带稳定身份
`pool_id` 与结算周期 `period` 的池，身份完全基于物化期来源证据：

| 配置形态 | 别名链证据 | 身份结论 |
| --- | --- | --- |
| 两处经别名/merge 指向**同一锚点** | 条目内键的 `alias_chain` 同起点锚点 | **同一池**（`hours::<anchor>`），共 5 不是各 5 |
| 两处各自写**显式字面量**，即使同值 | 无别名链、无 merge 证据 | **两个独立池**（`hours::explicit::<station>`），禁止合并 |
| 新版本只改 `name` | `pool_id` 不变 | 身份未变，记 `renamed_from`，旧发放仍占原余额 |
| 同锚点被读成不同限额/名字 | — | 发布期 `QuotaConfigError(pool-identity-conflict)` |

工时与托位是两类独立资源，任务按已有来源优先级（merge 从左到右、显式覆盖、别名链）
分别取得本站两类资源的**有效池**（`registry.pool_for(station, resource)`）。

### 消费仓需求计算（demand）

换算约定登记在同一产品的 `conversion` 段（随版本快照固定）：按 `key_column` 逐行匹配
`rules`，未命中走 `defaults`，都未登记则整批拒绝；工时逐行累加，托位支持
`pallets_divisor` 批量归并。`demand_for_task` 对消费仓**真实落库 CSV 明细**计算一批任务
同时所需的两种资源，逐行保留命中规则，重开任务仓结果不变。

### 中心账本与分站离线（quota_ledger）

* **划单即独占预留**：`issue_ticket` 在单事务内校验
  `已划未用 + 本次 ≤ 池限额`。共享池经别名划出的两张单共同受限——同周期两个别名各显
  5 小时共指一池时，两站不能各领 3；各 5 的独立池则可以各领 3。
* **离线原子承诺**：分站 `StationJournal` 只持有中心已独占划给本站、绑定周期与池的单。
  `offline_promise` 必须在本单内**同时**承诺工时与托位，任一不足 => 两种资源都不扣，
  落本地可复核回执（含需求量/余额/单号）。题面例：工时单 3h、需 2h/2 托、托位单仅 1
  时整单不扣；补足托位后重试一次，仅扣 2h/2 托。
* **重联幂等结算**：`drain_for_sync` 按 `request_id` 去重，每个承诺只结算一次，重复回传
  返回 `duplicated=True` 的原结果，绝不双扣；中心重验单归属/周期/状态/资源类型。
* **关单与过期**：`close_ticket` 退回未用额度（重新计入可再分配），关闭单上的新承诺拒收
  并落中心回执；`expire_period` 把过期周期 open 单置 `expired`，分站对过期单的离线承诺
  以 `ticket-expired` 回执拒收。
* **周结三笔量**（按池 `weekly_reconciliation`），恒满足
  `限额 == 已结算 settled + 已划未用 allocated_unused + 可再分配 reallocatable`：
  - 已结算：承诺实际占用（`SUM(used)`）；
  - 已划未用：仍 open 单里划出未用部分（closed/expired 单的未用已退回，不在此列）；
  - 可再分配：限额减去前两笔，中心可再次划单的余额。

## 运行

```bash
# 端到端演示（发布/检查/任务/拒绝/修订有效性/隔离/重开，含内置断言）
python3 demo/run_demo.py

# 额度池身份/离线额度单端到端演示（共享池 vs 独立池、原子承诺、交叉回传、
# 关单/过期回执、重命名身份不变、周结三笔量、重开复核，含内置断言）
python3 demo/run_quota_demo.py

# 全量测试（秒级）
python3 -m unittest discover -s tests -v

# 题面满规模基准（100 配置 / 万节点 / 10 万 CSV 行）
CONFCAT_FULL=1 python3 -m unittest tests.test_scale -v

# 人工检查 CLI
python -m confcat.cli publish data/catalog.db orders-product samples/orders_config.yaml
python -m confcat.cli products data/catalog.db
python -m confcat.cli query data/catalog.db orders-product doc-0 ingest/tier
python -m confcat.cli verify data/catalog.db orders-product

# 额度池人工检查
python -m confcat.cli pools data/demo_quota_catalog.db quota-product doc-0
python -m confcat.cli demand data/demo_quota_catalog.db data/demo_quota_tasks.db batch-1
python -m confcat.cli weekly data/demo_quota_ledger.db 2026-W47
python -m confcat.cli receipts data/demo_quota_ledger.db
```

无第三方依赖，仅用 Python 3.11 标准库；SQLite 数据库文件为真实持久化产物。
