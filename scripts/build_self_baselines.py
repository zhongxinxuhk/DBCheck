# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
为每个实例建立「自身基线」，并回写实例级阈值。

为什么需要
----------
需求清单 §4.6 脚注要求「首月为每个实例建立自身基线」，公式是：

    实例阈值 = max(通用阈值, 自身 P95 × db_self_baseline_p95_multiplier)

只用一套全局阈值套全部实例，会出现两类误报：
- 小实例（内存/CPU 小）长期贴着 70% 跑，天天报 CPU 高；
- 大实例正常波动就超阈值，告警被淹没后没人再看。

此前 ``db_self_baseline_p95_multiplier`` 只是表里一个空参数，没有计算实现。

数据来源
--------
``data/pro_metrics.db`` 的 ``metrics_snapshot(instance_id, ts, payload)``：
监控采集的逐点快照，payload 为 JSON，含 host_cpu / host_mem / host_swap_pct /
host_disk_read_ms / host_disk_read_count / host_disk_write_ms /
host_disk_write_count 等数值字段。

计算口径
--------
1. 按 ``--window-days``（默认取阈值 ``db_self_baseline_window_days``，30 天）
   取窗口内的快照；
2. 对每个映射字段求 **P95**（nearest-rank，非插值）；
3. 磁盘 await 由 ``(read_ms + write_ms) / (read_count + write_count)`` 逐点算出后取 P95；
4. ``实例阈值 = round(max(全局阈值, P95 × multiplier), 2)``；
5. 样本数低于 ``--min-samples``（默认 20）的字段跳过，避免用三五个点定基线；
6. 结果写入 ``inspection.db`` 的 ``instance_thresholds`` 表，重复运行按
   ``(instance_id, key)`` 覆盖（幂等）。

规则引擎消费方式：``rule_engine.load_thresholds(instance_id=...)`` 会把实例级
阈值叠加在全局阈值之上；调用方通过 ``context['_instance_id']`` 传入实例标识。

用法：
    python scripts/build_self_baselines.py --dry-run --report
    python scripts/build_self_baselines.py --report
    python scripts/build_self_baselines.py --window-days 30 --purge
