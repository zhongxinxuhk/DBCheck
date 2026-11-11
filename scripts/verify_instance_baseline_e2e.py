# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
端到端验证「实例自身基线」是否真的被规则引擎消费。

为什么单独写这个脚本
--------------------
`instance_thresholds` 表在不在、值写对没有，都不代表规则会用它——
中间隔着三层：`load_thresholds(instance_id=)` 的叠加、
`_run_rule` 从 `context['_instance_id']` 透传、`_safe_eval` 的命名空间注入。
任何一层断掉，表现都是「阈值改了但告警不变」，而且不会有任何报错。
本脚本用合成规则把这三层一次性打通验证，不依赖任何真实业务规则。

判定项
------
1. **叠加**：`load_thresholds(instance_id=X)` 必须等于「全局键 + 该实例覆盖键」，
   且全局键一个都不能少；
2. **回退**：未知实例 / 不传实例时，结果必须与全局完全一致（老库行为不变）；
3. **消费**：同一条规则、同一个指标值，
   - 不传实例 → 按全局阈值判定；
   - 传实例 → 按实例阈值判定，且**两者结论不同**（否则说明覆盖没生效）；
4. **接线**：巡检引擎必须有 `_bind_self_instance`，且它写的是 `_instance_id`。

用法：
    python scripts/verify_instance_baseline_e2e.py
    python scripts/verify_instance_baseline_e2e.py --instance-id 7d0cba7c-a0b --verbose

退出码：0 = 全部通过，1 = 有 FAIL。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import argparse
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DEFAULT_DB = os.path.join(PROJECT_ROOT, 'data', 'inspection.db')

PASS, FAIL, WARN, INFO = 'PASS', 'FAIL', 'WARN', 'INFO'


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, name, detail=''):
        self.rows.append({'level': level, 'name': name, 'detail': detail})

    def counts(self):
        c = {k: 0 for k in (PASS, FAIL, WARN, INFO)}
        for r in self.rows:
            c[r['level']] = c.get(r['level'], 0) + 1
        return c

    def render(self, verbose=False):
        if verbose:
            for r in self.rows:
                if r['detail']:
                    print('  [%s] %s\n        %s' % (r['level'], r['name'], r['detail']))
        else:
            for r in self.rows:
                if r['level'] in (FAIL, WARN):
                    print('  [%s] %s\n        %s' % (r['level'], r['name'], r['detail']))
        c = self.counts()
        print('-' * 72)
        print('结果: PASS %d / WARN %d / FAIL %d / INFO %d' %
              (c[PASS], c[WARN], c[FAIL], c[INFO]))
        return c[FAIL]


def _pick_instance(db_path, preferred=None):
    """挑一个有自身基线的实例做验证；没有就返回 None。

    优先用调用方指定的实例；否则取 instance_thresholds 里键最多的一条
    （覆盖键越多，验证越有说服力）。
    """
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            if preferred:
                cur.execute(
                    'SELECT COUNT(*) FROM instance_thresholds WHERE instance_id = ?',
                    (preferred,))
                if (cur.fetchone() or [0])[0]:
                    return preferred
                return None
            cur.execute('''
                SELECT instance_id, COUNT(*) AS n
                FROM instance_thresholds
                GROUP BY instance_id
                ORDER BY n DESC, instance_id
                LIMIT 1
            ''')
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _table_exists(db_path, table):
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,))
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False


def check_overlay(rep, db_path, iid):
    """判定 1 + 2：叠加正确、回退安全。"""
    from modules.pro.rule_engine import load_thresholds

    g = load_thresholds(db_path)
    if not g:
        rep.add(FAIL, '全局阈值', 'server_thresholds 读不到任何合法键，后续判定无意义')
        return None
    rep.add(PASS, '全局阈值', '%d 个键' % len(g))

    if not iid:
        rep.add(WARN, '实例基线', 'instance_thresholds 里没有任何实例数据，'
                              '先跑 scripts/build_self_baselines.py')
        return None

    inst = load_thresholds(db_path, instance_id=iid)
    missing = [k for k in g if k not in inst]
    if missing:
        rep.add(FAIL, '叠加未丢全局键',
                '实例阈值丢了 %d 个全局键（应叠加而非替换）: %s' %
                (len(missing), ', '.join(missing[:10])))
    else:
        rep.add(PASS, '叠加未丢全局键', '%d 个全局键全部保留' % len(g))

    diff = {k: (g.get(k), inst.get(k)) for k in set(g) | set(inst)
            if g.get(k) != inst.get(k)}
    if diff:
        rep.add(PASS, '实例覆盖生效', '%d 个键被覆盖: %s' % (
            len(diff), ', '.join('%s %s→%s' % (k, a, b)
                                 for k, (a, b) in sorted(diff.items())[:6])))
    else:
        rep.add(WARN, '实例覆盖生效',
                '实例 %s 的阈值与全局完全相同——可能是 P95 未超过全局阈值，'
                '消费链路无法被验证' % iid)

    # 回退：未知实例必须与全局一致
    unknown = load_thresholds(db_path, instance_id='__no_such_instance__')
    if unknown == g:
        rep.add(PASS, '未知实例回退全局', '与全局阈值完全一致')
    else:
        rep.add(FAIL, '未知实例回退全局',
                '未知实例拿到了与全局不同的阈值，可能存在串实例污染')

    if load_thresholds(db_path) == g:
        rep.add(PASS, '不传实例行为不变', '与改前一致（读纯全局）')
    else:
        rep.add(FAIL, '不传实例行为不变', '不传实例时的结果与首次读取不一致')

    return diff


