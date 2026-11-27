# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Interpret recorded inspection evidence without issuing additional queries."""

from dataclasses import dataclass
import math
import re

from modules.core.data_security import assert_readonly_sql, redact


def localized(lang, zh, en):
    return zh if lang.startswith('zh') else en


@dataclass(frozen=True)
class Guidance:
    meaning: tuple
    impact: tuple
    action: tuple
    acceptance: tuple


GUIDANCE = {
    'connection': Guidance(
        ('反映连接占用、会话并发或连接池压力。接近上限时应同时检查活跃和空闲连接。',
         'Shows connection occupancy, concurrency, or pool pressure. Near the limit, compare active and idle sessions.'),
        ('连接池耗尽可能导致登录拒绝、排队和请求超时。',
         'Pool exhaustion can cause rejected logins, backlog and request timeouts.'),
        ('先定位连接来源和空闲连接，检查泄漏与池上限。确有峰值需求时，再结合内存和线程预算调整。',
         'Identify connection owners and idle sessions. Check leaks and pool limits before resizing against memory and worker budgets.'),
        ('业务峰值复测连接占用、连接拒绝次数和请求超时，确认留有余量。',
         'During peak load, verify connection headroom, rejected logins and request timeouts.')),
    'storage': Guidance(
        ('反映数据库空间、文件、日志或所在卷的容量及增长情况。百分比需结合可用字节和增长速度。',
         'Shows storage, file, log or volume capacity. Compare percentages with free bytes and growth rate.'),
        ('可用空间耗尽可能阻断写入、日志归档、备份或扩容。',
         'Exhausted space can block writes, log archiving, backups or growth.'),
        ('核对数据与归档增长、卷余量及扩展上限，按增长预测安排扩容或经核实的归档清理。',
         'Check data and archive growth, volume headroom and growth limits. Schedule capacity or approved retention cleanup.'),
        ('复测容量和增长趋势，确认业务高峰及保留期内有足够余量。',
         'Verify capacity and growth again with peak load and retention in view.')),
    'backup': Guidance(
        ('核对备份、归档、恢复链路与最近可恢复时间。',
         'Validates backups, log archiving, restore chains and the last recoverable point.'),
        ('链路中断可能扩大数据丢失范围并拉长恢复时间。',
         'A broken chain may increase data loss or restore duration.'),
        ('核对备份调度、归档连续性和异地副本。用隔离环境执行恢复演练。',
         'Check the backup schedule, continuity of archived logs and off-site copies. Run a restore drill in isolation.'),
        ('记录最后一次成功备份和恢复演练证据，按业务约定的 RPO、RTO 验收。',
         'Record successful backup and restore-drill evidence. Validate against the agreed RPO and RTO.')),
    'availability': Guidance(
        ('反映副本、集群节点、角色与同步状态。延迟需结合采样时间和部署拓扑。',
         'Shows replica and cluster health, role or synchronization. Interpret lag in the context of time and topology.'),
        ('副本异常或延迟可能带来读到旧数据、冗余降低或切换失败。',
         'Replica issues may cause stale reads, reduced redundancy or failed failover.'),
        ('检查复制传输、回放或应用队列和节点日志，核对主从角色与剩余冗余。',
         'Review replication transport, replay or apply queues and node logs. Confirm roles and available redundancy.'),
        ('复测复制状态和延迟持续时间，在演练中验证业务容灾目标。',
         'Recheck replication and sustained lag, then validate the business failover objectives in a drill.')),
    'locking': Guidance(
        ('反映锁等待、长事务、死锁或阻塞链。锁本身是正常并发机制，持续等待才需要排查。',
         'Shows lock waits, long transactions, deadlocks or blocking chains. Investigate sustained contention.'),
        ('持续阻塞可能让业务请求排队、超时或因死锁回滚。',
         'Sustained blocking can queue requests, time them out or roll back transactions.'),
        ('定位阻塞源和事务边界，检查索引和访问顺序，先缩短持锁时间，再评估会话处置。',
         'Find the blocking session and transaction boundary. Review indexes and access order before any session intervention.'),
        ('对比同类负载下的锁等待、死锁、超时和事务耗时。',
         'Compare lock waits, deadlocks, timeouts and transaction duration at comparable load.')),
    'performance': Guidance(
        ('反映数据库处理效率。累计计数需按采样间隔取差值，平均值应与高分位和执行次数一起看。',
         'Shows processing efficiency. Use deltas for cumulative counters and compare averages with percentiles and frequency.'),
        ('瓶颈持续时可能拉长响应时间，降低高峰处理能力。',
         'A sustained bottleneck can increase response times and reduce peak capacity.'),
        ('按总耗时和调用频率选 SQL，使用参数化样本核对执行计划、读量和估算行数，再评估改写、统计信息及索引。',
         'Rank statements by total latency and frequency. Inspect plans, reads and row estimates before rewriting or changing statistics or indexes.'),
        ('在相近数据量和并发下对比 p95 耗时、逻辑读及写入成本。',
         'Compare p95 latency, logical reads and write cost at similar volume and concurrency.')),
    'resource': Guidance(
        ('反映 CPU、内存、磁盘 I/O 或缓存开销。应结合采样窗口、系统余量和数据库等待。',
         'Shows CPU, memory, disk I/O or cache pressure. Compare the sample window with headroom and database waits.'),
        ('资源长时间饱和可能增加排队、换页、I/O 等待和业务延迟。',
         'Sustained resource saturation can increase queues, paging, I/O waits and latency.'),
        ('区分峰值与持续饱和，确认数据库进程和相关工作负载，再评估限流、调度或扩容。',
         'Separate spikes from sustained saturation. Identify contributing workloads before tuning scheduling, limits or capacity.'),
        ('对比业务峰值的资源曲线、等待类型和请求耗时。',
         'Compare resource curves, wait types and service latency through peak periods.')),
    'security': Guidance(
        ('检查权限、账号、审计和安全参数，以业务授权与合规基线为判定依据。',
         'Checks users, privileges, auditing and security settings against the authorized baseline.'),
        ('配置偏离可能扩大访问范围或降低审计追溯能力。',
         'Configuration drift can widen access or reduce audit traceability.'),
        ('复核角色和最小权限，确认授权来源、影响范围和回退办法后再收敛权限。',
         'Review roles and least privilege. Confirm authority, affected services and rollback before narrowing access.'),
        ('验证业务账号可正常运行、越权操作被拒绝且审计记录完整。',
         'Verify normal business operations, denied unauthorized actions and intact audit records.')),
    'configuration': Guidance(
        ('记录配置、版本或结构对象，用于核对厂商支持范围和实例基线。',
         'Records configuration, version or schema objects for vendor support and instance-baseline checks.'),
        ('参数差异可能影响兼容性、资源分配或运维行为，需结合业务用途。',
         'Differences can affect compatibility, resource allocation or operations depending on workload.'),
        ('按当前产品和版本核对生效值、配置范围和重启要求。先记录原值，再走变更验证。',
         'Verify effective values, scope and restart requirements for the actual product and version. Capture the previous settings.'),
        ('复核变更前后参数、业务路径和回退条件。',
         'Verify before-and-after configuration, business paths and rollback criteria.')),
    'general': Guidance(
        ('这是巡检留存的观测证据，应结合采样范围、单位与同实例基线理解。',
         'This observation is evidence for the inspection. Interpret it with scope, units and the instance baseline.'),
        ('单个观测值有时不足以判断业务风险，需结合应用指标与前后变化。',
         'A single observation may need application signals and a comparison over time to establish impact.'),
        ('核对同实例、同周期的历史，以及相同版本和部署下适用的厂商定义。',
         'Compare the same instance and time window with historical data and the matching vendor definitions.'),
        ('用相同口径重新采样，记录变化及后续判断依据。',
         'Repeat the sample using the same method and record the result and decision criteria.')),
}