"""


import argparse
import json
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DEFAULT_DB = os.path.join(PROJECT_ROOT, 'data', 'inspection.db')
DEFAULT_METRICS_DB = os.path.join(PROJECT_ROOT, 'data', 'pro_metrics.db')

# 快照字段 → 需要按实例调整的阈值键。
# 只收录「该字段的 P95 能直接代表阈值语义」的映射，不做无依据的自动套用。
FIELD_TO_THRESHOLDS = {
    'host_cpu': ['db_cpu_avg_warn_pct', 'cpu_warning_pct'],
    'host_mem': ['mem_warning_pct'],
    'host_swap_pct': ['swap_warning_pct'],
}

# 需要先由多个字段算出派生值、再取 P95 的映射
DERIVED_TO_THRESHOLDS = {
    'disk_await_ms': ['db_disk_await_warn_ms'],
}


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:      # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def p95(values):
    """nearest-rank P95（不插值，符合运维修约习惯）。"""
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    k = max(1, int(round(0.95 * len(vs) + 0.5)))
    k = min(k, len(vs))
    return vs[k - 1]


def _disk_await(payload):
    """磁盘平均 await（毫秒/次 IO）= (读耗时+写耗时) / (读次数+写次数)。"""
    r_ms = _num(payload.get('host_disk_read_ms'))
    w_ms = _num(payload.get('host_disk_write_ms'))
    r_cnt = _num(payload.get('host_disk_read_count'))
    w_cnt = _num(payload.get('host_disk_write_count'))
    if None in (r_ms, w_ms, r_cnt, w_cnt):
        return None
    total = r_cnt + w_cnt
    if total <= 0:
        return None
    return (r_ms + w_ms) / total


def load_snapshots(metrics_db, window_days):
    """读取窗口内所有快照，返回 {instance_id: [payload_dict, ...]}。"""
    if not os.path.isfile(metrics_db):
        raise RuntimeError('监控快照库不存在：%s' % metrics_db)
    conn = sqlite3.connect(metrics_db)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT instance_id, payload FROM metrics_snapshot "
            "WHERE ts >= datetime('now', ?) ORDER BY instance_id, ts",
            ('-%d days' % int(window_days),))
        out = {}
        for inst, payload in cur.fetchall():
            try:
                d = json.loads(payload) if isinstance(payload, str) else (payload or {})
            except Exception:
                continue
            if isinstance(d, dict):
                out.setdefault(str(inst), []).append(d)
        return out
    finally:
        conn.close()


def load_globals(db_path):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM server_thresholds")
        return {k: _num(v) for k, v in cur.fetchall()}
    finally:
        conn.close()


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instance_thresholds (
            instance_id  VARCHAR(64) NOT NULL,
            key          VARCHAR(100) NOT NULL,
            value        REAL NOT NULL,
            base_value   REAL,
            self_p95     REAL,
            multiplier   REAL,
            sample_count INTEGER,
            window_days  INTEGER,
            computed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (instance_id, key)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inst_thr_key "
                 "ON instance_thresholds(key)")


def compute(snapshots, globals_, multiplier, min_samples):
    """计算 {instance_id: [(key, value, base, p95, n), ...]}。"""
    result = {}
    for inst, payloads in snapshots.items():
        per_inst = []

        # 直接字段
        for field, keys in FIELD_TO_THRESHOLDS.items():
            vals = [_num(p.get(field)) for p in payloads]
            vals = [v for v in vals if v is not None]
            if len(vals) < min_samples:
                continue
            p = p95(vals)
            if p is None:
                continue
            for k in keys:
                base = globals_.get(k)
                if base is None:
                    continue
                per_inst.append((k, round(max(base, p * multiplier), 2),
                                 base, round(p, 2), len(vals)))

        # 派生字段
        for dname, keys in DERIVED_TO_THRESHOLDS.items():
            if dname == 'disk_await_ms':
                vals = [_disk_await(p) for p in payloads]
                vals = [v for v in vals if v is not None]
                if len(vals) < min_samples:
                    continue
                p = p95(vals)
                if p is None:
                    continue
                for k in keys:
                    base = globals_.get(k)
                    if base is None:
                        continue
                    per_inst.append((k, round(max(base, p * multiplier), 2),
                                     base, round(p, 2), len(vals)))

        if per_inst:
            result[inst] = per_inst
    return result


def apply(db_path, computed, multiplier, window_days, dry_run=False, purge=False):
    conn = sqlite3.connect(db_path)
    try:
        ensure_table(conn)
        cur = conn.cursor()
        if purge:
            cur.execute("DELETE FROM instance_thresholds")
        n = 0
        for inst, rows in computed.items():
            for (key, value, base, p, samples) in rows:
                cur.execute(
                    """INSERT INTO instance_thresholds
                       (instance_id, key, value, base_value, self_p95,
                        multiplier, sample_count, window_days, computed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(instance_id, key) DO UPDATE SET
                         value = excluded.value,
                         base_value = excluded.base_value,
                         self_p95 = excluded.self_p95,
                         multiplier = excluded.multiplier,
                         sample_count = excluded.sample_count,
                         window_days = excluded.window_days,
                         computed_at = CURRENT_TIMESTAMP""",
                    (inst, key, value, base, p, multiplier, samples, window_days))
                n += 1
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return n
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description='建立实例自身基线（P95）并回写实例级阈值')
    ap.add_argument('--db', default=DEFAULT_DB, help='inspection.db 路径')
    ap.add_argument('--metrics-db', default=DEFAULT_METRICS_DB,
                    help='pro_metrics.db 路径')
    ap.add_argument('--window-days', type=int, default=None,
                    help='统计窗口天数（默认取阈值 db_self_baseline_window_days）')
    ap.add_argument('--multiplier', type=float, default=None,
                    help='P95 放大倍数（默认取 db_self_baseline_p95_multiplier）')
    ap.add_argument('--min-samples', type=int, default=20,
                    help='低于该样本数的字段跳过（默认 20）')
    ap.add_argument('--dry-run', action='store_true', help='只算不写库')
    ap.add_argument('--purge', action='store_true', help='写入前清空实例阈值表')
    ap.add_argument('--report', action='store_true', help='打印明细')
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print('[ERROR] 数据库不存在: %s' % args.db)
        return 2

    globals_ = load_globals(args.db)
    window = args.window_days or int(globals_.get('db_self_baseline_window_days') or 30)
    mult = args.multiplier if args.multiplier is not None else \
        float(globals_.get('db_self_baseline_p95_multiplier') or 1.2)

    try:
        snaps = load_snapshots(args.metrics_db, window)
    except Exception as e:
        print('[ERROR] %s' % e)
        return 2

    computed = compute(snaps, globals_, mult, args.min_samples)

    print('=' * 74)
    print('  实例自身基线（P95 × %s）建立' % mult)
    print('=' * 74)
    print('  窗口 %d 天 / 实例 %d 个 / 有足够样本的实例 %d 个 / 最小样本数 %d' % (
        window, len(snaps), len(computed), args.min_samples))

    skipped = [(i, len(p)) for i, p in snaps.items() if i not in computed]
    if skipped:
        for inst, n in skipped:
            print('  [SKIP] %s：窗口内仅 %d 个快照，样本不足' % (inst, n))

    if args.report and computed:
        for inst, rows in sorted(computed.items()):
            print('\n  实例 %s' % inst)
            print('    %-26s %10s %10s %10s %8s' % (
                '阈值键', '通用值', '自身P95', '实例值', '样本'))
            for (key, value, base, p, samples) in rows:
                flag = '  ← 上调' if value > base else ''
                print('    %-26s %10s %10s %10s %8d%s' % (
                    key, base, p, value, samples, flag))

    n = apply(args.db, computed, mult, window,
              dry_run=args.dry_run, purge=args.purge)
    tag = '[预演] ' if args.dry_run else ''
    print('\n  %s写入 instance_thresholds：%d 条' % (tag, n))
    if args.dry_run:
        print('  （--dry-run，已回滚）')
    print('=' * 74)
    return 0


if __name__ == '__main__':
    sys.exit(main())
