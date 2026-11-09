# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
生成《数据库运维服务清单》配套的业务专项巡检模板并写入 inspection.db。

设计约束（与合同清单一一对应）：
- 全部为「新建」，不覆盖任何既有模板：统一走 dal.import_template(overwrite=False)
- 专项模板作为内置预设；每产品的周巡检作为业务默认模板
- 阈值与故障判据不写死，落在章节 description 中作为可自定义基线
- 命名规则：{母模板名}_业务专项巡检_{专项名}

用法：
    python scripts/gen_service_inspection_templates.py --dry-run
    python scripts/gen_service_inspection_templates.py
    python scripts/gen_service_inspection_templates.py --cd-per-product
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


SUFFIX = '业务专项巡检'

PRODUCT_LABEL = {
    'oracle': 'Oracle 11g',
    'sqlserver': 'SQL Server 2005/2008/2014',
    'dm8': 'DM8 达梦',
    'gbase8a': 'GBase 8a MPP集群',
}
PRODUCT_VERSION = {
    'oracle': '11g',
    'sqlserver': '2005-2014',
    'dm8': '8',
    'gbase8a': '8a',
}
BASE_TEMPLATE_NAME = {
    'oracle': 'Oracle 11g 巡检模板',
    'sqlserver': 'SQL Server 巡检模板',
    'dm8': 'DM8 达梦巡检模板',
    'gbase8a': 'GBase 8a MPP集群巡检模板',
}

# ============================================================================
# 检查项 SQL 库：item_id -> (query_key, 中文说明, SQL)
# 缺失的 item 表示该项由主机采集/命令行完成，在章节 description 中说明
# ============================================================================
ORACLE = {
    'W01': ('W01_instance_status', '实例运行状态、主机名、启动时间', """
SELECT instance_name, host_name, version, status, database_status,
       TO_CHAR(startup_time,'YYYY-MM-DD HH24:MI:SS') startup_time,
       archiver, log_switch_wait
FROM v$instance"""),
    'W02': ('W02_session_env', '当前会话所属用户/库/主机（验证远程登录可用性）', """
SELECT SYS_CONTEXT('USERENV','SESSION_USER') AS login_user,
       SYS_CONTEXT('USERENV','DB_NAME')      AS db_name,
       SYS_CONTEXT('USERENV','SERVER_HOST')  AS server_host,
       SYS_CONTEXT('USERENV','IP_ADDRESS')   AS client_ip
FROM dual"""),
    'W03': ('W03_session_usage', '会话与进程使用率（当前值/上限/历史峰值）', """
SELECT resource_name, current_utilization, limit_value, max_utilization,
       ROUND(current_utilization * 100 / NULLIF(TO_NUMBER(CASE WHEN REGEXP_LIKE(limit_value, '^[0-9]+$') THEN limit_value END),0), 2) AS usage_pct
FROM v$resource_limit
WHERE resource_name IN ('sessions','processes')
ORDER BY resource_name"""),
    'W04': ('W04_outstanding_alerts', '当前未清除的服务告警', """
SELECT TO_CHAR(creation_time,'YYYY-MM-DD HH24:MI:SS') AS alert_time,
       reason, message_level, suggested_action
FROM dba_outstanding_alerts
ORDER BY creation_time DESC"""),
    'W05': ('W05_backup_job_7d', '近 7 天 RMAN 备份作业结果与耗时', """
SELECT session_key, input_type, status,
       TO_CHAR(start_time,'YYYY-MM-DD HH24:MI:SS') AS start_time,
       TO_CHAR(end_time,'YYYY-MM-DD HH24:MI:SS')   AS end_time,
       ROUND(input_bytes/1024/1024/1024, 2)  AS input_gb,
       ROUND(output_bytes/1024/1024/1024, 2) AS output_gb,
       ROUND(elapsed_seconds/60, 1) AS elapsed_min
FROM v$rman_backup_job_details
WHERE start_time > SYSDATE - 7
ORDER BY start_time DESC"""),
    'W06': ('W06_tablespace_usage', '表空间使用率（按使用率降序）', """
SELECT d.tablespace_name,
       ROUND(d.total_mb, 2) AS total_mb,
       ROUND(NVL(f.free_mb,0), 2) AS free_mb,
       ROUND((d.total_mb - NVL(f.free_mb,0)) / NULLIF(d.total_mb,0) * 100, 2) AS used_pct
FROM (SELECT tablespace_name, SUM(bytes)/1024/1024 AS total_mb
        FROM dba_data_files GROUP BY tablespace_name) d
LEFT JOIN (SELECT tablespace_name, SUM(bytes)/1024/1024 AS free_mb
             FROM dba_free_space GROUP BY tablespace_name) f
  ON d.tablespace_name = f.tablespace_name
ORDER BY used_pct DESC"""),
    'W07': ('W07_backup_validate_30d', '近 30 天备份校验（VALIDATE）执行记录', """
SELECT recid AS session_key, status, operation,
       TO_CHAR(start_time,'YYYY-MM-DD HH24:MI:SS') AS start_time,
       output_device_type
FROM v$rman_status
WHERE start_time > SYSDATE - 30
  AND operation LIKE '%VALIDATE%'
ORDER BY start_time DESC"""),
    'W08': ('W08_rac_instance', '集群各节点实例状态（单机返回 1 行）', """
SELECT inst_id, instance_name, host_name, status, database_status,
       TO_CHAR(startup_time,'YYYY-MM-DD HH24:MI:SS') AS startup_time
FROM gv$instance
ORDER BY inst_id"""),
    'W09': ('W09_param_drift', '各节点参数不一致检测（集群配置漂移）', """
SELECT name, COUNT(DISTINCT value) AS distinct_values,
       MIN(value) AS min_value, MAX(value) AS max_value
FROM gv$parameter
GROUP BY name
HAVING COUNT(DISTINCT value) > 1
ORDER BY name"""),
    'W10': ('W10_invalid_objects', '无效数据库对象统计', """
SELECT owner, object_type, status, COUNT(*) AS object_count
FROM dba_objects
WHERE status <> 'VALID'
GROUP BY owner, object_type, status
ORDER BY object_count DESC"""),
    'W11': ('W11_segment_top20', '占用空间最大的 20 个段（定位容量增长源）', """
SELECT * FROM (
  SELECT owner, segment_name, segment_type, tablespace_name,
         ROUND(bytes/1024/1024/1024, 2) AS size_gb
  FROM dba_segments
  ORDER BY bytes DESC
) WHERE ROWNUM <= 20"""),
    'W12': ('W12_stats_stale', '统计信息过期的大表（行数 > 1 万）', """
SELECT owner, table_name, TO_CHAR(last_analyzed,'YYYY-MM-DD HH24:MI:SS') AS last_analyzed,
       num_rows, stale_stats
FROM dba_tab_statistics
WHERE stale_stats = 'YES' AND NVL(num_rows,0) > 10000
ORDER BY num_rows DESC"""),
    'W13': ('W13_scheduler_jobs', '调度作业状态与失败次数', """
SELECT owner, job_name, enabled, state,
       TO_CHAR(last_start_date,'YYYY-MM-DD HH24:MI:SS') AS last_start,
       TO_CHAR(next_run_date,'YYYY-MM-DD HH24:MI:SS')   AS next_run,
       run_count, failure_count
FROM dba_scheduler_jobs
ORDER BY failure_count DESC, owner, job_name"""),
    'W14': ('W14_blocking_sessions', '阻塞会话与等锁会话（含等待时长）', """
SELECT s.sid, s.serial#, s.username, s.status, s.event, s.wait_class,
       s.seconds_in_wait, s.blocking_session, s.sql_id, s.machine
FROM v$session s
WHERE s.blocking_session IS NOT NULL
   OR s.event LIKE 'enq:%'
ORDER BY s.seconds_in_wait DESC"""),
    'W15': ('W15_top_sql_elapsed', '按累计耗时排序的 TOP20 SQL', """
SELECT * FROM (
  SELECT sql_id, executions,
         ROUND(elapsed_time/1000000, 2) AS elapsed_s,
         ROUND(elapsed_time/NULLIF(executions,0)/1000, 2) AS avg_ms,
         ROUND(buffer_gets/NULLIF(executions,0), 0) AS avg_lio,
         ROUND(cpu_time/1000000, 2) AS cpu_s,
         disk_reads, parsing_schema_name,
         SUBSTR(sql_text, 1, 200) AS sql_text
  FROM v$sql
  WHERE executions > 0
    AND parsing_schema_name NOT IN ('SYS','SYSTEM')
  ORDER BY elapsed_time DESC
) WHERE ROWNUM <= 20"""),
    'W16': ('W16_wait_class', '按等待类别汇总的累计等待（排除空闲等待）', """
SELECT wait_class,
       ROUND(SUM(time_waited)/100, 2) AS total_wait_s,
       ROUND(SUM(time_waited)/NULLIF(SUM(total_waits),0)/100, 4) AS avg_wait_s,
       SUM(total_waits) AS wait_count
FROM v$system_event
WHERE wait_class <> 'Idle'
GROUP BY wait_class
ORDER BY total_wait_s DESC"""),
    'W17': ('W17_users_status', '数据库账号状态与到期时间', """
SELECT username, account_status,
       TO_CHAR(expiry_date,'YYYY-MM-DD') AS expiry_date,
       TO_CHAR(created,'YYYY-MM-DD')     AS created_date,
       default_tablespace, profile
FROM dba_users
ORDER BY username"""),
    'W18': ('W18_audit_param', '审计相关参数取值', """
SELECT name, value
FROM v$parameter
WHERE name IN ('audit_trail','audit_sys_operations','audit_file_dest')
ORDER BY name"""),
    'W19': ('W19_database_info', '数据库基础信息（用于台账核对）', """
SELECT name, dbid, TO_CHAR(created,'YYYY-MM-DD HH24:MI:SS') AS created,
       log_mode, open_mode, flashback_on, force_logging, database_role
FROM v$database"""),
    'W20': ('W20_collect_stamp', '本次采集时间戳与实例标识（留痕用）', """
SELECT TO_CHAR(SYSDATE,'YYYY-MM-DD HH24:MI:SS') AS collected_at,
       SYS_CONTEXT('USERENV','DB_NAME')   AS db_name,
       SYS_CONTEXT('USERENV','INSTANCE_NAME') AS instance_name
FROM dual"""),
    'W03b': ('W03b_library_cache', '共享池库缓存命中率（硬解析过多会显著下降）', """
SELECT namespace, gets, gethits,
       ROUND(gethitratio*100, 2) AS get_hit_pct,
       pins, pinhits,
       ROUND(pinhitratio*100, 2) AS pin_hit_pct,
       reloads, invalidations
FROM v$librarycache
WHERE namespace IN ('SQL AREA','TABLE/PROCEDURE','BODY','TRIGGER')
ORDER BY namespace"""),
    'W16b': ('W16b_buffer_hit_ratio', '缓冲区命中率（含硬解析率）', """
SELECT ROUND((1 - pr.value / NULLIF(lr.value + cr.value,0)) * 100, 2) AS buffer_hit_pct,
       ROUND(hp.value / NULLIF(pc.value,0) * 100, 2) AS hard_parse_pct,
       lr.value AS logical_reads, pr.value AS physical_reads
FROM (SELECT value FROM v$sysstat WHERE name='db block gets')             lr,
     (SELECT value FROM v$sysstat WHERE name='consistent gets')           cr,
     (SELECT value FROM v$sysstat WHERE name='physical reads')            pr,
     (SELECT value FROM v$sysstat WHERE name='parse count (hard)')        hp,
     (SELECT value FROM v$sysstat WHERE name='parse count (total)')       pc"""),
    'M01': ('M01_spfile_check', '参数文件类型与路径（spfile/pfile）', """
SELECT name, value, isdefault
FROM v$parameter
WHERE name IN ('spfile','pfile','db_name','db_block_size','sga_target','pga_aggregate_target',
               'processes','sessions','open_cursors','cursor_sharing','optimizer_features_enable')
ORDER BY name"""),
    'M02': ('M02_controlfile', '控制文件副本数量与状态（单副本属单点风险）', """
SELECT name, status, is_recovery_dest_file, block_size, file_size_blks
FROM v$controlfile"""),
    'M04': ('M04_pwfile_users', '口令文件中的特权用户', """
SELECT * FROM v$pwfile_users"""),
    'M05': ('M05_stats_job_lastrun', '统计信息采集作业最近执行情况', """
SELECT owner, job_name, state,
       TO_CHAR(last_start_date,'YYYY-MM-DD HH24:MI:SS') AS last_start,
       run_count, failure_count
FROM dba_scheduler_jobs
WHERE job_name LIKE '%GATHER_STATS%' OR job_name LIKE '%STATS%'
ORDER BY owner, job_name"""),
    'M06': ('M06_tablespace_detail', '表空间/数据文件大小、自动扩展与上限', """
SELECT tablespace_name, file_name,
       ROUND(bytes/1024/1024, 2) AS size_mb,
       autoextensible,
       ROUND(maxbytes/1024/1024, 2) AS max_mb,
       status, online_status
FROM dba_data_files
ORDER BY tablespace_name, file_name"""),
    'M07': ('M07_segment_growth_source', '按表空间汇总的段占用（容量增长趋势基线）', """
SELECT tablespace_name,
       COUNT(*) AS segment_count,
       ROUND(SUM(bytes)/1024/1024/1024, 2) AS total_gb
FROM dba_segments
GROUP BY tablespace_name
ORDER BY total_gb DESC"""),
    'M08': ('M08_fra_usage', '快速恢复区（FRA）空间使用率', """
SELECT file_type,
       ROUND(percent_space_used, 2)        AS used_pct,
       ROUND(percent_space_reclaimable, 2) AS reclaimable_pct,
       ROUND(number_of_files, 0)           AS file_count
FROM v$flash_recovery_area_usage"""),
    'M11': ('M11_archive_gap', '归档日志断档检测（返回行即为断档）', """
SELECT * FROM v$archive_gap"""),
    'M11b': ('M11b_archived_log_stat', '近 7 天归档日志量与切换频率', """
SELECT TO_CHAR(TRUNC(first_time),'YYYY-MM-DD') AS log_date,
       COUNT(*) AS log_count,
       ROUND(SUM(blocks*block_size)/1024/1024/1024, 2) AS archive_gb
FROM v$archived_log
WHERE first_time > SYSDATE - 7
GROUP BY TRUNC(first_time)
ORDER BY log_date DESC"""),
    'M12': ('M12_param_nondefault', '非默认参数清单（参数基线 diff 依据）', """
SELECT name, value, isdefault, issys_modifiable
FROM v$parameter
WHERE isdefault = 'FALSE'
  AND name NOT IN ('audit_file_dest','background_dump_dest','user_dump_dest',
                   'core_dump_dest','db_domain','db_unique_name','instance_name',
                   'service_names','local_listener','remote_listener','control_files',
                   'log_archive_dest_1','log_archive_dest_state_1','spfile','pfile',
                   'compatible','db_name','dbid','dispatchers','sga_target',
                   'pga_aggregate_target','memory_target','undo_tablespace',
                   'cluster_database','cluster_database_instances','instance_number',
                   'thread','undo_management','audit_trail','license_max_sessions',
                   'license_sessions_warning','event','sessions','processes',
                   'db_block_buffers','shared_pool_size','large_pool_size','java_pool_size',
                   'streams_pool_size','db_cache_size','result_cache_max_size',
                   'db_recovery_file_dest','db_recovery_file_dest_size')
ORDER BY name"""),
    'H02': ('H02_background_process', '后台进程清单与启动时间（核查进程完整性）', """
SELECT paddr, ps.pid, ps.spid, ps.username AS os_user, name, description
FROM v$bgprocess bp, v$process ps
WHERE bp.paddr = ps.addr
  AND bp.paddr <> '00'
ORDER BY name"""),
    'H03': ('H03_deadlock_lock', '当前锁与阻塞明细（含锁类型与请求模式）', """
SELECT l.type, l.id1, l.id2, l.lmode, l.request, l.ctime, l.block,
       s.sid, s.serial#, s.username, s.machine, s.sql_id
FROM v$lock l
LEFT JOIN v$session s ON l.sid = s.sid
WHERE l.block = 1 OR l.request > 0
ORDER BY l.ctime DESC"""),
    'H11': ('H11_datafile_header', '数据文件头与视图一致性（ERROR 非空即异常）', """
SELECT file#, name, status, error, checkpoint_change#,
       TO_CHAR(checkpoint_time,'YYYY-MM-DD HH24:MI:SS') AS checkpoint_time
FROM v$datafile_header
WHERE error IS NOT NULL OR status <> 'ONLINE'"""),
    'H12': ('H12_redo_log', '在线日志组状态、成员数与大小', """
SELECT l.group#, l.thread#, l.sequence#, l.bytes/1024/1024 AS size_mb,
       l.members, l.status, l.archived,
       f.member
FROM v$log l
LEFT JOIN v$logfile f ON l.group# = f.group#
ORDER BY l.group#, f.member"""),
    'H14': ('H14_index_health', '索引状态与非空块分布（碎片/失效排查）', """
SELECT owner, index_name, index_type, status, tablespace_name,
       NVL(num_rows,0) AS num_rows, NVL(distinct_keys,0) AS distinct_keys,
       NVL(leaf_blocks,0) AS leaf_blocks,
       ROUND(NVL(num_rows,0)/NULLIF(NVL(leaf_blocks,0),0), 1) AS rows_per_leaf
FROM dba_indexes
WHERE status <> 'VALID'
   OR (NVL(leaf_blocks,0) > 10000 AND NVL(num_rows,0)/NULLIF(NVL(leaf_blocks,0),0) < 10)
ORDER BY leaf_blocks DESC"""),
    'H17': ('H17_unused_index', '索引使用监控结果（未使用索引清理候选）', """
SELECT index_name, table_name, monitoring, used, start_monitoring, end_monitoring
FROM v$object_usage
WHERE used = 'NO'
ORDER BY table_name, index_name"""),
    'H19': ('H19_session_load', '会话负载与历史峰值', """
SELECT resource_name, current_utilization, max_utilization, limit_value,
       ROUND(max_utilization * 100 / NULLIF(TO_NUMBER(CASE WHEN REGEXP_LIKE(limit_value, '^[0-9]+$') THEN limit_value END),0), 2) AS peak_pct
FROM v$resource_limit
WHERE resource_name IN ('sessions','processes','transactions')
ORDER BY resource_name"""),
    'H20': ('H20_file_io', '数据文件 I/O 统计（定位热点文件与 IO 瓶颈）', """
SELECT df.name AS file_name,
       fs.phyrds, fs.phywrts,
       ROUND(fs.readtim/NULLIF(fs.phyrds,0)*10, 2)  AS avg_read_ms,
       ROUND(fs.writetim/NULLIF(fs.phywrts,0)*10, 2) AS avg_write_ms
FROM v$filestat fs, v$datafile df
WHERE fs.file# = df.file#
ORDER BY fs.phyrds + fs.phywrts DESC"""),
    'H21': ('H21_temp_undo_usage', '临时表空间与回滚段使用情况', """
SELECT 'TEMP' AS seg_type, tablespace AS tablespace_name,
       ROUND(SUM(blocks * (SELECT value FROM v$parameter WHERE name='db_block_size'))/1024/1024, 2) AS used_mb
FROM v$tempseg_usage
GROUP BY tablespace
UNION ALL
SELECT 'UNDO' AS seg_type, tablespace_name,
       ROUND(SUM(bytes)/1024/1024, 2) AS used_mb
FROM dba_undo_extents
GROUP BY tablespace_name"""),
    'H25': ('H25_backup_trend', '近 30 天备份大小趋势（异常骤降说明备份内容缺失）', """
SELECT TO_CHAR(TRUNC(start_time),'YYYY-MM-DD') AS backup_date,
       COUNT(*) AS job_count,
       ROUND(SUM(output_bytes)/1024/1024/1024, 2) AS output_gb,
       ROUND(AVG(elapsed_seconds)/60, 1) AS avg_min
FROM v$rman_backup_job_details
WHERE start_time > SYSDATE - 30
GROUP BY TRUNC(start_time)
ORDER BY backup_date DESC"""),
    'H26': ('H26_backup_window', '备份执行窗口（是否与业务高峰重叠）', """
SELECT session_key, input_type, status,
       TO_CHAR(start_time,'HH24:MI') AS start_hm,
       TO_CHAR(end_time,'HH24:MI')   AS end_hm,
       ROUND(elapsed_seconds/60, 1)  AS elapsed_min,
       TO_CHAR(start_time,'YYYY-MM-DD') AS start_date
FROM v$rman_backup_job_details
WHERE start_time > SYSDATE - 30
ORDER BY elapsed_seconds DESC"""),
    'H28': ('H28_privileged_accounts', '特权角色授予情况（权限最小化核查）', """
SELECT grantee, granted_role, admin_option, default_role
FROM dba_role_privs
WHERE granted_role IN ('DBA','SYSDBA','SYSOPER','RESOURCE','IMP_FULL_DATABASE',
                       'EXP_FULL_DATABASE','SELECT_CATALOG_ROLE','EXECUTE_CATALOG_ROLE')
ORDER BY granted_role, grantee"""),
    'H29': ('H29_password_policy', '口令策略配置（复杂度/有效期/锁定策略）', """
SELECT profile, resource_name, limit
FROM dba_profiles
WHERE resource_type = 'PASSWORD'
ORDER BY profile, resource_name"""),
    'C_CL': ('C01_rac_cluster_view', 'RAC 集群视图可用性与实例分布', """
SELECT inst_id, instance_name, host_name, status, database_status,
       TO_CHAR(startup_time,'YYYY-MM-DD HH24:MI:SS') AS startup_time
FROM gv$instance ORDER BY inst_id"""),
    'C_ASM': ('C02_asm_diskgroup', 'ASM 磁盘组容量与状态', """
SELECT name, state, type, total_mb, free_mb,
       ROUND((total_mb - free_mb)/NULLIF(total_mb,0)*100, 2) AS used_pct,
       offline_disks
FROM v$asm_diskgroup
ORDER BY name"""),
    'C_GES': ('C03_ges_statistics', '全局缓存服务（GES）等待统计', """
SELECT inst_id, event, total_waits, time_waited
FROM gv$system_event
WHERE event LIKE 'gc %' OR event LIKE 'ges %'
ORDER BY time_waited DESC"""),
}

