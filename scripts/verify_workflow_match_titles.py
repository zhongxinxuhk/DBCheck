# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""
校验工作流 ``match_title`` 与巡检模板章节标题的一致性。

背景
----
工作流的 ``inspect`` 节点用 ``match_title`` 按**章节标题子串**（大小写不敏感）
匹配巡检组件（见 ``modules/intelligence/inspect_components.resolve_component``）。
这带来两类静默故障：

1. **改了模版章节标题却没改工作流** → 匹配不到 → 运行时报
   ``COMPONENT_NOT_FOUND``，工作流该节点直接失败。
2. **匹配是子串匹配，且优先命中 default 模板** → 标题改得「差不多」时不会报错，
   而是悄悄绑到另一个含相同关键词的章节、或绑回默认模板的同名章节。
   这种错配比找不到更难发现——工作流照常跑完，跑的是错的检查项。

本脚本把两类故障都变成可枚举的报告，并提供反向覆盖视图：哪些业务专项章节
没有被任何工作流引用。

判定口径
--------
- ``FAIL``  找不到任何组件 —— 必然 COMPONENT_NOT_FOUND。
- ``WARN``  命中但落在非目标模板（``--template-kw`` 不含于模板名，或 is_default=1）
            —— 静默错配。
- ``WARN``  关键词命中多个组件 —— 歧义，取第一个，模板微调即可能改变绑定目标。
- ``INFO``  业务专项模板中存在但未被任何工作流引用的章节 —— 覆盖缺口。

用法：
    python scripts/verify_workflow_match_titles.py
    python scripts/verify_workflow_match_titles.py --db-type oracle --db-type sqlserver
    python scripts/verify_workflow_match_titles.py --template-kw 业务专项 --verbose
