# 共享接入限额下的置信出力申报仓

这是本地演练用的合成来源资料。CSV 使用 UTF-8、有首行字段名，空字段表示未提供。除非字段语义明确为零，空值没有数值含义。

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

## 本地查看

适用 Python 3.11，无需安装第三方库或构建。

```sh
python3 inspect_inputs.py
python3 -m unittest -v test_input_pack
```

查看命令打印各原始表的记录数。单元测试校验资料结构、引用与文件完整性，可离线执行。
