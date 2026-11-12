# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""规则可执行率审计：判定每条规则是否"接线 + 取数键齐备"。

判定口径：
  规则可执行 = 分析函数已接线（analyze_with_plugins 被调用）
             AND 规则引用的每个 context 键都能被以下任一来源提供：
                 · 巡检模板 query_key（任意同产品模板）
                 · rules/metrics/<产品>.yaml 的 key
                 · 引擎注入键（system_info / version / ...）
                 · 派生键（_derive_metrics 计算得出）
"""


import ast
import glob
import os
import re
import sqlite3
import sys
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import yaml
from modules.pro.rule_engine import normalize_db_type, load_metrics, _derive_metrics

INJECTED = {'system_info', 'version', '_long_queries', '_safe_errors',
            'cache_hit_ratio_pct', 'auto_analyze', 'issues', 'db_type', 'db_version'}

# 派生键：跑一次空 context 看 _derive_metrics 会产出什么（只取键名）
DERIVED_ALL = set()
for _p in ('redis', 'redis-cluster', 'gbase8a'):
    pass  # 派生依赖输入，键名直接登记（见 _derive_metrics 源码）
DERIVED_KEYS = {'redis_conn_pct', 'gbase8a_conn_pct'}

# ── 已接线的分析函数及其库型 ────────────────────────────────────────────
src = open(os.path.join(ROOT, 'modules/inspection/analyzer.py'), encoding='utf-8').read()
tree = ast.parse(src)
wired = {}
for n in tree.body:
    if not isinstance(n, ast.FunctionDef):
        continue
    seg = ast.get_source_segment(src, n)
    parts = seg.split('"""')
    body = '""'.join(parts[2:]) if len(parts) >= 3 else seg
    for arg in re.findall(r"analyze_with_plugins\(\s*['\"]([a-z0-9_\-]+)['\"]", body):
        # 两侧都归一化：文件里写 'postgresql' 而 canonical 是 'pg'，不归一会误判未接线
        wired.setdefault(normalize_db_type(arg), []).append(n.name)

# collect_issues 的 db_type → 分析函数后缀（推断哪些库型会被真正分发到）
ALIASES = {}
m = re.search(r'_ISSUE_ANALYZER_ALIASES\s*=\s*\{(.*?)\n\}', src, re.S)
if m:
    for k, v in re.findall(r"'([^']+)'\s*:\s*'([^']+)'", m.group(1)):
        ALIASES[k] = v


def dispatches(db_type):
    """该库型在 collect_issues 中是否会分发到已接线函数。"""
    suffix = ALIASES.get(db_type, db_type)
    # 与 collect_issues 保持一致：连字符转下划线（redis-cluster → redis_cluster）
    fn = 'smart_analyze_' + suffix.replace('-', '_')
    return fn in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


# ── 模板 query_key 池 ───────────────────────────────────────────────────
conn = sqlite3.connect(os.path.join(ROOT, 'data/inspection.db'))
tpl_keys = collections.defaultdict(set)
for db, key in conn.execute("""
        SELECT DISTINCT t.db_type, q.query_key
        FROM inspection_query q
        JOIN inspection_chapter ch ON q.chapter_id = ch.id
        JOIN inspection_template t ON ch.template_id = t.id"""):
    tpl_keys[db].add(key)
TMPL_DB_MAP = {'pg': 'pg', 'dm': 'dm8', 'gbase8a': 'gbase8a', 'gbase': 'gbase'}


def available_keys(canonical):
    pool = set(INJECTED) | DERIVED_KEYS
    pool |= {m_['key'] for m_ in load_metrics(canonical) if m_.get('key')}
    # 同产品模板键（含别名库）
    for db, keys in tpl_keys.items():
        if normalize_db_type(db) == canonical or db == canonical:
            pool |= keys
        if TMPL_DB_MAP.get(canonical) == db:
            pool |= keys
    # 全库键池兜底（规则键常带产品前缀，不依赖 db_type 归属）
    for keys in tpl_keys.values():
        pool |= keys
    return pool


# ── 逐产品审计 ──────────────────────────────────────────────────────────
print('=' * 84)
print(f"{'产品':14s} {'规则':>4s} {'已接线':>6s} {'键齐备':>6s} {'可执行':>6s} {'不执行原因':<28s}")
print('=' * 84)

totals = collections.Counter()
detail_missing = collections.defaultdict(list)

for path in sorted(glob.glob(os.path.join(ROOT, 'modules/pro/rules/builtin/*.yaml'))):
    prod = os.path.basename(path)[:-5]
    canonical = normalize_db_type(prod)
    data = yaml.safe_load(open(path, encoding='utf-8')) or {}
    rules = data.get('rules') or []
    if not rules:
        print(f'{prod:14s} {0:4d} {"—":>6s} {"—":>6s} {"—":>6s} 空壳（rules: []）')
        continue

    pool = available_keys(canonical)
    is_wired = canonical in wired
    dispatch_ok = dispatches(canonical)

    ok = keys_ok_cnt = 0
    reasons = collections.Counter()
    for r in rules:
        need = set()
        for v in (r.get('params') or {}).values():
            for mm in re.finditer(r'\$\{\s*context\.([A-Za-z_]\w*)', str(v)):
                k = mm.group(1)
                if k not in INJECTED:
                    need.add(k)
        missing = need - pool
        if not is_wired or not dispatch_ok:
            reasons['未接线'] += 1
            continue
        if missing:
            reasons['取数键缺失'] += 1
            detail_missing[prod].append((r.get('id'), sorted(missing)))
            continue
        ok += 1

    totals['rules'] += len(rules)
    totals['ok'] += ok
    totals['nowire'] += reasons['未接线']
    totals['nokey'] += reasons['取数键缺失']
    reason_txt = ', '.join(f'{k}×{v}' for k, v in reasons.most_common()) or '—'
    print(f'{prod:14s} {len(rules):4d} {("是" if is_wired and dispatch_ok else "否"):>6s} '
          f'{("是" if reasons["取数键缺失"] == 0 else "否"):>6s} {ok:4d}  {reason_txt:<28s}')

print('=' * 84)
total_rules = totals['rules']
print(f"{'合计':14s} {total_rules:4d} {'':>6s} {'':>6s} {totals['ok']:4d}")
print(f"  可执行 {totals['ok']} / {total_rules} = {totals['ok'] / total_rules * 100:.1f}%")
print(f"  未接线 {totals['nowire']} 条 | 取数键缺失 {totals['nokey']} 条")

if detail_missing:
    print()
    print('键缺失明细（前 12 个产品）:')
    for prod, items in list(detail_missing.items())[:12]:
        print(f'  {prod}: {len(items)} 条')
        for rid, miss in items[:2]:
            print(f'      {rid} 缺 {miss}')
