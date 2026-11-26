<!-- SPDX-License-Identifier: Apache-2.0
Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck
SPDX-License-Identifier: Apache-2.0
Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck -->

# 数据库类型与旧版本适配

数据库接入沿用既有类型注册表、数据源表单、巡检任务、指标模板与报告渲染器。GBase 8a 的类型标识为 `gbase8a`，与 `gbase`（GBase 8s）分开；不增加独立的数据库管理页面。

| 数据库 | 原有系统中的位置 | 连接与版本处理 | 指标 |
|---|---|---|---|
| GBase 8a MPP Cluster | 数据库类型、数据源管理、数据库巡检、数据库驱动管理 | PyMySQL 协议；默认端口 5258；服务端身份核对；验证证书的 TLS 或明确配置的 SSH 隧道 | 8a 专用模板 14 项，包括集群空间、节点任务、连接、参数、对象、索引、统计信息 |
| DM8 | 原有 DM8 类型与 JDBC/dmPython 入口 | 保留原有连接策略；受控证据采集补充独立进程 JDBC 与 SSH 隧道 | 继续使用 DM8 预置指标；元数据证据与报告覆盖共用指标目录 |
| Oracle 10g–23ai | 原有 Oracle / Oracle JDBC 类型 | 原生 11g 需要兼容的 Thick 客户端；11.1 与 11.2 分开验证；不把 23c 客户端默认当作 11g 兼容客户端；JDBC 入口同样读取 `v$instance` 后按版本改写 SQL | 沿用 Oracle 11g 专用模板；10g/11g 自动回退旧视图和旧列，12c+ 保留新语法 |
| SQL Server 2005–2025 | 原有 SQL Server 类型 | 验证证书的 ODBC 18 或显式 SSH 隧道；认证后读取 `SERVERPROPERTY('ProductVersion')`，按 2005/2008/2014/默认模板自动选择 | 2005/2008/2014 专用模板各 18 项；2012、2016–2025 使用默认模板 21 章 34 查询；2005 按 DMV 兼容层回退 |
| PostgreSQL 9.6–18 | 原有 PostgreSQL 类型与原生入口 | JDBC 优先、psycopg2 回退；认证后读取 `server_version_num` / `server_version` / `version()`，先探测版本再改写 SQL | 默认模板 21 章 49 查询；9.x 的 xlog/WAL、复制列名、归档目录，13 前的 `pg_stat_statements` 统计列，17 起的检查点视图自动适配 |
| KingbaseES V8R2–V9R4 | 原有 KingbaseES 类型与原生入口 | JDBC 优先、psycopg2 回退；一条 SQL 同时读取 `version()` 与 `server_version_num`，失败时回滚后逐级回退 `SHOW` / `server_version` | 默认 19 章模板；按 PostgreSQL 兼容内核改写 WAL/xlog、复制列名、`FILTER`、`usebypassrls`；`pg_*` 不可见时回退 `sys_*` 同义对象 |

SQL Server 版本矩阵覆盖 2005–2025。服务端版本自动选择模板，用户显式选择的模板仍然优先；2008 R2 归入 2008 模板，2012 与 2016–2025 使用默认模板。模板初始化采用增量添加；重复执行不会替换已有查询和用户阈值。采集失败或权限不足必须保留实际状态，不能以 0 或正常替代。

SQL Server JDBC 插件的声明范围仍是 2017/2019/2022/2025：它读取 `SERVERPROPERTY`/`@@VERSION` 后按版本适配模板查询，但自定义采集 SQL 未宣称覆盖 2005–2014；这些旧版本由原生 SQL Server 入口和 2005/2008/2014 专用模板覆盖。

Oracle JDBC 插件（`oracle_jdbc`）与原生 Oracle 入口共用 `modules.inspection.oracle_compat`：运行时读取实际版本后改写 `AUTHENTICATION_TYPE`、`FETCH FIRST`、`DBA_REGISTRY_SQLPATCH` 等差异，并在结构类错误时尝试版本候选。Oracle JDBC 模板只有一套 `oracle_jdbc v1`，由适配层兼容 10g–23ai，不另建重复模板。

### SQL Server 版本矩阵

