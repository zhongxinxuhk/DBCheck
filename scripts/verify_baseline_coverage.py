# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck
基线配置覆盖率与阈值引用校验
============================

两类校验：

1. **参数基线覆盖** —— 业务在保四产品（GBase 8a MPP / DM8 / Oracle /
   MSSQL）各自是否达到最低项数，且八维度（内存/并发/IO/日志/超时/安全/
   高可用/运维）无整体缺口。

2. **阈值引用完整性** —— 规则 condition 里引用的每个 ``db_*`` /
   ``*_pct`` 阈值键，必须在 ``server_thresholds`` 中真实存在。
   引用不存在的键会让 condition 抛 NameError，规则被静默跳过——
   比硬编码数字更糟，必须拦住。

用法
----
    python scripts/verify_baseline_coverage.py
    python scripts/verify_baseline_coverage.py --db data/inspection.db
"""

import argparse
import ast
import glob
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import yaml  # noqa: E402

# 业务在保四产品的最低基线项数要求（按 §4.2 参数视图 + D-03 字段要求推定）
MIN_ITEMS = {
    'gbase8a': 17,
    'dm8': 20,
    'oracle': 17,
    'sqlserver': 20,
}

# 维度层已知例外：该产品在该维度上不存在「系统参数」这一载体，
# 事实由其它手段采集——不是漏配，故不算缺口。
DIM_EXCEPTIONS = {
    # GBase 8a MPP 的备份与集群高可用由 gcadmin / gcrcman 命令行与集群状态
    # 采集项覆盖，客户端系统变量层没有归档模式、备份策略这类参数。
    'gbase8a': {'高可用': '由 gcadmin 集群状态 + gcrcman 备份检查覆盖，参数层不适用'},
}

# 条件表达式里可用的安全内置函数（与 rule_engine._safe_eval 保持一致）
SAFE_BUILTINS = {
    'min', 'max', 'len', 'int', 'float', 'str', 'abs', 'round',
    'any', 'all', 'sum', '_int', 'None', 'True', 'False',
}

PASS, FAIL, WARN = 'PASS', 'FAIL', 'WARN'


def check_param_baselines(db_path):
    """校验四产品参数基线项数与维度覆盖。"""
    results = []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT db_type, COUNT(*) FROM inspection_baseline GROUP BY db_type")
        counts = dict(cur.fetchall())

        for db_type, minimum in MIN_ITEMS.items():
            n = counts.get(db_type, 0)
            if n == 0:
                results.append((FAIL, db_type, f'基线为空（要求 >= {minimum} 项）'))
            elif n < minimum:
                results.append((WARN, db_type, f'仅 {n} 项（建议 >= {minimum}）'))
            else:
                results.append((PASS, db_type, f'{n} 项'))

        # 维度覆盖：按描述关键词粗略判定
        dims = {
            '内存': ('内存', 'buffer', 'cache', 'pool', 'sga', 'pga', 'heap'),
            '并发': ('连接', '会话', '游标', '并行', '线程', 'worker', 'session', 'connection'),
            'IO': ('io', 'awaits', 'slaves', 'io_thr', 'asynch'),
            '日志': ('log', 'rlog', 'redo', '归档', 'archive', 'wal'),
            '超时': ('超时', 'timeout', 'expire'),
            '安全': ('审计', '口令', '密码', 'audit', 'pwd', 'password', 'xp_cmdshell', 'case_sensitive'),
            '高可用': ('归档', 'archiv', '备份', 'backup', 'force_logging', 'checksum'),
            '运维': ('保留', 'retention', 'trace', '统计', '作业', 'job'),
        }
        for db_type in MIN_ITEMS:
            cur.execute(
                "SELECT param_name, description_zh, description_en "
                "FROM inspection_baseline WHERE db_type = ?", (db_type,))
            blob = ' '.join(
                f"{r[0]} {r[1] or ''} {r[2] or ''}".lower() for r in cur.fetchall())
            missing = [d for d, kws in dims.items()
                       if not any(k.lower() in blob for k in kws)]
            excused = DIM_EXCEPTIONS.get(db_type, {})
            real_missing = [d for d in missing if d not in excused]
            if real_missing:
                results.append((WARN, db_type, '维度缺口: ' + '/'.join(real_missing)))
            elif missing:
                notes = '; '.join(f'{d}={excused[d]}' for d in missing)
                results.append((PASS, db_type, f'八维度齐备（例外：{notes}）'))
            else:
                results.append((PASS, db_type, '八维度齐备'))
    finally:
        conn.close()
    return results


def check_threshold_entries(db_path):
    """校验 §4.6 阈值条目是否存在且数值合理。"""
    results = []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM server_thresholds")
        th = dict(cur.fetchall())

        required = [
            'db_fs_usage_warn_pct', 'db_fs_usage_alert_pct', 'db_fs_usage_crit_pct',
            'db_tbs_usage_warn_pct', 'db_tbs_usage_alert_pct', 'db_tbs_usage_crit_pct',
            'db_session_usage_warn_pct', 'db_session_usage_alert_pct', 'db_session_usage_crit_pct',
            'db_oracle_buf_cache_warn_min_pct', 'db_oracle_buf_cache_alert_min_pct',
            'db_oracle_buf_cache_crit_min_pct',
            'db_mssql_ple_warn_min_s', 'db_mssql_ple_alert_min_s', 'db_mssql_ple_crit_min_s',
            'db_cpu_avg_warn_pct', 'db_cpu_avg_alert_pct', 'db_cpu_avg_crit_pct',
            'db_disk_await_warn_ms', 'db_disk_await_alert_ms', 'db_disk_await_crit_ms',
            'db_index_frag_warn_pct', 'db_index_frag_alert_pct',
            'db_stats_stale_warn_days', 'db_stats_stale_alert_days',
            'db_backup_success_required_pct', 'db_archive_gap_crit_count',
            'db_cluster_lag_warn_s', 'db_cluster_lag_alert_s', 'db_cluster_lag_crit_s',
            'db_self_baseline_window_days', 'db_self_baseline_p95_multiplier',
        ]
        missing = [k for k in required if k not in th]
        if missing:
            results.append((FAIL, 'server_thresholds', f'缺失 {len(missing)} 项: {missing[:5]}'))
        else:
            results.append((PASS, 'server_thresholds',
                            f'§4.6 关键阈值 {len(required)} 项齐备（表内共 {len(th)} 项）'))

        # 三级递增方向校验（占用率越高越差 → warn < alert <= crit）
        triples = [
            ('文件系统', 'db_fs_usage_warn_pct', 'db_fs_usage_alert_pct', 'db_fs_usage_crit_pct'),
            ('表空间', 'db_tbs_usage_warn_pct', 'db_tbs_usage_alert_pct', 'db_tbs_usage_crit_pct'),
            ('会话', 'db_session_usage_warn_pct', 'db_session_usage_alert_pct', 'db_session_usage_crit_pct'),
            ('CPU', 'db_cpu_avg_warn_pct', 'db_cpu_avg_alert_pct', 'db_cpu_avg_crit_pct'),
            ('磁盘await', 'db_disk_await_warn_ms', 'db_disk_await_alert_ms', 'db_disk_await_crit_ms'),
            ('集群延迟', 'db_cluster_lag_warn_s', 'db_cluster_lag_alert_s', 'db_cluster_lag_crit_s'),
        ]
        for name, w, a, c in triples:
            if all(k in th for k in (w, a, c)):
                if not (th[w] < th[a] <= th[c]):
                    results.append((FAIL, name, f'三级阈值方向错误 {th[w]}/{th[a]}/{th[c]}'))
                else:
                    results.append((PASS, name, f'{th[w]} < {th[a]} <= {th[c]}'))

        # 反向：命中率越低越差，warn 应 > alert > crit
        rev = [
            ('Oracle命中率', 'db_oracle_buf_cache_warn_min_pct',
             'db_oracle_buf_cache_alert_min_pct', 'db_oracle_buf_cache_crit_min_pct'),
            ('MSSQL PLE', 'db_mssql_ple_warn_min_s',
             'db_mssql_ple_alert_min_s', 'db_mssql_ple_crit_min_s'),
        ]
        for name, w, a, c in rev:
            if all(k in th for k in (w, a, c)):
                if not (th[w] > th[a] > th[c]):
                    results.append((FAIL, name, f'三级阈值方向错误 {th[w]}/{th[a]}/{th[c]}'))
                else:
                    results.append((PASS, name, f'{th[w]} > {th[a]} > {th[c]}'))
    finally:
        conn.close()
    return results


def _condition_names(expr):
    """提取 condition 中的自由变量名（排除函数名与属性访问）。"""
    names = set()
    try:
        tree = ast.parse(expr, mode='eval')
    except SyntaxError:
        return None
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in called:
                names.add(node.id)
    return names


def _scan_rules():
    """遍历全部内置规则，产出 (文件名, 规则 dict)。

    「文件名」用于告警定位；规则 dict 原样返回，由各校验函数自行判定。
    YAML 解析失败的条目以 (文件名, None) 产出，避免静默丢掉整个文件。
    """
    for path in sorted(glob.glob(os.path.join(
            PROJECT_ROOT, 'modules', 'pro', 'rules', 'builtin', '*.yaml'))):
        base = os.path.basename(path)
        try:
            data = yaml.safe_load(open(path, encoding='utf-8')) or {}
        except Exception as e:
            yield base, {'__error__': f'YAML 解析失败: {e}'}
            continue
        for rule in (data.get('rules') or []):
            yield base, rule


def _rule_threshold_refs(rule):
    """返回规则 condition 中「引用阈值」的变量名集合。

    判定口径：condition 里出现、但没在 params 里绑定、也不是安全白名单函数的
    自由变量，且以 ``db_`` 开头（或属于历史遗留的 mem_* 阈值）。
    正常的 context 取值都已写进 params，因此不会误判。
    """
    cond = rule.get('condition') or ''
    if not cond:
        return set()
    names = _condition_names(cond)
    if names is None:
        return None
    bound = set((rule.get('params') or {}).keys())
    free = names - bound - SAFE_BUILTINS
    return {n for n in free if n.startswith('db_') or n in (
        'mem_warning_pct', 'mem_critical_pct')}


def check_rule_threshold_refs(db_path):
    """校验规则引用的阈值键全部存在、且 condition 全部可解析。"""
    from modules.pro.rule_engine import load_thresholds  # noqa: F401

    results = []
    thresholds = set(load_thresholds(db_path)) | {
        'mem_warning_pct', 'mem_critical_pct'}
    bad_syntax = []
    missing = []
    ref_count = 0

    for base, rule in _scan_rules():
        if rule.get('__error__'):
            bad_syntax.append((base, rule['__error__']))
            continue
        refs = _rule_threshold_refs(rule)
        if refs is None:
            bad_syntax.append((base + '::' + str(rule.get('id')),
                               'condition 语法错误'))
            continue
        ref_count += len(refs)
        for n in sorted(refs):
            if n not in thresholds:
                missing.append((base + '::' + str(rule.get('id')), n))

    if bad_syntax:
        for f, msg in bad_syntax[:5]:
            results.append((FAIL, f, msg))
    else:
        results.append((PASS, 'condition 语法', '全部规则 condition 可解析'))

    if missing:
        for f, n in missing[:8]:
            results.append((FAIL, f, f'引用未定义阈值 {n}'))
    else:
        results.append((PASS, '阈值引用', f'{ref_count} 处阈值引用全部命中'))

    return results


# §4.6 中「必须被至少一条规则消费」的阈值键。
#
# 为什么单独校验：阈值入库、Web UI 上能改、却没有任何规则引用 —— 改阈值不会有
# 任何行为变化，是典型的「配置空转」。正向校验（引用的键必须存在）抓不到这种
# 反向缺口，所以单列一张清单，新增阈值键时同步登记并补消费规则。
CONSUMED_REQUIRED = {
    'db_mssql_ple_warn_min_s': 'MSSQL 页生命周期(PLE)',
    'db_mssql_lazy_writes_warn_per_s': 'MSSQL 惰性写入(Lazy writes)',
    'db_oracle_lib_cache_warn_min_pct': 'Oracle Library Cache 命中率',
    'db_oracle_pga_warn_min_pct': 'Oracle PGA 命中率',
    'db_cpu_avg_warn_pct': 'CPU 平均利用率',
    'db_disk_await_warn_ms': '磁盘平均 await',
    'db_deadlock_warn_multiplier': '死锁',
    'db_slow_sql_warn_per_week': '慢 SQL',
    'db_backup_success_required_pct': '备份成功率',
    'db_archive_gap_crit_count': '归档断档',
    'db_cluster_lag_warn_s': '集群延迟',
    'db_hrisk_account_new_unreported_alert': '高危账号新增未上报',
}


def check_threshold_consumers(db_path):
    """反向校验：§4.6 关键阈值是否真的被规则消费（否则改阈值等于空转）。

    注意与 ``check_rule_threshold_refs`` 的方向相反：
      正向 —— 规则引用的键必须在 server_thresholds 里（防拼错键名）；
      反向 —— 清单里的键必须被至少一条规则引用（防空转）。
    """
    consumers = {k: [] for k in CONSUMED_REQUIRED}
    for base, rule in _scan_rules():
        if rule.get('__error__') or rule.get('enabled') is False:
            continue
        refs = _rule_threshold_refs(rule) or set()
        for n in refs:
            if n in consumers:
                consumers[n].append('%s::%s' % (base, rule.get('id')))

    orphans = [k for k, v in consumers.items() if not v]
    results = []
    if orphans:
        for k in orphans:
            results.append((FAIL, CONSUMED_REQUIRED[k],
                            f'{k} 无任何规则引用（阈值空转）'))
        results.append((FAIL, '阈值消费',
                        f'{len(orphans)}/{len(CONSUMED_REQUIRED)} 个 §4.6 阈值'
                        f'未被任何规则消费'))
    else:
        results.append((PASS, '阈值消费',
                        f'§4.6 关键阈值 {len(CONSUMED_REQUIRED)} 项全部有规则消费'))
    return results


def main():
    ap = argparse.ArgumentParser(description='基线配置覆盖率与阈值引用校验')
    ap.add_argument('--db', default=os.path.join(PROJECT_ROOT, 'data', 'inspection.db'))
    args = ap.parse_args()

    print(f"\n{'=' * 74}")
    print("  基线配置覆盖率与阈值引用校验")
    print(f"{'=' * 74}")

    all_results = []
    all_results += check_param_baselines(args.db)
    all_results += check_threshold_entries(args.db)
    all_results += check_rule_threshold_refs(args.db)
    all_results += check_threshold_consumers(args.db)

    icon = {PASS: '[PASS]', FAIL: '[FAIL]', WARN: '[WARN]'}
    for level, name, msg in all_results:
        print(f"  {icon[level]:7s} {name:22s} {msg}")

    fails = sum(1 for r in all_results if r[0] == FAIL)
    warns = sum(1 for r in all_results if r[0] == WARN)
    print(f"\n  合计 {len(all_results)} 项："
          f"PASS {len(all_results) - fails - warns} / WARN {warns} / FAIL {fails}")
    print(f"{'=' * 74}\n")
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