SQLSERVER = {
    'W01': ('W01_instance_info', '实例版本、补丁级别与主机信息', """
SELECT @@SERVERNAME AS server_name,
       SERVERPROPERTY('ProductVersion') AS product_version,
       SERVERPROPERTY('ProductLevel')   AS product_level,
       SERVERPROPERTY('Edition')        AS edition,
       SERVERPROPERTY('InstanceName')   AS instance_name,
       SERVERPROPERTY('IsClustered')    AS is_clustered"""),
    'W02': ('W02_login_env', '当前登录环境（验证远程登录可用性）', """
SELECT SUSER_SNAME() AS login_name,
       DB_NAME()     AS cur_db,
       HOST_NAME()   AS cur_host,
       @@SPID        AS spid"""),
    'W03': ('W03_session_usage', '会话总数、活跃会话与用户会话数', """
SELECT COUNT(*) AS total_sessions,
       SUM(CASE WHEN status <> 'sleeping' THEN 1 ELSE 0 END) AS active_sessions,
       SUM(CASE WHEN is_user_process = 1 THEN 1 ELSE 0 END)  AS user_sessions,
       (SELECT @@MAX_CONNECTIONS) AS max_connections
FROM sys.dm_exec_sessions"""),
    'W04': ('W04_errorlog_recent', '错误日志中的 Error 级记录（sp_readerrorlog）', """
EXEC master.dbo.sp_readerrorlog 0, 1, N'Error'"""),
    'W05': ('W05_backup_history_7d', '近 7 天备份历史（类型/大小/耗时）', """
SELECT bs.database_name,
       CASE bs.type WHEN 'D' THEN 'Full' WHEN 'I' THEN 'Diff'
                    WHEN 'L' THEN 'Log'  ELSE bs.type END AS backup_type,
       bs.backup_finish_date,
       CAST(bs.backup_size/1048576.0 AS DECIMAL(18,2)) AS size_mb,
       DATEDIFF(MINUTE, bs.backup_start_date, bs.backup_finish_date) AS duration_min,
       mf.physical_device_name
FROM msdb.dbo.backupset bs
LEFT JOIN msdb.dbo.backupmediafamily mf ON bs.media_set_id = mf.media_set_id
WHERE bs.backup_finish_date > DATEADD(DAY, -7, GETDATE())
ORDER BY bs.backup_finish_date DESC"""),
    'W06': ('W06_file_capacity', '数据/日志文件容量与自动增长设置', """
SELECT DB_NAME(database_id) AS db_name,
       name, physical_name, type_desc,
       CAST(size*8.0/1024 AS DECIMAL(18,2)) AS size_mb,
       CASE WHEN max_size = -1 THEN -1
            ELSE CAST(max_size*8.0/1024 AS DECIMAL(18,2)) END AS max_mb,
       growth, is_percent_growth
FROM sys.master_files
ORDER BY db_name, type_desc"""),
    'W07': ('W07_backup_coverage', '各库最近一次全备时间（覆盖完整性核查）', """
SELECT d.name AS database_name, d.recovery_model_desc,
       MAX(CASE WHEN bs.type = 'D' THEN bs.backup_finish_date END) AS last_full_backup,
       MAX(CASE WHEN bs.type = 'I' THEN bs.backup_finish_date END) AS last_diff_backup,
       MAX(CASE WHEN bs.type = 'L' THEN bs.backup_finish_date END) AS last_log_backup,
       DATEDIFF(HOUR,
                MAX(CASE WHEN bs.type = 'D' THEN bs.backup_finish_date END),
                GETDATE()) AS hours_since_full
FROM sys.databases d
LEFT JOIN msdb.dbo.backupset bs ON d.name = bs.database_name
WHERE d.database_id > 4
GROUP BY d.name, d.recovery_model_desc
ORDER BY hours_since_full DESC"""),
    'W08': ('W08_cluster_nodes', '集群节点清单（sys.dm_os_cluster_nodes；未装群集返回空集）', """
SELECT NodeName, status_description, is_current_owner
FROM sys.dm_os_cluster_nodes"""),
    'W08b': ('W08b_db_state', '各数据库状态、恢复模式与日志复用等待', """
SELECT name, state_desc, recovery_model_desc, user_access_desc,
       is_auto_close_on, is_auto_shrink_on, is_read_only,
       compatibility_level, log_reuse_wait_desc
FROM sys.databases
ORDER BY name"""),
    'W09': ('W09_config_nondefault', '实例配置项（参数基线 diff 依据）', """
SELECT name, value, value_in_use, is_dynamic, is_advanced
FROM sys.configurations
ORDER BY name"""),
    'W10': ('W10_invalid_index', '禁用/假设索引与非预期对象状态（SQL Server 无"无效对象"概念，以此替代）', """
SELECT OBJECT_NAME(i.object_id) AS table_name,
       i.name AS index_name, i.type_desc,
       i.is_disabled, i.is_hypothetical,
       OBJECTPROPERTY(i.object_id, 'IsUserTable') AS is_user_table
FROM sys.indexes i
WHERE i.is_disabled = 1 OR i.is_hypothetical = 1
ORDER BY table_name, index_name"""),
    'W11': ('W11_space_usage', '逻辑存储空间使用率（数据文件分配/已用/可用）', """
SELECT CONVERT(VARCHAR(50), database_id) AS db_id, file_id,
       CAST(total_page_count*8.0/1024 AS DECIMAL(18,2)) AS total_mb,
       CAST(allocated_extent_page_count*8.0/1024 AS DECIMAL(18,2)) AS used_mb,
       CAST(unallocated_extent_page_count*8.0/1024 AS DECIMAL(18,2)) AS free_mb
FROM sys.dm_db_file_space_usage"""),
    'W12': ('W12_stats_stale', '统计信息超过 7 天未更新的用户表', """
SELECT OBJECT_NAME(id) AS table_name,
       STATS_DATE(id, indid) AS last_updated,
       DATEDIFF(DAY, STATS_DATE(id, indid), GETDATE()) AS days_stale
FROM sys.sysindexes
WHERE OBJECTPROPERTY(id, 'IsUserTable') = 1
  AND indid IN (0, 1)
  AND (STATS_DATE(id, indid) IS NULL
       OR STATS_DATE(id, indid) < DATEADD(DAY, -7, GETDATE()))
ORDER BY last_updated"""),
    'W13': ('W13_job_history', '近 7 天作业执行结果与耗时', """
SELECT j.name AS job_name,
       CASE h.run_status WHEN 0 THEN 'Failed' WHEN 1 THEN 'Succeeded'
                         WHEN 2 THEN 'Retry'  WHEN 3 THEN 'Canceled'
                         ELSE 'Unknown' END AS run_status,
       msdb.dbo.agent_datetime(h.run_date, h.run_time) AS run_time,
       h.run_duration, h.message
FROM msdb.dbo.sysjobhistory h
JOIN msdb.dbo.sysjobs j ON h.job_id = j.job_id
WHERE h.step_id = 0
  AND msdb.dbo.agent_datetime(h.run_date, h.run_time) > DATEADD(DAY, -7, GETDATE())
ORDER BY h.run_date DESC, h.run_time DESC"""),
    'W14': ('W14_blocking', '当前阻塞链与等锁会话（含等待类型与阻塞时长）', """
SELECT r.session_id, r.blocking_session_id, r.wait_type, r.wait_time,
       r.status, r.command,
       DB_NAME(r.database_id) AS db_name,
       SUBSTRING(t.text, 1, 200) AS sql_text
FROM sys.dm_exec_requests r
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.blocking_session_id <> 0
ORDER BY r.wait_time DESC"""),
    'W15': ('W15_top_sql', '慢查询 TOP20（按累计耗时排序）', """
SELECT TOP 20 qs.execution_count,
       CAST(qs.total_elapsed_time/1000.0 AS DECIMAL(18,2)) AS total_ms,
       CAST(qs.total_elapsed_time/NULLIF(qs.execution_count,0)/1000.0 AS DECIMAL(18,2)) AS avg_ms,
       qs.total_logical_reads,
       qs.total_physical_reads,
       SUBSTRING(st.text, 1, 200) AS sql_text
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY qs.total_elapsed_time DESC"""),
    'W16': ('W16_wait_stats', '服务器资源与等待事件 TOP20（已排除空闲等待）', """
SELECT TOP 20 wait_type, waiting_tasks_count, wait_time_ms,
       CAST(wait_time_ms/NULLIF(waiting_tasks_count,0) AS DECIMAL(18,1)) AS avg_wait_ms
FROM sys.dm_os_wait_stats
WHERE wait_type NOT IN ('CLR_SEMAPHORE','LAZYWRITER_SLEEP','RESOURCE_QUEUE',
      'SLEEP_TASK','SLEEP_SYSTEMTASK','SQLTRACE_BUFFER_FLUSH','WAITFOR',
      'LOGMGR_QUEUE','CHECKPOINT_QUEUE','REQUEST_FOR_DEADLOCK_SEARCH',
      'XE_TIMER_EVENT','BROKER_TO_FLUSH','BROKER_TASK_STOP','CLR_MANUAL_EVENT',
      'CLR_AUTO_EVENT','DISPATCHER_QUEUE_SEMAPHORE','FT_IFTS_SCHEDULER_IDLE_WAIT',
      'XE_DISPATCHER_WAIT','XE_DISPATCHER_JOIN','BROKER_EVENTHANDLER','TRACEWRITE',
      'SQLTRACE_INCREMENTAL_FLUSH_SLEEP','DIRTY_PAGE_POLL','HADR_FILESTREAM_IOMGR_IOCOMPLETION')
ORDER BY wait_time_ms DESC"""),
    'W17': ('W17_logins', '服务器登录名与状态', """
SELECT p.name, p.type_desc, p.is_disabled, p.create_date, p.modify_date,
       l.is_policy_checked, l.is_expiration_checked
FROM sys.server_principals p
LEFT JOIN sys.sql_logins l ON p.principal_id = l.principal_id
WHERE p.type IN ('S','U','G')
ORDER BY p.name"""),
    'W18': ('W18_sysadmin_members', 'sysadmin 固定角色成员（高危账号核查）', """
SELECT p.name AS member_name, p.type_desc, p.is_disabled
FROM sys.server_role_members rm
JOIN sys.server_principals r ON rm.role_principal_id = r.principal_id
JOIN sys.server_principals p ON rm.member_principal_id = p.principal_id
WHERE r.name = 'sysadmin'
ORDER BY p.name"""),
    'W19': ('W19_db_options', '各库自动统计/快照隔离/页面校验选项', """
SELECT name, is_auto_create_stats_on, is_auto_update_stats_on,
       is_auto_update_stats_async_on, is_read_committed_snapshot_on,
       snapshot_isolation_state_desc, page_verify_option_desc
FROM sys.databases
ORDER BY name"""),
    'W20': ('W20_collect_stamp', '本次采集时间戳与实例标识（留痕用）', """
SELECT GETDATE() AS collected_at, @@SERVERNAME AS server_name,
       DB_NAME() AS cur_db, SUSER_SNAME() AS login_name"""),
    'W10b': ('W10b_tempdb_usage', 'tempdb 空间使用分布', """
SELECT SUM(unallocated_extent_page_count)*8.0/1024 AS free_mb,
       SUM(user_object_reserved_page_count)*8.0/1024 AS user_object_mb,
       SUM(internal_object_reserved_page_count)*8.0/1024 AS internal_object_mb,
       SUM(version_store_reserved_page_count)*8.0/1024 AS version_store_mb
FROM tempdb.sys.dm_db_file_space_usage"""),
    'M01': ('M01_startup_params', '实例启动参数与内存配置', """
SELECT name, value, value_in_use, is_dynamic
FROM sys.configurations
WHERE name IN ('max server memory (MB)','min server memory (MB)',
               'max degree of parallelism','cost threshold for parallelism',
               'max worker threads','min server memory (MB)',
               'backup compression default','recovery interval (min)',
               'remote query timeout (s)','user connections')
ORDER BY name"""),
    'M02': ('M02_system_db_state', '系统库（master/model/msdb/tempdb）状态', """
SELECT d.name, d.state_desc, d.recovery_model_desc,
       CAST(SUM(mf.size)*8.0/1024 AS DECIMAL(18,2)) AS total_mb
FROM sys.master_files mf
JOIN sys.databases d ON mf.database_id = d.database_id
WHERE d.database_id <= 4
GROUP BY d.name, d.state_desc, d.recovery_model_desc
ORDER BY d.name"""),
    'M03': ('M03_network_protocol', '监听端点与协议配置', """
SELECT name, protocol_desc, type_desc, port, is_dynamic_port, state_desc
FROM sys.tcp_endpoints
WHERE type_desc = 'TSQL'
ORDER BY name"""),
    'M04': ('M04_log_space', '各库事务日志空间使用率（DBCC SQLPERF）', """
DBCC SQLPERF(LOGSPACE)"""),
    'M05': ('M05_vlf_count', '当前库虚拟日志文件数量（过多影响恢复速度）', """
DBCC LOGINFO"""),
    'M06': ('M06_data_usage', '当前库数据文件空间与可用空间', """
SELECT CONVERT(VARCHAR(50), database_id) AS db_id,
       file_id,
       CAST(total_page_count*8.0/1024 AS DECIMAL(18,2)) AS total_mb,
       CAST(allocated_extent_page_count*8.0/1024 AS DECIMAL(18,2)) AS used_mb,
       CAST(unallocated_extent_page_count*8.0/1024 AS DECIMAL(18,2)) AS free_mb
FROM sys.dm_db_file_space_usage"""),
    'M07': ('M07_table_size_top', '占用空间最大的 20 张表', """
SELECT TOP 20 OBJECT_NAME(p.object_id) AS table_name,
       SUM(p.rows) AS row_count,
       CAST(SUM(a.total_pages)*8.0/1024 AS DECIMAL(18,2)) AS total_mb,
       CAST(SUM(a.used_pages)*8.0/1024 AS DECIMAL(18,2)) AS used_mb
FROM sys.partitions p
JOIN sys.allocation_units a ON p.partition_id = a.container_id
WHERE p.index_id IN (0,1)
GROUP BY p.object_id
ORDER BY total_mb DESC"""),
    'M11': ('M11_log_reuse_wait', '日志无法截断的库（log_reuse_wait 非 NOTHING 即需关注）', """
SELECT name, recovery_model_desc, log_reuse_wait_desc
FROM sys.databases
WHERE log_reuse_wait_desc <> 'NOTHING'
ORDER BY name"""),
    'M12': ('M12_config_snapshot', '全量实例配置快照（供与上月逐项 diff，识别未报备变更）', """
SELECT name, value, value_in_use, is_dynamic, is_advanced
FROM sys.configurations
ORDER BY name"""),
    'H02': ('H02_service_state', 'SQL Server 相关服务运行状态（sys.dm_server_services 需 2008R2+，更低版本请禁用）', """
SELECT servicename, status_desc, startup_type_desc, process_id
FROM sys.dm_server_services"""),
    'H03': ('H03_os_threads', '线程/调度器状态（排查线程僵死与调度器阻塞）', """
SELECT scheduler_id, cpu_id, status, is_online, current_tasks_count,
       runnable_tasks_count, work_queue_count, pending_disk_io_count
FROM sys.dm_os_schedulers
WHERE scheduler_id < 255
ORDER BY scheduler_id"""),
    'H11': ('H11_file_state', '数据/日志文件状态异常清单（非 ONLINE 即需处置）', """
SELECT DB_NAME(mf.database_id) AS db_name, mf.file_id, mf.name AS logical_name,
       mf.state_desc, mf.is_read_only, mf.is_media_read_only, mf.is_percent_growth
FROM sys.master_files mf
WHERE mf.state_desc <> 'ONLINE'
ORDER BY db_name, mf.file_id"""),
    'H12': ('H12_io_stats', '数据库文件 I/O 延迟统计（sys.dm_io_virtual_file_stats 需 2008+，2005 改用 fn_virtualfilestats）', """
SELECT DB_NAME(vfs.database_id) AS db_name,
       mf.physical_name,
       vfs.num_of_reads, vfs.num_of_writes,
       CAST(vfs.io_stall_read_ms/NULLIF(vfs.num_of_reads,0) AS DECIMAL(18,2)) AS avg_read_ms,
       CAST(vfs.io_stall_write_ms/NULLIF(vfs.num_of_writes,0) AS DECIMAL(18,2)) AS avg_write_ms
FROM sys.dm_io_virtual_file_stats(NULL, NULL) vfs
JOIN sys.master_files mf
  ON vfs.database_id = mf.database_id AND vfs.file_id = mf.file_id
ORDER BY vfs.io_stall DESC"""),
    'H14': ('H14_index_fragmentation', '索引碎片率超 30% 的索引（页数 > 1000）', """
SELECT DB_NAME() AS db_name,
       OBJECT_NAME(ips.object_id) AS table_name,
       i.name AS index_name, ips.index_type_desc,
       CAST(ips.avg_fragmentation_in_percent AS DECIMAL(5,2)) AS frag_pct,
       ips.page_count
FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'SAMPLED') ips
JOIN sys.indexes i ON ips.object_id = i.object_id AND ips.index_id = i.index_id
WHERE ips.page_count > 1000 AND ips.avg_fragmentation_in_percent > 30
ORDER BY ips.avg_fragmentation_in_percent DESC"""),
    'H15': ('H15_object_count', '当前库对象统计（表/索引/存储过程/触发器）', """
SELECT type_desc, COUNT(*) AS obj_count
FROM sys.objects
WHERE is_ms_shipped = 0
GROUP BY type_desc
ORDER BY obj_count DESC"""),
    'H17': ('H17_unused_index', '从未被使用的索引（清理候选，需观察一个业务周期）', """
SELECT OBJECT_NAME(i.object_id) AS table_name,
       i.name AS index_name, i.type_desc,
       ISNULL(s.user_seeks,0) AS user_seeks,
       ISNULL(s.user_scans,0) AS user_scans,
       ISNULL(s.user_lookups,0) AS user_lookups,
       ISNULL(s.user_updates,0) AS user_updates
FROM sys.indexes i
LEFT JOIN sys.dm_db_index_usage_stats s
  ON i.object_id = s.object_id AND i.index_id = s.index_id
 AND s.database_id = DB_ID()
WHERE i.is_primary_key = 0 AND i.is_unique_constraint = 0
  AND i.type > 1
  AND ISNULL(s.user_seeks,0) + ISNULL(s.user_scans,0) + ISNULL(s.user_lookups,0) = 0
ORDER BY ISNULL(s.user_updates,0) DESC"""),
    'H20': ('H20_file_latency', '文件级 I/O 延迟（> 20ms 需关注；2005 改用 fn_virtualfilestats）', """
SELECT DB_NAME(database_id) AS db_name, file_id,
       CAST(io_stall_read_ms/NULLIF(num_of_reads,0) AS DECIMAL(18,2)) AS read_latency_ms,
       CAST(io_stall_write_ms/NULLIF(num_of_writes,0) AS DECIMAL(18,2)) AS write_latency_ms,
       num_of_reads, num_of_writes
FROM sys.dm_io_virtual_file_stats(NULL, NULL)
ORDER BY io_stall DESC"""),
    'H22': ('H22_top_waits', '核心等待事件 TOP15（排除空闲等待，定位真实瓶颈）', """
SELECT TOP 15 wait_type, waiting_tasks_count, wait_time_ms,
       CAST(wait_time_ms/NULLIF(waiting_tasks_count,0) AS DECIMAL(18,1)) AS avg_wait_ms,
       CAST(wait_time_ms*100.0/SUM(wait_time_ms) OVER() AS DECIMAL(5,2)) AS pct_of_total
FROM sys.dm_os_wait_stats
WHERE wait_type NOT IN ('CLR_SEMAPHORE','LAZYWRITER_SLEEP','RESOURCE_QUEUE','SLEEP_TASK',
      'SLEEP_SYSTEMTASK','SQLTRACE_BUFFER_FLUSH','WAITFOR','LOGMGR_QUEUE','CHECKPOINT_QUEUE',
      'REQUEST_FOR_DEADLOCK_SEARCH','XE_TIMER_EVENT','BROKER_TO_FLUSH','BROKER_TASK_STOP',
      'CLR_MANUAL_EVENT','CLR_AUTO_EVENT','DISPATCHER_QUEUE_SEMAPHORE','FT_IFTS_SCHEDULER_IDLE_WAIT',
      'XE_DISPATCHER_WAIT','XE_DISPATCHER_JOIN','BROKER_EVENTHANDLER','TRACEWRITE',
      'SQLTRACE_INCREMENTAL_FLUSH_SLEEP','DIRTY_PAGE_POLL')
ORDER BY wait_time_ms DESC"""),
    'H22b': ('H22b_memory_clerk', '内存 Clerk TOP20（定位内存异常消耗）', """
SELECT TOP 20 type, SUM(pages_kb)/1024.0 AS mb
FROM sys.dm_os_memory_clerks
GROUP BY type
ORDER BY mb DESC"""),
    'H19': ('H19_session_load', '会话负载与历史峰值', """
SELECT COUNT(*) AS total_sessions,
       SUM(CASE WHEN status <> 'sleeping' THEN 1 ELSE 0 END) AS active_sessions,
       (SELECT @@MAX_CONNECTIONS) AS max_connections,
       CAST(COUNT(*)*100.0/(SELECT @@MAX_CONNECTIONS) AS DECIMAL(5,2)) AS usage_pct
FROM sys.dm_exec_sessions"""),
    'H21': ('H21_space_deep', '存储空间深度（数据文件分配/已用/可用）', """
SELECT CONVERT(VARCHAR(50), database_id) AS db_id, file_id,
       CAST(total_page_count*8.0/1024 AS DECIMAL(18,2)) AS total_mb,
       CAST(allocated_extent_page_count*8.0/1024 AS DECIMAL(18,2)) AS used_mb,
       CAST(unallocated_extent_page_count*8.0/1024 AS DECIMAL(18,2)) AS free_mb
FROM sys.dm_db_file_space_usage"""),
    'H23': ('H23_cpu_pressure', 'CPU 压力（调度器运行队列与信号等待）', """
SELECT scheduler_id, cpu_id, runnable_tasks_count, work_queue_count,
       pending_disk_io_count
FROM sys.dm_os_schedulers
WHERE scheduler_id < 255
  AND (runnable_tasks_count > 0 OR work_queue_count > 0)
ORDER BY runnable_tasks_count DESC"""),
    'H24': ('H24_hit_ratio', '缓冲命中率与页生命周期（内存压力核心指标）', """
SELECT MAX(CASE WHEN counter_name = 'Buffer cache hit ratio'
                THEN cntr_value END) AS buffer_hit_raw,
       MAX(CASE WHEN counter_name = 'Buffer cache hit ratio base'
                THEN cntr_value END) AS buffer_hit_base,
       MAX(CASE WHEN counter_name = 'Page life expectancy'
                THEN cntr_value END) AS page_life_expectancy,
       MAX(CASE WHEN counter_name = 'Lazy writes/sec'
                THEN cntr_value END) AS lazy_writes_per_sec,
       MAX(CASE WHEN counter_name = 'Page splits/sec'
                THEN cntr_value END) AS page_splits_per_sec
FROM sys.dm_os_performance_counters"""),
    'H25': ('H25_backup_size_trend', '近 30 天备份量趋势', """
SELECT CAST(bs.backup_finish_date AS DATE) AS backup_date,
       COUNT(*) AS job_count,
       CAST(SUM(bs.backup_size)/1048576.0 AS DECIMAL(18,2)) AS total_mb
FROM msdb.dbo.backupset bs
WHERE bs.backup_finish_date > DATEADD(DAY, -30, GETDATE())
GROUP BY CAST(bs.backup_finish_date AS DATE)
ORDER BY backup_date DESC"""),
    'H26': ('H26_backup_window', '备份执行时段分布（是否与业务高峰重叠）', """
SELECT bs.database_name,
       DATEPART(HOUR, bs.backup_start_date) AS start_hour,
       DATEDIFF(MINUTE, bs.backup_start_date, bs.backup_finish_date) AS duration_min,
       bs.backup_finish_date
FROM msdb.dbo.backupset bs
WHERE bs.backup_finish_date > DATEADD(DAY, -30, GETDATE())
ORDER BY duration_min DESC"""),
    'H27': ('H27_backup_verify', '最近备份文件与可校验性信息', """
SELECT TOP 50 bs.database_name, bs.type, bs.backup_finish_date,
       mf.physical_device_name, bs.is_copy_only, bs.has_backup_checksums
FROM msdb.dbo.backupset bs
LEFT JOIN msdb.dbo.backupmediafamily mf ON bs.media_set_id = mf.media_set_id
WHERE bs.backup_finish_date > DATEADD(DAY, -7, GETDATE())
ORDER BY bs.backup_finish_date DESC"""),
    'H28': ('H28_security_roles', '固定服务器角色成员清单', """
SELECT r.name AS role_name, p.name AS member_name, p.is_disabled
FROM sys.server_role_members rm
JOIN sys.server_principals r ON rm.role_principal_id = r.principal_id
JOIN sys.server_principals p ON rm.member_principal_id = p.principal_id
ORDER BY r.name, p.name"""),
    'H29': ('H29_login_policy', '登录口令策略与到期状态', """
SELECT name, is_policy_checked, is_expiration_checked, is_disabled,
       LOGINPROPERTY(name, 'PasswordLastSetTime') AS pwd_last_set,
       LOGINPROPERTY(name, 'DaysUntilExpiration') AS days_until_expire,
       LOGINPROPERTY(name, 'IsLocked') AS is_locked,
       LOGINPROPERTY(name, 'BadPasswordCount') AS bad_pwd_count
FROM sys.sql_logins
ORDER BY name"""),
    'C_MIR': ('C01_mirroring_state', '数据库镜像状态与角色', """
SELECT DB_NAME(database_id) AS db_name,
       mirroring_role_desc, mirroring_state_desc,
       mirroring_safety_level_desc, mirroring_witness_state_desc,
       mirroring_partner_instance
FROM sys.database_mirroring
WHERE mirroring_guid IS NOT NULL"""),
    'C_AG': ('C02_ag_replica', 'AlwaysOn 副本状态（仅 SQL Server 2012+，2005/2008 请禁用本项）', """
SELECT ag.name AS ag_name,
       ar.replica_server_name, ar.availability_mode_desc,
       ars.role_desc, ars.operational_state_desc,
       ars.connected_state_desc, ars.synchronization_health_desc
FROM sys.dm_hadr_availability_replica_states ars
JOIN sys.availability_replicas ar ON ars.replica_id = ar.replica_id
JOIN sys.availability_groups  ag ON ar.group_id    = ag.group_id
ORDER BY ag.name, ar.replica_server_name"""),
    'C_LS': ('C03_log_shipping', '日志传送监控状态', """
SELECT primary_server, primary_database, last_backup_file, last_backup_date,
       backup_threshold, threshold_alert_enabled, history_retention_period
FROM msdb.dbo.log_shipping_monitor_primary
ORDER BY primary_database"""),
}

