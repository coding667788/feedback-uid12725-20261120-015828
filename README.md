# 本地值守裁定器（Adjudicator）

将两类**合成**告警按规则表关联为可复核事件、按封存水位安全封存，并依据站点值守表
计算每个事件的**双人复核期限**。纯虚构数据，无网络、无真实告警、无外部服务。

- 环境：Windows 原生 Python 3.12，仅标准库，存储用内置 SQLite
- 运行方式：`python -I runner.py <command> ...`（`-I` 隔离模式，不读取 cwd/PYTHONPATH 中的第三方模块）

## 1. 目录结构

```
adjudicator/
  hashing.py    确定性哈希：封存事件 ID、事件摘要
  domain.py     领域规则：候选边合法性、连通分量、半开值守区间、覆盖分钟与期限
  storage.py    SQLite schema、事务与一致快照
  service.py    五个命令的全部业务规则
  cli.py        命令行入口
generator/
  synthetic.py  确定性虚构样本生成器（批次、排班、操作指引）
tests/
  reference_minute.py        独立逐分钟参考实现（不导入生产代码）
  test_domain.py             边规则、分量、值守区间
  test_deadline_reference.py 期限：生产实现 vs 独立参考逐分钟核对
  test_service_graph.py      导入、边、第三条告警合并、封存边界、迟到、版本
  test_service_review.py     期限、双签、排班版本、核验、中断恢复、CLI 冒烟
  test_generator_story.py    生成器样本端到端叙事
runner.py       根启动器
```

## 2. 输入文件格式

顶层是一个 JSON 对象：`{"batches": [...], "schedules": [...]}`，两者皆为列表，可混合多批次。

### 批次

```json
{
  "batch_id": "B1",
  "rule_version": "R1",
  "watermark": 10,
  "rules": [{"type_a": "SMOKE", "type_b": "TEMP", "max_diff": 10}],
  "alerts": [
    {"source_event_id": "a1", "channel": "CH1", "site": "S1",
     "area": "Z1", "minute": 0, "alert_type": "SMOKE"}
  ]
}
```

- 含告警的批次必须随批声明 `rules`；纯推进水位的**空批次**（`alerts: []`）可省略规则。
- 告警字段：通道 `channel`、站点 `site`、安全区域 `area`、整数分钟 `minute`、
  类型 `alert_type`、来源事件 ID `source_event_id`。
- `watermark` 由输入声明，只允许单调推进（非递减），回退直接拒绝。

### 排班

```json
{
  "schedule_version": "S1-SCH-v1",
  "site": "S1",
  "entries": [
    {"person": "alice", "group": "G1", "start_minute": 100, "end_minute": 140},
    {"person": "bob",   "group": "G2", "start_minute": 105, "end_minute": 140},
    {"person": "finn",  "group": "G3", "start_minute": 1430,
     "end_minute": 15, "cross_day": true}
  ]
}
```

- 半开区间 `[start_minute, end_minute)`：第 start 分钟当班，第 end 分钟已下班。
- `cross_day: true` 表示跨日班次，`end_minute` 为次日相对零点的分钟，内部规范化为 `end + 1440`。
- 班次允许重叠；同一人员在同一分钟不得分属不同组（校验拒绝）。

## 3. 核心语义（与需求逐条对应）

### 3.1 候选边与事件边界

候选边必须**同时**满足：

1. 两条告警**通道不同**——同通道告警之间永不建边，只能经合法跨通路边间接归入同一事件；
2. 同站点、同安全区域；
3. 类型对（无序）在规则表中声明允许；
4. 整数分钟差的绝对值 `<=` 该类型对的最大时差——**恰等也合法**。

事件 = 活动告警图上的一个**连通分量**；回执同时列出分量内的**实际边证据**。
未获任何关联的单条告警（孤立点）同样形成一个单成员事件。

### 3.2 水位、迟到与封存

- **迟到档**：导入时 `minute < 导入前当前 watermark` 的新告警写入 `late_alerts`，不参与关联、不产生边。
  首批导入前尚无 watermark，不存在迟到。
- **封存安全条件**：仅当 `分量最晚告警分钟 max_minute + 规则表全局最大时差 < watermark`
  （**严格小于**）时才可封存；恰相等时仍可能有后到合法边串连，一律保留暂存。
- 封存会把分量成员与边从活动表移除并冻结进回执，因此**新规则永远无法改写已封存事件**。
- 全局最大时差取所有已声明规则版本的最大值，保证任何规则下的后到边都来不及串连。

### 3.3 封存事件 ID 与回执

- 事件 ID = `EVT-` + `SHA256(规则版本 | 排序后的来源事件 ID)` 的前 16 位；成员顺序不影响 ID。
- 事件摘要哈希绑定分量内全部告警字段与实际边；双签绑定该摘要，篡改即被 `verify` 发现。
- 回执保留：采用的水位、全局最大时差、分量边（含边所用规则版本与时差）、原事件摘要、
  排班版本、审核截止与覆盖分钟数。

### 3.4 审核截止（双人复核期限）

从**封存采用的 watermark** 起，按该站点**封存时最新版本**值守表逐分钟扫描：

- 某一分钟有**至少两名不同组**人员当班才算一个覆盖分钟；按去重分钟累计，**不按人头重复**；
- 累加满 30 个覆盖分钟，截止时刻取第 30 个覆盖分钟的**下一分钟**
  （合法签发区间为半开 `[watermark, deadline)`）；
- 窗口内不足 30 个覆盖分钟（含空班、只有单组）→ `deadline_minute = null`，报告标注**“无可排期限”**。

