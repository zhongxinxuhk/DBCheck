# 超大实例规模：监控与巡检的最优配置

本文给的是**软件层面的容量参数**（并发、间隔、超时），与机器型号无关；机器只需满足"并发数 × 单次采集开销"的 CPU/内存预算。

所有参数按优先级生效：**环境变量 > `data/runtime_tuning.json` > 代码默认值**。
改完重启进程生效；当前生效值见启动日志或 `GET /api/system/runtime-tuning`。

---

## 一、先看结论（按托管的数据库实例数选）

| 托管实例数 | `monitor.max_workers` | `monitor.interval` | `screen_workers` / `screen_interval` | `inspection.max_concurrent` | 预期形态 |
| --- | --- | --- | --- | --- | --- |
| ≤ 50 | 8 | 10s | 8 / 15s | 2 | 默认配置即可，监控页秒级刷新 |
| 50–150 | 8 | 15–20s | 8 / 15s | 2–3 | 单轮采集 2–5s，留足余量 |
| 150–300 | 12–16 | 30s | 16 / 30s | 3 | 单轮 5–15s，需关注目标库连接抖动 |
| 300–500 | 16–24 | 60s | 32 / 60s | 3–4 | 建议按业务域拆成 2 套部署（每套独立 `data/`） |
| > 500 | — | — | — | — | 单进程架构不再推荐：按业务域分片，多套部署 + 前置统一入口 |

> 单进程 Web 是当前架构的硬边界：`tasks`（任务表）、调度器、SQLite 都在进程内。**不要**用 gunicorn 多 worker 跑同一份 `data/`，会重复触发定时任务、任务状态互相看不见。

---

## 二、参数怎么算出来的

### 1. 监控采集（`MonitorEngine`）

每轮对每个实例做 2–3 次"新建连接 + 查询"（慢查询、连接数、最大连接数）。现在按实例**有界并发**执行，单轮耗时近似：

```text
单轮耗时 ≈ ceil(实例数 / max_workers) × 单实例耗时
单实例耗时 ≈ 建连(0.02–0.3s) + 2 条查询(0.02–0.5s)
```

采集线程是"跑完一轮再睡 interval"，所以**实际采样周期 = max(interval, 单轮耗时)**。
规划时保证 `单轮耗时 ≤ interval × 0.6`，否则监控页会表现为"跳秒、停更"，而不是报错。

若某个库网络不通或视图极慢，它只影响自己那一个 worker 槽位：整轮到 `round_budget`（≈波数 × `instance_timeout` + 2s）后返回已完成部分，超时实例下一轮重试，不再阻塞整轮。

### 2. 大屏采样（`ScreenCollector`）

每 15s（可配）对全部实例采样一次，8 并发。500 实例、单实例 0.5–1s 时单轮需要 30–60s，所以大屏间隔必须同步放大到 60s，否则只是把线程池排满。

### 3. 定时巡检（`scheduler`）

执行名额分两池：

- `inspection.max_concurrent`：原生驱动巡检（MySQL/PG/Oracle/达梦等）；
- `inspection.jvm_concurrent`：JDBC/JVM 类（Oracle JDBC、SQL Server JDBC、DB2、GBase、ClickHouse、HGDB）。

两池分开是为了避免"一个 JVM 巡检把 CPU 吃满、拖慢所有原生巡检"。
日巡检能力近似：

```text
日巡检次数 ≈ max_concurrent × 维护窗口秒数 / 单次巡检秒数
```

例：并发 3、窗口 8 小时、单次 6 分钟 → 3 × 480 / 6 ≈ 240 次/天。
若同一条 cron 上堆了很多任务，排队超过 `acquire_timeout`（默认 30 分钟）的那次会被**跳过**并在 `scheduler.log` 记 WARNING——这就是"任务列表有、当天没报告"的典型原因。大规模部署请把任务错峰（例如按分钟位散开），不要都压在 02:00。

### 4. 看门狗（`watchdog_timeout` / `watchdog_grace`）

超时后先请求中止（JVM 子进程可秒级杀掉），**名额保留到线程真正退出**，超过宽限期才强制回收并记 ERROR。
因此：`watchdog_timeout` 要大于最慢一类巡检的正常耗时（Oracle 全量 + AI 诊断建议 2400–3600s），否则会出现"超时日志 + 实际并发超出配置"。