_CATEGORY_PATTERNS = [
    ('backup', r'backup|archive|restore|rman|recover|备份|归档|恢复'),
    ('availability', r'repl|cluster|standby|failover|shard|had[rsa]|dataguard|副本|集群|主从|容灾|同步|高可用'),
    ('locking', r'lock|deadlock|block|事务|阻塞|死锁|锁'),
    ('connection', r'connection|session|thread|client|processlist|连接|会话|并发'),
    ('storage', r'tablespace|datafile|storage|capacity|disk_space|volume|表空间|存储|容量|数据文件|磁盘空间'),
    ('performance', r'slow|latency|sql|query|index|execution|qps|tps|wait|慢查询|查询|耗时|性能|吞吐|索引|等待'),
    ('resource', r'cpu|mem|cache|buffer|swap|iops|disk|load|内存|缓存|资源|负载|磁盘'),
    ('security', r'privilege|password|role|audit|security|auth|权限|密码|账号|审计|安全'),
    ('configuration', r'config|setting|param|version|baseline|instance|配置|参数|版本|基线|实例'),
]
_CATEGORY_PATTERNS = [(key, re.compile(pattern, re.I)) for key, pattern in _CATEGORY_PATTERNS]
RULE_CATEGORIES = {'replication': 'availability', 'cluster': 'availability', 'ha': 'availability',
                  'lock': 'locking', 'memory': 'resource', 'config': 'configuration',
                  'log': 'general', 'capacity': 'storage', 'tablespace': 'storage'}