def check_consumption(rep, db_path, iid, diff):
    """判定 3：同一指标值，全局判定 vs 实例判定必须不同。"""
    if not diff:
        return
    from modules.pro.rule_engine import get_rule_engine

    # 挑一个「实例阈值被上调」的键：只有上调才存在「全局报、实例不报」的差异区
    cand = None
    for k, (base, inst_v) in sorted(diff.items()):
        if base is not None and inst_v is not None and inst_v > base:
            cand = (k, base, inst_v)
            break
    if not cand:
        rep.add(INFO, '消费验证',
                '覆盖键中没有被上调的阈值，不存在判定差异区，跳过消费验证')
        return

    key, base, inst_v = cand
    probe = (base + inst_v) / 2.0  # 落在 (全局阈值, 实例阈值) 之间

    # 注意：_safe_eval 的命名空间只由「阈值 + 规则 params + 派生量」组成，
    # context 本身**不**注入（真实规则一律写 ${context.xxx} 由 _resolve_param
    # 取值）。探针必须遵循同一约定，否则会因变量未定义而恒不触发，
    # 把「接线断了」误判成「阈值没生效」。
    rule = {
        'id': '__VERIFY_SELF_BASELINE__',
        'condition': 'probe_value > %s' % key,
        'params': {'probe_value': '${context.probe_value}'},
        'severity': 'high',
        'message_zh': '探针指标 {probe_value}',
        'message_en': 'probe {probe_value}',
    }
    engine = get_rule_engine()

    hit_global = engine._run_rule(rule, 'mysql', {'probe_value': probe, '_lang': 'zh'})
    hit_inst = engine._run_rule(rule, 'mysql',
                                {'probe_value': probe, '_lang': 'zh',
                                 '_instance_id': iid})

    detail = ('键 %s 全局 %s → 实例 %s，探针值取中点 %s' % (key, base, inst_v, probe))
    if hit_global and not hit_inst:
        rep.add(PASS, '规则引擎消费实例阈值',
                detail + '\n        全局阈值下触发、实例阈值下不触发 → 覆盖链路已通')
    elif hit_global and hit_inst:
        rep.add(FAIL, '规则引擎消费实例阈值',
                detail + '\n        两者都触发 → `_instance_id` 没被 _run_rule 取到')
    elif not hit_global and not hit_inst:
        rep.add(FAIL, '规则引擎消费实例阈值',
                detail + '\n        两者都不触发 → 阈值根本没进 _safe_eval 命名空间')
    else:
        rep.add(FAIL, '规则引擎消费实例阈值',
                detail + '\n        只有实例阈值触发 → 覆盖方向反了')


def check_engine_wiring(rep):
    """判定 4：巡检引擎的绑定入口存在且写入正确的键。"""
    try:
        from modules.inspection.engine import BaseInspectionEngine
    except Exception as e:
        rep.add(FAIL, '巡检引擎绑定入口', '无法导入 BaseInspectionEngine: %s' % e)
        return
    fn = getattr(BaseInspectionEngine, '_bind_self_instance', None)
    if fn is None:
        rep.add(FAIL, '巡检引擎绑定入口', 'BaseInspectionEngine 缺少 _bind_self_instance')
        return
    import inspect
    try:
        src = inspect.getsource(fn)
    except Exception:
        src = ''
    if "_instance_id" in src:
        rep.add(PASS, '巡检引擎绑定入口', '_bind_self_instance 写入 context["_instance_id"]')
    else:
        rep.add(FAIL, '巡检引擎绑定入口',
                '_bind_self_instance 未写入 _instance_id，绑定结果不会被规则引擎读到')


def main():
    ap = argparse.ArgumentParser(description='验证实例自身基线是否被规则引擎消费')
    ap.add_argument('--db', default=DEFAULT_DB, help='inspection.db 路径')
    ap.add_argument('--instance-id', default=None, help='指定实例主键（默认自动挑一个）')
    ap.add_argument('--verbose', action='store_true', help='打印全部判定明细')
    args = ap.parse_args()

    print('=' * 72)
    print('  实例自身基线 · 端到端验证')
    print('=' * 72)

    rep = Report()

    if not _table_exists(args.db, 'instance_thresholds'):
        rep.add(WARN, 'instance_thresholds 表',
                '表不存在，先跑 scripts/build_self_baselines.py')
    else:
        rep.add(PASS, 'instance_thresholds 表', '存在')

    iid = _pick_instance(args.db, args.instance_id)
    if args.instance_id and not iid:
        rep.add(FAIL, '实例选择',
                '指定实例 %s 在 instance_thresholds 中无记录' % args.instance_id)
    elif iid:
        print('  验证实例: %s' % iid)

    diff = check_overlay(rep, args.db, iid)
    check_consumption(rep, args.db, iid, diff)
    check_engine_wiring(rep)

    fails = rep.render(verbose=args.verbose)
    print('=' * 72)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