| 服务端版本 | 版本标识 | 模板选择 | 关键兼容处理 |
|---|---|---|---|
| SQL Server 2005 | 9.x | 2005 专用模板（18 项） | 启动时间、文件 I/O、内存 clerk 使用 2005 回退；旧版不具备的功能标记“不适用” |
| SQL Server 2008 / 2008 R2 | 10.0 / 10.50 | 2008 专用模板（18 项） | 2008 R2 与 2008 共用；扩展事件、资源调控器可用，AlwaysOn 标记“不适用” |
| SQL Server 2012 | 11.x | 默认模板（21 章 34 查询） | AlwaysOn 可用；Query Store 标记“不适用” |
| SQL Server 2014 | 12.x | 2014 专用模板（18 项） | 内存指标使用 `PAGES_KB`；AlwaysOn 可用，Query Store 标记“不适用” |
| SQL Server 2016 | 13.x | 默认模板（21 章 34 查询） | Query Store 可用 |
| SQL Server 2017 | 14.x | 默认模板（21 章 34 查询） | 按 2016+ 功能集采集 |
| SQL Server 2019 | 15.x | 默认模板（21 章 34 查询） | 按 2016+ 功能集采集 |
| SQL Server 2022 | 16.x | 默认模板（21 章 34 查询） | 按 2016+ 功能集采集 |
| SQL Server 2025 | 17.x | 默认模板（21 章 34 查询） | 按 2016+ 功能集采集 |

### SQL Server 2005 的 DMV 回退与“不适用”

SQL Server 2005 缺少部分后续版本的 DMV 或列。运行时适配层在读取版本后按版本或结构类错误回退，不把兼容差异误报为采集失败：

- **启动时间**：`sys.dm_os_sys_info.sqlserver_start_time` 不可用，改用 `master..sysdatabases` 中 `tempdb` 的 `crdate` 计算运行时长。
- **文件 I/O**：`sys.dm_io_virtual_file_stats` 不可用，改用 `::fn_virtualfilestats(NULL, NULL)` 获取读写次数和 I/O 延迟。
- **内存 clerk**：`sys.dm_os_memory_clerks.pages_kb` 不可用，改用 `single_pages_kb + multi_pages_kb`；2012+ 反向归一为 `pages_kb`。

以下功能/查询在低版本明确标记为“不适用”，报告保留该状态，不输出为 0，也不记为查询失败：

| 功能或查询 | 最低版本 | 低版本处理 |
|---|---|---|
| 扩展事件 `sys.dm_xe_sessions` | SQL Server 2008 | 2005 标记“不适用” |
| 资源调控器 `sys.resource_governor_*` | SQL Server 2008 | 2005 标记“不适用” |
| `sys.dm_server_services` | SQL Server 2008 R2 | 2005、2008 标记“不适用” |
| AlwaysOn `sys.availability_*` / `sys.dm_hadr_*` | SQL Server 2012 | 2005/2008 标记“不适用” |
| Query Store | SQL Server 2016 | 2005–2014 标记“不适用” |
| 复制分发库 `distribution.dbo.MSpublications` | 已配置复制分发 | 实例无 `distribution` 库时标记“不适用” |

候选 SQL 回退只处理无效列、无效对象、语法等结构类错误；权限、登录、连接和超时错误直接保留原始失败原因，不进入候选链。

### PostgreSQL 版本矩阵

| 服务端版本 | 关键兼容处理 | 版本探测/报告 |
|---|---|---|
| PostgreSQL 9.6 | `pg_current_wal_lsn` / `pg_walfile_name` / `pg_wal_lsn_diff` / `pg_last_wal_receive_lsn` 改写为 `*_xlog_*`；`pg_stat_replication` 的 `sent_lsn` 等改写为 `*_location`；`pg_wal/archive_status` 改写为 `pg_xlog/archive_status`；`pg_stat_statements` 使用 `*_time` | `pg_stat_activity.wait_event` 从 9.6 起可用 |
| PostgreSQL 10–12 | WAL 函数、复制列名和归档目录使用新名称；慢查询统计仍使用 `total_time` / `mean_time` / `max_time` | 读取 `server_version_num` 优先，缺少权限时回退 `SHOW` / `version()` |
| PostgreSQL 13–16 | `pg_stat_statements` 使用 `total_exec_time` / `mean_exec_time` / `max_exec_time`；其余模板保持现代名称 | 版本号写入核心属性，报告显示主/次/补丁版本 |
| PostgreSQL 17–18 | 检查点统计从 `pg_stat_bgwriter` 拆分到 `pg_stat_checkpointer`；`SELECT * FROM pg_stat_bgwriter` 自动合并 `num_timed`、`num_requested`、`write_time`、`sync_time` 等检查点字段 | `pg_stat_checkpointer` 不可见或旧版模板执行结构类错误时回退原始 `pg_stat_bgwriter` |