DM8 = {
    'W01': ('W01_instance_status', '实例状态、模式与主机信息', """
SELECT * FROM V$INSTANCE"""),
    'W02': ('W02_login_env', '当前登录用户与环境（验证远程登录）', """
SELECT USER AS login_user, SYS_CONTEXT('USERENV','DB_NAME') AS db_name FROM DUAL"""),
    'W03': ('W03_session_count', '当前会话总数', """
SELECT COUNT(*) AS session_count FROM V$SESSIONS"""),
    'W03b': ('W03b_max_sessions', '最大会话数等容量参数', """
SELECT PARA_NAME, PARA_VALUE FROM V$DM_INI
WHERE PARA_NAME IN ('MAX_SESSIONS','MAX_CONNECTIONS','MEMORY_POOL','BUFFER','MAX_BUFFER')"""),
    'W04': ('W04_active_sessions', '非空闲会话明细（含客户端 IP 与登录时间）', """
SELECT SESS_ID, USER_NAME, STATE, CLNT_IP, CLNT_HOST,
       TO_CHAR(CREATE_TIME,'YYYY-MM-DD HH24:MI:SS') AS create_time
FROM V$SESSIONS
WHERE STATE <> 'IDLE'
ORDER BY CREATE_TIME"""),
    'W05': ('W05_backupset', '备份集记录（最近备份结果）', """
SELECT * FROM V$BACKUPSET"""),
    'W06': ('W06_tablespace_usage', '表空间总大小与状态', """
SELECT T.NAME AS TABLESPACE_NAME, T.STATUS$,
       ROUND(SUM(D.BYTES)/1024/1024, 2) AS TOTAL_MB
FROM V$TABLESPACE T
LEFT JOIN DBA_DATA_FILES D ON T.NAME = D.TABLESPACE_NAME
GROUP BY T.NAME, T.STATUS$
ORDER BY TOTAL_MB DESC"""),
    'W06b': ('W06b_datafile_autoextend', '数据文件大小、自动扩展与上限', """
SELECT TABLESPACE_NAME, FILE_NAME,
       ROUND(BYTES/1024/1024, 2) AS SIZE_MB,
       AUTOEXTENSIBLE,
       ROUND(MAXBYTES/1024/1024, 2) AS MAX_MB
FROM DBA_DATA_FILES
ORDER BY TABLESPACE_NAME, FILE_NAME"""),
    'W06c': ('W06c_free_space', '各表空间剩余空间', """
SELECT TABLESPACE_NAME, ROUND(SUM(BYTES)/1024/1024, 2) AS FREE_MB
FROM DBA_FREE_SPACE
GROUP BY TABLESPACE_NAME
ORDER BY FREE_MB"""),
    'W07': ('W07_backup_param', '备份参数与工具配置（备份窗口/路径核查）', """
SELECT PARA_NAME, PARA_VALUE FROM V$DM_INI
WHERE PARA_NAME LIKE '%BACKUP%' OR PARA_NAME LIKE '%BAK%'
ORDER BY PARA_NAME"""),
    'W08': ('W08_database_info', '数据库基础信息与归档模式', """
SELECT * FROM V$DATABASE"""),
    'W09': ('W09_param_core', '核心实例参数（参数基线 diff 依据）', """
SELECT PARA_NAME, PARA_VALUE FROM V$DM_INI
WHERE PARA_NAME IN ('MEMORY_POOL','BUFFER','MAX_BUFFER','SORT_BUF_SIZE','HJ_BUF_SIZE',
                    'MAX_SESSIONS','MAX_CONNECTIONS','TEMP_SIZE','UNDO_RETENTION',
                    'PWD_POLICY','ENABLE_MONITOR','SVR_LOG','AUDIT_SWITCH')
ORDER BY PARA_NAME"""),
    'W10': ('W10_invalid_objects', '无效数据库对象统计', """
SELECT OWNER, OBJECT_TYPE, COUNT(*) AS OBJ_CNT
FROM DBA_OBJECTS
WHERE STATUS <> 'VALID'
GROUP BY OWNER, OBJECT_TYPE
ORDER BY OBJ_CNT DESC"""),
    'W11': ('W11_tablespace_usage', '逻辑存储空间使用率（表空间/数据文件/剩余空间）', """
SELECT D.TABLESPACE_NAME,
       ROUND(SUM(D.BYTES)/1024/1024, 2) AS TOTAL_MB,
       ROUND(NVL(SUM(F.FREE_MB), 0), 2)  AS FREE_MB,
       ROUND((SUM(D.BYTES)/1024/1024 - NVL(SUM(F.FREE_MB),0)) / NULLIF(SUM(D.BYTES)/1024/1024,0) * 100, 2) AS USED_PCT
FROM DBA_DATA_FILES D
LEFT JOIN (SELECT TABLESPACE_NAME, SUM(BYTES)/1024/1024 AS FREE_MB
             FROM DBA_FREE_SPACE GROUP BY TABLESPACE_NAME) F
  ON D.TABLESPACE_NAME = F.TABLESPACE_NAME
GROUP BY D.TABLESPACE_NAME
ORDER BY USED_PCT DESC"""),
    'W12': ('W12_stats_missing', '未收集统计信息的大表', """
SELECT OWNER, TABLE_NAME, NUM_ROWS, LAST_ANALYZED
FROM DBA_TABLES
WHERE LAST_ANALYZED IS NULL AND NUM_ROWS > 10000
ORDER BY NUM_ROWS DESC"""),
    'W13': ('W13_jobs', '代理作业清单与状态（SYSJOB 模式）', """
SELECT * FROM SYSJOB.SYSJOBS"""),
    'W14': ('W14_lock', '当前锁信息（BLOCK=1 为阻塞源）', """
SELECT * FROM V$LOCK WHERE BLOCKED = 1 OR LMODE <> 'NONE'"""),
    'W15': ('W15_long_exec_sql', '当前长时间执行 SQL', """
SELECT * FROM V$LONG_EXEC_SQLS"""),
    'W16': ('W16_system_event', '系统等待事件统计', """
SELECT * FROM V$SYSTEM_EVENT"""),
    'W16b': ('W16b_bufferpool', '缓冲池命中情况', """
SELECT * FROM V$BUFFERPOOL"""),
    'W17': ('W17_users', '用户清单与账号状态', """
SELECT USERNAME, ACCOUNT_STATUS, DEFAULT_TABLESPACE, PROFILE
FROM DBA_USERS
ORDER BY USERNAME"""),
    'W17b': ('W17b_role_privs', '角色授予关系（DBA 类角色核查）', """
SELECT GRANTEE, GRANTED_ROLE, ADMIN_OPTION
FROM DBA_ROLE_PRIVS
ORDER BY GRANTED_ROLE, GRANTEE"""),
    'W17c': ('W17c_sys_privs', '系统权限授予清单', """
SELECT GRANTEE, PRIVILEGE
FROM DBA_SYS_PRIVS
ORDER BY PRIVILEGE, GRANTEE"""),
    'W18': ('W18_audit_param', '审计与服务日志参数', """
SELECT PARA_NAME, PARA_VALUE FROM V$DM_INI
WHERE PARA_NAME LIKE '%AUDIT%' OR PARA_NAME IN ('SVR_LOG','ENABLE_MONITOR')"""),
    'W19': ('W19_database_info', '数据库基础信息与归档模式（台账核对依据）', """
SELECT * FROM V$DATABASE"""),
    'W20': ('W20_collect_stamp', '本次采集时间戳与实例标识（留痕用）', """
SELECT SYSDATE AS collected_at, SYS_CONTEXT('USERENV','DB_NAME') AS db_name FROM DUAL"""),
    'M01': ('M01_core_params', '关键参数文件相关配置（dm.ini 基线）', """
SELECT PARA_NAME, PARA_VALUE FROM V$DM_INI
WHERE PARA_NAME IN ('MEMORY_POOL','BUFFER','MAX_BUFFER','SORT_BUF_SIZE','HJ_BUF_SIZE',
                    'TEMP_SIZE','UNDO_RETENTION','PWD_POLICY','ENABLE_ENCRYPT',
                    'MAX_SESSIONS','MAX_CONNECTIONS','ENABLE_MONITOR')
ORDER BY PARA_NAME"""),
    'M02': ('M02_control_info', '控制文件与归档配置', """
SELECT ARCH_TYPE, ARCH_DEST, ARCH_FILE_SIZE, ARCH_SPACE_LIMIT
FROM V$DM_ARCH_INI"""),
    'M05': ('M05_stats_tables', '统计信息待更新表清单', """
SELECT OWNER, TABLE_NAME, NUM_ROWS, LAST_ANALYZED
FROM DBA_TABLES
WHERE LAST_ANALYZED IS NULL OR LAST_ANALYZED < SYSDATE - 7
ORDER BY NUM_ROWS DESC
LIMIT 50"""),
    'M06': ('M06_tablespace_detail', '表空间与数据文件明细（含自动扩展上限）', """
SELECT D.TABLESPACE_NAME, D.FILE_NAME,
       ROUND(D.BYTES/1024/1024, 2) AS SIZE_MB,
       D.AUTOEXTENSIBLE, ROUND(D.MAXBYTES/1024/1024, 2) AS MAX_MB,
       T.STATUS$
FROM DBA_DATA_FILES D
LEFT JOIN V$TABLESPACE T ON D.TABLESPACE_NAME = T.NAME
ORDER BY D.TABLESPACE_NAME"""),
    'M07': ('M07_space_by_tablespace', '按表空间汇总的段占用（容量趋势基线）', """
SELECT TABLESPACE_NAME, COUNT(*) AS SEG_CNT,
       ROUND(SUM(BYTES)/1024/1024/1024, 2) AS TOTAL_GB
FROM DBA_SEGMENTS
GROUP BY TABLESPACE_NAME
ORDER BY TOTAL_GB DESC"""),
    'M11': ('M11_archive_stat', '归档日志统计（条数与时间跨度）', """
SELECT COUNT(*) AS LOG_CNT,
       MIN(FIRST_TIME) AS MIN_TIME,
       MAX(FIRST_TIME) AS MAX_TIME
FROM V$ARCHIVED_LOG"""),
    'M12': ('M12_param_full', '全量非默认参数（参数基线 diff）', """
SELECT PARA_NAME, PARA_VALUE FROM V$DM_INI
WHERE PARA_VALUE IS NOT NULL
ORDER BY PARA_NAME"""),
    'H02': ('H02_bg_process', '实例与线程信息', """
SELECT * FROM V$THREADS"""),
    'H11': ('H11_datafile_status', '数据文件状态异常清单', """
SELECT TABLESPACE_NAME, FILE_NAME, STATUS, ONLINE_STATUS, ROUND(BYTES/1024/1024, 2) AS SIZE_MB
FROM DBA_DATA_FILES
WHERE STATUS <> 'AVAILABLE' OR ONLINE_STATUS <> 'ONLINE'"""),
    'H12': ('H12_redo_log', '联机日志文件状态', """
SELECT * FROM V$RLOG"""),
    'H14': ('H14_index_status', '索引状态异常清单', """
SELECT OWNER, INDEX_NAME, TABLE_NAME, TABLESPACE_NAME, STATUS
FROM DBA_INDEXES
WHERE STATUS <> 'VALID'
ORDER BY OWNER, INDEX_NAME"""),
    'H19': ('H19_session_load', '会话负载分布（按用户/状态）', """
SELECT USER_NAME, STATE, COUNT(*) AS SESS_CNT
FROM V$SESSIONS
GROUP BY USER_NAME, STATE
ORDER BY SESS_CNT DESC"""),
    'H21': ('H21_segment_top', '占用空间最大的段', """
SELECT OWNER, SEGMENT_NAME, SEGMENT_TYPE, TABLESPACE_NAME,
       ROUND(BYTES/1024/1024/1024, 2) AS SIZE_GB
FROM DBA_SEGMENTS
ORDER BY BYTES DESC
LIMIT 20"""),
    'H28': ('H28_privileged', 'DBA 角色成员清单', """
SELECT GRANTEE, GRANTED_ROLE FROM DBA_ROLE_PRIVS
WHERE GRANTED_ROLE IN ('DBA','SYSDBA','SYSOPER','RESOURCE','PUBLIC')
ORDER BY GRANTED_ROLE, GRANTEE"""),
    'H29': ('H29_pwd_policy', '口令策略参数', """
SELECT PARA_NAME, PARA_VALUE FROM V$DM_INI
WHERE PARA_NAME IN ('PWD_POLICY','PWD_GRACE_TIME','PWD_REUSE_MAX','PWD_LIFE_TIME',
                    'PWD_MIN_LEN','FAILED_LOGIN_ATTEMPTS','PASSWORD_LOCK_TIME')
ORDER BY PARA_NAME"""),
    'C_MAL': ('C02_mal_topology', 'MAL 通信拓扑与守护端口', 'SELECT * FROM V$DM_MAL_INI'),
    'C_ARCH': ('C03_archive_status', '归档目的端与实时归档状态', 'SELECT * FROM V$ARCH_STATUS'),
    'C_WATCH': ('C04_watcher_status', '数据守护组、主备实例与守护状态', 'SELECT * FROM V$DMWATCHER'),
    'C_CONFIG': ('C05_archive_config', '实时/异步归档配置与目标', 'SELECT * FROM V$DM_ARCH_INI'),
    'C_EP': ('C01_dsc_ep_info', 'DMDSC 节点信息（仅 DMDSC 部署，单机/守护会报错请禁用）', """
SELECT * FROM V$DSC_EP_INFO"""),
}