---

## 三、推荐配置文件

把下面内容保存为 `data/runtime_tuning.json`（目录不存在时先创建），重启后生效。

### 100 实例（推荐起点）

```json
{
  "inspection": {
    "max_concurrent": 3,
    "jvm_concurrent": 1,
    "acquire_timeout": 1800,
    "watchdog_timeout": 2400,
    "watchdog_grace": 300
  },
  "monitor": {
    "interval": 15,
    "max_workers": 8,
    "instance_timeout": 8,
    "screen_interval": 15,
    "screen_workers": 8
  }
}
```

### 300 实例（单套部署上限附近）

```json
{
  "inspection": {
    "max_concurrent": 3,
    "jvm_concurrent": 1,
    "acquire_timeout": 3600,
    "watchdog_timeout": 3600,
    "watchdog_grace": 300
  },
  "monitor": {
    "interval": 30,
    "max_workers": 16,
    "instance_timeout": 10,
    "screen_interval": 30,
    "screen_workers": 16
  }
}
```

### 500 实例（分片部署，每套 250 左右）

```json
{
  "inspection": { "max_concurrent": 2, "jvm_concurrent": 1, "acquire_timeout": 3600 },
  "monitor": {
    "interval": 60,
    "max_workers": 24,
    "instance_timeout": 12,
    "screen_interval": 60,
    "screen_workers": 32
  }
}
```

环境变量等价写法（容器/服务单元里更省事）：

```bash
DBPULSE_MONITOR_WORKERS=16
DBPULSE_MONITOR_INTERVAL=30
DBPULSE_INSPECTION_MAX_CONCURRENT=3
DBPULSE_INSPECTION_JVM_CONCURRENT=1
```

取值会被钳制：`max_workers` 1–64、`interval` 5–300、`max_concurrent` 1–32、`jvm_concurrent` 1–8，避免误配把目标库打挂。

---

## 四、别忘了目标库那一侧

- **连接抖动**：每轮每实例新建 2–3 个连接。200 实例、15s 间隔 ≈ 27–40 次建连/秒。Oracle 若开了 logon 审计、或 MySQL `max_connect_errors` 较小，请把间隔放到 60s。
- **慢查询采集依赖**：`pg_stat_statements`、`performance_schema`、`v$` 视图缺失时会走 fallback，单实例耗时会上升，规划时按 1s/实例留余量。
- **巡检并发与业务争抢**：巡检查询以 catalog/统计视图为主，但 Oracle AWR、DM8 离线库检查会明显加重 IO。生产库建议巡检并发 ≤ 3，且避开业务高峰。
- **账号权限**：监控与巡检使用只读账号时，`max_connection` 等查询可能失败并降级，容量评估要按降级后的查询数算。

---

## 五、存储与清理

| 项目 | 量级（实测/估算） | 建议 |
| --- | --- | --- |
| 单份 Word 报告 | 约 0.6 MB（实测 21 份 13 MB） | 200 份/天 ≈ 120 MB/天 |
| 报告目录清理 | **无自动清理** | 加 cron：`find data/reports -name '*.docx' -mtime +90 -delete` |
| 监控历史 `samples` | 2 行/实例/轮，保留 30 天 | 500 实例 @60s ≈ 144 万行/天；超过 300 实例建议缩短保留期或外置时序库 |
| SQLite | WAL 模式、单写者 | 巡检并发 ≤ 4；备份前先 `sqlite3 db ".backup"` |

---

## 六、上线验收清单

1. 启动日志出现 `巡检并发=… 监控并发=…`，且与配置文件一致；
2. `GET /api/system/runtime-tuning` 返回的 `summary` 与预期一致；
3. 监控日志出现 `监控采集完成：N/N 实例，并发 X，耗时 Y s`，且 `Y < interval × 0.6`；
4. `data/logs/scheduler.log` 连续 3 天无「等待执行名额超时」WARNING；
5. 页面在巡检高峰（并发跑满）时接口 p95 仍在可接受范围；
6. 若启用服务化：`systemctl restart` / `launchctl kickstart` 后 10s 内页面恢复，报告不丢。