PostgreSQL 适配层只改写仓库内已有的巡检、慢查询和监控入口，不新增数据库类型或页面。`pg_stat_statements` 未安装时慢查询仍按既有逻辑标记未启用，不把扩展缺失误报为版本兼容失败。旧于 9.6 的实例不属于当前 PostgreSQL 模板支持范围；如版本探测到 9.0–9.5，`wait_event` 查询会标记“不适用”，其余 WAL/xlog 改写仍按 9.x 处理。

### KingbaseES 版本矩阵

KingbaseES 的版本号形如 `V008R006C007B0012`，同时暴露 PostgreSQL 兼容内核版本（例如 V8R6 的 `sys_ctl -V` 为 `(Kingbase) 12.1`）。巡检入口先读取 `version()` 与 `server_version_num`，以实际内核版本覆盖静态映射；无法读取时才按版本线推断，并由执行期的结构类错误候选链兜底。

| KingbaseES 版本 | 兼容内核 | 关键兼容处理 | 版本探测/报告 |
|---|---|---|---|
| V8R2 | PostgreSQL 9.6 | `pg_current_wal_lsn` / `pg_walfile_name` / `pg_wal_lsn_diff` 改写为 `*_xlog_*`；`pg_stat_replication` 的 `*_lsn` 改写为 `*_location`；`pg_wal/archive_status` 改写为 `pg_xlog/archive_status` | `wait_event` 从 9.6 起可用 |
| V8R3 | PostgreSQL 9.6 或 10 | 默认按 9.6 旧名称改写；实际内核为 10 时由 `server_version_num` 覆盖，结构类错误再尝试新名称 | 报告保留 `V8R3` 与实测内核版本 |
| V8R6 | PostgreSQL 12 | WAL 函数、复制列名和归档目录使用现代名称；保留 `pg_stat_bgwriter` 检查点列 | 优先 `server_version_num`，缺失时回退 `SHOW` / `version()` |
| V9R1–V9R4 | PostgreSQL 12 | 与 V8R6 使用同一现代 WAL/复制命名；**不套用** PostgreSQL 17 的 `pg_stat_checkpointer` 拆分，V9 仍保留旧列；V9R2C16 起事务 ID 扩展到 64 位，回卷风险分母同步改写 | 读取 `version()` banner 与 `server_version_num` |
| V7 及更早 | 非 PostgreSQL 兼容内核 | 不属于本入口的模板支持范围；仅保留版本号识别，用于给出明确诊断 | 不承诺 V8/V9 模板可用 |

KingbaseES 兼容模式或裁剪安装可能只暴露 `sys_*` 系统对象。候选链会把已知 `pg_*` 视图/函数改写为 `sys_*`，并把 `pg_catalog` 同步改为 `sys_catalog`；只在无效列、无效对象、函数不存在、语法错误等结构类错误时尝试，权限、认证、连接和超时错误不回退。低于 9.6 的内核把 `wait_event` 标记“不适用”，低于 9.5 的内核移除 `pg_user.usebypassrls`，不以 0 或 `false` 伪造采集结果。

适配范围是仓库已有的 KingbaseES 入口和默认巡检模板，不新增数据库类型、页面或独立查询入口。当前没有 KingbaseES 实库，版本矩阵、SQL 改写和模拟游标回归属于静态与合成验证；补丁号、兼容模式、授权视图和实际返回字段仍需在实库逐项验收。


GBase 8a 原生连接不依赖 JDBC JAR。JAR 就绪检查核对实际驱动类；目录名或登记名不能证明驱动适配。8s 的 `com.gbasedbt.jdbc.Driver` 不可代替 8a 的 `com.gbase.jdbc.Driver`。

SSH 隧道使用系统 `known_hosts` 验证数据库主机；远端数据库连接限定为该主机的回环地址。不能以跳板机到数据库的未加密网络连接代替上述边界。未配置可信主机密钥、CA 或兼容客户端时给出阻断原因，不自动关闭证书校验。

## 数据外发与运行环境

