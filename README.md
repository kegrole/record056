# cdcapply

`cdcapply` 是一个只使用 Python 标准库实现的变更流应用器。它读取 JSON Lines 格式的 insert、update、delete 事件，处理乱序、重复投递和事务原子性，并把最终表状态持久化到 SQLite。

## 事件格式

输入文件每行是一个 JSON 对象，空行会被忽略。字段如下：

- `event_id`：字符串，事件的全局唯一 ID，用于幂等判断。
- `op`：操作类型，只能是 `insert`、`update` 或 `delete`。
- `primary_key`：主键，支持字符串、整数或浮点数；布尔值不合法。
- `txn`：事务 ID，相同 `txn` 的事件属于同一个事务。
- `ts`：事件序号或时间戳，支持整数、浮点数；布尔值不合法。
- `before`：变更前数据，可选；类型必须是对象。
- `after`：变更后数据；`insert`、`update` 必须提供对象，`delete` 可省略。

当前示例表结构要求 `after` 中的必填字段为：

- `name`：字符串。
- `value`：数字。
- `deleted`：可选布尔值；未提供时按 `false` 处理。

`update` 事件缺少 `before` 时不会报错，会按 `after` 执行 upsert 式合并；目标主键不存在时会插入新行。

## 顺序与幂等

每个主键维护一个已成功应用的最大 `ts`：

- 新事件的 `ts` 大于或等于该主键当前最大 `ts` 时，可以参与应用。
- 新事件的 `ts` 严格小于当前最大 `ts` 时，事件会被丢弃，明细 `applied=false`，并计入 `out_of_order_dropped`。
- 乱序事件不会修改任何已有字段，也不会推进水位线。
- 已成功应用过的 `event_id` 再次到达时会被识别为重复投递，明细 `applied=false`，并计入 `duplicates`。
- 重复投递既不会改变行数，也不会改变任何字段值。

水位线和已应用事件 ID 都持久化在 SQLite 中，因此重新运行同一个数据库也能继续保持乱序判断和幂等。

## 事务语义

同一个 `txn` 的所有事件先缓冲并完整校验，再统一提交：

- 缺主键、缺 `event_id`、非法 `ts`、非法操作类型、`after` 缺必填字段或字段类型不符，都会让整个事务非法。
- 多事件事务中只要有一条非法，所有事件都不会落地。
- 被回滚事务中的每条事件都会输出 `applied=false`，`reason` 包含“事务被回滚”。
- 每回滚一个事务，`rolled_back_txns` 增加 1。
- 合法事件在单个 SQLite 事务中批量写入，保证同一 `txn` 的事件原子提交。

重复事件不会再次应用。乱序事件按主键水位线规则跳过并计数，不会触发数据库写入；同一事务中的其他合法事件仍按事务校验结果处理。

## 删除模式

`--delete-mode` 控制 delete 的落地方式：

- `soft`：默认模式。行保留在表中，并写入 `deleted=true` 墓碑标记。
- `physical`：物理删除。目标行从 SQLite 表中移除；主键的乱序水位线仍会保留，防止旧 delete 或旧更新晚到后重新写入数据。

示例：

```bash
python -m cdcapply.cli --events demo/events.jsonl --db out/physical.db --out out/physical-report.json --delete-mode physical
```

## 报告字段

报告 JSON 包含 `events` 明细数组和 `summary` 汇总对象。每条明细严格包含以下五个字段：

- `event_id`：被处理的事件 ID。
- `op`：事件操作类型。
- `applied`：是否成功应用。
- `reason`：处理原因，例如 `applied`、重复、乱序或事务回滚原因。
- `table_rows`：该条事件处理完成后的表总行数；同一事务提交时，事务内已应用事件会看到提交后的最终行数。

汇总字段：

- `applied`：成功应用的事件数。
- `skipped`：未成功应用的事件数，包含重复、乱序、非法事件和事务回滚事件。
- `duplicates`：重复 `event_id` 数。
- `out_of_order_dropped`：因主键水位线判定为乱序而丢弃的事件数。
- `rolled_back_txns`：被回滚的事务数。

## Demo

运行：

```bash
python -m cdcapply.cli --events demo/events.jsonl --db out/cdc.db --out out/report.json
```

命令成功后：

- SQLite 数据库写入 `out/cdc.db`。
- JSON 报告写入 `out/report.json`。
- stdout 打印一行摘要，包含事件数、应用数、跳过数和最终行数。

演示流包含 12 条事件：6 条 insert、2 条 update、2 条 delete、1 条重复投递和 1 条乱序事件。默认软删模式下最终有 7 行，其中 `k2`、`k3` 为带删除标记的墓碑行。

## 测试

在仓库根目录运行：

```bash
python -m unittest test_cdcapply -v
```
