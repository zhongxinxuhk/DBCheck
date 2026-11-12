#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""§4.6 关键指标规则的功能性验证（问题 4 的闭合判据）。

背景
----
问题 4 的现象是「阈值已入库、Web UI 上能改，但没有任何规则引用它」——改阈值
不产生任何行为变化。补完取数键与规则后，``verify_baseline_coverage.py`` 能做
静态校验（阈值键存在、且有规则消费）。

但静态校验只能证明「引用了」，不能证明「引用得对」。以下错误静态校验一律看不
出来，却会让规则**永不触发**或**恒触发**：

  - condition 拼写错、变量名与 params 不一致；
  - 取数路径写错（``pg_deadlocks[0].deadlocks`` / ``slow_query_count[0].Value``
    / ``replication_lag[0].Seconds_Behind_Master``），列名大小写不符；
  - ``len()`` 用在非 list 上，或空集被当成「有数据」；
  - ``ISNULL(..., 0)`` 让「没采到」退化成 0 值，触发误报。

本脚本因此对每条新规则构造「应触发 / 不应触发」两组合成 context，直接调用
``RuleEngine._run_rule`` 断言结论。合成 context 的形状严格照抄真实采集结构
（对应 ``modules/pro/rules/metrics/*.yaml`` 的 ``shape`` 字段），因此覆盖了
取数路径本身。

阈值不写死
----------
用 ``load_thresholds()`` 取全局基线，再按「阈值 × 0.8 / × 1.2」构造越界与未
越界样本。用户改了阈值本脚本仍然成立——这本身也是对「阈值真的在参与判定」的
验证（末段还有一条显式的阈值替换断言）。

用法::

    python3 scripts/verify_s46_rules_e2e.py [--verbose]
"""


import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from modules.pro.rule_engine import get_rule_engine, load_thresholds  # noqa: E402

CASES = []


def case(rule_id, db_type, th_key, bad, good, expect='trigger', note=''):
    """登记一条断言。

    :param bad:  越界样本 —— 生成 context 补丁的函数，入参为阈值 t
    :param good: 未越界样本
    :param expect: ``trigger`` = bad 触发且 good 不触发；
                   ``never``   = 两者都不触发（用于验证存在性守卫）
    """
    CASES.append(dict(rule_id=rule_id, db_type=db_type, th_key=th_key,
                      bad=bad, good=good, expect=expect, note=note))


# ── SQL Server ────────────────────────────────────────────────
case('sqlserver_ple_low', 'sqlserver', 'db_mssql_ple_warn_min_s',
     bad=lambda t: {'mssql_ple_s': t * 0.8},
     good=lambda t: {'mssql_ple_s': t * 1.2},
     note='PLE 低于阈值告警')

# 存在性守卫：DMV 取不到计数器时 ISNULL 返回 0，规则必须把它判成「未采集」，
# 否则每次取数失败都会报一条「PLE 为 0」的假告警。
case('sqlserver_ple_low', 'sqlserver', 'db_mssql_ple_warn_min_s',
     bad=lambda t: {'mssql_ple_s': 0},
     good=lambda t: {'mssql_ple_s': 0},
     expect='never',
     note='PLE=0 视为未采集，不告警')

case('sqlserver_lazy_writes_high', 'sqlserver', 'db_mssql_lazy_writes_warn_per_s',
     bad=lambda t: {'mssql_lazy_writes_per_s': t * 1.2},
     good=lambda t: {'mssql_lazy_writes_per_s': t * 0.8},
     note='Lazy writes 超阈值告警')

case('sqlserver_backup_coverage_low', 'sqlserver',
     'db_backup_success_required_pct',
     bad=lambda t: {'backup_success_pct': t * 0.8},
     good=lambda t: {'backup_success_pct': t * 1.2},
     note='备份覆盖率不足告警')

case('sqlserver_cpu_avg_high', 'sqlserver', 'db_cpu_avg_warn_pct',
     bad=lambda t: {'cpu_avg_pct': t * 1.2},
     good=lambda t: {'cpu_avg_pct': t * 0.8},
     note='CPU 平均利用率超阈值告警')

# ── Oracle ────────────────────────────────────────────────────
case('oracle_cpu_avg_high', 'oracle', 'db_cpu_avg_warn_pct',
     bad=lambda t: {'cpu_avg_pct': t * 1.2},
     good=lambda t: {'cpu_avg_pct': t * 0.8})

case('oracle_library_cache_low', 'oracle', 'db_oracle_lib_cache_warn_min_pct',
     bad=lambda t: {'library_cache_hit_ratio': t * 0.8},
     good=lambda t: {'library_cache_hit_ratio': t * 1.2},
     note='Library Cache 命中率低于阈值告警')

case('oracle_pga_hit_low', 'oracle', 'db_oracle_pga_warn_min_pct',
     bad=lambda t: {'pga_cache_hit_pct': t * 0.8},
     good=lambda t: {'pga_cache_hit_pct': t * 1.2},
     note='PGA 命中率低于阈值告警（本轮新增取数键）')

case('oracle_disk_await_high', 'oracle', 'db_disk_await_warn_ms',
     bad=lambda t: {'db_disk_await_ms': t * 1.5},
     good=lambda t: {'db_disk_await_ms': t * 0.5},
     note='磁盘 await 超阈值告警（本轮新增取数键）')

# 归档断档：规则用 len(rows)，空集必须不触发
case('oracle_archive_gap_found', 'oracle', 'db_archive_gap_crit_count',
     bad=lambda t: {'archive_gap': [{'GAP_HOURS': 999}] * max(1, int(t))},
     good=lambda t: {'archive_gap': []},
     note='断档条数达阈值告警；空集不告警')

case('oracle_hrisk_account_found', 'oracle',
     'db_hrisk_account_new_unreported_alert',
     bad=lambda t: {'privileged_users': [{'USERNAME': 'SYS'}] * max(1, int(t))},
     good=lambda t: {'privileged_users': []},
     note='特权账号数达阈值告警；空集不告警')

# ── DM8 ───────────────────────────────────────────────────────
case('dm_cpu_avg_high', 'dm', 'db_cpu_avg_warn_pct',
     bad=lambda t: {'cpu_avg_pct': t * 1.2},
     good=lambda t: {'cpu_avg_pct': t * 0.8})

case('dm_disk_await_high', 'dm', 'db_disk_await_warn_ms',
     bad=lambda t: {'db_disk_await_ms': t * 1.5},
     good=lambda t: {'db_disk_await_ms': t * 0.5},
     note='DM8 V$FILESTAT 同形，本轮新增取数键')

# ── PostgreSQL 系 ─────────────────────────────────────────────
case('pg_cpu_avg_high', 'pg', 'db_cpu_avg_warn_pct',
     bad=lambda t: {'cpu_avg_pct': t * 1.2},
     good=lambda t: {'cpu_avg_pct': t * 0.8})

# 死锁：rows 形状，规则读 pg_deadlocks[0].deadlocks
case('pg_deadlock_over_threshold', 'pg', 'db_deadlock_warn_multiplier',
     bad=lambda t: {'pg_deadlocks': [{'datname': 'app',
                                      'deadlocks': int(t) + 1}]},
     good=lambda t: {'pg_deadlocks': [{'datname': 'app', 'deadlocks': 0}]},
     note='死锁计数达倍数阈值告警；0 不告警')

# ── MySQL 系 ──────────────────────────────────────────────────
case('mysql_cpu_avg_high', 'mysql', 'db_cpu_avg_warn_pct',
     bad=lambda t: {'cpu_avg_pct': t * 1.2},
     good=lambda t: {'cpu_avg_pct': t * 0.8})

# 慢 SQL：列名是大写 Value（MySQL 8 SHOW GLOBAL STATUS 保留原样），
# 写成小写 value 会取不到数而静默跳过 —— 这条用例就是在钉住列名大小写。
case('mysql_slow_sql_over_threshold', 'mysql', 'db_slow_sql_warn_per_week',
     bad=lambda t: {'slow_query_count': [{'Variable_name': 'Slow_queries',
                                          'Value': str(int(t) + 1)}]},
     good=lambda t: {'slow_query_count': [{'Variable_name': 'Slow_queries',
                                           'Value': '0'}]},
     note='慢 SQL 超阈值告警；列名大小写须与采集一致')

# 主从延迟：列名 Seconds_Behind_Master
case('mysql_repl_lag_over_threshold', 'mysql', 'db_cluster_lag_warn_s',
     bad=lambda t: {'replication_lag': [{'Seconds_Behind_Master': int(t) + 5}]},
     good=lambda t: {'replication_lag': [{'Seconds_Behind_Master': 0}]},
     note='集群延迟超阈值告警')

# ── rows 取数空集守卫 ─────────────────────────────────────────
# 这是最常见的真实场景，也最容易写成恒崩溃或恒误报：
#   非从库实例上 `SHOW SLAVE STATUS` 返回 0 行 → replication_lag 为 []；
#   `[0].Seconds_Behind_Master` 索引越界，_safe_eval_param 兜成 None，
#   规则靠 `is not None` 拦住。三条用例把这条链路钉死。
case('mysql_repl_lag_over_threshold', 'mysql', 'db_cluster_lag_warn_s',
     bad=lambda t: {'replication_lag': []},
     good=lambda t: {'replication_lag': []},
     expect='never',
     note='非从库实例返回空集，不告警也不崩')

case('mysql_slow_sql_over_threshold', 'mysql', 'db_slow_sql_warn_per_week',
     bad=lambda t: {'slow_query_count': []},
     good=lambda t: {'slow_query_count': []},
     expect='never',
     note='慢 SQL 查询失败返回空集，不告警')

case('pg_deadlock_over_threshold', 'pg', 'db_deadlock_warn_multiplier',
     bad=lambda t: {'pg_deadlocks': []},
     good=lambda t: {'pg_deadlocks': []},
     expect='never',
     note='pg_stat_database 取数为空，不告警')


class Res(object):
    def __init__(self, name, status, detail=''):
        self.name, self.status, self.detail = name, status, detail


def _lookup(engine, rule_id, db_type):
    """按 id 取规则，并校验它在目标库型上确实会执行。

    只取到 id 不够：``db_types`` 漏写产品时规则会被 ``get_enabled_rules``
    过滤掉，静态检查发现不了（文件里明明有这条规则）。
    """
    rule = engine.get_rule(rule_id)
    if not rule:
        return None, '规则未加载（id 拼错或 YAML 解析失败）'
    types = [str(x).strip().lower() for x in (rule.get('db_types') or [])]
    if db_type not in types:
        return None, 'db_types %s 不含 %s' % (types, db_type)
    if rule.get('enabled') is False:
        return None, '规则已停用'
    return rule, ''


def run(verbose=False):
    engine = get_rule_engine()
    th = load_thresholds()
    results = []

    for c in CASES:
        rid, dt, tk = c['rule_id'], c['db_type'], c['th_key']
        label = '%s [%s]%s' % (rid, dt, '' if c['expect'] == 'trigger' else ' 守卫')

        rule, err = _lookup(engine, rid, dt)
        if rule is None:
            results.append(Res(label, 'FAIL', err))
            continue

        t = th.get(tk)
        if t is None:
            results.append(Res(label, 'FAIL', '阈值 %s 不在 server_thresholds' % tk))
            continue

        contexts = []
        for factory in (c['bad'], c['good']):
            ctx = dict(factory(t))
            ctx['_lang'] = 'zh'
            contexts.append(ctx)

        try:
            hit_bad = bool(engine._run_rule(dict(rule), dt, contexts[0]))
            hit_good = bool(engine._run_rule(dict(rule), dt, contexts[1]))
        except Exception as e:  # 求值异常通常就是 condition 写错
            results.append(Res(label, 'FAIL', '求值异常: %r' % (e,)))
            continue

        if c['expect'] == 'never':
            ok = not hit_bad and not hit_good
            detail = '越界样本触发=%s 未越界样本触发=%s（均应为 False）' % (
                hit_bad, hit_good)
        else:
            ok = hit_bad and not hit_good
            detail = '越界触发=%s 未越界触发=%s（应 True/False）' % (
                hit_bad, hit_good)

        if verbose:
            detail += '  阈值 %s=%s' % (tk, t)
        results.append(Res(label, 'PASS' if ok else 'FAIL',
                           (c['note'] + '。' + detail).strip('。')))

    # 阈值真的参与判定：把 condition 里的阈值变量替换成一个「必然不可越过」的
    # 极端值后，同一个越界样本必须不再触发。
    #
    # 方向很关键：`metric > tk`（下限）要把 tk 抬到 +1e9；`metric < tk`（上限，
    # 如 PLE）必须把 tk 压到 -1e9 —— 上限类阈值抬大会让条件**更容易**成立，
    # 一度让本脚本把「规则正确」误报成「阈值没生效」。
    import re as _re
    for c in CASES:
        if c['expect'] != 'trigger':
            continue
        rid, dt, tk = c['rule_id'], c['db_type'], c['th_key']
        rule, _ = _lookup(engine, rid, dt)
        t = th.get(tk)
        if rule is None or t is None:
            continue
        cond = rule.get('condition') or ''
        m = _re.search(r'([<>]=?)\s*\(?\s*' + _re.escape(tk) + r'\b', cond)
        if not m:
            continue
        extreme = '-1e9' if m.group(1).startswith('<') else '1e9'
        probe = dict(c['bad'](t))
        probe['_lang'] = 'zh'
        patched = dict(rule)
        patched['condition'] = cond.replace(tk, '(%s)' % extreme)
        try:
            hit = bool(engine._run_rule(patched, dt, probe))
        except Exception as e:
            results.append(Res('%s 阈值生效性' % rid, 'FAIL', '求值异常: %r' % (e,)))
            continue
        results.append(Res('%s 阈值生效性' % rid,
                           'PASS' if not hit else 'FAIL',
                           '%s %s 换成 %s 后越界样本不再触发=%s'
                           % (m.group(1), tk, extreme, not hit)))

    return results


def main():
    verbose = '--verbose' in sys.argv or '-v' in sys.argv
    results = run(verbose)
    npass = sum(1 for r in results if r.status == 'PASS')
    nfail = sum(1 for r in results if r.status == 'FAIL')
    for r in results:
        print('%-4s %-40s %s' % (r.status, r.name, r.detail))
    print('\n合计 PASS %d / FAIL %d' % (npass, nfail))
    return 0 if nfail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