GBASE8A = {
    'W01': ('W01_version', '集群版本信息', """
SELECT VERSION() AS version"""),
    'W02': ('W02_login_env', '当前登录环境（验证远程登录）', """
SELECT CURRENT_USER() AS login_user, DATABASE() AS cur_db, @@hostname AS host_name"""),
    'W03': ('W03_session_count', '当前连接会话数', """
SELECT COUNT(*) AS session_count FROM information_schema.PROCESSLIST"""),
    'W03b': ('W03b_max_connections', '最大连接数与并行度参数', """
SHOW VARIABLES LIKE 'max_connections'"""),
    'W04': ('W04_processlist', '会话明细（用户/主机/命令/状态）', """
SHOW PROCESSLIST"""),
    'W05': ('W05_table_size_top', '数据量最大的 20 张表（备份体积与容量评估）', """
SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ROWS,
       ROUND(DATA_LENGTH/1024/1024, 2) AS DATA_MB
FROM information_schema.TABLES
WHERE TABLE_SCHEMA NOT IN ('information_schema','gbase','gclusterdb','performance_schema')
ORDER BY DATA_LENGTH DESC
LIMIT 20"""),
    'W07': ('W07_backup_param', '备份工具与路径参数配置', """
SHOW VARIABLES LIKE '%backup%'"""),
    'W06': ('W06_schema_space', '各库数据量汇总（容量趋势基线）', """
SELECT TABLE_SCHEMA, COUNT(*) AS table_count,
       SUM(TABLE_ROWS) AS total_rows,
       ROUND(SUM(DATA_LENGTH)/1024/1024, 2) AS data_mb
FROM information_schema.TABLES
GROUP BY TABLE_SCHEMA
ORDER BY data_mb DESC"""),
    'W11': ('W11_schema_storage', '逻辑存储空间使用情况（按库汇总数据/索引占用）', """
SELECT TABLE_SCHEMA,
       COUNT(*) AS table_count,
       ROUND(SUM(DATA_LENGTH)/1024/1024, 2) AS data_mb,
       ROUND(SUM(INDEX_LENGTH)/1024/1024, 2) AS index_mb
FROM information_schema.TABLES
WHERE TABLE_SCHEMA NOT IN ('information_schema','gbase','gclusterdb','performance_schema')
GROUP BY TABLE_SCHEMA
ORDER BY data_mb DESC"""),
    'W06b': ('W06b_charset', '字符集配置（跨库一致性核查）', """
SHOW VARIABLES LIKE 'character_set%'"""),
    'W09': ('W09_gbase_params', 'GBase 关键参数快照（逐节点执行后做 diff，作为配置漂移依据）', """
SHOW VARIABLES LIKE 'gbase%'"""),
    'W17': ('W17_grants', '当前用户权限（权限最小化核查）', """
SHOW GRANTS FOR CURRENT_USER()"""),
    'W18': ('W18_audit_var', '审计相关参数', """
SHOW VARIABLES LIKE '%audit%'"""),
    'W20': ('W20_collect_stamp', '本次采集时间戳与实例标识（留痕用）', """
SELECT NOW() AS collected_at, VERSION() AS version, @@hostname AS host_name"""),
    'M01': ('M01_core_params', '集群核心参数（连接/内存/临时空间）', """
SHOW VARIABLES LIKE 'gbase_heap%'"""),
    'M02': ('M02_parallel_core', '集群并行与分片参数', """
SHOW VARIABLES LIKE 'gbase_parallel%'"""),
    'M04': ('M04_temp_param', '临时空间与排序缓冲参数', """
SHOW VARIABLES LIKE '%sort%'"""),
    'M05': ('M05_stats_check', '表统计信息（ANALYZE 待执行判断）', """
SELECT TABLE_SCHEMA, COUNT(*) AS table_count, SUM(TABLE_ROWS) AS rows_sum
FROM information_schema.TABLES
GROUP BY TABLE_SCHEMA
ORDER BY rows_sum DESC"""),
    'M06': ('M06_table_space_detail', '表级空间明细（大表容量跟踪）', """
SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ROWS,
       ROUND(DATA_LENGTH/1024/1024, 2) AS DATA_MB
FROM information_schema.TABLES
WHERE DATA_LENGTH > 1024*1024*100
ORDER BY DATA_LENGTH DESC
LIMIT 50"""),
    'M07': ('M07_growth_baseline', '库级容量基线（用于月度增长趋势对比）', """
SELECT TABLE_SCHEMA,
       ROUND(SUM(DATA_LENGTH)/1024/1024/1024, 3) AS total_gb,
       COUNT(*) AS table_count
FROM information_schema.TABLES
GROUP BY TABLE_SCHEMA
ORDER BY total_gb DESC"""),
    'M12': ('M12_param_full', '全量 GBase 参数快照（参数基线 diff）', """
SHOW VARIABLES"""),
    'H19': ('H19_conn_load', '连接负载与峰值', """
SHOW STATUS LIKE 'Threads_connected'"""),
    'H21': ('H21_index_info', '索引信息（冗余/缺失评估输入）', """
SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA NOT IN ('information_schema','gbase','gclusterdb','performance_schema')
ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
LIMIT 200"""),
    'H28': ('H28_privileged', '全部账号与主机授权范围', """
SELECT USER, HOST FROM gbase.user ORDER BY USER, HOST"""),
    'H29': ('H29_pwd_var', '口令与认证相关参数', """
SHOW VARIABLES LIKE '%password%'"""),
    'C_NODES': ('C03_data_nodes', '数据节点在线状态与分片角色', 'SHOW NODES'),
    'C_COORD': ('C04_coordinator_nodes', '协调节点在线状态', 'SHOW GCLUSTER NODES'),
    'C_MONIT': ('C05_cluster_monitor', 'gcware/协调/数据/同步状态与逐节点磁盘余量', 'SELECT * FROM information_schema.CLUSTER_MONIT_INFO'),
    'C_POOL': ('C06_node_resources', '逐节点资源池运行/等待任务及 CPU/内存/IO', 'SELECT * FROM information_schema.GNODES_RESOURCE_POOL_USAGE'),
    'C_DISK': ('C07_node_disk_usage', '逐节点用户存储使用量与配额', 'SELECT * FROM information_schema.GNODES_USER_DISKSPACE_USAGE'),
    'C_DIST': ('C02_table_distribution', '表分布信息（分片与数据均衡评估）', """
SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_ROWS, DATA_LENGTH
FROM information_schema.TABLES
WHERE TABLE_SCHEMA NOT IN ('information_schema','gbase','gclusterdb','performance_schema')
ORDER BY TABLE_ROWS DESC
LIMIT 50"""),
}