_STATE_LABELS = {
    'critical': ('告警', 'Alert'), 'warning': ('预警', 'Warning'), 'notice': ('关注', 'Attention'),
    'healthy': ('健康', 'Healthy'), 'observe': ('已采集待判定', 'Collected, assessment pending'),
    'missing': ('未采集或证据不足', 'Not collected or insufficient evidence'),
    'unavailable': ('采集失败', 'Collection failed'), 'partial': ('采集不完整', 'Partial collection'),
    'not_applicable': ('不适用', 'Not applicable'), 'excluded': ('本次未执行', 'Not run'),
}


def guidance_for(key, title=''):
    return next((category for category, pattern in _CATEGORY_PATTERNS
                 if pattern.search(f'{key} {title}')), 'general')


def state_label(state, lang):
    return localized(lang, *_STATE_LABELS[state])


def issue_state(issue):
    value = str(issue.get('col2') or issue.get('severity') or issue.get('risk_level') or '').lower()
    if any(word in value for word in ('critical', 'high', '高', '严重', '紧急')):
        return 'critical'
    if any(word in value for word in ('medium', 'mid', 'warn', '中', '预警')):
        return 'warning'
    return 'notice'


def issue_title(issue):
    return issue.get('col1') or issue.get('title') or issue.get('name') or issue.get('description') or ''


def matching_issue_states(issues, key, title):
    names = {str(value).strip().casefold() for value in (key, title) if value}
    return {
        issue_state(issue) for issue in issues if isinstance(issue, dict)
        if any(str(issue.get(field) or '').strip().casefold() in names
               for field in ('query_key', 'metric_key'))
        or str(issue_title(issue)).strip().casefold() in names
    }


def diagnostic_sql(text):
    """Keep report SQL examples read-only, complete and free of unresolved values."""
    sql = str(text or '').strip()
    if not sql or len(sql) > 1200 or re.search(r'\{[^\n]*\}|\[REDACTED\]|\?', sql):
        return ''
    try:
        assert_readonly_sql(sql)
    except ValueError:
        return ''
    return redact(sql, mask_identity=True, preserve_network=True)


