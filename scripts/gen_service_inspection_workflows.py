# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
生成《数据库运维服务清单》配套的巡检工作流（日 / 周 / 月 / 健康检查 / 异常闭环）。

设计要点
--------
1. **跨产品通用**：工作流的 ``inspect`` 节点用 ``match_title`` 按章节标题匹配组件，
   运行时由目标实例的 ``db_type`` 自动选中对应产品的「业务专项巡检」模板，
   因此一套工作流即可覆盖 Oracle / SQL Server / DM8 / GBase 8a 四类库。
2. **只匹配业务专项模板**：``match_title`` 使用本套模板独有的章节标题
   （如「日常核心指标」「性能与对象健康」），避免命中既有默认模板的同名章节。
3. **条件分支**：``hub`` 智能诊断仅在 ``min_severity`` 达阈值时执行，
   无异常不空跑；报告节点用 ``min_findings`` 控制，避免产出空报告。
4. **不覆盖已有工作流**：同名已存在则跳过，只做新建。

用法：
    python scripts/gen_service_inspection_workflows.py --dry-run
    python scripts/gen_service_inspection_workflows.py
"""


import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from modules.intelligence.service_workflow_templates import build_workflows


def main():
    parser = argparse.ArgumentParser(description='生成业务专项巡检工作流')
    parser.add_argument('--dry-run', action='store_true', help='只打印不写库')
    args = parser.parse_args()

    from modules.intelligence import workflow_store as ws

    defs = build_workflows()
    print('待创建工作流 %d 条' % len(defs))
    if args.dry_run:
        for w in defs:
            print('  %-32s 节点 %2d 边 %2d' % (w['name'], len(w['steps']), len(w['edges'])))
        return 0

    existing = {w.get('name') for w in ws.list_workflows()}
    created, skipped, failed = [], [], []
    for w in defs:
        if w['name'] in existing:
            skipped.append(w['name'])
            print('SKIP 已存在：%s' % w['name'])
            continue
        try:
            res = ws.save_workflow(
                name=w['name'], steps=w['steps'], edges=w['edges'],
                description=w['description'], author=w['author'],
                version=w['version'], category=w['category'],
                tags=w['tags'], source='user', visibility='local',
            )
            wid = res.get('id') if isinstance(res, dict) else None
            created.append((wid, w['name']))
            print('OK   id=%s %s' % (wid, w['name']))
        except Exception as e:
            failed.append((w['name'], str(e)))
            print('FAIL %s -> %s' % (w['name'], e))

    print('\n新建 %d 条，跳过 %d 条，失败 %d 条' % (
        len(created), len(skipped), len(failed)))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
