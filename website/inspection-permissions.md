<!--
Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
-->

# 巡检专用账号与权限

由 DBA 在目标数据库执行授权。将示例中的数据库、账号、密码和允许连接的来源替换为现场值。选择实际启用的巡检章节，创建独立登录账号，再按章节添加只读权限。SQL 文本、会话、账户清单与数据库结构均可能包含业务信息，请按现场授权范围开放。

巡检账号用于读取数据。数据库备份恢复、变更、会话处置和主机 SSH 采集使用各自的账号及审批流程。若实例版本或组件没有相应对象，应跳过该检查项。授权后以巡检账号重新连接，执行相应验证 SQL 并重跑原章节。

## Oracle

在目标 PDB 或非 CDB 中创建账号。每个待巡检 PDB 分别配置。涉及根容器或 RAC 的检查，按现场容器与节点范围单独授权。

```sql
CREATE USER DBCHECK_RO IDENTIFIED BY "替换为随机强密码";
GRANT CREATE SESSION TO DBCHECK_RO;
-- 会话、锁、SQL、等待、参数和实例状态
GRANT SELECT ON SYS.V_$SESSION TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$SQL TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$SQLAREA TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$LOCK TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$TRANSACTION TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$SYSSTAT TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$SYSTEM_EVENT TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$SESSION_EVENT TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$SESSION_WAIT TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$RESOURCE_LIMIT TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$PARAMETER TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$INSTANCE TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$DATABASE TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$VERSION TO DBCHECK_RO;
-- 容量和备份
GRANT SELECT ON SYS.DBA_TABLESPACES TO DBCHECK_RO;
GRANT SELECT ON SYS.DBA_DATA_FILES TO DBCHECK_RO;
GRANT SELECT ON SYS.DBA_FREE_SPACE TO DBCHECK_RO;
GRANT SELECT ON SYS.DBA_SEGMENTS TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$TEMP_SPACE_HEADER TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$RMAN_BACKUP_JOB_DETAILS TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$ARCHIVED_LOG TO DBCHECK_RO;
-- 用巡检账号验证
SELECT COUNT(*) FROM V$SESSION;
SELECT COUNT(*) FROM DBA_TABLESPACES;
```

其余预置检查涉及以下对象。逐项确认存在后，可按格式 `GRANT SELECT ON SYS.<对象名> TO DBCHECK_RO;` 补授权。视图的查询名以 `V$` 或 `GV$` 开头时，授权对象写作 `V_$` 或 `GV_$`。

- 存储、重做与性能：`V_$ASM_DISKGROUP`、`V_$CONTROLFILE`、`V_$DATAFILE`、`V_$DATAFILE_HEADER`、`V_$FILESTAT`、`V_$FLASH_RECOVERY_AREA_USAGE`、`V_$LOG`、`V_$LOGFILE`、`V_$LOG_HISTORY`、`V_$TEMPFILE`、`V_$TEMPSEG_USAGE`、`V_$UNDOSTAT`、`V_$LIBRARYCACHE`、`V_$SEGMENT_STATISTICS`、`V_$PROCESS`、`V_$BGPROCESS`、`V_$OBJECT_USAGE`、`DBA_UNDO_EXTENTS`。
- 元数据与作业：`DBA_OBJECTS`、`DBA_TABLES`、`DBA_INDEXES`、`DBA_TAB_STATISTICS`、`DBA_IND_STATISTICS`、`DBA_TRIGGERS`、`DBA_JOBS`、`DBA_SCHEDULER_JOBS`、`DBA_OUTSTANDING_ALERTS`、`DBA_REGISTRY`、`DBA_REGISTRY_SQLPATCH`。
- 账号与安全专项：`DBA_USERS`、`DBA_ROLE_PRIVS`、`DBA_PROFILES`、`DBA_DB_LINKS`、`V_$PWFILE_USERS`。按审计范围授权。
- RAC、Data Guard 与备份：`GV_$INSTANCE`、`GV_$PARAMETER`、`GV_$SYSTEM_EVENT`、`V_$ARCHIVE_DEST`、`V_$ARCHIVE_GAP`、`V_$DATAGUARD_STATS`、`V_$RMAN_STATUS`。
- AWR 与顾问：`DBA_HIST_BASELINE`、`DBA_HIST_SNAPSHOT`、`DBA_HIST_WR_CONTROL`、`DBA_ADVISOR_FINDINGS`、`DBA_ADVISOR_RECOMMENDATIONS`。启用前由 DBA 核对数据库产品许可、数据范围和功能配置。旧补丁历史检查先确认是否可使用公开字典视图；对内部对象 `SYS.REGISTRY$HISTORY` 保持 DBA 验证。
- `ORA-01045` 表示账号缺少 `CREATE SESSION`。出现 `ORA-00942` 时，先由 DBA 在同一容器核对对象是否存在，再补已存在对象的读取权限。

