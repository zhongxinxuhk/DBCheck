# 日志生命周期管理（长期运行不撑爆磁盘）

DBPulse 的日志分三层管理：**写入侧按大小轮转 → 归档按份数/天数保留 → 全局预算与磁盘余量保护**。
任何一层生效都能阻止单文件无限增长；三层同时生效时，日志占用有明确上限。

---

## 一、日志清单与默认策略

| 日志 | 位置 | 写入方 | 轮转阈值 | 保留 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 巡检任务日志 | `data/logs/inspection-YYYYMMDD.log` | Web 主进程（`_append_task_log`） | 单文件 50MB | 按天保留 30 天，归档留 14 份 | 按天分文件，当天超 50MB 再切 |
| 定时巡检日志 | `data/logs/scheduler.log` | 调度器（`RotatingLogHandler`） | 10MB | 5 份 / 30 天 | |
| 监控采集日志 | `data/pro_data/monitor.log` | 监控引擎（`RotatingLogHandler`） | 20MB | 3 份 / 14 天 | 与 `data/` 同卷，归档进 `data/logs/_archive/monitor/` |
| AI 护栏审计 | `data/logs/ai_guard_audit.jsonl` | `ai_guard` | 20MB | 10 份 / 180 天 | 合规留痕，保留期最长 |
| 控制台重定向 | `data/logs/web_service_restart.log` | `deploy/start.sh` / `start.bat` | 启动时检查 20MB | 3 份 / 14 天 | shell 持有 fd，只能启动前归档；服务化后由 journald 管 |
| Windows 服务输出 | `data/logs/service.out.log` 等 | NSSM | 20MB（`AppRotateBytes`） | 3 份 | 脚本已写入轮转配置 |

**归档位置统一为 `data/logs/_archive/<family>/`**，命名 `<原名>.<YYYYmmdd-HHMMSS>.log[.gz]`。
归档目录不会被前端「运行日志」页误读（页面读的是内存环形缓冲，最多 3000 条）。

内存环形缓冲（`modules/loghub.py`，3000 条）与会话日志不受轮转影响，页面查看体验不变。

---

## 二、轮转规则

- **触发**：写入方每次写日志时累计字节数，达到阈值立即轮转（不是等定时任务）。
  这样即使一天写 10GB，文件也不会超过阈值太多。
- **跨进程安全**：轮转时对 `data/logs/.rotate.lock` 加文件锁（POSIX `flock` / Windows `msvcrt`），
  多进程同时写同一文件时不会互相踩踏。
- **跨挂载点**：`data/pro_data` 与 `data/logs` 在 Docker 里可能是两个卷，`os.replace` 会失败；
  此时自动退化为「复制到归档 → 截断原文件」，当天日志不丢。
- **压缩**：归档默认 gzip（`compress: true`），文本日志通常压到 10%–20%。

```text
data/logs/
├── scheduler.log                     # 活跃文件，最大 10MB
├── inspection-20261110.log           # 当天任务日志，最大 50MB
├── ai_guard_audit.jsonl              # 审计，最大 20MB
└── _archive/
    ├── scheduler/scheduler.20261110-031500.log.gz
    ├── inspection/inspection-20261110.20261110-142000.log.gz
    ├── monitor/monitor.20261109-235900.log.gz
    └── ai_audit/ai_guard_audit.20261001-000000.jsonl.gz
```

---

## 三、保留与预算

清理线程每 **30 分钟**（`sweep_interval_seconds`）执行一次，启动时也立即跑一次：

1. **按份数/天数**：每个归档族只保留最近 `keep` 份；超过 `keep_days` 天的一律删除（两者取严）。
2. **按天日志**：`inspection-YYYYMMDD.log` 的文件日期早于 `keep_days` 天即整份删除（当天文件不动）。
3. **总预算**：活跃 + 归档总量超过 `total_budget_mb` 时，从最旧的归档开始删，降到预算的 80%。
4. **磁盘余量**：可用空间低于 `min_free_mb` 进入紧急模式，继续删最旧归档；仍不足时记 ERROR，
   明确要求扩容或调小保留天数（不会静默失败）。

默认上限（全部取满时）：约 1.1GB 归档 + 活跃文件，**总预算 2GB**。
按每天新增 200MB 估算，预算内可保留约 10 天全量日志；超过部分从最旧开始丢，审计日志单独保留 180 天。

---

## 四、策略配置

保存为 `data/log_policy.json`（可选，改完重启生效；`policy()` 会按 mtime 自动重载）：

```json
{
  "total_budget_mb": 2048,
  "min_free_mb": 512,
  "sweep_interval_seconds": 1800,
  "compress": true,
  "families": {
    "inspection": { "max_mb": 50, "keep": 14, "keep_days": 30 },
    "scheduler":  { "max_mb": 10, "keep": 5,  "keep_days": 30 },
    "monitor":    { "max_mb": 20, "keep": 3,  "keep_days": 14 },
    "ai_audit":   { "max_mb": 20, "keep": 10, "keep_days": 180 },
    "console":    { "max_mb": 20, "keep": 3,  "keep_days": 14 }
  }
}
```

环境变量（优先级最高，容器/服务单元里最方便）：

