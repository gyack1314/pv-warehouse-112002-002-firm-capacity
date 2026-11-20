# 共享接入限额下的置信出力申报仓

系统缺电时段，光储站按核验组抽检证据获得可用额度，市场组据此申报出力，事后按受理清单追履约。本仓接收窗口通知、时段计量、认证决定、接入限额与申报回执，回放成台账，对外提供完整的申报数据和可核验的履约依据。

适用 Python 3.11，仅需标准库，无需构建。

```sh
python3 inspect_inputs.py                      # 校验资料包结构、引用与摘要
python3 run_pipeline.py --out report.json      # 回放事件，打印摘要并输出完整 JSON
python3 -m unittest -v test_ledger test_input_pack
```

## 资料契约

功率和额度均用 MW。firm_threshold 按抽检通知定义，按必需格的净上网值比较，所有格达标才支撑认证。所有窗口以同一天 UTC 的 HH:MM 表示，end 不含。证据另带 test 键，市场交割实测无 test 键。批准容量、接入点限额及申请均以每格为单位。received_at 是仓方记录的到达时间，rev 在同一对象或测量上递增。

以下文件位于 `data/`。

- `assemblies.csv`（4 条）：`site`、`assembly`、`connection`、`from`、`to`
- `test_windows.csv`（3 条）：`test`、`assembly`、`start`、`end`、`minutes`、`evidence_deadline`、`firm_threshold`
- `measurements.csv`（9 条）：`test`、`assembly`、`at`、`mw`、`received_at`、`rev`
- `certifications.csv`（3 条）：`certificate`、`test`、`assembly`、`start`、`end`、`max_mw`、`decided_at`、`previous_eligible`
- `export_limits.csv`（3 条）：`connection`、`start`、`end`、`mw`、`published_at`、`rev`
- `requests.csv`（5 条）：`request`、`submission`、`assembly`、`received_at`、`hold_until`、`start`、`end`、`mw`
- `market_receipts.csv`（5 条）：`receipt`、`submission`、`at`、`decision`、`start`、`end`、`mw`

`manifest.json` 记录完整资料包的列名、行数、校验值和跨表引用。完整资料包可含业务冲突或同一来源的多次投递，校验器保留这些原始记录。

## 结构

- `inspect_inputs.py` — 资料包校验（结构、sha256、跨表引用），装载前的入口关卡。
- `firmcap/model.py` — 领域对象；时刻一律折算为当日 UTC 分钟数，区间左闭右开。
- `firmcap/ingest.py` — 校验并装载 CSV，按 (时刻, 类型, 原序) 回放事件；一个申请的多行归并为一次提交。
- `firmcap/ledger.py` — 台账核心：证据状态、认证状态、资格/限额逐点占用、暂占与受理、调减、待核 claim，以及全部回看视图。
- `firmcap/report.py` — 汇总为 JSON 友好的完整报告。
- `run_pipeline.py` — 命令行入口。
- `test_ledger.py` — 逐条业务规则的测试。

## 语义约定

**资格认定。** 资格按"站点 + 能力组成"（assembly）认定；直流扩容等组成变更投产即新 assembly，不沿用旧组成的认证，除非某条 usable 认证以 `previous_eligible` 明确允许沿用同站点前一组成的能力（此时容量取两者 max）。认证记录给出批准容量、适用时段与是否可沿用旧能力。

**试验证据。** 必需格 = [start, end) 按 minutes 划分。格子最早版本须在正式截止（evidence_deadline）前到达才算建立；已建立格的更正（更高 rev）无论何时到达都生效。缺任何必需格结果为 `incomplete`——既不达标也不是零出力。全部格有效值 ≥ firm_threshold 为 `pass`，否则 `fail`。

**认证状态。** `usable`：无试验或试验当前达标；`under_review`：决定时达标、事后计量更正使其不再成立（挂复核，其覆盖范围禁止新占额，已受理量不释放但逐段列出）；`unsupported`：决定时证据即不完整或不达标。试验结果只影响引用该试验的认证。

**申请。** 一次提交可含多个时段段、各段请求量不同。站点资格与接入点外送限额均按重叠部分逐点约束（同一时刻：已占 + 请求 ≤ 容量/限额）。只接受全额全段；任一窗口冲突即整单拒绝，拒绝列出每个冲突窗口的约束类型、容量、缺口和占用来源（哪次提交、以何种形态占了多少）。

**暂占与受理。** 评估通过即暂占，至 hold_until 未受理自动释放。已受理份额只凭市场显式调减（reduce 回执）释放，不因资格复核、限额缩减或内部重试而悄悄释放；重复投递的申请/回执幂等，不再占一次。回执必须认得它针对的提交，否则记为无法识别。暂占已过期（或申请已被拒）后的受理不生效也不丢弃：记为待核 claim，列出风险（暂占过期、容量是否已被再分配、按当前资格/限额是否仍放得下），不占容量，持续出现在存量风险与余量视图中。

**回看。** `why(submission)` 给出评估时刻的快照（用了哪条认证、哪版限额、当时占用、当时证据），回答"当时为何能申报"；`availability(now)` 逐组成逐窗口给出资格/限额余量、阻塞原因与待核 claim，回答"目前还有多少能申报"；`correction_impacts` 给出从计量勘误到认证挂复核、范围封锁、受影响已受理量的传导链；`stock_risks` 列出确定占用越限、待核 claim 放不下、已受理量靠复核中资格支撑三类存量风险。

**交割核对。** 按 10 分钟格逐申请逐段对照实测：缺实测为 `missing_evidence`，实测低于有效义务为 `shortfall`（含偏差量），否则 `fulfilled`。有效义务 = 原请求量经调减后的值，调减回执逐格标注。资格复核中、受理待核、义务被调减、与他单共享实测的申请列入交割对照清单，交割时单独对照实测。

## 报告结构（run_pipeline.py --out）

`tests`（逐格证据与结论）、`certifications`（状态）、`connection_limits`（限额版本）、`submissions`（状态、快照、冲突、调减、待核 claim）、`availability_now`、`stock_risks`、`delivery_watchlist`、`reconciliation`、`correction_impacts`、`unrecognized_receipts`、`event_log`（全部推导事件，含认证状态变迁及其计量成因）。每条结论都引用来源记录（认证号、测量 rev、回执号、限额 rev），配合 `inspect_inputs` 的摘要校验即可核验。