```sql
-- 可选示例，确认对象在当前容器存在后，复制对应语句
GRANT SELECT ON SYS.DBA_OBJECTS TO DBCHECK_RO;
GRANT SELECT ON SYS.DBA_INDEXES TO DBCHECK_RO;
GRANT SELECT ON SYS.DBA_SCHEDULER_JOBS TO DBCHECK_RO;
GRANT SELECT ON SYS.DBA_REGISTRY_SQLPATCH TO DBCHECK_RO;
GRANT SELECT ON SYS.DBA_USERS TO DBCHECK_RO;
GRANT SELECT ON SYS.DBA_ROLE_PRIVS TO DBCHECK_RO;
-- 仅 RAC / Data Guard 场景
GRANT SELECT ON SYS.GV_$INSTANCE TO DBCHECK_RO;
GRANT SELECT ON SYS.V_$DATAGUARD_STATS TO DBCHECK_RO;
```

[Oracle 官方授权文档](https://docs.oracle.com/en/database/oracle/oracle-database/19/dbseg/grants-user-privileges-and-roles.html) · [Oracle 动态性能视图授权示例](https://docs.oracle.com/en/database/oracle/oracle-database/19/dvadm/database-vault-administrators-guide.pdf)

## GBase 8a

账号的来源主机应限制为巡检服务地址。以下示例为一个业务库。只需要连通性、全局状态及本账号可见的元数据时，从账号与业务库只读权限开始。

```sql
CREATE USER 'dbcheck_ro'@'192.0.2.10' IDENTIFIED BY '替换为随机强密码';
GRANT SELECT ON `业务库`.* TO 'dbcheck_ro'@'192.0.2.10';
-- 用巡检账号验证
SELECT VERSION();
SHOW GLOBAL STATUS LIKE 'Threads_connected';
SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = '业务库';
SHOW PROCESSLIST;
```

- 元数据与连接：`information_schema.TABLES`、`STATISTICS`、`EVENTS`、`PROCESSLIST`、`TABLE_LOCKS` 与 `SHOW GLOBAL STATUS/VARIABLES`。按目标 schema 及厂商版本的会话可见性验证。
- MPP 专项：`SHOW NODES`、`SHOW GCLUSTER NODES`、`SHOW VC`、`information_schema.CLUSTER_MONIT_INFO`、`COORDINATORS_TASK_INFORMATION`、`GNODES_RESOURCE_POOL_USAGE`、`GNODES_USER_DISKSPACE_USAGE`、`performance_schema.CLUSTER_MONIT_INFO`、`CLUSTER_DISK_USAGE_INFO`、`DISK_USAGE_INFO`。单节点或未启用组件时相关对象可能不存在。需要集群管理权限的命令由 DBA 单独核验。
- 全局 `PROCESS` 可扩大其他账号会话可见范围，厂商也提示此权限具有管理能力风险。仅在明确审批后考虑下面的可选授权，无法接受该范围时保留本账号会话采样。
- 用户审计涉及 `gbase.user`。由 DBA 提供仅包含所需字段的审计结果或脱敏视图。系统用户表可能含认证信息，请勿直接将系统库读权限交给常规巡检账号。

```sql
-- 可选，仅审批后开放跨用户会话可见性
GRANT PROCESS ON *.* TO 'dbcheck_ro'@'192.0.2.10';
```

[GBase 8a 厂商赋权说明](https://www.gbase.cn/community/post/5403) · [GBase 8a SHOW 命令](https://www.gbase.cn/community/post/614)

## SQL Server

在 `master` 创建服务器登录名，在每个待巡检库创建映射用户。服务器级和数据库级授权分别执行。下面两组 DMV 权限按服务器版本和启用章节选择。SQL Server 2022 起，个别安全 DMV 与日志使用独立的安全状态权限。

```sql
USE [master];
CREATE LOGIN [DBCHECK_RO] WITH PASSWORD = '替换为随机强密码', CHECK_POLICY = ON;
-- SQL Server 2019 及更早版本，需要采集服务器 DMV 时
-- GRANT VIEW SERVER STATE TO [DBCHECK_RO];
-- SQL Server 2022 及更新版本，需要采集性能 DMV 时
-- GRANT VIEW SERVER PERFORMANCE STATE TO [DBCHECK_RO];
-- SQL Server 2022 及更新版本，启用相应安全 DMV 时
-- GRANT VIEW SERVER SECURITY STATE TO [DBCHECK_RO];
USE [业务数据库];
CREATE USER [DBCHECK_RO] FOR LOGIN [DBCHECK_RO];
-- 按具体 DMV 和版本选择
-- GRANT VIEW DATABASE STATE TO [DBCHECK_RO];
-- GRANT VIEW DATABASE PERFORMANCE STATE TO [DBCHECK_RO];
-- GRANT VIEW DATABASE SECURITY STATE TO [DBCHECK_RO];
-- 需要完整对象元数据时在当前库开放
-- GRANT VIEW DEFINITION TO [DBCHECK_RO];
```

```sql
USE [msdb];
CREATE USER [DBCHECK_RO] FOR LOGIN [DBCHECK_RO];
-- 启用备份、恢复、作业或日志传送章节时，按需要选用
GRANT SELECT ON OBJECT::dbo.backupset TO [DBCHECK_RO];
GRANT SELECT ON OBJECT::dbo.backupmediafamily TO [DBCHECK_RO];
GRANT SELECT ON OBJECT::dbo.restorehistory TO [DBCHECK_RO];
GRANT SELECT ON OBJECT::dbo.suspect_pages TO [DBCHECK_RO];
GRANT SELECT ON OBJECT::dbo.sysjobs TO [DBCHECK_RO];
GRANT SELECT ON OBJECT::dbo.sysjobhistory TO [DBCHECK_RO];
GRANT SELECT ON OBJECT::dbo.log_shipping_monitor_primary TO [DBCHECK_RO];
```

- 服务器性能与会话：`sys.dm_exec_query_stats`、`sys.dm_exec_sql_text`、`sys.dm_exec_requests`、`sys.dm_exec_sessions`、`sys.dm_os_sys_info`、各 `sys.dm_os_*` 性能 DMV、`sys.dm_io_virtual_file_stats`。用巡检账号运行 `SELECT COUNT(*) FROM sys.dm_exec_sessions;` 核对可见性。
- 容量与索引：`sys.database_files`、`sys.master_files`、`sys.dm_db_file_space_usage`、`sys.dm_db_index_physical_stats`、`sys.dm_db_index_usage_stats`、`sys.indexes`、`sys.objects`、`sys.stats`、`sys.partitions`、`sys.allocation_units`。在目标库执行 `SELECT name, size FROM sys.database_files;`。
- 高可用与复制：`sys.dm_hadr_availability_replica_states`、`sys.availability_groups`、`sys.availability_replicas`、`sys.dm_os_cluster_nodes`、`sys.database_mirroring`、`distribution.dbo.MSpublications`。按版本与功能确认 DMV 权限，复制库对象读权限在实际分发库内授予。
- 实例与安全专项：`sys.configurations`、`sys.databases`、`sys.server_principals`、`sys.server_role_members`、`sys.sql_logins`、`sys.tcp_endpoints`、`sys.traces`、`sys.dm_xe_sessions`、`sys.dm_server_services`、`sys.resource_governor_resource_pools`。仅对确需跨库或完整实例元数据的检查评估 `VIEW ANY DATABASE`、`VIEW ANY DEFINITION` 或该 DMV 明示的权限。
- 错误日志读取按版本单独审批。SQL Server 2022 起参考 `VIEW ANY ERROR LOG`，旧版本应按支持的日志访问角色及运维职责决定由谁采集。

[Microsoft Learn 服务器授权](https://learn.microsoft.com/en-us/sql/t-sql/statements/grant-server-permissions-transact-sql) · [DMV 权限](https://learn.microsoft.com/en-us/sql/relational-databases/system-dynamic-management-views/system-dynamic-management-views) · [错误日志权限](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sp-readerrorlog-transact-sql)

## 达梦 DM8

由管理员创建普通账号，按实际部署的 DM8 版本逐项授予动态视图读取权限。对于只授权部分章节的账号，应确认确实需要视图读取角色后再评估更宽权限。

```sql
CREATE USER DBCHECK_RO IDENTIFIED BY "替换为随机强密码";
GRANT CREATE SESSION TO DBCHECK_RO;
GRANT SELECT ON V$VERSION TO DBCHECK_RO;
GRANT SELECT ON V$INSTANCE TO DBCHECK_RO;
GRANT SELECT ON V$DATABASE TO DBCHECK_RO;
GRANT SELECT ON V$DM_INI TO DBCHECK_RO;
GRANT SELECT ON V$SESSIONS TO DBCHECK_RO;
GRANT SELECT ON V$LOCK TO DBCHECK_RO;
GRANT SELECT ON V$TRX TO DBCHECK_RO;
GRANT SELECT ON V$TRXWAIT TO DBCHECK_RO;
GRANT SELECT ON V$SYSTEM_EVENT TO DBCHECK_RO;
GRANT SELECT ON V$TABLESPACE TO DBCHECK_RO;
GRANT SELECT ON V$BACKUPSET TO DBCHECK_RO;
-- 用巡检账号验证
SELECT COUNT(*) FROM V$SESSIONS;
SELECT COUNT(*) FROM V$TRXWAIT;
```

- 容量与对象：`DBA_DATA_FILES`、`DBA_FREE_SPACE`、`DBA_TABLESPACES`、`DBA_SEGMENTS`、`DBA_TABLES`、`DBA_INDEXES`、`DBA_OBJECTS`。
- 性能与历史：`V$BUFFERPOOL`、`V$PARAMETER`、`V$THREADS`、`V$LONG_EXEC_SQLS`、`V$SQL_HISTORY`、`V$ERR_INFO`。SQL 历史由 DBA 按产品说明启用，授权本身不会开启采集。
- 审计与作业：`DBA_USERS`、`DBA_ROLE_PRIVS`、`DBA_SYS_PRIVS`、`DBA_DB_LINKS`、`SYSJOB.SYSJOBS`。由管理员确认所在模式与敏感内容后，按 `GRANT SELECT ON <对象名> TO DBCHECK_RO;` 补齐。
- 归档和高可用：`V$ARCHIVED_LOG`、`V$ARCH_STATUS`、`V$DM_ARCH_INI`、`V$RLOG`、`V$RLOGFILE`、`V$DMWATCHER`、`V$DM_MAL_INI`、`V$MAL_INFO`、`V$MAL_LINK_STATUS`、`V$DSC_EP_INFO`。根据单机、主备或 DSC 拓扑选择。对象不存在时无需授权。

```sql
-- 可选示例，确认启用 SQL 历史、作业或高可用后按需选用
GRANT SELECT ON V$SQL_HISTORY TO DBCHECK_RO;
GRANT SELECT ON V$LONG_EXEC_SQLS TO DBCHECK_RO;
GRANT SELECT ON SYSJOB.SYSJOBS TO DBCHECK_RO;
GRANT SELECT ON V$DMWATCHER TO DBCHECK_RO;
GRANT SELECT ON V$DSC_EP_INFO TO DBCHECK_RO;
```

[达梦自主访问控制](https://eco.dameng.com/document/dm/zh-cn/pm/discretionary-access-control.html) · [达梦动态性能视图](https://eco.dameng.com/document/dm/zh-cn/pm/dm8-admin-manual-appendix2.html)

## 其他数据库与自定义检查项

用户自建模板和第三方插件可以查询额外的表、视图或函数。由 DBA 根据实际 SQL 审核对象权限，按插件与厂商文档补齐。数据库加密认证、登录失败、证书或网络错误、功能未启用和对象不存在，应沿对应原因处理。报告中给出的每条授权建议都应结合失败章节与目标库作用域复核。