ITEM_SQL = {
    'oracle': ORACLE,
    'sqlserver': SQLSERVER,
    'dm8': DM8,
    'gbase8a': GBASE8A,
}

# 检查项 ID -> 复用关系：健康检查/月专项中与周巡检同口径的项，直接复用 SQL
ALIAS = {
    'H01': 'W01', 'H04': 'W02', 'H06': 'W04', 'H09': 'W04', 'H10': 'W12',
    'H13': 'H11', 'H15': 'W10', 'H16': 'W02', 'H18': 'M12', 'H22': 'W16',
    'H23': 'W16b', 'H24': 'W16b', 'H30': 'W18', 'H05': 'W05', 'H07': 'H03',
    'H08': 'W04', 'H11b': 'H11', 'M03': 'M02', 'M08': 'W06', 'M09': 'W04',
    'M10': 'W04', 'M13': 'W18', 'M14': 'W20', 'W19b': 'W20',
}


def _resolve_sql(product, item_id):
    """按 item_id 取 SQL，支持别名复用。"""
    lib = ITEM_SQL[product]
    hit = lib.get(item_id)
    if hit:
        return hit
    iid = ALIAS.get(item_id)
    return lib.get(iid) if iid else None


# ============================================================================
# 阈值与判据文本（写入章节 description，供现场按实例自定义）
# ============================================================================
THRESHOLD_NOTE = """
【阈值基线（可自定义，按实例覆盖）】
- 文件系统使用率：预警 75% / 告警 85% / 紧急 90% 或预测 7 天内耗尽
- 表空间使用率：预警 80% / 告警 90% / 无扩展余地且 >=95% 为一级
- 会话使用率：预警 80% / 告警 90% / 100% 拒绝连接为一级
- CPU 平均：预警 70% / 告警 85% / 连续 15 分钟 >=95% 为一级
- 磁盘 await：预警 20ms / 告警 50ms / 紧急 100ms
- 备份成功率：要求 100%；单次失败为二级，连续 2 次或校验失败为一级
- 归档断档：0 容忍，出现即一级
生效值 = max(配置阈值, 该实例近 30 天 P95 x 1.2)，避免一套阈值套全部实例
""".strip()

GRADE_NOTE = """
【故障判据（可自定义，P1/P2/P3）】
P1（一级）：实例不可用 / 集群失去仲裁或脑裂 / 数据文件损坏丢失 / 备份连续2次失败 /
            备份校验失败 / 归档断档 / 空间 7 天内耗尽 / 控制文件仅单副本 / 参数文件损坏
P2（二级）：单节点异常但集群可服务 / 会话使用率 >= 告警值 / 阻塞链超 5 分钟 /
            备份单次失败 / 内部错误(ORA-00600 等) / 新增慢SQL超告警值 /
            无效对象存在 / 表空间超告警 / 未报备账号变更 / 审计被关闭 / 配置漂移
P3（三级）：容量预警 / 统计信息过期 / 索引碎片 >30% / 冗余索引 / 日志增长偏快 /
            低效SQL积压 / 增长趋势 <6 个月
""".strip()


def _chapter(num, title_zh, title_en, desc, items, product):
    """组装一个章节：description 写检查项 + 阈值 + 判据，queries 只有可执行 SQL。"""
    queries = []
    sql_items, manual_items = [], []
    for order, entry in enumerate(items, start=1):
        if isinstance(entry, str):
            item_id = entry
            hit_title = _resolve_sql(product, entry)
            item_title = hit_title[1] if hit_title else entry
        else:
            item_id, item_title = entry
        hit = _resolve_sql(product, item_id)
        if hit:
            key, q_desc, sql = hit
            queries.append({
                'query_key': f'{item_id}_{key}' if not key.startswith(item_id) else key,
                'query_sql': sql.strip(),
                'query_description_zh': f'[{item_id}] {item_title}：{q_desc}',
                'enabled': 1,
                'sort_order': order,
            })
            sql_items.append(f'{item_id} {item_title}')
        else:
            manual_items.append(f'{item_id} {item_title}')

    body = [desc.strip()]
    if sql_items:
        body.append('【SQL 自动采集项】\n' + '\n'.join(f'- {s}' for s in sql_items))
    if manual_items:
        body.append('【主机/命令行采集项（由主机采集或 SSH 执行）】\n'
                    + '\n'.join(f'- {m}' for m in manual_items))
    return {
        'chapter_number': num,
        'chapter_title_zh': title_zh,
        'chapter_title_en': title_en,
        'description': '\n\n'.join(body),
        'enabled': 1,
        'sort_order': num,
        'queries': queries,
    }


# ============================================================================
# 各周期的章节定义
# ============================================================================
BACKUP_CAP_ITEMS = {
    'oracle': [('W05', '每日备份任务执行日志'), ('W06', '文件系统与表空间空间'),
               ('W07', '备份可用性校验')],
    'sqlserver': [('W05', '每日备份任务执行日志'), ('W06', '文件系统与表空间空间'),
                  ('W07', '备份覆盖完整性')],
    'dm8': [('W05', '备份集记录核查'), ('W06', '文件系统与表空间空间'),
            ('W07', '备份参数与工具配置')],
    'gbase8a': [('W05', '大表容量（备份体积评估）'), ('W06', '文件系统与表空间空间'),
                ('W07', '备份工具与参数配置')],
}


DAILY_ITEMS = {
    'oracle': [
        [('W01', '实例运行状态'), ('W03', '实时会话连接数'),
         ('W05', '昨日备份任务结果'), ('W06', '表空间与空间水位')],
        [('W04', '新增告警日志错误'), ('W14', '锁与阻塞会话'), ('W15', '新增慢查询')],
    ],
    'sqlserver': [
        [('W01', '实例运行状态'), ('W03', '实时会话连接数'),
         ('W05', '昨日备份任务结果'), ('W06', '文件与空间水位')],
        [('W04', '新增错误日志'), ('W14', '锁与阻塞会话'), ('W15', '新增慢查询')],
    ],
    'dm8': [
        [('W01', '实例运行状态'), ('W03', '实时会话连接数'),
         ('W05', '昨日备份集结果'), ('W06', '表空间与空间水位')],
        [('W04', '非空闲会话与新错误'), ('W14', '锁与阻塞'), ('W15', '长时间执行 SQL')],
    ],
    'gbase8a': [
        [('W01', '集群版本与状态'), ('W03', '实时连接数'),
         ('W05', '数据容量水位'), ('W06', '库级空间水位')],
        [('W04', '会话明细与异常会话')],
    ],
}


def build_daily(product):
    """日巡检：轻量高频，5-10 分钟出结果，只答四个问题——
    还活着吗 / 备份成没成 / 空间够不够 / 有没有新异常。"""
    items = DAILY_ITEMS[product]
    return [
        _chapter(1, '日常核心指标', 'Daily Core Metrics',
                 '日巡检定位：轻量、高频、快速。只核查"实例是否在线、连接是否吃紧、昨日备份是否成功、空间是否够用"四项，'
                 '单实例目标 5-10 分钟出结果。深度排查留给周巡检与月度健康检查。'
                 '\n\n【日巡检纪律】任一项判为异常，须当日建单并进入「巡检-告警-整改-闭环」链路；'
                 '连续 2 日同一异常未闭环，自动升级为周巡检重点项。'
                 '\n\n【阈值基线（可自定义，按实例覆盖）】'
                 '\n- 会话使用率：预警 80% / 告警 90% / 拒绝连接为一级'
                 '\n- 空间使用率：预警 75% / 告警 85% / 90% 或 7 天内耗尽为一级'
                 '\n- 备份成功率：要求 100%，当日失败即二级，连续 2 日失败为一级'
                 '\n- 生效值 = max(配置阈值, 该实例近 30 天 P95 x 1.2)',
                 items[0], product),
        _chapter(2, '日常异常明细', 'Daily Anomalies',
                 '只输出"今天新出现的异常"，不做趋势与根因分析。'
                 '\n\n【故障判据】'
                 '\nP1：实例不可用 / 备份连续 2 日失败 / 空间 7 天内耗尽 / 归档断档'
                 '\nP2：会话使用率超告警 / 存在阻塞链超 5 分钟 / 出现新内部错误 / 新增慢 SQL 超阈值'
                 '\nP3：容量预警 / 统计信息过期 / 日志增长偏快'
                 '\n\n【命令行采集项（主机/SSH）】主机 CPU/内存/磁盘/负载；数据库进程存活；告警日志增量扫描。',
                 items[1], product),
    ]


def build_weekly(product):
    return [
        _chapter(1, '实例与连接状态', 'Instance & Connection',
                 '核查实例在线状态、远程登录可用性、会话连接数与告警日志，聚焦"是否活着、能否连上、连接是否吃紧"。'
                 + '\n\n' + GRADE_NOTE,
                 [('W01', '实例运行状态'), ('W02', '客户端远程登录可用性'),
                  ('W03', '实时会话连接数'), ('W03b', '共享池/缓存命中率'),
                  ('W04', '告警日志与未清除告警')], product),
        _chapter(2, '备份与容量', 'Backup & Capacity',
                 '核查每日备份任务结果与耗时、备份集大小波动、备份校验；核查安装目录/数据文件/归档目录所在文件系统空间。'
                 + ('\n\n注意：GBase 8a 的备份由 gcrcman 或甲方备份平台执行，SQL 侧只能核查备份参数与数据容量，'
                    '备份成功与否须查备份工具日志。' if product == 'gbase8a' else '')
                 + '\n\n' + THRESHOLD_NOTE,
                 BACKUP_CAP_ITEMS[product], product),
        _chapter(3, '集群状态', 'Cluster Status',
                 '核查集群节点数、角色、心跳与同步延迟、各节点配置是否一致。单机部署时本组降级为"单实例一致性检查"。',
                 [('W08', '集群各节点实例与运行状态'), ('W09', '集群配置漂移核对')], product),
        _chapter(4, '性能与对象健康', 'Performance & Objects',
                 '核查无效对象、存储空间、统计信息新鲜度、作业执行结果、锁与死锁、慢查询、服务器资源。',
                 [('W10', '无效对象与失效索引'), ('W11', '逻辑存储空间使用率'),
                  ('W12', '统计信息新鲜度'), ('W13', '作业与定时任务执行结果'),
                  ('W14', '锁与死锁'), ('W15', '慢查询 TOP N'),
                  ('W16', '服务器资源与等待事件')], product),
        _chapter(5, '安全与合规', 'Security & Compliance',
                 '核查账号与权限变更、高危角色成员、口令策略、审计开启与日志留存。',
                 [('W17', '账号与权限'), ('W18', '日志与审计留存')], product),
        _chapter(6, '留痕与台账信息', 'Traceability & Base Info',
                 '核查巡检原始记录完整性与异常建单率（对应"巡检完成率 100%、整改闭环率 100%"），'
                 '并采集本实例基础信息用于台账核对。'
                 '\n\n【引擎自动执行项】每次采集的命令、原始输出、时间戳、执行账号、目标实例须与报告同编号归档；'
                 '异常项必须 100% 建单，未建单即扣分。',
                 [('W19', '数据库基础信息（台账核对）'), ('W20', '采集时间戳留痕')], product),
    ]


def build_monthly(product):
    return [
        _chapter(1, '核心文件完整性', 'Core Files Integrity',
                 '核查参数文件、核心元数据文件、网络配置文件、口令文件的存在性、属性与内容有效性。'
                 + '\n\n' + GRADE_NOTE,
                 [('M01', '参数文件完整性'), ('M02', '核心元数据文件'),
                  ('M03', '网络连接配置文件'), ('M04', '密码口令文件')], product),
        _chapter(2, '统计信息采集', 'Statistics',
                 '对核心表执行统计信息采集与更新。采集前必须先备份统计信息，采集后 24h 观察执行计划是否劣化，劣化立即回滚。',
                 [('M05', '统计信息采集与更新')], product),
        _chapter(3, '容量与增长趋势', 'Capacity Trend',
                 '统计逻辑存储空间使用率、数据增长趋势，输出预测耗尽天数。' + '\n\n' + THRESHOLD_NOTE,
                 [('M06', '逻辑存储空间使用率'), ('M07', '数据增长趋势分析'),
                  ('M08', '表空间明细')], product),
        _chapter(4, '日志治理与清理', 'Log Governance',
                 '核查客户端访问日志、告警日志、跟踪日志大小与轮转；按清理白名单归档后清理，禁止删除未过保留期的审计与备份日志。',
                 [('M09', '客户端连接访问日志'), ('M10', '告警日志大小与冗余清理'),
                  ('M11', '归档日志完整性'), ('M13', '日志轮转与保留策略')], product),
        _chapter(5, '参数基线与漂移', 'Parameter Baseline',
                 '与上月参数快照逐项 diff，识别未报备变更。无工单的参数变更视为违规变更，须上报。',
                 [('M12', '参数基线月度 diff')], product),
        _chapter(6, '覆盖率自证', 'Coverage Attestation',
                 '核对"应巡对象数 vs 实巡对象数"，逐对象比对台账，输出覆盖率。',
                 [('M14', '巡检覆盖对象数核对')], product),
    ]


