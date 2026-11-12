# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""

业务要求基线种子数据（单一数据源）
==================================

本模块是「业务要求基线」的唯一数据定义处，供两侧共用：

- ``scripts/gen_business_baselines.py``：写入 inspection.db；
- ``modules/inspection/config_baseline._get_default_baselines()``：
  供 ``dal.force_reset_baselines`` 重置时使用。

历史问题：重置入口用的是一份 113 条的旧清单，而初始化/增量补齐用的是
另一份 206 条的种子，两者长期不同步——执行「重置基线」会把库里的基线
削回旧集合。全部收敛到本模块后，重置只会回到同一份权威定义。

数据来源：《数据库运维服务需求拆解清单》
- 参数基线：M1-07 / 交付物 D-03《数据库参数表》
- 指标阈值：§4.6 阈值基线建议
"""


# ═══════════════════════════════════════════════════════════════════════
#  一、参数基线（inspection_baseline）
#
#  每条: (param_name, query_sql, operator, expected_value,
#         risk_level, description_zh, description_en)
#
#  风险级别含义:
#    CRITICAL 触碰即可能造成数据不可用/可被接管
#    HIGH     安全或可用性硬要求
#    MEDIUM   性能与稳定性，偏差需有结论
#    LOW      建议值，偏差可记录
# ═══════════════════════════════════════════════════════════════════════

# ── GBase 8a MPP Cluster（gcluster + gnode，MySQL 协议）──────────────
# 说明：8a 与 8s（Informix 血统，仓库内 db_type='gbase'）是两条完全不同的
# 产品线，参数体系不通用。此前 8a 没有任何基线，8s 的 BUFFERS/LOCKS/
# SHMVIRTSIZE 被误当作 8a 基线，本次拆开。
# gbase_heap_* / gbase_buffer_* 单位均为 MB；max_allowed_packet 为字节。
_GBASE8A = [
    # 内存
    ('gbase_heap_data',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_heap_data';",
     '>=', '1024', 'HIGH',
     '数据堆内存（MB），建议不低于 1024，过小将频繁刷盘导致扫描变慢',
     'Data heap memory (MB), recommended >= 1024; too small causes frequent spill'),
    ('gbase_heap_temp',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_heap_temp';",
     '>=', '512', 'MEDIUM',
     '临时堆内存（MB），建议不低于 512，影响排序与临时结果落盘',
     'Temp heap memory (MB), recommended >= 512; affects sort/temp spill'),
    ('gbase_heap_large',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_heap_large';",
     '>=', '512', 'MEDIUM',
     '大对象堆内存（MB），建议不低于 512',
     'Large-object heap memory (MB), recommended >= 512'),
    ('gbase_buffer_result',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_buffer_result';",
     '>=', '128', 'LOW',
     '结果集缓冲区（MB），建议不低于 128，过小导致大结果集反复落盘',
     'Result-set buffer (MB), recommended >= 128'),
    ('gbase_buffer_sort',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_buffer_sort';",
     '>=', '128', 'LOW',
     '排序缓冲区（MB），建议不低于 128',
     'Sort buffer (MB), recommended >= 128'),
    ('gbase_buffer_hgr',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_buffer_hgr';",
     '>=', '256', 'LOW',
     'Hash Group 缓冲区（MB），建议不低于 256，影响聚合性能',
     'Hash-group buffer (MB), recommended >= 256'),
    # 并发
    ('max_connections',
     "SHOW GLOBAL VARIABLES LIKE 'max_connections';",
     '>=', '1000', 'HIGH',
     '最大连接数，集群建议不低于 1000；过小将直接拒绝新连接',
     'Max connections, cluster recommended >= 1000; too small rejects new sessions'),
    ('gbase_parallel_degree',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_parallel_degree';",
     '>=', '1', 'MEDIUM',
     '并行度，建议不低于 1（0 为自动，易在混合负载下失控）',
     'Parallel degree, recommended >= 1 (0 = auto, may thrash under mixed load)'),
    ('gbase_parallel_max_thread_in_pool',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_parallel_max_thread_in_pool';",
     '>=', '4', 'LOW',
     '并行线程池上限，建议不低于 4',
     'Parallel thread-pool upper bound, recommended >= 4'),
    # 日志与审计（合规硬要求）
    ('gbase_express_log',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_express_log';",
     '=', '1', 'HIGH',
     'Express 日志应开启（=1），否则故障后无执行流水可回溯',
     'Express log must be ON (=1); otherwise no execution trail after an incident'),
    ('gbase_audit_log',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_audit_log';",
     '=', '1', 'HIGH',
     '审计日志应开启（=1），满足安全合规留痕要求',
     'Audit log must be ON (=1) to satisfy compliance trail requirements'),
    ('gbase_sql_trace',
     "SHOW GLOBAL VARIABLES LIKE 'gbase_sql_trace';",
     '=', '1', 'MEDIUM',
     'SQL 追踪应开启（=1），用于慢 SQL 定位与执行计划留证',
     'SQL trace should be ON (=1) for slow-SQL diagnosis and plan evidence'),
    # 超时与网络
    ('wait_timeout',
     "SHOW GLOBAL VARIABLES LIKE 'wait_timeout';",
     '<=', '600', 'LOW',
     '空闲连接超时（秒），建议不超过 600，避免连接长期占用',
     'Idle connection timeout (s), recommended <= 600'),
    ('interactive_timeout',
     "SHOW GLOBAL VARIABLES LIKE 'interactive_timeout';",
     '<=', '600', 'LOW',
     '交互式连接超时（秒），建议与 wait_timeout 一致',
     'Interactive timeout (s), recommended equal to wait_timeout'),
    ('max_allowed_packet',
     "SHOW GLOBAL VARIABLES LIKE 'max_allowed_packet';",
     '>=', '16777216', 'LOW',
     '单包上限（字节），建议不低于 16MB，过小导致大字段写入失败',
     'Max packet size (bytes), recommended >= 16MB'),
    ('long_query_time',
     "SHOW GLOBAL VARIABLES LIKE 'long_query_time';",
     '<=', '2', 'MEDIUM',
     '慢 SQL 阈值（秒），建议不超过 2',
     'Slow-query threshold (s), recommended <= 2'),
    ('character_set_server',
     "SHOW GLOBAL VARIABLES LIKE 'character_set_server';",
     'like', 'utf8', 'MEDIUM',
     '服务端字符集应为 utf8 / utf8mb4，避免中文与生僻字截断',
     'Server charset should be utf8 / utf8mb4 to avoid truncation'),
]


# ── DM8（达梦）─────────────────────────────────────────────────────────
# 方言：SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = '<NAME>'
# 内存类参数单位 MB。风险：参数名写错 → 该条返回空集 → 巡检静默跳过。
_DM8 = [
    # 内存
    ('MEMORY_TARGET',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'MEMORY_TARGET';",
     '>=', '4096', 'HIGH',
     '内存总目标（MB），建议不低于物理内存 70% 且 >= 4096',
     'Total memory target (MB), recommended >= 70% of RAM and >= 4096'),
    ('BUFFER',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'BUFFER';",
     '>=', '1024', 'HIGH',
     '数据缓冲区（MB），建议不低于 1024，直接决定缓冲命中率',
     'Data buffer (MB), recommended >= 1024; drives buffer hit ratio'),
    ('MAX_BUFFER',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'MAX_BUFFER';",
     '>=', '2048', 'MEDIUM',
     '最大数据缓冲区（MB），建议不低于 BUFFER 的 2 倍以留出动态扩展',
     'Max data buffer (MB), recommended >= 2x BUFFER for dynamic growth'),
    ('MEMORY_POOL',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'MEMORY_POOL';",
     '>=', '512', 'MEDIUM',
     '共享内存池（MB），建议不低于 512',
     'Shared memory pool (MB), recommended >= 512'),
    ('TEMP_SIZE',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'TEMP_SIZE';",
     '>=', '1024', 'MEDIUM',
     '临时表空间初始大小（MB），建议不低于 1024',
     'Temp tablespace initial size (MB), recommended >= 1024'),
    # 并发
    ('MAX_SESSIONS',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'MAX_SESSIONS';",
     '>=', '500', 'HIGH',
     '最大会话数，建议不低于 500',
     'Max sessions, recommended >= 500'),
    ('MAX_CONNECTIONS',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'MAX_CONNECTIONS';",
     '>=', '200', 'MEDIUM',
     '最大连接数，建议不低于 200',
     'Max connections, recommended >= 200'),
    ('OPEN_CURSORS',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'OPEN_CURSORS';",
     '>=', '500', 'MEDIUM',
     '单会话最大游标数，建议不低于 500',
     'Open cursors per session, recommended >= 500'),
    # IO
    ('IO_THR_GROUPS',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'IO_THR_GROUPS';",
     '>=', '8', 'MEDIUM',
     'IO 线程组数，建议不低于 8，过小在高并发下形成 IO 排队',
     'IO thread groups, recommended >= 8; too small queues IO under concurrency'),
    ('DBWR_IO_SLAVES',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'DBWR_IO_SLAVES';",
     '>=', '4', 'LOW',
     'DBWR IO 从线程数，建议不低于 4（异步 IO 可用时可放宽）',
     'DBWR IO slaves, recommended >= 4 (relaxable when async IO is enabled)'),
    ('SORT_BUF_SIZE',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'SORT_BUF_SIZE';",
     '>=', '16', 'LOW',
     '排序缓冲区（MB），建议不低于 16，过小导致排序落盘',
     'Sort buffer (MB), recommended >= 16; too small spills to disk'),
    ('HJ_BUF_SIZE',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'HJ_BUF_SIZE';",
     '>=', '512', 'LOW',
     'Hash Join 缓冲区（MB），建议不低于 512',
     'Hash-join buffer (MB), recommended >= 512'),
    # 日志与归档（可用性）
    ('RLOG_BUF_SIZE',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'RLOG_BUF_SIZE';",
     '>=', '4096', 'MEDIUM',
     'Redo 日志缓冲区（页），建议不低于 4096',
     'Redo log buffer (pages), recommended >= 4096'),
    ('RLOG_PARALLEL_ENABLE',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'RLOG_PARALLEL_ENABLE';",
     '=', '1', 'MEDIUM',
     'Redo 并行写应开启（=1），提升高并发写入吞吐',
     'Parallel redo write should be ON (=1) for write throughput'),
    ('ARCH_INI',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'ARCH_INI';",
     '=', '1', 'CRITICAL',
     '归档模式必须开启（=1），否则无法做时间点恢复，RPO 无法保障',
     'Archive mode must be ON (=1); otherwise PITR is impossible'),
    # 安全
    ('ENABLE_AUDIT',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'ENABLE_AUDIT';",
     '>=', '1', 'HIGH',
     '审计应开启（>=1），满足安全合规留痕要求',
     'Audit must be enabled (>=1) for compliance trail'),
    ('PWD_POLICY',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'PWD_POLICY';",
     '>=', '2', 'HIGH',
     '口令策略位图，建议 >= 2（至少启用口令复杂度校验）',
     'Password policy bitmap, recommended >= 2 (enable complexity check)'),
    ('ENABLE_ENCRYPT',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'ENABLE_ENCRYPT';",
     '>=', '1', 'MEDIUM',
     '通信加密应开启（>=1），避免口令与数据明文过网',
     'Communication encryption should be ON (>=1)'),
    # 运维
    ('UNDO_RETENTION',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'UNDO_RETENTION';",
     '>=', '3600', 'MEDIUM',
     'UNDO 保留时间（秒），建议不低于 3600，支撑长事务回滚与闪回',
     'UNDO retention (s), recommended >= 3600 for long transactions'),
    ('TASK_THREAD_TIMEOUT',
     "SELECT PARA_VALUE FROM V$DM_INI WHERE PARA_NAME = 'TASK_THREAD_TIMEOUT';",
     '>=', '60', 'LOW',
     '任务线程超时（秒），建议不低于 60，过小会误杀长查询',
     'Task thread timeout (s), recommended >= 60; too small kills long queries'),
    # 安全（概要文件 PROFILE 资源，非 V$DM_INI 参数）
    # 说明：登录失败次数与口令有效期在 DM8 中由概要文件（PROFILE）管理，
    # 不存在于 V$DM_INI。历史版本误用 MAX_LOGIN_FAILURE / PASSWORD_LIFE_DAYS
    # 去查 V$DM_INI，必然返回空集，已在 DEPRECATED_PARAMS 中停用。
    ('FAILED_LOGIN_ATTEMPTS',
     "SELECT LIMIT FROM DBA_PROFILES WHERE RESOURCE_NAME = 'FAILED_LOGIN_ATTEMPTS' "
     "AND PROFILE = 'DEFAULT';",
     '<=', '5', 'HIGH',
     '登录失败锁定阈值（次），建议 <= 5，防止口令被在线爆破',
     'Failed login attempt limit, recommended <= 5 to resist online brute force'),
    ('PASSWORD_LIFE_TIME',
     "SELECT LIMIT FROM DBA_PROFILES WHERE RESOURCE_NAME = 'PASSWORD_LIFE_TIME' "
     "AND PROFILE = 'DEFAULT';",
     '<=', '180', 'MEDIUM',
     '口令有效期（天），建议 <= 180，超期强制更换',
     'Password lifetime (days), recommended <= 180 to force rotation'),
]


# ── Oracle ─────────────────────────────────────────────────────────────
# 方言：SELECT name, value FROM V$PARAMETER WHERE name = '<name>'
_ORACLE = [
    # 内存
    ('db_block_size',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'db_block_size'",
     '>=', '8192', 'MEDIUM',
     '数据块大小（字节），建议 8192，创建后不可变更',
     'DB block size (bytes), recommended 8192; immutable after creation'),
    ('db_cache_size',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'db_cache_size'",
     '>=', '1073741824', 'HIGH',
     'Buffer Cache 大小（字节），建议不低于 1GB',
     'Buffer cache size (bytes), recommended >= 1GB'),
    ('shared_pool_size',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'shared_pool_size'",
     '>=', '536870912', 'HIGH',
     'Shared Pool 大小（字节），建议不低于 512MB，过小导致硬解析飙升',
     'Shared pool size (bytes), recommended >= 512MB; too small spikes hard parses'),
    ('large_pool_size',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'large_pool_size'",
     '>=', '134217728', 'LOW',
     'Large Pool（字节），建议不低于 128MB，供 RMAN/共享服务器使用',
     'Large pool (bytes), recommended >= 128MB for RMAN/shared server'),
    ('log_buffer',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'log_buffer'",
     '>=', '8388608', 'MEDIUM',
     'Redo 日志缓冲区（字节），建议不低于 8MB',
     'Redo log buffer (bytes), recommended >= 8MB'),
    # 并发
    ('open_cursors',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'open_cursors'",
     '>=', '500', 'MEDIUM',
     '单会话最大游标数，建议不低于 500',
     'Open cursors per session, recommended >= 500'),
    ('job_queue_processes',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'job_queue_processes'",
     '>=', '10', 'LOW',
     '作业队列进程数，建议不低于 10，过小导致调度任务堆积',
     'Job queue processes, recommended >= 10; too small backs up schedulers'),
    ('parallel_max_servers',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'parallel_max_servers'",
     '>=', '16', 'LOW',
     '并行执行服务进程上限，建议不低于 16',
     'Parallel max servers, recommended >= 16'),
    # IO
    ('filesystemio_options',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'filesystemio_options'",
     'like', 'ASYNCH', 'MEDIUM',
     '文件系统 IO 应启用异步（含 ASYNCH），否则 IO 串行化',
     'Filesystem IO should enable ASYNCH; otherwise IO serializes'),
    ('disk_asynch_io',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'disk_asynch_io'",
     '=', 'TRUE', 'MEDIUM',
     '磁盘异步 IO 应开启（TRUE）',
     'Disk async IO should be TRUE'),
    # 优化器与游标
    ('cursor_sharing',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'cursor_sharing'",
     '=', 'EXACT', 'MEDIUM',
     '游标共享应为 EXACT；FORCE 会造成执行计划劣化与 SQL 注入面扩大',
     'Cursor sharing should be EXACT; FORCE degrades plans and widens injection surface'),
    ('optimizer_index_cost_adj',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'optimizer_index_cost_adj'",
     '<=', '100', 'LOW',
     '索引成本调整系数，建议不超过 100（100 为标准值）',
     'Optimizer index cost adjustment, recommended <= 100 (100 = neutral)'),
    # 安全
    ('sec_case_sensitive_logon',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'sec_case_sensitive_logon'",
     '=', 'TRUE', 'HIGH',
     '口令大小写敏感应开启（TRUE）；关闭将削弱口令强度',
     'Case-sensitive logon should be TRUE; disabling weakens password strength'),
    ('audit_sys_operations',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'audit_sys_operations'",
     '=', 'TRUE', 'HIGH',
     'SYSDBA 操作审计应开启（TRUE），满足特权操作留痕',
     'Audit of SYSDBA operations should be TRUE for privileged-action trail'),
    ('resource_limit',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'resource_limit'",
     '=', 'TRUE', 'MEDIUM',
     '资源限制应启用（TRUE），否则 PROFILE 中的口令与资源策略不生效',
     'Resource limits should be TRUE; otherwise PROFILE policies are inert'),
    ('deferred_segment_creation',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'deferred_segment_creation'",
     '=', 'FALSE', 'MEDIUM',
     '段延迟创建应关闭（FALSE）；开启会让空表不占空间但空间统计失真',
     'Deferred segment creation should be FALSE; TRUE distorts space accounting'),
    # 超时
    ('distributed_lock_timeout',
     "SELECT name, value FROM V$PARAMETER WHERE name = 'distributed_lock_timeout'",
     '<=', '60', 'MEDIUM',
     '分布式锁等待超时（秒），建议不超过 60，避免跨库事务无限挂起',
     'Distributed lock timeout (s), recommended <= 60 to avoid hung cross-DB transactions'),
]


# ── SQL Server ─────────────────────────────────────────────────────────
# 方言：SELECT CAST(value AS INT) FROM sys.configurations WHERE name = '<name>'
_SQLSERVER = [
    # 内存
    ('max server memory (MB)',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'max server memory (MB)';",
     '>=', '4096', 'HIGH',
     '最大服务器内存（MB），建议留 15% 给 OS：设为物理内存 85%',
     'Max server memory (MB), recommended 85% of RAM (keep 15% for OS)'),
    ('min server memory (MB)',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'min server memory (MB)';",
     '>=', '1024', 'MEDIUM',
     '最小服务器内存（MB），建议不低于 1024，防止内存被 OS 挤压',
     'Min server memory (MB), recommended >= 1024'),
    # 并发
    ('max worker threads',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'max worker threads';",
     '>=', '512', 'MEDIUM',
     '最大工作线程数，建议不低于 512（0 为系统自动，高并发下易线程耗尽）',
     'Max worker threads, recommended >= 512 (0 = auto, may exhaust under load)'),
    # 实例安全基线（0 为关闭）
    ('xp_cmdshell',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'xp_cmdshell';",
     '=', '0', 'CRITICAL',
     'xp_cmdshell 必须关闭（=0）；开启即等于给数据库账号一个 OS shell',
     'xp_cmdshell must be OFF (=0); ON hands DB accounts an OS shell'),
    ('Ole Automation Procedures',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'Ole Automation Procedures';",
     '=', '0', 'HIGH',
     'OLE 自动化过程应关闭（=0），可被用于提权与横向移动',
     'OLE Automation Procedures should be OFF (=0); abusable for privilege escalation'),
    ('Ad Hoc Distributed Queries',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'Ad Hoc Distributed Queries';",
     '=', '0', 'HIGH',
     '即席分布式查询应关闭（=0），可被用于跨库数据外带',
     'Ad Hoc Distributed Queries should be OFF (=0); abusable for cross-instance exfiltration'),
    ('cross db ownership chaining',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'cross db ownership chaining';",
     '=', '0', 'HIGH',
     '跨库所有权链应关闭（=0），否则同所有者库之间可越权访问',
     'Cross-DB ownership chaining should be OFF (=0) to prevent privilege crossover'),
    ('clr enabled',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'clr enabled';",
     '=', '0', 'MEDIUM',
     'CLR 应关闭（=0），除非业务明确依赖 CLR 存储过程',
     'CLR should be OFF (=0) unless the application explicitly depends on it'),
    ('contained database authentication',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'contained database authentication';",
     '=', '0', 'MEDIUM',
     '包含数据库身份验证应关闭（=0），避免库级账号绕过实例审计',
     'Contained DB authentication should be OFF (=0) to avoid bypassing instance audit'),
    ('remote admin connections',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'remote admin connections';",
     '=', '0', 'MEDIUM',
     '远程管理员连接建议关闭（=0）；仅在 DAC 排障时临时开启',
     'Remote admin connections recommended OFF (=0); enable only for DAC triage'),
    ('priority boost',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'priority boost';",
     '=', '0', 'MEDIUM',
     '优先级提升必须关闭（=0），开启会饿死 OS 其他进程',
     'Priority boost must be OFF (=0); ON starves other OS processes'),
    ('lightweight pooling',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'lightweight pooling';",
     '=', '0', 'LOW',
     '轻量池化建议关闭（=0），仅在确认 CPU 上下文切换瓶颈后开启',
     'Lightweight pooling recommended OFF (=0)'),
    # 备份与恢复（可用性）
    ('backup checksum default',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'backup checksum default';",
     '=', '1', 'HIGH',
     '备份校验和应开启（=1），否则备份集损坏无法在恢复前发现',
     'Backup checksum should be ON (=1); otherwise corruption surfaces only at restore'),
    ('backup compression default',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'backup compression default';",
     '=', '1', 'MEDIUM',
     '备份压缩默认开启（=1），降低备份窗口与介质占用',
     'Backup compression should be ON (=1) to cut window and media usage'),
    ('default trace enabled',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'default trace enabled';",
     '=', '1', 'MEDIUM',
     '默认跟踪应开启（=1），提供配置变更与对象变更的事后取证线索',
     'Default trace should be ON (=1) for post-incident change forensics'),
    # 性能
    ('optimize for ad hoc workloads',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'optimize for ad hoc workloads';",
     '=', '1', 'MEDIUM',
     '即席工作负载优化应开启（=1），减少一次性计划撑爆计划缓存',
     'Optimize for ad hoc workloads should be ON (=1) to protect plan cache'),
    ('cost threshold for parallelism',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'cost threshold for parallelism';",
     '>=', '25', 'MEDIUM',
     '并行开销阈值建议 25-50；默认 5 会让小查询也并行，拖垮整体',
     'Cost threshold for parallelism recommended 25-50; default 5 parallelizes too eagerly'),
    ('max degree of parallelism',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'max degree of parallelism';",
     '<=', '8', 'MEDIUM',
     '最大并行度建议不超过 8（0 为不限，易造成 CXPACKET 等待）',
     'MAXDOP recommended <= 8 (0 = unlimited, causes CXPACKET waits)'),
    ('network packet size (B)',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'network packet size (B)';",
     '>=', '4096', 'LOW',
     '网络包大小（字节），建议不低于 4096',
     'Network packet size (B), recommended >= 4096'),
    # 超时
    ('remote query timeout (s)',
     "SELECT CAST(value AS INT) FROM sys.configurations WHERE name = 'remote query timeout (s)';",
     '<=', '600', 'LOW',
     '远程查询超时（秒），建议不超过 600，避免分布式查询无限挂起',
     'Remote query timeout (s), recommended <= 600'),
]


PARAM_BASELINES = {
    'gbase8a': _GBASE8A,
    'dm8': _DM8,
    'oracle': _ORACLE,
    'sqlserver': _SQLSERVER,
}


# ═══════════════════════════════════════════════════════════════════════
#  二、指标阈值基线（server_thresholds）
#
#  严格对齐《数据库运维服务需求拆解清单》§4.6 阈值基线建议。
#  每条: (key, value, description_zh, description_en, 对应 §4.6 行)
#
#  命名规约:
#    db_<对象>_<级别>_<量纲>
#    级别: warn = 预警(三级) / alert = 告警(二级) / crit = 紧急(一级)
#    命中率类统一用 _min_pct（越低越差），占用率类用 _pct（越高越差）。
# ═══════════════════════════════════════════════════════════════════════

METRIC_THRESHOLDS = [
    # ── 容量：文件系统 ──
    ('db_fs_usage_warn_pct', 75.0,
     '数据库主机文件系统使用率预警阈值 (%)', 'DB host filesystem usage warning (%)', 'FS'),
    ('db_fs_usage_alert_pct', 85.0,
     '数据库主机文件系统使用率告警阈值 (%)', 'DB host filesystem usage alert (%)', 'FS'),
    ('db_fs_usage_crit_pct', 90.0,
     '数据库主机文件系统使用率紧急阈值 (%)', 'DB host filesystem usage critical (%)', 'FS'),
    ('db_fs_exhaust_days_crit', 7.0,
     '按增长率预测剩余天数低于此值即紧急（天）', 'Predicted days-to-exhaust below this is critical (days)', 'FS'),

    # ── 容量：表空间 ──
    ('db_tbs_usage_warn_pct', 80.0,
     '表空间使用率预警阈值 (%)', 'Tablespace usage warning (%)', 'TBS'),
    ('db_tbs_usage_alert_pct', 90.0,
     '表空间使用率告警阈值 (%)', 'Tablespace usage alert (%)', 'TBS'),
    ('db_tbs_usage_crit_pct', 95.0,
     '表空间使用率紧急阈值 (%)（且无扩展余地）', 'Tablespace usage critical (%), with no autoextend headroom', 'TBS'),

    # ── 容量：会话 ──
    ('db_session_usage_warn_pct', 80.0,
     '会话使用率预警阈值 (%)', 'Session usage warning (%)', 'SESSION'),
    ('db_session_usage_alert_pct', 90.0,
     '会话使用率告警阈值 (%)', 'Session usage alert (%)', 'SESSION'),
    ('db_session_usage_crit_pct', 100.0,
     '会话使用率达到即拒绝连接（%）', 'Session usage at which new connections are rejected (%)', 'SESSION'),

    # ── Oracle 缓存命中率（越低越差）──
    ('db_oracle_buf_cache_warn_min_pct', 95.0,
     'Oracle Buffer Cache 命中率预警下限 (%)', 'Oracle buffer cache hit ratio warning floor (%)', 'ORA_BUF'),
    ('db_oracle_buf_cache_alert_min_pct', 90.0,
     'Oracle Buffer Cache 命中率告警下限 (%)', 'Oracle buffer cache hit ratio alert floor (%)', 'ORA_BUF'),
    ('db_oracle_buf_cache_crit_min_pct', 85.0,
     'Oracle Buffer Cache 命中率紧急下限 (%)', 'Oracle buffer cache hit ratio critical floor (%)', 'ORA_BUF'),
    ('db_oracle_lib_cache_warn_min_pct', 95.0,
     'Oracle Library Cache 命中率预警下限 (%)，偏低表示硬解析过多',
     'Oracle library cache hit ratio warning floor (%)', 'ORA_LIB'),
    ('db_oracle_lib_cache_alert_min_pct', 90.0,
     'Oracle Library Cache 命中率告警下限 (%)', 'Oracle library cache hit ratio alert floor (%)', 'ORA_LIB'),
    ('db_oracle_pga_warn_min_pct', 90.0,
     'Oracle PGA 命中率预警下限 (%)', 'Oracle PGA hit ratio warning floor (%)', 'ORA_PGA'),
    ('db_oracle_pga_alert_min_pct', 85.0,
     'Oracle PGA 命中率告警下限 (%)', 'Oracle PGA hit ratio alert floor (%)', 'ORA_PGA'),

    # ── SQL Server 缓存与内存压力 ──
    ('db_mssql_buf_cache_warn_min_pct', 97.0,
     'MSSQL Buffer Cache 命中率预警下限 (%)', 'MSSQL buffer cache hit ratio warning floor (%)', 'MSSQL_BUF'),
    ('db_mssql_buf_cache_alert_min_pct', 95.0,
     'MSSQL Buffer Cache 命中率告警下限 (%)', 'MSSQL buffer cache hit ratio alert floor (%)', 'MSSQL_BUF'),
    ('db_mssql_buf_cache_crit_min_pct', 90.0,
     'MSSQL Buffer Cache 命中率紧急下限 (%)', 'MSSQL buffer cache hit ratio critical floor (%)', 'MSSQL_BUF'),
    ('db_mssql_ple_warn_min_s', 300.0,
     'MSSQL Page Life Expectancy 预警下限（秒）', 'MSSQL page life expectancy warning floor (s)', 'MSSQL_PLE'),
    ('db_mssql_ple_alert_min_s', 180.0,
     'MSSQL Page Life Expectancy 告警下限（秒）', 'MSSQL page life expectancy alert floor (s)', 'MSSQL_PLE'),
    ('db_mssql_ple_crit_min_s', 60.0,
     'MSSQL Page Life Expectancy 紧急下限（秒）', 'MSSQL page life expectancy critical floor (s)', 'MSSQL_PLE'),
    ('db_mssql_lazy_writes_warn_per_s', 20.0,
     'MSSQL Lazy writes/sec 预警上限', 'MSSQL lazy writes/sec warning ceiling', 'MSSQL_LAZY'),
    ('db_mssql_lazy_writes_alert_per_s', 50.0,
     'MSSQL Lazy writes/sec 告警上限', 'MSSQL lazy writes/sec alert ceiling', 'MSSQL_LAZY'),

    # ── 主机资源 ──
    ('db_cpu_avg_warn_pct', 70.0,
     'CPU 平均使用率预警阈值 (%)', 'CPU average usage warning (%)', 'CPU'),
    ('db_cpu_avg_alert_pct', 85.0,
     'CPU 平均使用率告警阈值 (%)', 'CPU average usage alert (%)', 'CPU'),
    ('db_cpu_avg_crit_pct', 95.0,
     'CPU 平均使用率紧急阈值 (%)', 'CPU average usage critical (%)', 'CPU'),
    ('db_cpu_crit_hold_min', 15.0,
     'CPU 需持续超过紧急阈值多少分钟才升级为一级（分钟）',
     'Minutes CPU must stay above critical before escalating (min)', 'CPU'),

    # ── 磁盘 IO ──
    ('db_disk_await_warn_ms', 20.0,
     '磁盘 await 预警阈值（毫秒，机械盘基线）', 'Disk await warning (ms, spinning-disk baseline)', 'IO'),
    ('db_disk_await_alert_ms', 50.0,
     '磁盘 await 告警阈值（毫秒）', 'Disk await alert (ms)', 'IO'),
    ('db_disk_await_crit_ms', 100.0,
     '磁盘 await 紧急阈值（毫秒）', 'Disk await critical (ms)', 'IO'),

    # ── 锁与死锁 ──
    ('db_deadlock_warn_multiplier', 1.0,
     '月死锁次数超过自身基线倍数即预警', 'Monthly deadlocks exceeding self-baseline by this factor = warning', 'DEADLOCK'),
    ('db_deadlock_alert_multiplier', 2.0,
     '月死锁次数超过自身基线倍数即告警', 'Monthly deadlocks exceeding self-baseline by this factor = alert', 'DEADLOCK'),

    # ── 慢 SQL ──
    ('db_slow_sql_warn_per_week', 1.0,
     '慢 SQL（>5s）每周新增条数预警阈值', 'New slow SQL (>5s) per week warning count', 'SLOWSQL'),
    ('db_slow_sql_alert_per_week', 5.0,
     '慢 SQL（>5s）每周新增条数告警阈值', 'New slow SQL (>5s) per week alert count', 'SLOWSQL'),
    ('db_slow_sql_crit_single_s', 60.0,
     '单条 SQL 执行超过此秒数即一级（秒）', 'A single statement longer than this is critical (s)', 'SLOWSQL'),

    # ── 索引与统计信息 ──
    ('db_index_frag_warn_pct', 30.0,
     '索引碎片率预警阈值 (%)', 'Index fragmentation warning (%)', 'INDEX'),
    ('db_index_frag_alert_pct', 50.0,
     '索引碎片率告警阈值 (%)', 'Index fragmentation alert (%)', 'INDEX'),
    ('db_stats_stale_warn_days', 7.0,
     '统计信息过期预警天数（天）', 'Stale statistics warning (days)', 'STATS'),
    ('db_stats_stale_alert_days', 30.0,
     '统计信息过期告警天数（天）', 'Stale statistics alert (days)', 'STATS'),

    # ── 备份与归档（RPO 底线）──
    ('db_backup_success_required_pct', 100.0,
     '备份成功率要求 (%)，低于即不达标', 'Required backup success rate (%)', 'BACKUP'),
    ('db_backup_consecutive_fail_crit', 2.0,
     '连续失败次数达到即升为一级', 'Consecutive backup failures that escalate to critical', 'BACKUP'),
    ('db_backup_size_drop_alert_pct', 30.0,
     '备份集体积较基线骤降超过此比例视为可疑 (%)', 'Backup size dropping below baseline by this percentage is suspicious (%)', 'BACKUP'),
    ('db_backup_duration_drift_alert_pct', 30.0,
     '备份耗时偏离基线超过此比例即告警 (%)', 'Backup duration drifting from baseline by this percentage = alert (%)', 'BACKUP'),
    ('db_archive_gap_crit_count', 1.0,
     '归档断档容忍条数（0 容忍，达到 1 即一级）', 'Tolerated archive gaps (1 means zero tolerance)', 'ARCHIVE'),

    # ── 集群（GBase 8a / DM8 DSC / Oracle RAC / MSSQL AG）──
    ('db_cluster_lag_warn_s', 5.0,
     '集群同步延迟预警阈值（秒）', 'Cluster replication lag warning (s)', 'CLUSTER'),
    ('db_cluster_lag_alert_s', 30.0,
     '集群同步延迟告警阈值（秒）', 'Cluster replication lag alert (s)', 'CLUSTER'),
    ('db_cluster_lag_crit_s', 300.0,
     '集群同步延迟紧急阈值（秒）', 'Cluster replication lag critical (s)', 'CLUSTER'),

    # ── 账号安全 ──
    ('db_hrisk_account_new_unreported_alert', 1.0,
     '出现未报备的新增高危账号即告警（1=启用）', 'Unreported new high-risk account triggers alert (1=on)', 'ACCOUNT'),
    ('db_hrisk_account_unauthorized_grant_crit', 1.0,
     '出现未授权提权即一级（1=启用）', 'Unauthorized privilege escalation is critical (1=on)', 'ACCOUNT'),

    # ── 实例自身基线（首月必须建立，避免一套阈值套全实例）──
    ('db_self_baseline_window_days', 30.0,
     '建立实例自身基线所用的历史窗口天数（天）',
     'History window used to derive per-instance baselines (days)', 'SELF'),
    ('db_self_baseline_p95_multiplier', 1.2,
     '实例阈值 = max(通用阈值, 自身 P95 × 该倍数)',
     'Instance threshold = max(generic, own P95 x this multiplier)', 'SELF'),
]


# ── 已废弃参数基线（名称有误 / 来源错位）────────────────────────────────
# 这些条目存在于 inspection_baseline，但参数名或取数视图与产品官方口径不一致，
# 在真库上必然返回空集 → 判定为 UNKNOWN，既不误报也查不出，等于白占一条。
#
# 处置：置 enabled=0 保留历史（不物理删除，便于审计追溯），并在需要时补一条
# 名称正确的替代条目。迁移脚本：scripts/fix_deprecated_baselines.py
#
# 格式：(param_name, reason_zh, replacement_param_name)
DEPRECATED_PARAMS = {
    'dm8': {
        'BUFFER_SIZE': (
            'DM8 无 BUFFER_SIZE 参数，正确名称为 BUFFER；已有 BUFFER 条目，本条必然空集',
            'BUFFER',
        ),
        'MAX_BUFFER_SIZE': (
            'DM8 无 MAX_BUFFER_SIZE 参数，正确名称为 MAX_BUFFER；已有 MAX_BUFFER 条目',
            'MAX_BUFFER',
        ),
        'MAX_LOGIN_FAILURE': (
            'V$DM_INI 无此参数；登录失败锁定由概要文件管理，'
            '应查 DBA_PROFILES.FAILED_LOGIN_ATTEMPTS',
            'FAILED_LOGIN_ATTEMPTS',
        ),
        'PASSWORD_LIFE_DAYS': (
            'V$DM_INI 无此参数；口令有效期由概要文件管理，'
            '应查 DBA_PROFILES.PASSWORD_LIFE_TIME',
            'PASSWORD_LIFE_TIME',
        ),
    },
}
