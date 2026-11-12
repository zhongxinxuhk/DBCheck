# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

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

AUTHOR = 'sec.hn.cn (X.U zx)'
CATEGORY = '业务专项巡检'
TAGS = ['业务专项', '数据库运维服务清单', '巡检']


def _step(sid, kind, label, x, y, args=None, ref=''):
    return {'id': sid, 'kind': kind, 'label': label, 'ref': ref,
            'args': args or {}, 'x': x, 'y': y}


def _inspect_chain(start_y=60, gap=100):
    """生成 inspect 节点的坐标序列。"""
    return [(260, start_y + i * gap) for i in range(20)]


def _build(name, cycle_hint, match_titles, hub_min_sev, with_email,
           report_min_findings, description):
    """构造一条「start → N×inspect → (hub) → output(report) → (email) → end」工作流。"""
    steps = []
    edges = []
    steps.append(_step('n0', 'start', '开始', 40, 60))

    positions = _inspect_chain(start_y=60, gap=100)
    node_ids = []
    for i, title in enumerate(match_titles):
        sid = 'i%d' % (i + 1)
        x, y = positions[i]
        steps.append(_step(sid, 'inspect', '巡检组件：%s' % title, x, y,
                           args={'match_title': title}))
        edges.append(['n0', sid])          # start 扇出到全部组件
        node_ids.append(sid)

    last = 'n0'
    if hub_min_sev:
        steps.append(_step('h1', 'hub', '智能诊断（仅异常时）', 520, 60 + 50,
                           args={'when': {'min_severity': hub_min_sev}}, ref='hub'))
        for sid in node_ids:
            edges.append([sid, 'h1'])
        last = 'h1'

    # inspect 节点若未接 hub，直接汇入输出节点
    out_args = {'action': 'report'}
    if report_min_findings:
        out_args['when'] = {'min_findings': report_min_findings}
    steps.append(_step('o1', 'output', '输出分析报告', 740, 60 + 50, args=out_args))
    if last == 'h1':
        edges.append(['h1', 'o1'])
    else:
        for sid in node_ids:
            edges.append([sid, 'o1'])

    tail = 'o1'
    if with_email:
        steps.append(_step('o2', 'output', '邮件通知', 900, 60 + 50,
                           args={'action': 'email', 'to': '',
                                 'when': {'min_severity': 'warning'}}))
        edges.append(['o1', 'o2'])
        tail = 'o2'

    steps.append(_step('n9', 'end', '结束', 1080, 60 + 50))
    edges.append([tail, 'n9'])

    full_desc = description.strip() + '\n\n'
    full_desc += '【周期要求】%s\n' % cycle_hint
    full_desc += ('【适配说明】本工作流跨产品通用：巡检节点按章节标题匹配组件，'
                  '运行时按目标实例库型自动选取对应产品的「业务专项巡检」模板章节，'
                  '无需为每类数据库单独建流。\n')
    full_desc += '【条件控制】诊断仅在出现 %s 及以上发现时触发；报告按发现数阈值输出，避免空跑。' % (
        hub_min_sev or '不触发')
    return {
        'name': name,
        'steps': steps,
        'edges': edges,
        'description': full_desc,
        'author': AUTHOR,
        'version': '1.0.0',
        'category': CATEGORY,
        'tags': TAGS,
    }


def build_workflows():
    """返回待创建的工作流定义列表。"""
    out = []

    out.append(_build(
        name='日巡检工作流（业务专项）',
        cycle_hint='每日 1 次，建议 07:30 起跑（业务早高峰前完成），单实例 5-10 分钟出结果。',
        match_titles=['日常核心指标', '日常异常明细'],
        hub_min_sev='',
        with_email=False,
        report_min_findings=1,
        description='日巡检编排：核查实例是否在线、连接是否吃紧、昨日备份是否成功、空间是否够用，'
                    '并只输出当日新增异常（锁阻塞、新慢 SQL、新告警错误）。不做趋势与根因分析，'
                    '深度排查交给周巡检与月度健康检查。有异常才出报告，无异常不打扰。',
    ))

    out.append(_build(
        name='周巡检工作流（业务专项）',
        cycle_hint='每周 1 次，建议周一 09:00 起跑，3 个工作日内完成全部实例并出周报（交付物《数据库日运行监控表》）。',
        match_titles=['实例与连接状态', '备份与容量', '集群状态',
                      '性能与对象健康', '安全与合规', '留痕与台账信息'],
        hub_min_sev='high',
        with_email=False,
        report_min_findings=1,
        description='周巡检编排：覆盖 6 组检查项——实例与连接、备份与容量、集群状态、性能与对象健康、'
                    '安全与合规、留痕与台账信息。出现 high 及以上发现时自动触发智能诊断定位根因，'
                    '随后统一出周报。单机部署时「集群状态」组自动降级为单实例一致性检查。',
    ))

    out.append(_build(
        name='月巡检工作流（业务专项）',
        cycle_hint='每月首个工作日执行，建议 1 个工作日内跑完并出《数据库月运行监控表》。',
        match_titles=['核心文件完整性', '统计信息采集', '容量与增长趋势',
                      '日志治理与清理', '参数基线与漂移', '覆盖率自证'],
        hub_min_sev='',
        with_email=False,
        report_min_findings=1,
        description='月度专项巡检编排：核查参数文件/元数据/网络配置/口令文件完整性，刷新统计信息，'
                    '统计容量与增长趋势并预测耗尽时间，治理日志与清理冗余文件，做参数基线月度 diff，'
                    '最后输出巡检覆盖率自证。对应合同「每月首个工作日专项巡检」。',
    ))

    out.append(_build(
        name='月度健康检查工作流（业务专项）',
        cycle_hint='每月 1 次，建议月专项巡检后 2 个工作日内完成，出《数据库健康检查表》。',
        match_titles=['基础状态深度核查', '日志深度分析', '对象状态深度校验',
                      '资源与性能深度分析', '备份深度核查', '安全与合规深度核查'],
        hub_min_sev='warning',
        with_email=True,
        report_min_findings=1,
        description='月度健康检查编排：6 大域深度体检——进程僵死与 core dump、错误码归类与隐患挖掘、'
                    '对象与数据文件完整性、等待事件与命中率、备份真实可恢复性、安全合规。'
                    '出现 warning 及以上发现即触发智能诊断并出报告，同时邮件通知责任人。'
                    '健康评分 < 60 分应转「性能问题专项诊断」模板继续定位。',
    ))

    out.append(_build(
        name='巡检异常整改闭环工作流（业务专项）',
        cycle_hint='事件驱动：日/周巡检判定异常后即时触发，或在监控告警触发时执行。',
        match_titles=['日常异常明细', '性能与对象健康'],
        hub_min_sev='warning',
        with_email=True,
        report_min_findings=1,
        description='异常整改闭环编排：把巡检发现的异常转成可执行整改动作——'
                    '先复采异常明细与性能对象两章确认现象，再由智能诊断给出根因与处置方案，'
                    '输出整改建议报告并邮件通知责任人。对应合同「巡检-告警-整改-闭环管理流程」与'
                    '《巡检异常整改说明及闭环记录》交付物。',
    ))

    return out


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