BACKUP_ITEMS = {
    'oracle': [('H25', '备份数据增量变化统计'), ('H26', '备份时间窗口稳定性'),
               ('H27', '备份数据可用性验证')],
    'sqlserver': [('H25', '备份数据增量变化统计'), ('H26', '备份时间窗口稳定性'),
                  ('H27', '备份数据可用性验证')],
    'dm8': [('W05', '备份集记录核查'), ('M07', '容量基线（备份容量评估）'),
            ('W19', '检查点与 IO 压力（备份窗口影响）')],
    'gbase8a': [('W07', '备份工具与参数配置'), ('W05', '大表容量（备份体积评估）'),
                ('M07', '库级容量基线（备份容量评估）')],
}


def build_health(product):
    return [
        _chapter(1, '基础状态深度核查', 'Deep Instance State',
                 '进程僵死、核心转储、OOM 记录、网络连接服务完整性、依赖组件与时钟同步。区别于周巡检只看"活没活"。'
                 + '\n\n' + GRADE_NOTE,
                 [('H01', '实例运行状态深度核查'), ('H02', '后台服务进程完整性'),
                  ('H03', '进程异常终止与僵死排查'), ('H04', '网络连接服务完整性'),
                  ('H05', '依赖组件与时钟同步')], product),
        _chapter(2, '日志深度分析', 'Log Deep Analysis',
                 '错误码归类计数、核心转储分析、系统告警邮件、历史异常背后的隐患挖掘、审计日志高危操作。',
                 [('H06', '告警日志全月分析'), ('H07', '核心转储目录分析'),
                  ('H08', '系统用户邮件告警分析'), ('H09', '历史异常隐患挖掘'),
                  ('H10', '审计日志异常操作')], product),
        _chapter(3, '对象状态深度校验', 'Object Integrity',
                 '元数据文件、事务日志、数据文件头一致性、表与索引有效性、存储过程触发器有效性、冗余对象排查。',
                 [('H11', '核心元数据文件校验'), ('H12', '事务日志文件校验'),
                  ('H13', '数据文件校验'), ('H14', '表与索引有效性'),
                  ('H15', '存储过程/触发器/视图有效性'), ('H16', '网络连接服务对象校验'),
                  ('H17', '对象冗余与性能问题排查')], product),
        _chapter(4, '资源与性能深度分析', 'Resource & Performance',
                 '参数合理性、会话负载、磁盘与 IO、逻辑存储深度、核心等待事件、CPU、缓冲与共享池命中率。' + '\n\n' + THRESHOLD_NOTE,
                 [('H18', '初始化参数合理性'), ('H19', '会话连接负载'),
                  ('H20', '磁盘空间与 IO'), ('H21', '逻辑存储空间深度'),
                  ('H22', '核心等待事件分析'), ('H23', 'CPU 使用率分析'),
                  ('H24', '缓冲与共享池命中率')], product),
        _chapter(5, '备份深度核查', 'Backup Deep Check',
                 '备份增量变化统计、时间窗口稳定性、备份数据可用性验证（含抽样恢复）。周巡检查"成没成功"，这里查"能不能真恢复"。'
                 '\n\nGBase 8a 与 DM8 的备份由专用工具完成（gcrcman / dmrman），'
                 'SQL 侧只能核查备份记录与参数，实际校验与恢复须按备份工具执行。',
                 BACKUP_ITEMS[product], product),
        _chapter(6, '安全与合规深度核查', 'Security Compliance Deep',
                 '高危账号与权限收敛、口令策略、审计与等保项。'
                 + '\n\n【健康评分权重】可用性25 / 空间容量20 / 备份有效性20 / 性能健康15 / 对象完整性10 / 安全合规10；'
                   '总分 <60 触发性能专项诊断，60-79 列入下月整改。',
                 [('H28', '高危账号与权限收敛'), ('H29', '口令策略与到期'),
                  ('H30', '审计与等保项')], product),
    ]


def build_cluster(product):
    """集群架构专项：按产品的实际技术栈选择 SQL 项，命令行项自动列入手工清单。"""
    plans = {
        'oracle': {
            'subs': ['RAC 集群资源与节点', 'ASM 与存储', '全局缓存与等待', '节点一致性'],
            'ch1': [('C_CL', 'RAC 节点与实例状态'),
                    ('C_ASM', 'ASM 磁盘组')],
            'ch2': [('C_GES', 'GES 全局缓存等待'),
                    ('C_CL', '实例启动时间与角色')],
            'ch3': [('W16', '集群层等待事件'),
                    ('W14', '集群层阻塞')],
            'ch4': [('W09', '节点参数快照（逐节点执行后 diff）'), ('M12', '非默认参数清单')],
            'manual1': 'crsctl check crs / crsctl stat res -t / olsnodes -n -s / crsctl query css votedisk / ocrcheck / ocrconfig -showbackup',
            'manual2': 'oifcfg getif / 心跳网丢包统计（netstat -i 的 Ierrs/Oerrs）/ asmcmd lsdg',
        },
        'sqlserver': {
            'subs': ['镜像与 AlwaysOn 副本', '日志传送与端点', '同步延迟', '节点一致性'],
            'ch1': [('C_MIR', '数据库镜像状态'), ('C_AG', 'AlwaysOn 副本状态')],
            'ch2': [('C_LS', '日志传送监控'), ('M03', '监听端点与协议')],
            'ch3': [('W12', '同步相关等待类型'), ('W11', '阻塞链')],
            'ch4': [('W09', '节点配置快照（逐节点执行后 diff）'), ('M12', '配置快照 diff 依据')],
            'manual1': 'WSFC 群集节点状态（Get-ClusterNode / cluster node）；端点端口 5022 连通性',
            'manual2': '镜像端点与见证配置核查；AG 监听端口连通性与只读路由',
        },
        'dm8': {
            'subs': ['守护与 DMDSC 节点', '归档双开与路径', '同步延迟', '节点一致性'],
            'ch1': [('C_EP', 'DMDSC 节点信息（仅 DMDSC）'), ('C_WATCH', '守护与主备状态'), ('C_MAL', 'MAL 拓扑'), ('W14', '集群层锁')],
            'ch2': [('C_ARCH', '归档状态'), ('C_CONFIG', '实时/异步归档配置'), ('M02', '归档配置与目标'), ('M11', '归档日志统计')],
            'ch3': [('W16', '系统等待事件'), ('W19', '检查点信息')],
            'ch4': [('W09', '节点参数快照（逐节点执行后 diff）'), ('M12', '全量参数 diff 依据')],
            'manual1': 'dmmonitor 执行 show global info / show database；dmwatcher 进程与日志逐节点核查',
            'manual2': 'dmmal.ini 私网配置与连通性；dmwatcher.ini 自动切换配置（DW_MODE / AUTO RESTART）',
        },
        'gbase8a': {
            'subs': ['节点与进程状态', '分片与数据分布', '节点容量均衡', '节点一致性与日志'],
            'ch1': [('C_NODES', '数据节点状态'), ('C_COORD', '协调节点状态'), ('C_MONIT', '集群组件状态'), ('C_DIST', '表分布与数据量'), ('W03', '集群会话数')],
            'ch2': [('W06', '库级数据分布'), ('W05', '大表分布')],
            'ch3': [('C_POOL', '节点资源池'), ('C_DISK', '节点存储占用'), ('W06b', '字符集与节点参数')],
            'ch4': [('W09', '节点参数快照（逐节点执行后 diff）'), ('M12', '全量参数 diff 依据')],
            'manual1': 'gcadmin / gcadmin showcluster / gcadmin showdistribution / gcadmin showlock / gcadmin showddlevent / gcadmin showdmlevent',
            'manual2': '逐节点 df -hT 与数据目录 du；节点间 ping 时延；gclusterd/gbased/gc_sync_server 进程；express log 与 gcluster/gnode 日志',
        },
    }
    plan = plans[product]
    subs = plan['subs']
    name = PRODUCT_LABEL[product]
    return [
        _chapter(1, subs[0], 'Cluster Core',
                 f'{name} 集群核心状态：节点齐全性、角色、仲裁/投票、心跳网络。'
                 '\n\n纪律：任何重启/切换动作前先完成取证。'
                 '\n\n【命令行采集项】' + plan['manual1']
                 + '\n\n' + GRADE_NOTE,
                 plan['ch1'], product),
        _chapter(2, subs[1], 'Storage & Endpoint',
                 '存储与端点层面：ASM 磁盘组、归档双开、日志传送、监听端点、表分布。'
                 '\n\n【命令行采集项】' + plan['manual2'],
                 plan['ch2'], product),
        _chapter(3, subs[2], 'Latency & Cache',
                 '节点间交互开销：全局缓存等待、同步延迟、心跳时延。同步延迟建议阈值：预警 5s / 告警 30s / 紧急 300s。',
                 plan['ch3'], product),
        _chapter(4, subs[3], 'Node Consistency',
                 '节点参数、版本、补丁、OS 关键参数一致性核对；双主/脑裂检测。'
                 '出现双 PRIMARY 或多数节点离线即判 P1。'
                 '\n\n注意：单次连接只能读到本节点，历史上限一致性须逐节点执行本组查询后做 diff。'
                 '\n\n【命令行采集项】逐节点 uname -a / ulimit -a / 数据库版本与补丁号；节点间时间同步（ntpq -p / chronyc tracking）。',
                 plan['ch4'], product),
    ]


def build_event(product, kind):
    specs = {
        'fault': ('故障应急取证与处置', [
            ('现场取证（处置前必须完成）', '时间线锚定、影响面、实例/会话/锁/等待快照、日志尾部、OS 资源快照、变更关联。'
             '\n\n纪律：先取证，后处置。任何重启/杀进程/切换动作之前，本组必须完成。', ['W04', 'W14', 'W03', 'W16']),
            ('根因定位与方案', '排除法顺序：近期变更 → 资源耗尽 → 锁/等待 → 存储 → 网络 → 程序缺陷。同时确认备份可用性以决定恢复路径。',
             ['W05', 'W07', 'W15']),
            ('闭环验证与记录', '业务验证、数据完整性校验、故障记录六要素、重大故障复盘判定。', ['W19', 'W20']),
        ]),
        'change': ('变更前评估与变更后验证', [
            ('变更前评估', '语句审核（锁表/大事务/烂SQL）、执行计划预演、依赖对象、回退点、窗口与授权。',
             ['W15', 'W12', 'W19']),
            ('变更后验证', '对象状态、数据一致性、执行计划与耗时对比、锁与会话观察30分钟、日志无新增错误、台账登记。',
             ['W10', 'W13', 'W14']),
            ('变更台账', '变更单号/对象/语句/时间/执行人/耗时/影响行数/结果/回退方案。', ['W20']),
        ]),
        'restore': ('备份集校验与恢复验证', [
            ('备份集可校验性', '备份存在性与大小合理性、校验和、可读性。恢复验证需在隔离环境执行。', ['W05', 'W07', 'H27']),
            ('恢复与指标实测', '全备+增量+日志/归档链路恢复；记录 RTO 实测耗时与 RPO 实测数据时点。', ['H25', 'H26']),
            ('一致性与结论', '对象数/行数/关键表校验和比对；偏差分析；改进项与报告签署。', ['M07', 'H17']),
        ]),
        'onboard': ('新库上线与迁移验收', [
            ('架构与版本', '拓扑核对、版本补丁、参数基线、字符集、存储规划。', ['M01', 'M12', 'W19']),
            ('安全与网络', '账号权限最小化、网络与监听、集群配置。', ['W17', 'W18', 'W09']),
            ('备份与监控', '备份作业已建且成功跑过一次、恢复验证过；监控采集项与阈值已配。', ['W05', 'W07']),
            ('迁移一致性', '对象数/行数/抽样明细/约束/索引/序列/LOB 一致性比对。', ['W11', 'W10', 'H15']),
        ]),
        'perf': ('性能问题专项诊断', [
            ('现象与影响面', '什么时候慢、慢多少、可复现吗、影响哪些业务。', ['W20', 'W01']),
            ('等待与资源定位', 'Top 等待事件排除空闲类，判断瓶颈类型；CPU/内存/IO/网络四点定位。',
             ['W16', 'H22', 'H20']),
            ('SQL 与计划', 'TOP SQL 四维排序、执行计划劣化判断、索引与统计信息、锁与并发。',
             ['W15', 'W13', 'W12', 'W14']),
            ('优化与验证', '方案+收益预估+风险+回退；优化前后耗时/逻辑读/CPU 对比；7 天后回访。',
             ['H14', 'H17']),
        ]),
    }
    name, chapters = specs[kind]
    return [
        _chapter(i, t, t, d + '\n\n' + GRADE_NOTE, items, product)
        for i, (t, d, items) in enumerate(chapters, start=1)
    ]


PERF_ITEMS = {
    'oracle': [('W15', 'TOP SQL 累计耗时'), ('W16', '等待事件'), ('H24', '缓冲与共享池命中率')],
    'sqlserver': [('W13', 'TOP SQL 累计耗时'), ('H22', '等待事件'), ('H24', '缓冲命中率与 PLE')],
    'dm8': [('W15', '长时间执行 SQL'), ('W16', '系统等待事件'), ('H24', '缓冲池命中情况')],
    'gbase8a': [('W09', '关键参数快照'), ('W11', '逻辑存储空间'), ('W03', '会话负载')],
}


def build_quarterly(product):
    return [
        _chapter(1, '可用性与 SLA', 'Availability & SLA',
                 '逐实例季度可用率、响应/处置/修复达标率。数据来源：监控平台 + 故障工单。',
                 [('W01', '实例可用性'), ('W20', '采集时间窗')], product),
        _chapter(2, '容量趋势', 'Capacity Trend',
                 '3 个月增量与 6 个月预测。', [('M07', '库级容量'), ('M06', '表级容量'), ('W11', '大表 Top20')], product),
        _chapter(3, '性能趋势', 'Performance Trend',
                 '慢 SQL 数趋势、TOP10 SQL、命中率、等待事件。',
                 PERF_ITEMS[product], product),
        _chapter(4, '短板与优化输入', 'Gap & Improvement',
                 'Top5 短板，映射到参数/架构/容量/规范 4 类优化方案。', [('M12', '参数基线'), ('H17', '冗余索引')], product),
    ]


def build_yearly(product):
    return [
        _chapter(1, '台账全量核对', 'Ledger Reconciliation',
                 '资产底账、集群参数、实例参数、基础信息、存储空间、用户权限、备份策略 7 类台账逐项实勘比对，准确率要求 100%。',
                 [('W19', '数据库基础信息'), ('M12', '参数全量'), ('M06', '存储全量'),
                  ('W17', '用户权限全量'), ('W05', '备份策略')], product),
        _chapter(2, '版本与生命周期', 'Version & EOL',
                 '逐实例版本与厂商支持期比对，输出 EOL 清单与风险等级。Oracle 11g 与 SQL Server 2005/2008/2014 均已过厂商支持期，无官方补丁。',
                 [('W01', '版本与补丁')], product),
        _chapter(3, '容量与架构规划', 'Planning',
                 '1-3 年容量方案；读写分离/升级/替换建议；单点与瓶颈评估。',
                 [('M07', '容量基线'), ('W09', '配置一致性')], product),
        _chapter(4, '僵尸台账与规范', 'Housekeeping',
                 '已下线/已迁移实例销账；应急预案与运维规范对照全年实际故障修订。',
                 [('W20', '留痕与台账时间戳')], product),
    ]