### 3.5 双签规则

- 两签必须来自**不同组**的不同人员；两人各自在签发分钟当班（半开区间）；
- 签发时间满足 `watermark <= sign_minute < deadline`；
- 两签可**异步**完成，互换提交顺序结论一致；
- **同内容签收幂等**：同事件 + 同人 + 同分钟的重复提交不新增记录（事件已审查完成后重放仍幂等）；
- 无可排期限的事件不接受签收。

### 3.6 版本不可变与一致快照

- 规则版本、排班版本一经写入即不可变；内容变更必须使用新版本号，重声明旧版本且内容不同会被拒绝。
- 规则变更只影响**整轮新批次**产生的边，旧边不回溯重算，已封存事件永久绑定原规则版本。
- 排班更正建立新版本，**旧封存事件与审查结论永久绑定原排班版本**，期限不随新版本变化。
- 写操作（导入/封存/签收）在单个 `BEGIN IMMEDIATE` 事务内完成；读操作（report/verify）
  使用 `BEGIN DEFERRED` 一致快照。

## 4. 命令

| 命令 | 作用 |
| --- | --- |
| `import-batch --db <db> --file <json>` | 导入批次与排班（幂等、可安全重放） |
| `seal --db <db>` | 封存所有越过安全水位的连通分量，输出封存回执清单 |
| `review --db <db> --event-id <id> --person <p> --minute <m> [--summary-hash <h>]` | 提交一次签收 |
| `report --db <db> [--out <file>]` | 全量状态 JSON：水位、版本、暂存分量、迟到档、封存事件与签收 |
| `verify --db <db>` | 独立复核全部持久化状态；有问题退出码为 1 |
| `generate [--out-dir <dir>]` | 生成确定性虚构样本（不带参数则打印单文件全量样本） |

所有正常结果以 UTF-8 JSON 打印到 stdout；业务错误打印到 stderr 并以退出码 2 退出；
`verify` 发现问题以退出码 1 退出。

## 5. 快速上手

```bash
# 1. 生成虚构样本（6 个分步文件 + 操作指引）
python -I runner.py generate --out-dir samples

# 2. 依次导入（中断后原样重跑即可，全部幂等）
for %f in (01_b1 02_b2 03_b3 04_b4 05_b5) do python -I runner.py import-batch --db demo.db --file samples/%f.json

# 3. 封存（wm=100，S1 主线事件期限 135；S2 空班、S3 覆盖不足 → 无可排期限）
python -I runner.py seal --db demo.db

# 4. 双签（异步、异组；事件 ID 取自 seal 输出）
python -I runner.py review --db demo.db --event-id EVT-c73d999520232c03 --person alice --minute 110
python -I runner.py review --db demo.db --event-id EVT-c73d999520232c03 --person bob   --minute 130

# 5. 排班更正（新版本；旧事件期限不变）
python -I runner.py import-batch --db demo.db --file samples/06_schedule_fix.json

# 6. 报告与核验
python -I runner.py report --db demo.db
python -I runner.py verify --db demo.db
```

> 上面的 `for %f` 是 cmd 语法；Git Bash 用 `for f in ...; do python -I runner.py import-batch --db demo.db --file samples/$f.json; done`。

## 6. 样本叙事覆盖的需求点

| 需求点 | 样本/测试位置 |
| --- | --- |
| 第三条告警把两个暂存事件合并 | B2 的 `S1-a3`（TEMP）桥接两条异通道 SMOKE |
| 与最大时差恰等的合法边 | B3 `S1-eq1/eq2`（时差 10 == max_diff 10） |
| 封存水位恰相等不封存 | B4 `S1-eq3/eq4`：max=30，30+10 == wm=40 |
| 迟到告警 | B5 `S1-late1`：minute=30 < 导入前水位 40 |
| 空班 | 站点 S2 零条目 → 无可排期限 |
| 重叠班 | S1 v1 中 carol 与 alice 同组重叠；期限只按去重分钟计 |
| 截止分钟逐分钟核对 | `tests/test_deadline_reference.py` + `tests/reference_minute.py` |
| 异组与同组签收 | alice/bob 异组完成；carol 与 alice 同组不能完成 |
| 批次中断后恢复 | 同一文件导入两次，状态逐字节一致（`test_interrupted_batch_recovery_by_replay`） |

## 7. 运行测试

```bash
# 普通模式
python -m unittest discover -s . -p "test_*.py"
# 隔离模式（推荐，模拟不可信输入环境）
python -I -m unittest discover -s . -p "test_*.py"
```

共 59 个测试。注意用工程根作为发现起始目录（`-s .`），`-I` 模式下 `-s tests`
会因隔离模式剥离 sys.path 而无法导入工程包。

## 8. 关键设计取舍说明

- **watermark 与事件分钟相互独立**：输入流的 watermark 是窗口边界，可低于同批最晚事件分钟。
  样本据此安排数据，使“及时到达的桥接告警”不被迟到规则误伤——迟到判定严格按需求字面规则
  `minute < 当前 watermark` 执行，不引入额外启发式。
- **边只在告警到达时按当时规则版本建立一次**，之后永不重算；封存再把边冻结，双重保证
  “改变规则仅影响整轮新批次、禁止改写已封存事件”。
- **期限半开语义统一**：值守区间、签发区间均为 `[start, end)`，因此截止时刻是第 30 个
  覆盖分钟的下一分钟，边界测试覆盖 `== watermark` 合法与 `== deadline` 非法。