"""


import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DEFAULT_DB_TYPES = ['gbase8a', 'dm8', 'oracle', 'sqlserver']


def _iter_inspect_titles(wf):
    """取出一个工作流里所有 inspect 节点的 match_title（保序去重）。"""
    out = []
    for s in (wf.get('steps') or []):
        if s.get('kind') != 'inspect':
            continue
        t = str((s.get('args') or {}).get('match_title') or '').strip()
        if t and t not in out:
            out.append(t)
    return out


def verify(db_types, template_kw, only_business):
    from modules.intelligence import workflow_store as ws
    from modules.intelligence.inspect_components import (
        list_components,
        resolve_component,
    )

    workflows = ws.list_workflows() or []
    if only_business:
        workflows = [w for w in workflows
                     if '业务专项' in str(w.get('name') or '')]

    fails, warns, infos = [], [], []
    referenced = set()   # (db_type, template_id, chapter_id)

    for wf in workflows:
        wname = str(wf.get('name') or '')
        titles = _iter_inspect_titles(wf)
        if not titles:
            continue
        for title in titles:
            for db_type in db_types:
                comps = list_components(db_type=db_type)
                kw = title.lower()
                exact = [c for c in comps
                         if str(c.get('title') or '').strip().lower() == kw]
                cands = [c for c in comps
                         if kw in str(c.get('title') or '').lower()]
                picked = resolve_component(db_type, title)

                if picked is None:
                    fails.append({
                        'workflow': wname, 'db_type': db_type, 'title': title,
                        'reason': 'COMPONENT_NOT_FOUND：该库型下没有标题包含此关键词的章节',
                    })
                    continue

                referenced.add((db_type, picked['template_id'], picked['chapter_id']))
                tname = str(picked.get('template_name') or '')
                if template_kw and (template_kw not in tname
                                    or int(picked.get('is_default') or 0) == 1):
                    warns.append({
                        'workflow': wname, 'db_type': db_type, 'title': title,
                        'reason': '静默错配：命中 %s / is_default=%s / 章节「%s」，'
                                  '不含关键词「%s」' % (
                                      tname, picked.get('is_default'),
                                      picked.get('title'), template_kw),
                    })

                # 歧义判定：解析已优先「标题完全相等」。存在精确命中时绑定是确定的，
                # 只是仍想知道子串层面的其他候选，故降为 --verbose 明细，不计 WARN。
                if not exact and len(cands) > 1:
                    others = [str(c.get('template_name')) + ' / ' + str(c.get('title'))
                              for c in cands if c['id'] != picked['id']]
                    warns.append({
                        'workflow': wname, 'db_type': db_type, 'title': title,
                        'reason': '歧义：无精确同名章节，%d 个章节标题均含此关键词，'
                                  '实际取「%s / %s」，其余候选：%s' % (
                                      len(cands), tname, picked.get('title'),
                                      '；'.join(others[:4])),
                    })

    # 反向覆盖：目标模板中未被引用的章节
    if template_kw:
        for db_type in db_types:
            for c in list_components(db_type=db_type):
                if template_kw not in str(c.get('template_name') or ''):
                    continue
                key = (db_type, c['template_id'], c['chapter_id'])
                if key not in referenced:
                    infos.append({
                        'db_type': db_type,
                        'title': c.get('title'),
                        'template': c.get('template_name'),
                        'reason': '未被任何工作流引用的章节',
                    })

    return {
        'workflow_count': len(workflows),
        'fails': fails,
        'warns': warns,
        'infos': infos,
        'referenced': referenced,
    }


def main():
    ap = argparse.ArgumentParser(description='校验工作流 match_title 与模板章节一致性')
    ap.add_argument('--db-type', action='append', dest='db_types',
                    help='目标库型，可重复；默认四类在保产品')
    ap.add_argument('--template-kw', default='业务专项',
                    help='期望命中的模板名关键词（默认「业务专项」）')
    ap.add_argument('--all-workflows', action='store_true',
                    help='校验全部工作流（默认只校验业务专项那几条）')
    ap.add_argument('--verbose', action='store_true', help='打印每条匹配明细')
    args = ap.parse_args()

    db_types = args.db_types or DEFAULT_DB_TYPES
    res = verify(db_types, args.template_kw,
                 only_business=not args.all_workflows)

    print('=' * 74)
    print('  工作流 match_title 一致性校验')
    print('=' * 74)
    print('  工作流 %d 条 / 目标库型 %s / 期望模板关键词「%s」' % (
        res['workflow_count'], ', '.join(db_types), args.template_kw))

    if args.verbose:
        print('\n  明细：')
        from modules.intelligence.inspect_components import resolve_component
        from modules.intelligence import workflow_store as ws
        for wf in ws.list_workflows() or []:
            if '业务专项' not in str(wf.get('name') or ''):
                continue
            for t in _iter_inspect_titles(wf):
                for d in db_types:
                    c = resolve_component(d, t)
                    print('    %-24s %-9s -> %s' % (
                        t, d,
                        (str(c.get('template_name')) + ' / ' + str(c.get('title')))
                        if c else 'MISS'))

    if res['fails']:
        print('\n  [FAIL] %d 项（必然 COMPONENT_NOT_FOUND）' % len(res['fails']))
        for f in res['fails']:
            print('    %-10s %-22s %s' % (f['db_type'], f['title'], f['workflow']))
            print('               %s' % f['reason'])

    if res['warns']:
        print('\n  [WARN] %d 项（静默错配 / 歧义）' % len(res['warns']))
        for w in res['warns']:
            print('    %-10s %-22s %s' % (w['db_type'], w['title'], w['workflow']))
            print('               %s' % w['reason'])

    if res['infos']:
        print('\n  [INFO] %d 个章节未被任何工作流引用' % len(res['infos']))
        for i in res['infos']:
            print('    %-10s %-26s %s' % (i['db_type'], i['title'],
                                          i['template']))

    n_f, n_w = len(res['fails']), len(res['warns'])
    print('\n  合计：FAIL %d / WARN %d / 引用章节 %d 个' % (
        n_f, n_w, len(res['referenced'])))
    print('=' * 74)
    return 1 if n_f else 0


if __name__ == '__main__':
    sys.exit(main())