# ============================================================================
# 模板清单
# ============================================================================
def build_template_specs(cd_per_product=False):
    """返回 [(product, 专项名, description, builder)] 列表。"""
    specs = []
    for product in ('oracle', 'sqlserver', 'dm8', 'gbase8a'):
        label = PRODUCT_LABEL[product]
        specs.append((product, '日巡检',
                      f'{label} 业务专项巡检 - 日巡检。轻量高频，单实例 5-10 分钟出结果，'
                      f'只核查实例在线、连接水位、昨日备份、空间水位与当日新增异常。',
                      build_daily))
        specs.append((product, '周巡检',
                      f'{label} 业务专项巡检 - 周巡检。覆盖实例与连接、备份与容量、集群状态、性能与对象、安全合规、留痕建单 6 组检查项。'
                      f'阈值与 P1/P2/P3 判据写在章节说明中，可按实例自定义。',
                      build_weekly))
        specs.append((product, '月度专项巡检',
                      f'{label} 业务专项巡检 - 每月首个工作日专项巡检。覆盖核心文件完整性、统计信息采集、容量趋势、日志治理、参数漂移、覆盖率自证 6 组。',
                      build_monthly))
        specs.append((product, '月度健康检查',
                      f'{label} 业务专项巡检 - 月度健康检查。6 大域 30 检查项，含健康评分模型，输出风险清单、根因、整改方案与时限。',
                      build_health))
        specs.append((product, '集群架构专项',
                      f'{label} 业务专项巡检 - 集群架构专项。集群核心状态、存储与分布、节点间开销、节点一致性 4 组深度检查。',
                      build_cluster))

    event_targets = {
        'fault': '故障应急取证',
        'change': '变更前评估与变更后验证',
        'restore': '备份校验与恢复验证',
        'onboard': '新库上线与迁移验收',
        'perf': '性能问题专项诊断',
    }
    default_owner = {
        'fault': 'oracle', 'change': 'oracle', 'restore': 'sqlserver',
        'onboard': 'dm8', 'perf': 'oracle',
    }
    for kind, name in event_targets.items():
        owners = ['oracle', 'sqlserver', 'dm8', 'gbase8a'] if cd_per_product else [default_owner[kind]]
        for product in owners:
            specs.append((product, name,
                          f'{PRODUCT_LABEL[product]} 业务专项巡检 - {name}。事件驱动型模板，'
                          f'按阶段组织取证/评估/验证动作，产出对应交付物。',
                          lambda p, k=kind: build_event(p, k)))

    period_targets = [
        ('季度运行分析', build_quarterly, 'gbase8a', 'GBase 8a MPP集群'),
        ('年度台账核对', build_yearly, 'sqlserver', 'SQL Server 2005/2008/2014'),
    ]
    for name, builder, default_product, _label in period_targets:
        owners = ['oracle', 'sqlserver', 'dm8', 'gbase8a'] if cd_per_product else [default_product]
        for product in owners:
            specs.append((product, name,
                          f'{PRODUCT_LABEL[product]} 业务专项巡检 - {name}。周期型汇总模板，'
                          f'采集口径为统计聚合，用于季度/年度报告取数。',
                          builder))

    return specs


EN_SUFFIX = {
    '日巡检': 'Daily Patrol',
    '周巡检': 'Weekly Patrol',
    '月度专项巡检': 'Monthly Special Patrol',
    '月度健康检查': 'Monthly Health Check',
    '集群架构专项': 'Cluster Architecture Special',
    '故障应急取证': 'Incident Forensics',
    '变更前评估与变更后验证': 'Change Pre-Assessment and Post-Verification',
    '备份校验与恢复验证': 'Backup Verification and Restore Drill',
    '新库上线与迁移验收': 'Onboarding and Migration Acceptance',
    '性能问题专项诊断': 'Performance Troubleshooting',
    '季度运行分析': 'Quarterly Operation Analysis',
    '年度台账核对': 'Annual Ledger Reconciliation',
}
EN_BASE = {
    'oracle': 'Oracle 11g Inspection',
    'sqlserver': 'SQL Server Inspection',
    'dm8': 'DM8 Inspection',
    'gbase8a': 'GBase 8a MPP Cluster Inspection',
}


def make_name_en(product, suffix_name):
    return f'{EN_BASE[product]} - Business Special - {EN_SUFFIX.get(suffix_name, suffix_name)}'


def make_template_config(product, suffix_name, description, builder):
    chapters = builder(product)
    if product in ('dm8', 'gbase8a') and suffix_name == '周巡检':
        extra = [('C_NODES', '数据节点'), ('C_COORD', '协调节点'), ('C_MONIT', '组件状态'), ('C_POOL', '资源池'), ('C_DISK', '存储配额')] if product == 'gbase8a' else [('C_EP', 'DSC 节点'), ('C_MAL', 'MAL 拓扑'), ('C_WATCH', '数据守护'), ('C_ARCH', '归档状态'), ('C_CONFIG', '归档配置')]
        chapters.append(_chapter(len(chapters)+1, '集群拓扑与复制状态', 'Cluster Topology & Replication',
            '按实际部署采集；单节点或未配置功能不等价于多节点高可用已验证。心跳网、配置漂移、日志与切换演练仍需逐节点主机证据。', extra, product))
    return {
        'db_type': product,
        'template_name': f'{BASE_TEMPLATE_NAME[product]}_{SUFFIX}_{suffix_name}',
        'version': PRODUCT_VERSION[product],
        'description': description,
        'is_default': int(suffix_name == '周巡检'),
        'chapters': chapters,
    }



def ensure_business_defaults(db_path=None):
    """Seed missing business presets and parameter checks without overwriting edits.

    The weekly business preset replaces only the shipped generic default. A user
    selected default is preserved. Deleted individual SQL rows are not recreated.
    """
    from modules.inspection import dal
    from modules.inspection.business_baseline_seed import PARAM_BASELINES
    created = 0
    for product, name, desc, builder in build_template_specs(cd_per_product=True):
        cfg = make_template_config(product, name, desc, builder)
        conn = dal.get_db_connection(db_path)
        try:
            existing = conn.execute(
                'SELECT id FROM inspection_template WHERE db_type=? AND template_name_zh=?',
                (product, cfg['template_name'])).fetchone()
            current = conn.execute(
                'SELECT id, is_preset, template_name_zh FROM inspection_template '
                'WHERE db_type=? AND is_default=1 ORDER BY id', (product,)).fetchall()
        finally:
            conn.close()
        # Import must not reset a user-selected default or the separate 12c+ Oracle default.
        cfg['is_default'] = 0
        if existing:
            tid = existing['id']
            conn = dal.get_db_connection(db_path)
            try:
                conn.execute('UPDATE inspection_template SET is_preset=1 WHERE id=?', (tid,))
                conn.commit()
            finally:
                conn.close()
        else:
            tid = dal.import_template(cfg, db_path=db_path, overwrite=False)
            created += 1
            conn = dal.get_db_connection(db_path)
            try:
                conn.execute('UPDATE inspection_template SET is_preset=1, template_name_en=? WHERE id=?',
                             (make_name_en(product, name), tid))
                conn.commit()
            finally:
                conn.close()
        if existing and product in ('dm8', 'gbase8a'):
            for ch in cfg['chapters']:
                if not (ch['chapter_title_zh'] == '集群拓扑与复制状态' or name == '集群架构专项'):
                    continue
                conn = dal.get_db_connection(db_path)
                try:
                    chapter = conn.execute('SELECT id FROM inspection_chapter WHERE template_id=? AND chapter_number=?',
                                           (tid,ch['chapter_number'])).fetchone()
                    if not chapter:
                        cid = conn.execute('INSERT INTO inspection_chapter '
                            '(template_id,chapter_number,chapter_title_zh,chapter_title_en,description,sort_order) '
                            'VALUES (?,?,?,?,?,?)', (tid,ch['chapter_number'],ch['chapter_title_zh'],ch.get('chapter_title_en'),ch['description'],ch['sort_order'])).lastrowid
                    else:
                        cid = chapter['id']
                    for q in ch['queries']:
                        if q['query_key'].startswith(('C_NODES_', 'C_COORD_', 'C_MONIT_', 'C_POOL_', 'C_DISK_', 'C_MAL_', 'C_WATCH_', 'C_ARCH_', 'C_CONFIG_', 'C_EP_')):
                            conn.execute('INSERT OR IGNORE INTO inspection_query '
                                '(chapter_id,query_key,query_sql,query_description_zh,enabled,sort_order) VALUES (?,?,?,?,?,?)',
                                (cid,q['query_key'],q['query_sql'],q['query_description_zh'],1,q['sort_order']))
                    conn.commit()
                finally:
                    conn.close()
        if name == '周巡检':
            eligible = [r for r in current if product != 'oracle' or '11g' in r['template_name_zh']]
            if not eligible or all(r['is_preset'] and '业务专项巡检' not in r['template_name_zh'] for r in eligible):
                dal.update_template(tid, is_default=1, db_path=db_path)
    conn = dal.get_db_connection(db_path)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS inspection_builtin_migration (revision TEXT PRIMARY KEY)')
        revision = 'business-compatibility-20261110-v2'
        if not conn.execute('SELECT 1 FROM inspection_builtin_migration WHERE revision=?', (revision,)).fetchone():
            for old_sql, new_sql in LEGACY_SQL_FIXES.items():
                conn.execute('UPDATE inspection_query SET query_sql=? WHERE query_sql=? AND chapter_id IN '
                    '(SELECT c.id FROM inspection_chapter c JOIN inspection_template t ON t.id=c.template_id '
                    'WHERE t.template_name_zh LIKE ?)', (new_sql, old_sql, '%_业务专项巡检_%'))
            import re
            for row in conn.execute('SELECT id,db_type,param_name,query_sql FROM inspection_baseline').fetchall():
                new_sql = row['query_sql']
                match = re.fullmatch(r"EXEC sp_configure '([^']+)'", new_sql.strip(), re.I)
                if row['db_type'] == 'sqlserver' and match:
                    param = match.group(1)
                    if param in ('max server memory', 'min server memory'):
                        param += ' (MB)'
                    new_sql = "SELECT name, value_in_use FROM sys.configurations WHERE name='" + param.replace("'", "''") + "'"
                if row['db_type'] == 'oracle':
                    for param in ('password_life_time','failed_login_attempts'):
                        if new_sql == "SELECT NAME, VALUE FROM V$PARAMETER WHERE NAME='"+param+"'":
                            new_sql = "SELECT RESOURCE_NAME, LIMIT FROM DBA_PROFILES WHERE PROFILE='DEFAULT' AND RESOURCE_NAME='"+param.upper()+"'"
                    if new_sql == "SELECT NAME, VALUE FROM V$PARAMETER WHERE NAME='open_cursors_per_session'":
                        new_sql = "SELECT NAME, VALUE FROM V$PARAMETER WHERE NAME='open_cursors'"
                if new_sql != row['query_sql']:
                    conn.execute('UPDATE inspection_baseline SET query_sql=? WHERE id=?', (new_sql,row['id']))
            conn.execute('INSERT INTO inspection_builtin_migration VALUES (?)', (revision,))
        for product, items in PARAM_BASELINES.items():
            for param, sql, op, expected, risk, zh, en in items:
                conn.execute('INSERT INTO inspection_baseline '
                    '(db_type,param_name,query_sql,operator,expected_value,risk_level,description_zh,description_en) '
                    'SELECT ?,?,?,?,?,?,?,? WHERE NOT EXISTS '
                    '(SELECT 1 FROM inspection_baseline WHERE db_type=? AND param_name=?)',
                    (product,param,sql,op,expected,risk,zh,en,product,param))
        conn.commit()
    finally:
        conn.close()
    return created