# Fixed examples use metadata supported by the named product, not interpolated inspection data.
DIAGNOSTICS = {
    'mysql': ('connection', "SHOW GLOBAL STATUS LIKE 'Threads_connected';"),
    'mariadb': ('connection', "SHOW GLOBAL STATUS LIKE 'Threads_connected';"),
    'gbase8a': ('connection', "SHOW GLOBAL STATUS LIKE 'Threads_connected';"),
    'tidb': ('connection', 'SHOW PROCESSLIST;'),
    'oceanbase': ('connection', 'SHOW PROCESSLIST;'),
    'oracle': ('locking', 'SELECT status, COUNT(*) FROM v$session GROUP BY status;'),
    'dm': ('connection', 'SELECT STATE, COUNT(*) FROM V$SESSIONS GROUP BY STATE;'),
    'sqlserver': ('locking', 'SELECT status, COUNT(*) AS sessions FROM sys.dm_exec_sessions GROUP BY status;'),
    'pg': ('connection', 'SELECT state, COUNT(*) FROM pg_stat_activity GROUP BY state;'),
    'clickhouse': ('performance', 'SELECT count() AS running_queries FROM system.processes;'),
    'db2': ('storage', 'SELECT TBSPACE FROM SYSCAT.TABLESPACES FETCH FIRST 20 ROWS ONLY;'),
}
PRODUCT_REFERENCES = {
    'pg': ('PostgreSQL documentation', 'https://www.postgresql.org/docs/18/using-explain.html'),
    'mysql': ('MySQL Reference Manual', 'https://dev.mysql.com/doc/refman/8.4/en/execution-plan-information.html'),
    'mariadb': ('MariaDB Server documentation', 'https://mariadb.com/kb/en/explain/'),
    'tidb': ('TiDB documentation', 'https://docs.pingcap.com/tidb/stable/explain-overview/'),
    'clickhouse': ('ClickHouse documentation', 'https://clickhouse.com/docs/optimize/query-optimization'),
    'mongodb': ('MongoDB Manual', 'https://www.mongodb.com/docs/manual/reference/explain-results/'),
    'redis': ('Redis documentation', 'https://redis.io/docs/latest/commands/scan/'),
    'redis-cluster': ('Redis documentation', 'https://redis.io/docs/latest/commands/scan/'),
}
COMMAND_DIAGNOSTICS = {
    'redis': ('resource', 'INFO memory'),
    'redis-cluster': ('availability', 'CLUSTER INFO'),
    'mongodb': ('connection', 'db.serverStatus().connections'),
}


def build_rule_index(db_type, context):
    from modules.pro.rule_engine import get_rule_engine, load_thresholds, report_rule_evidence

    thresholds = load_thresholds(instance_id=context.get('_instance_id'))
    rules = get_rule_engine().get_enabled_rules(db_type)
    by_key, by_id = {}, {}
    for rule in rules:
        evidence = report_rule_evidence(rule, context, thresholds)
        if evidence is None:
            continue
        rule_id = str(rule.get('id') or '')
        if rule_id:
            by_id[rule_id] = evidence
        for key in evidence['keys']:
            by_key.setdefault(key, []).append(evidence)
    return by_key, by_id


def _observed(value):
    if value is None or value == '':
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return bool(value) and any(_observed(row) for row in value[:20])
    if isinstance(value, dict):
        return not any(str(key).lower() in {'error', '_error', '_unavailable', 'placeholder'}
                       for key in value) and any(_observed(item) for item in value.values())
    return not (isinstance(value, str) and value.strip().lower() in {
        'n/a', 'unknown', '[redacted]', '[masked]', '--', '未知', '未采集', '不适用',
    })


def metric_state(value, state, rules, finding_states=()):
    status = state.get('status')
    if status == 'not_applicable':
        return 'not_applicable'
    if status in ('error', 'timeout', 'failed', 'unavailable'):
        return 'unavailable'
    if status in ('disabled', 'skipped', 'excluded'):
        return 'excluded'
    triggered = {row['state'] for row in rules if row.get('triggered')} | set(finding_states)
    for risk in ('critical', 'warning', 'notice'):
        if risk in triggered:
            return risk
    if state.get('truncated') or status in ('partial', 'incomplete'):
        return 'partial'
    if rules and all(row.get('assessed') for row in rules):
        return 'healthy'
    return 'observe' if _observed(value) or status == 'collected' else 'missing'