```bash
DBPULSE_LOG_BUDGET_MB=4096      # 总预算（64–102400）
DBPULSE_LOG_MIN_FREE_MB=1024    # 磁盘余量警戒（0–102400）
DBPULSE_LOG_SWEEP_SECONDS=1800  # 清理间隔（60–86400）
```

所有值都会被钳制，避免把预算配成 1MB 导致当天日志被立刻删掉。

---

## 五、操作系统 / 容器层

应用内轮转只管 Python 写入的文件；进程 stdout 与容器日志需要平台侧配置，二者都要有上限。

### systemd（推荐的生产运行方式）

stdout 由 journald 收集，自带容量控制。按需在 `/etc/systemd/journald.conf`：

```ini
[Journal]
SystemMaxUse=2G
SystemMaxFileSize=100M
MaxRetentionSec=30day
```

### macOS launchd

`launchd.out.log` / `launchd.err.log` 由 launchd 持有 fd，应用无法安全轮转。
`deploy/service/dbpulse.newsyslog.conf` 已按 `10MB × 5` 配置（安装目录默认 `/opt/dbpulse`，如不同请改路径）：

```bash
sudo cp deploy/service/dbpulse.newsyslog.conf /etc/newsyslog.d/dbpulse.conf
sudo newsyslog -v      # 干跑确认配置被解析
```

限制说明：launchd 不会因文件改名而重新打开日志，轮转后进程仍写旧文件，直到任务重启。
因此 macOS 下「当前」输出可能出现在归档文件里，但容量上限（份数 × 单文件大小）依然成立。
监控/调度/巡检日志不经过 launchd，不受此限制。

### Windows（NSSM）

`install_windows_service.ps1` 已设置 `AppRotateFiles=1`、`AppRotateOnline=1`、
`AppRotateBytes=20971520`，服务输出超过 20MB 自动轮转。用计划任务方式运行时，
请在任务里把 stdout 重定向到 `data\logs\service.out.log`，并依赖应用内的清理线程管理该文件。

### Docker

`deploy/docker-compose.yml` 已限制：

```yaml
logging:
  driver: json-file
  options: { max-size: "10m", max-file: "3" }
```

`docker run` 方式请自行加 `--log-opt max-size=10m --log-opt max-file=3`。

### 手动启动脚本

`start.sh` / `start.bat` 在启动前检查 `web_service_restart.log`，超过 20MB 先移入
`data/logs/_archive/console/`。运行期间 shell 持有 fd，无法中途轮转——长期运行请用服务方式。

---

## 六、容量测算

单条巡检/调度日志约 120–400 字节。按 100 个实例、每天 200 次巡检、每次 300 行估算：

```text
巡检日志/天 ≈ 200 × 300 × 250B ≈ 15MB
监控日志/天 ≈ 100 实例 × 1440 轮 × 80B ≈ 11MB
调度日志/天 ≈ 200 条 × 200B ≈ 0.04MB
```

即日常增量约 25–30MB/天，2GB 预算足够保留两个月以上。异常场景（巡检失败疯狂重试、
监控采集报错刷屏）可能到 GB/天，此时总预算与磁盘余量保护会先删最旧归档，
并通过 `/api/system/log-storage` 的 `usage_pct` 与 `disk_free_mb` 暴露出来。

---

## 七、运维操作

```bash
# 查看各日志族大小、预算占用、磁盘余量、最近一次清理结果
curl -s http://127.0.0.1:5003/api/system/log-storage | python -m json.tool

# 手动触发一次清理（进程内，等价于等下一次 30 分钟周期）
python -c "from modules.core.log_manager import sweep; print(sweep())"

# 应急：只清归档（不碰活跃文件）
find data/logs/_archive -type f -mtime +7 -delete
```

`usage_pct` 长期 > 80% 说明预算偏小或日志异常增长，优先查：

- `scheduler.log` 是否在刷「等待执行名额超时」；
- `monitor.log` 是否在刷连接失败（大规模实例下线时会明显增多）；
- 审计日志是否被高频触发（说明 AI 护栏命中过多，应查规则而非删日志）。

---

## 八、故障场景与处理

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 磁盘满但日志目录不大 | Docker json-file / journald 未限容 | 按第五节配置日志驱动与 journald |
| 当天日志"消失" | 曾用外部工具轮转被 handler 持有的文件 | 只通过本模块轮转；外部工具请用 copytruncate |
| 归档越删越多还是超预算 | 活跃文件本身就超过预算 | 调小对应 `max_mb`，或临时停写排查 |
| 审计日志被删导致合规缺口 | `keep_days` 太小 | 单独调大 `ai_audit.keep_days` 与 `keep` |
| 清理线程报「磁盘余量仍然不足」 | 磁盘真的满了，归档已删无可删 | 扩容磁盘；或把 `data/logs` 迁到独立卷 |

---

## 九、验证与单测

```bash
python -m unittest modules.core.tests.test_log_manager -v   # 11 例：轮转/保留/预算/紧急清理/handler
```

覆盖点：阈值内不轮转、强制轮转 + gzip、份数与天数裁剪、过期按天文件删除、
总预算降级到 80%、低磁盘紧急清理、`append_line` 写前轮转、handler 自动翻滚、
状态接口字段完整。