# Full-text migrations of known invalid shipped SQL; edited SQL is never replaced.
LEGACY_SQL_FIXES = {"SELECT 'TEMP' AS seg_type, tablespace_name,\n       ROUND(SUM(bytes_used)/1024/1024, 2) AS used_mb\nFROM v$tempseg_usage\nGROUP BY tablespace_name\nUNION ALL\nSELECT 'UNDO' AS seg_type, tablespace_name,\n       ROUND(SUM(bytes)/1024/1024, 2) AS used_mb\nFROM dba_undo_extents\nGROUP BY tablespace_name": 'SELECT '
                                                                                                                                                                                                                                                                                                                  "'TEMP' "
                                                                                                                                                                                                                                                                                                                  'AS '
                                                                                                                                                                                                                                                                                                                  'seg_type, '
                                                                                                                                                                                                                                                                                                                  'tablespace '
                                                                                                                                                                                                                                                                                                                  'AS '
                                                                                                                                                                                                                                                                                                                  'tablespace_name,\n'
                                                                                                                                                                                                                                                                                                                  '       '
                                                                                                                                                                                                                                                                                                                  'ROUND(SUM(blocks '
                                                                                                                                                                                                                                                                                                                  '* '
                                                                                                                                                                                                                                                                                                                  '(SELECT '
                                                                                                                                                                                                                                                                                                                  'value '
                                                                                                                                                                                                                                                                                                                  'FROM '
                                                                                                                                                                                                                                                                                                                  'v$parameter '
                                                                                                                                                                                                                                                                                                                  'WHERE '
                                                                                                                                                                                                                                                                                                                  "name='db_block_size'))/1024/1024, "
                                                                                                                                                                                                                                                                                                                  '2) '
                                                                                                                                                                                                                                                                                                                  'AS '
                                                                                                                                                                                                                                                                                                                  'used_mb\n'
                                                                                                                                                                                                                                                                                                                  'FROM '
                                                                                                                                                                                                                                                                                                                  'v$tempseg_usage\n'
                                                                                                                                                                                                                                                                                                                  'GROUP '
                                                                                                                                                                                                                                                                                                                  'BY '
                                                                                                                                                                                                                                                                                                                  'tablespace\n'
                                                                                                                                                                                                                                                                                                                  'UNION '
                                                                                                                                                                                                                                                                                                                  'ALL\n'
                                                                                                                                                                                                                                                                                                                  'SELECT '
                                                                                                                                                                                                                                                                                                                  "'UNDO' "
                                                                                                                                                                                                                                                                                                                  'AS '
                                                                                                                                                                                                                                                                                                                  'seg_type, '
                                                                                                                                                                                                                                                                                                                  'tablespace_name,\n'
                                                                                                                                                                                                                                                                                                                  '       '
                                                                                                                                                                                                                                                                                                                  'ROUND(SUM(bytes)/1024/1024, '
                                                                                                                                                                                                                                                                                                                  '2) '
                                                                                                                                                                                                                                                                                                                  'AS '
                                                                                                                                                                                                                                                                                                                  'used_mb\n'
                                                                                                                                                                                                                                                                                                                  'FROM '
                                                                                                                                                                                                                                                                                                                  'dba_undo_extents\n'
                                                                                                                                                                                                                                                                                                                  'GROUP '
                                                                                                                                                                                                                                                                                                                  'BY '
                                                                                                                                                                                                                                                                                                                  'tablespace_name',
 'SELECT * FROM V$LOCK WHERE BLOCK = 1 OR LMODE > 0': 'SELECT * FROM V$LOCK WHERE BLOCKED = 1 OR LMODE <> '
                                                      "'NONE'",
 'SELECT D.TABLESPACE_NAME, D.FILE_NAME,\n       ROUND(D.BYTES/1024/1024, 2) AS SIZE_MB,\n       D.AUTO_EXTEND, ROUND(D.MAX_BYTES/1024/1024, 2) AS MAX_MB,\n       T.STATUS$\nFROM DBA_DATA_FILES D\nLEFT JOIN V$TABLESPACE T ON D.TABLESPACE_NAME = T.NAME\nORDER BY D.TABLESPACE_NAME': 'SELECT '
                                                                                                                                                                                                                                                                                          'D.TABLESPACE_NAME, '
                                                                                                                                                                                                                                                                                          'D.FILE_NAME,\n'
                                                                                                                                                                                                                                                                                          '       '
                                                                                                                                                                                                                                                                                          'ROUND(D.BYTES/1024/1024, '
                                                                                                                                                                                                                                                                                          '2) '
                                                                                                                                                                                                                                                                                          'AS '
                                                                                                                                                                                                                                                                                          'SIZE_MB,\n'
                                                                                                                                                                                                                                                                                          '       '
                                                                                                                                                                                                                                                                                          'D.AUTOEXTENSIBLE, '
                                                                                                                                                                                                                                                                                          'ROUND(D.MAXBYTES/1024/1024, '
                                                                                                                                                                                                                                                                                          '2) '
                                                                                                                                                                                                                                                                                          'AS '
                                                                                                                                                                                                                                                                                          'MAX_MB,\n'
                                                                                                                                                                                                                                                                                          '       '
                                                                                                                                                                                                                                                                                          'T.STATUS$\n'
                                                                                                                                                                                                                                                                                          'FROM '
                                                                                                                                                                                                                                                                                          'DBA_DATA_FILES '
                                                                                                                                                                                                                                                                                          'D\n'
                                                                                                                                                                                                                                                                                          'LEFT '
                                                                                                                                                                                                                                                                                          'JOIN '
                                                                                                                                                                                                                                                                                          'V$TABLESPACE '
                                                                                                                                                                                                                                                                                          'T '
                                                                                                                                                                                                                                                                                          'ON '
                                                                                                                                                                                                                                                                                          'D.TABLESPACE_NAME '
                                                                                                                                                                                                                                                                                          '= '
                                                                                                                                                                                                                                                                                          'T.NAME\n'
                                                                                                                                                                                                                                                                                          'ORDER '
                                                                                                                                                                                                                                                                                          'BY '
                                                                                                                                                                                                                                                                                          'D.TABLESPACE_NAME',
 "SELECT TABLESPACE_NAME, FILE_NAME, STATUS$, ROUND(BYTES/1024/1024, 2) AS SIZE_MB\nFROM DBA_DATA_FILES\nWHERE STATUS$ <> 'ONLINE'": 'SELECT '
                                                                                                                                     'TABLESPACE_NAME, '
                                                                                                                                     'FILE_NAME, '
                                                                                                                                     'STATUS, '
                                                                                                                                     'ONLINE_STATUS, '
                                                                                                                                     'ROUND(BYTES/1024/1024, '
                                                                                                                                     '2) '
                                                                                                                                     'AS '
                                                                                                                                     'SIZE_MB\n'
                                                                                                                                     'FROM '
                                                                                                                                     'DBA_DATA_FILES\n'
                                                                                                                                     'WHERE '
                                                                                                                                     'STATUS '
                                                                                                                                     '<> '
                                                                                                                                     "'AVAILABLE' "
                                                                                                                                     'OR '
                                                                                                                                     'ONLINE_STATUS '
                                                                                                                                     '<> '
                                                                                                                                     "'ONLINE'",
 "SELECT TO_CHAR(timestamp,'YYYY-MM-DD HH24:MI:SS') AS alert_time,\n       reason, message_level, suggested_action\nFROM dba_outstanding_alerts\nORDER BY timestamp DESC": 'SELECT '
                                                                                                                                                                           "TO_CHAR(creation_time,'YYYY-MM-DD "
                                                                                                                                                                           "HH24:MI:SS') "
                                                                                                                                                                           'AS '
                                                                                                                                                                           'alert_time,\n'
                                                                                                                                                                           '       '
                                                                                                                                                                           'reason, '
                                                                                                                                                                           'message_level, '
                                                                                                                                                                           'suggested_action\n'
                                                                                                                                                                           'FROM '
                                                                                                                                                                           'dba_outstanding_alerts\n'
                                                                                                                                                                           'ORDER '
                                                                                                                                                                           'BY '
                                                                                                                                                                           'creation_time '
                                                                                                                                                                           'DESC',
 'SELECT inst_id, ROUND(SUM(wait_time)/100, 2) AS wait_s, SUM(wait_count) AS wait_cnt\nFROM gv$ges_statistics\nGROUP BY inst_id\nORDER BY inst_id': 'SELECT '
                                                                                                                                                    'inst_id, '
                                                                                                                                                    'event, '
                                                                                                                                                    'total_waits, '
                                                                                                                                                    'time_waited\n'
                                                                                                                                                    'FROM '
                                                                                                                                                    'gv$system_event\n'
                                                                                                                                                    'WHERE '
                                                                                                                                                    'event '
                                                                                                                                                    'LIKE '
                                                                                                                                                    "'gc "
                                                                                                                                                    "%' "
                                                                                                                                                    'OR '
                                                                                                                                                    'event '
                                                                                                                                                    'LIKE '
                                                                                                                                                    "'ges "
                                                                                                                                                    "%'\n"
                                                                                                                                                    'ORDER '
                                                                                                                                                    'BY '
                                                                                                                                                    'time_waited '
                                                                                                                                                    'DESC',
 'SELECT name, state, type, total_mb, free_mb,\n       ROUND((total_mb - free_mb)/NULLIF(total_mb,0)*100, 2) AS used_pct,\n       offlining_disks, offline_disks\nFROM v$asm_diskgroup\nORDER BY name': 'SELECT '
                                                                                                                                                                                                        'name, '
                                                                                                                                                                                                        'state, '
                                                                                                                                                                                                        'type, '
                                                                                                                                                                                                        'total_mb, '
                                                                                                                                                                                                        'free_mb,\n'
                                                                                                                                                                                                        '       '
                                                                                                                                                                                                        'ROUND((total_mb '
                                                                                                                                                                                                        '- '
                                                                                                                                                                                                        'free_mb)/NULLIF(total_mb,0)*100, '
                                                                                                                                                                                                        '2) '
                                                                                                                                                                                                        'AS '
                                                                                                                                                                                                        'used_pct,\n'
                                                                                                                                                                                                        '       '
                                                                                                                                                                                                        'offline_disks\n'
                                                                                                                                                                                                        'FROM '
                                                                                                                                                                                                        'v$asm_diskgroup\n'
                                                                                                                                                                                                        'ORDER '
                                                                                                                                                                                                        'BY '
                                                                                                                                                                                                        'name',
 'SELECT name, state_desc, recovery_model_desc,\n       CAST(SUM(size)*8.0/1024 AS DECIMAL(18,2)) AS total_mb\nFROM sys.master_files mf\nJOIN sys.databases d ON mf.database_id = d.database_id\nWHERE d.database_id <= 4\nGROUP BY name, state_desc, recovery_model_desc\nORDER BY name': 'SELECT '
                                                                                                                                                                                                                                                                                           'd.name, '
                                                                                                                                                                                                                                                                                           'd.state_desc, '
                                                                                                                                                                                                                                                                                           'd.recovery_model_desc,\n'
                                                                                                                                                                                                                                                                                           '       '
                                                                                                                                                                                                                                                                                           'CAST(SUM(mf.size)*8.0/1024 '
                                                                                                                                                                                                                                                                                           'AS '
                                                                                                                                                                                                                                                                                           'DECIMAL(18,2)) '
                                                                                                                                                                                                                                                                                           'AS '
                                                                                                                                                                                                                                                                                           'total_mb\n'
                                                                                                                                                                                                                                                                                           'FROM '
                                                                                                                                                                                                                                                                                           'sys.master_files '
                                                                                                                                                                                                                                                                                           'mf\n'
                                                                                                                                                                                                                                                                                           'JOIN '
                                                                                                                                                                                                                                                                                           'sys.databases '
                                                                                                                                                                                                                                                                                           'd '
                                                                                                                                                                                                                                                                                           'ON '
                                                                                                                                                                                                                                                                                           'mf.database_id '
                                                                                                                                                                                                                                                                                           '= '
                                                                                                                                                                                                                                                                                           'd.database_id\n'
                                                                                                                                                                                                                                                                                           'WHERE '
                                                                                                                                                                                                                                                                                           'd.database_id '
                                                                                                                                                                                                                                                                                           '<= '
                                                                                                                                                                                                                                                                                           '4\n'
                                                                                                                                                                                                                                                                                           'GROUP '
                                                                                                                                                                                                                                                                                           'BY '
                                                                                                                                                                                                                                                                                           'd.name, '
                                                                                                                                                                                                                                                                                           'd.state_desc, '
                                                                                                                                                                                                                                                                                           'd.recovery_model_desc\n'
                                                                                                                                                                                                                                                                                           'ORDER '
                                                                                                                                                                                                                                                                                           'BY '
                                                                                                                                                                                                                                                                                           'd.name',
 "SELECT name, type_desc, is_disabled, create_date, modify_date,\n       is_policy_checked, is_expiration_checked\nFROM sys.server_principals\nWHERE type IN ('S','U','G')\nORDER BY name": 'SELECT '
                                                                                                                                                                                            'p.name, '
                                                                                                                                                                                            'p.type_desc, '
                                                                                                                                                                                            'p.is_disabled, '
                                                                                                                                                                                            'p.create_date, '
                                                                                                                                                                                            'p.modify_date,\n'
                                                                                                                                                                                            '       '
                                                                                                                                                                                            'l.is_policy_checked, '
                                                                                                                                                                                            'l.is_expiration_checked\n'
                                                                                                                                                                                            'FROM '
                                                                                                                                                                                            'sys.server_principals '
                                                                                                                                                                                            'p\n'
                                                                                                                                                                                            'LEFT '
                                                                                                                                                                                            'JOIN '
                                                                                                                                                                                            'sys.sql_logins '
                                                                                                                                                                                            'l '
                                                                                                                                                                                            'ON '
                                                                                                                                                                                            'p.principal_id '
                                                                                                                                                                                            '= '
                                                                                                                                                                                            'l.principal_id\n'
                                                                                                                                                                                            'WHERE '
                                                                                                                                                                                            'p.type '
                                                                                                                                                                                            'IN '
                                                                                                                                                                                            "('S','U','G')\n"
                                                                                                                                                                                            'ORDER '
                                                                                                                                                                                            'BY '
                                                                                                                                                                                            'p.name',
 'SELECT primary_server, primary_database, secondary_server, secondary_database,\n       last_backup_file, last_restored_file, last_backup_date, last_restored_date,\n       backup_threshold, threshold_alert_enabled, history_retention_period\nFROM msdb.dbo.log_shipping_monitor_primary\nORDER BY primary_database': 'SELECT '
                                                                                                                                                                                                                                                                                                                          'primary_server, '
                                                                                                                                                                                                                                                                                                                          'primary_database, '
                                                                                                                                                                                                                                                                                                                          'last_backup_file, '
                                                                                                                                                                                                                                                                                                                          'last_backup_date,\n'
                                                                                                                                                                                                                                                                                                                          '       '
                                                                                                                                                                                                                                                                                                                          'backup_threshold, '
                                                                                                                                                                                                                                                                                                                          'threshold_alert_enabled, '
                                                                                                                                                                                                                                                                                                                          'history_retention_period\n'
                                                                                                                                                                                                                                                                                                                          'FROM '
                                                                                                                                                                                                                                                                                                                          'msdb.dbo.log_shipping_monitor_primary\n'
                                                                                                                                                                                                                                                                                                                          'ORDER '
                                                                                                                                                                                                                                                                                                                          'BY '
                                                                                                                                                                                                                                                                                                                          'primary_database',
 "SELECT resource_name, current_utilization, limit_value, max_utilization,\n       ROUND(current_utilization * 100 / NULLIF(limit_value,0), 2) AS usage_pct\nFROM v$resource_limit\nWHERE resource_name IN ('sessions','processes')\nORDER BY resource_name": 'SELECT '
                                                                                                                                                                                                                                                              'resource_name, '
                                                                                                                                                                                                                                                              'current_utilization, '
                                                                                                                                                                                                                                                              'limit_value, '
                                                                                                                                                                                                                                                              'max_utilization,\n'
                                                                                                                                                                                                                                                              '       '
                                                                                                                                                                                                                                                              'ROUND(current_utilization '
                                                                                                                                                                                                                                                              '* '
                                                                                                                                                                                                                                                              '100 '
                                                                                                                                                                                                                                                              '/ '
                                                                                                                                                                                                                                                              'NULLIF(TO_NUMBER(CASE '
                                                                                                                                                                                                                                                              'WHEN '
                                                                                                                                                                                                                                                              'REGEXP_LIKE(limit_value, '
                                                                                                                                                                                                                                                              "'^[0-9]+$') "
                                                                                                                                                                                                                                                              'THEN '
                                                                                                                                                                                                                                                              'limit_value '
                                                                                                                                                                                                                                                              'END),0), '
                                                                                                                                                                                                                                                              '2) '
                                                                                                                                                                                                                                                              'AS '
                                                                                                                                                                                                                                                              'usage_pct\n'
                                                                                                                                                                                                                                                              'FROM '
                                                                                                                                                                                                                                                              'v$resource_limit\n'
                                                                                                                                                                                                                                                              'WHERE '
                                                                                                                                                                                                                                                              'resource_name '
                                                                                                                                                                                                                                                              'IN '
                                                                                                                                                                                                                                                              "('sessions','processes')\n"
                                                                                                                                                                                                                                                              'ORDER '
                                                                                                                                                                                                                                                              'BY '
                                                                                                                                                                                                                                                              'resource_name',
 "SELECT resource_name, current_utilization, max_utilization, limit_value,\n       ROUND(max_utilization * 100 / NULLIF(limit_value,0), 2) AS peak_pct\nFROM v$resource_limit\nWHERE resource_name IN ('sessions','processes','transactions')\nORDER BY resource_name": 'SELECT '
                                                                                                                                                                                                                                                                        'resource_name, '
                                                                                                                                                                                                                                                                        'current_utilization, '
                                                                                                                                                                                                                                                                        'max_utilization, '
                                                                                                                                                                                                                                                                        'limit_value,\n'
                                                                                                                                                                                                                                                                        '       '
                                                                                                                                                                                                                                                                        'ROUND(max_utilization '
                                                                                                                                                                                                                                                                        '* '
                                                                                                                                                                                                                                                                        '100 '
                                                                                                                                                                                                                                                                        '/ '
                                                                                                                                                                                                                                                                        'NULLIF(TO_NUMBER(CASE '
                                                                                                                                                                                                                                                                        'WHEN '
                                                                                                                                                                                                                                                                        'REGEXP_LIKE(limit_value, '
                                                                                                                                                                                                                                                                        "'^[0-9]+$') "
                                                                                                                                                                                                                                                                        'THEN '
                                                                                                                                                                                                                                                                        'limit_value '
                                                                                                                                                                                                                                                                        'END),0), '
                                                                                                                                                                                                                                                                        '2) '
                                                                                                                                                                                                                                                                        'AS '
                                                                                                                                                                                                                                                                        'peak_pct\n'
                                                                                                                                                                                                                                                                        'FROM '
                                                                                                                                                                                                                                                                        'v$resource_limit\n'
                                                                                                                                                                                                                                                                        'WHERE '
                                                                                                                                                                                                                                                                        'resource_name '
                                                                                                                                                                                                                                                                        'IN '
                                                                                                                                                                                                                                                                        "('sessions','processes','transactions')\n"
                                                                                                                                                                                                                                                                        'ORDER '
                                                                                                                                                                                                                                                                        'BY '
                                                                                                                                                                                                                                                                        'resource_name',
 "SELECT session_key, status, operation,\n       TO_CHAR(start_time,'YYYY-MM-DD HH24:MI:SS') AS start_time,\n       output_device_type\nFROM v$rman_status\nWHERE start_time > SYSDATE - 30\n  AND operation LIKE '%VALIDATE%'\nORDER BY start_time DESC": 'SELECT '
                                                                                                                                                                                                                                                           'recid '
                                                                                                                                                                                                                                                           'AS '
                                                                                                                                                                                                                                                           'session_key, '
                                                                                                                                                                                                                                                           'status, '
                                                                                                                                                                                                                                                           'operation,\n'
                                                                                                                                                                                                                                                           '       '
                                                                                                                                                                                                                                                           "TO_CHAR(start_time,'YYYY-MM-DD "
                                                                                                                                                                                                                                                           "HH24:MI:SS') "
                                                                                                                                                                                                                                                           'AS '
                                                                                                                                                                                                                                                           'start_time,\n'
                                                                                                                                                                                                                                                           '       '
                                                                                                                                                                                                                                                           'output_device_type\n'
                                                                                                                                                                                                                                                           'FROM '
                                                                                                                                                                                                                                                           'v$rman_status\n'
                                                                                                                                                                                                                                                           'WHERE '
                                                                                                                                                                                                                                                           'start_time '
                                                                                                                                                                                                                                                           '> '
                                                                                                                                                                                                                                                           'SYSDATE '
                                                                                                                                                                                                                                                           '- '
                                                                                                                                                                                                                                                           '30\n'
                                                                                                                                                                                                                                                           '  '
                                                                                                                                                                                                                                                           'AND '
                                                                                                                                                                                                                                                           'operation '
                                                                                                                                                                                                                                                           'LIKE '
                                                                                                                                                                                                                                                           "'%VALIDATE%'\n"
                                                                                                                                                                                                                                                           'ORDER '
                                                                                                                                                                                                                                                           'BY '
                                                                                                                                                                                                                                                           'start_time '
                                                                                                                                                                                                                                                           'DESC'}