数据库巡检不因需求目录存在而切换全局“客户模式”。身份认证、资源归属、CSRF、凭据加密、只读查询、结果限量、报告脱敏分别作用在对应环节。

客户数据相关的 AI、向量化、BIC-QA、Webhook 和邮件通道默认仅允许回环服务；外部 HTTPS 主机由部署管理员通过 `DBPULSE_ALLOWED_EGRESS_HOSTS` 明确列出。邮件同样要求允许 SMTP 主机并验证 TLS。数据发送不跟随重定向。网络代理仍需验证最终服务端证书。备份第三方通知通过第一方适配层应用同样控制，不修改第三方许可证。

登录后直接进入系统，在原用户中心自行改密；不强制跳转口令设置页。不存在通过需求目录禁用原有巡检入口的行为。

## 验证边界

### 2026-11-26 四类客户数据库实测

| 数据库 | Docker 实测版本 | 验证路径 |
|---|---|---|
| Oracle | Database Free 23 | 保存 JDBC 连接串并以 SYSDBA 测试连接，从数据源发起巡检、下载 Word、大屏采集会话和统计计数 |
| GBase 8a | 8.6.2.43 Express | 明文直连，生成巡检报告并采集会话、吞吐和样例数据空间。该版本不提供 MPP 节点命令，报告标明不适用 |
| SQL Server | SQL Server 2022 | 配置 JDBC 版本与加密选项，执行巡检和采样，读取当前数据库的数据文件容量及可用空间 |
| 达梦 | DM8 8.1.4.6（244896） | 使用指定 JDBC 驱动实测巡检、监控、动态视图和 Word 输出。单机未初始化的作业系统按实测标明 |

巡检报告保留原厂列名和指标标识。对匹配到厂商定义的内容显示中文释义与单位，对版本新增或自定义字段显示原始名称和核对说明。SQL Server 日志文件和未取得有效上限的数据文件的剩余比例保持缺失状态，避免估算出错误的容量告警。监控展示的 Oracle SQL 执行次数、GBase 8a 客户端语句数、SQL Server T-SQL 批次数和 DM8 SQL 执行次数均由对应原厂累计计数差分获得。

四库测试使用隔离容器和合成数据。集群拓扑、生产证书和最小权限仍按现场配置单独验收，特别是 Oracle RAC/Data Guard、GBase 8a 9.5 MPP、SQL Server Always On 和达梦 DSC/数据守护。指标参考 [Oracle Database Reference](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/V-SYSSTAT.html)、[GBase 8a 运维监控](https://www.gbase.cn/news/3587)、[Microsoft 性能计数器](https://learn.microsoft.com/zh-cn/sql/relational-databases/system-dynamic-management-views/sys-dm-os-performance-counters-transact-sql) 和 [达梦动态视图](https://eco.dameng.com/document/dm/zh-cn/pm/dm8-admin-manual-appendix2.html)。

`python scripts/verify_service_management.py` 使用隔离目录与模拟连接，不连接实际数据库。检查类型注册、指标初始化幂等、SQL Server 版本选择、只读规则、传输参数、租户权限、加密、报告覆盖和登录入口等。组件可导入、JAR 类可加载、接口模拟回归、实库兼容验收是不同证据，驱动页面分别显示组件状态与连接待验证状态。

待实库提供后逐一核对补丁号、证书、授权视图、节点角色、查询返回字段、集群范围及报告证据。极端损坏恢复能力需要实际工具、合法授权和隔离恢复验证；登记工具名称或哈希不代表已具备恢复能力。

官方参考：[Oracle 驱动兼容说明](https://oracle.github.io/python-oracledb/index.html)、[DM Python 接口参数](https://eco.dameng.com/document/dm/zh-cn/pm/dmpython-interface.html)、[GBase 8a 应用接入说明](https://www.gbase.cn/community/post/9704)、[Microsoft ODBC 兼容说明](https://learn.microsoft.com/en-us/sql/connect/odbc/windows/system-requirements-installation-and-driver-files)、[KingbaseES V9 文档](https://help.kingbase.com.cn/v9/)。

回归测试默认读取 `scripts/fixtures/service_catalog.json` 的合成数据，不读取客户目录。客户合同回归需在本地显式设置 `DBPULSE_TEST_CATALOG` 指向私有目录。CI 使用 Python 3.12 和 `scripts/requirements-validation.txt` 固定验证依赖；正常推送与自动上游同步均执行服务及数据安全回归。
