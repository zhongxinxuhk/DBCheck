# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Cutoff-aware, source-linked datasets for the 24 service deliverables."""

from datetime import datetime, time, timedelta, timezone
import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from modules.core.data_security import redact
from modules.inspection.basic_info import FIELD_ORDER
from .catalog import all_items
from .delivery_catalog import DELIVERABLES, WORKFLOW_KEYS
from .labels import FIELD_LABELS
from .service import period_bounds, timestamp, sla_result
from .store import canonical, utcnow
from .delivery_store import digest

TZ = ZoneInfo('Asia/Shanghai')
STATUS = {'pass': '通过', 'fail': '不符合', 'error': '采集失败', 'blocked': '阻断',
          'collected': '已采集', 'not_collected': '未采集', 'needs_review': '待复核',
          'not_applicable': '不适用', 'draft': '草稿', 'submitted': '待审批',
          'approved': '已批准', 'executing': '执行中', 'verified': '待闭环复核',
          'closed': '已闭环', 'failed': '执行失败', 'rejected': '已驳回'}
EXTRA_LABELS = {
    'name': '名称', 'value': '配置值', 'value_in_use': '运行值', 'is_dynamic': '动态生效',
    'username': '用户名', 'user_name': '用户名', 'role': '角色', 'granted_role': '授权角色',
    'grantee': '授权主体', 'privilege': '权限', 'grantor': '授权人', 'table_name': '表名称',
    'table_schema': '数据库名称', 'tablespace_name': '逻辑存储空间名称', 'file_name': '文件路径',
    'physical_name': '文件路径', 'bytes': '容量（字节）', 'used_percent': '使用率（%）',
    'autoextensible': '自动扩展', 'sql_id': 'SQL标识', 'executions': '执行次数',
    'elapsed_time': '累计耗时（原始单位）', 'index_name': '索引名称', 'type_desc': '产品类型',
    'node': '节点', 'node_name': '节点名称', 'observed_at': '观测时间',
}


def label(key):
    return FIELD_LABELS.get(key, EXTRA_LABELS.get(key, dict(FIELD_ORDER).get(key, key)))


def request_options(data, assets):
    if not isinstance(data, dict):
        raise ValueError('报告参数须为对象')
    for flag in ('collect_first', 'split_sources'):
        if not isinstance(data.get(flag, False), bool):
            raise ValueError('采集与拆分选项须为布尔值')
    types = data.get('deliverables', ['D09'])
    if not isinstance(types, list) or not types or len(types) > len(DELIVERABLES) or any(not isinstance(t, str) or t not in DELIVERABLES for t in types):
        raise ValueError('交付物类型无效')
    types = list(dict.fromkeys(types))
    ids = data.get('source_ids', data.get('asset_ids'))
    if ids is None and 'source_ids' not in data:
        ids = [a['id'] for a in assets]
    if not isinstance(ids, list) or not ids or any(not isinstance(v, str) for v in ids):
        raise ValueError('请选择一个或多个数据源')
    allowed = {a['id'] for a in assets}
    if not set(ids) <= allowed:
        raise PermissionError('报告包含无权访问的资产')
    period = data.get('period', 'month')
    if not isinstance(period, str) or period not in {'week', 'month', 'quarter', 'year'}:
        raise ValueError('报告周期无效')
    start, end = period_bounds(period, data.get('date'))
    cutoff = timestamp(data.get('cutoff') or utcnow())
    if cutoff > datetime.now(timezone.utc) + timedelta(seconds=10):
        raise ValueError('数据截止时间不能晚于当前时间')
    formats = data.get('formats', ['xlsx'])
    if not isinstance(formats, list) or not formats or any(not isinstance(v, str) or v not in {'xlsx', 'docx'} for v in formats):
        raise ValueError('导出格式须为 xlsx 或 docx')
    run_id = str(data.get('run_id') or '')
    if 'RUN' in types and not run_id and 'source_ids' not in data:
        raise ValueError('单次巡检导出须指定采集批次')
    selected_sheets = data.get('sheets') or []
    sheet_names = {s for t in types for s in DELIVERABLES[t][2]}
    if not isinstance(selected_sheets, list) or any(not isinstance(v, str) or v not in sheet_names for v in selected_sheets):
        raise ValueError('所选工作表不属于当前交付物')
    if not isinstance(data.get('layout', 'separate'), str) or data.get('layout', 'separate') not in {'separate', 'combined'}:
        raise ValueError('工作簿组织方式无效')
    workflow_ids = data.get('workflow_ids') or []
    if not isinstance(workflow_ids, list) or any(not isinstance(value, str) for value in workflow_ids):
        raise ValueError('事件范围须为记录编号列表')
    return {'deliverables': types, 'asset_ids': sorted(set(ids)), 'period': period,
            'date': data.get('date') or datetime.now(TZ).date().isoformat(),
            'start': start.isoformat(), 'end': end.isoformat(),
            'cutoff': cutoff.astimezone(timezone.utc).isoformat(), 'formats': list(dict.fromkeys(formats)),
            'run_id': run_id, 'sheets': selected_sheets, 'layout': data.get('layout', 'separate'),
            'workflow_ids': list(dict.fromkeys(workflow_ids)),
            **({'source_ids': sorted(set(ids))} if 'source_ids' in data else {}),
            'collect_first': bool(data.get('collect_first', False)),
            'split_sources': bool(data.get('split_sources', False))}


def sources_from_context(context):
    """Preserve native query names and their explicit execution status."""
    result = {}
    statuses = context.get('_query_status') or {}
    for key, value in context.items():
        if isinstance(value, list) and (value or key in statuses) and (not value or isinstance(value[0], dict)):
            status = statuses.get(key, {})
            result[key] = {'rows': value, 'status': status.get('status', 'collected'),
                           'reason': status.get('reason', ''), 'scope': 'selected_inspection_template',
                           'completeness': 'template_scope', 'truncated': status.get('truncated', False)}
    basic = context.get('basic_info_db') or context.get('basic_info')
    if isinstance(basic, dict):
        result['basic_info'] = {'rows': [basic], 'status': 'collected', 'scope': 'current_connection'}
    return redact(result, mask_identity=True, preserve_network=True)


def business_row(asset, record=None):
    row = {'资产编号': asset['id'], '配置项编号': asset['payload'].get('configuration_id'),
           '资产名称': asset['payload'].get('asset_name'), '台账版本': asset['version']}
    if record:
        row.update({'记录编号': record['id'], '记录版本': record.get('version', 1),
                    '采集批次': record['payload'].get('run_id'),
                    '证据台账版本': record['payload'].get('asset_version'),
                    '记录时间': record.get('updated_at'), '来源': record['payload'].get('provenance')})
    return row


def detail_rows(value):
    if isinstance(value, list):
        return [v if isinstance(v, dict) else {'value': v} for v in value]
    if isinstance(value, dict):
        return [value]
    return [] if value in (None, '') else [{'value': value}]


def source_matches(key, section):
    aliases = {
        '集群节点': ('cluster_nodes', 'rac_instance', 'ha_nodes'),
        '数据库参数': ('parameters', 'db_parameters', 'parameter', 'global_variables', 'para', 'params'),
        '逻辑存储空间': ('storage', 'tablespace', 'tablespaces', 'tablespace_usage', 'tablespace_info', 'ts_usage'),
        '存储文件': ('files', 'datafiles', 'data_files', 'db_files'),
        '用户账号': ('accounts', 'server_accounts', 'users', 'user_info', 'db_users', 'user_status'),
        '角色成员': ('role_members', 'server_role_members', 'user_roles', 'role_grants', 'role_privs', 'dba_role_privs'),
        '权限明细': ('grants', 'permissions', 'server_permissions', 'schema_permissions', 'object_permissions', 'user_privileges', 'privilege_details', 'sys_privs', 'tab_privs'),
        '备份执行': ('backups', 'backup_jobs', 'backup_info'),
        '慢SQL': ('slow_sql', 'slow_queries', 'top_sql', 'slow_query', 'long_sql', 'top_sql_elapsed', 'top_elapsed'),
        '索引观测': ('indexes', 'index_health', 'index_usage'),
        '性能观测': ('waits', 'cache', 'sessions', 'target_cpu', 'target_filesystems', 'shared_pool'),
        '数据库基本信息': ('basic_info', 'version', 'instance', 'archive'),
    }
    native = key.lower()
    for prefix in ('oracle_', 'sqlserver_', 'dm_', 'gbase8a_', 'mysql_', 'pg_'):
        if native.startswith(prefix):
            native = native[len(prefix):]
            break
    return native in aliases.get(section, ())


def build_snapshot(services, archive, actor, catalog, options):
    start, end, cutoff = map(timestamp, (options['start'], options['end'], options['cutoff']))
    effective_cutoff = min(end - timedelta(microseconds=1), cutoff)
    records = services.reporting_records(actor, effective_cutoff.astimezone(timezone.utc).isoformat())
    ids = options['asset_ids']
    assets = {r['id']: r for r in records if r['kind'] == 'asset' and r['id'] in ids}
    missing_history = []
    for asset_id in set(ids) - assets.keys():
        current = services.get(actor, asset_id)
        identity_fields = {key: current['payload'].get(key) for key in
                           ['configuration_id', 'asset_name', 'instance_id', 'db_type']}
        assets[asset_id] = {**current, 'version': 0, 'payload': identity_fields}
        missing_history.append(asset_id)
    items = all_items(catalog)
    runs = [r for r in records if r['asset_id'] in assets and r['kind'] == 'inspection'
            and r['item_id'] == 'run-evidence']
    for observation in archive.observations(actor, ids):
        if timestamp(observation['observed_at']) <= effective_cutoff:
            runs.append({'id': observation['id'], 'asset_id': observation['asset_id'], 'version': 1,
                         'updated_at': observation['observed_at'], 'created_at': observation['observed_at'],
                         'payload': {'run_id': observation['id'], 'source_results': observation['source_results'],
                                     'provenance': 'inspection_snapshot', 'collected_at': observation['observed_at']}})
    legacy_runs, legacy_notes = legacy_observations(assets, start, effective_cutoff) if getattr(
        services, 'include_legacy_history', True) else ([], '按指定单次巡检批次导出')
    captured = {r['payload']['run_id'] for r in runs if r['payload'].get('run_id')}
    runs.extend(r for r in legacy_runs if r['payload'].get('native_task_id') not in captured)
    runs.sort(key=lambda r: timestamp(r['payload'].get('collected_at') or r['updated_at']))
    if options['run_id']:
        runs = [r for r in runs if r['payload'].get('run_id') == options['run_id']]
        if not runs:
            raise ValueError('采集批次不存在、晚于截止时间或不在所选范围')
    elif options['deliverables'] == ['RUN']:
        runs = list({run['asset_id']: run for run in runs}.values())
    latest = {}
    for run in runs:
        latest[run['asset_id']] = run
    in_period = [r for r in runs if start <= timestamp(r['payload'].get('collected_at') or r['updated_at']) < end]
    if options['deliverables'] == ['RUN']:
        in_period = runs
    workflows = []
    for record in records:
        if record['asset_id'] not in assets or record['kind'] != 'workflow':
            continue
        payload = record['payload']
        details = payload.get('details', {})
        occurred = details.get('effective_at') or details.get('completed_at') or payload.get('detected_at') or record['created_at']
        closed_at = next((v['time'] for v in reversed(payload.get('timeline', [])) if v.get('state') == 'closed'), None)
        if timestamp(occurred) >= end:
            continue
        if timestamp(occurred) < start and closed_at and timestamp(closed_at) < start:
            continue
        workflows.append(record)
    selected_events = set(options.get('workflow_ids', []))
    if selected_events:
        if not selected_events <= {r['id'] for r in workflows}:
            raise ValueError('事件不存在、不在所选资产周期内或截止时点缺少对应版本')
        references = set(selected_events)
        for record in workflows:
            if record['id'] in selected_events:
                reference = record['payload'].get('details', {}).get('incident_reference')
                if reference:
                    references.add(reference)
        workflows = [r for r in workflows if r['id'] in selected_events or
                     r['payload'].get('details', {}).get('incident_reference') in references]
    checks = [r for r in records if r['asset_id'] in assets and r['kind'] == 'inspection'
              and r['item_id'] in items and start <= timestamp(r['payload'].get('collected_at') or r['updated_at']) < end
              and (not options['run_id'] or r['payload'].get('run_id') == options['run_id'])]
    source_refs = [{'id': r['id'], 'version': r.get('version', 1), 'asset_id': r.get('asset_id') or r['id'],
                    'payload_sha256': digest(canonical(r['payload']).encode())}
                   for r in [*assets.values(), *runs, *checks, *workflows]]
    sheets = {}

    def put(name, rows):
        sheets[name] = rows

    register_fields = {
        '资产底账': ['configuration_id', 'asset_name', 'project_id', 'db_type', 'version',
            'patch_level', 'host', 'port', 'instance_id', 'cluster_id', 'system', 'business', 'owner', 'lifecycle',
            'deployment', 'hardware', 'online_date', 'host_model', 'location', 'os_version', 'storage_platform'],
        '集群基本信息': ['cluster_id', 'cluster_type', 'architecture', 'node_count', 'node_names', 'cluster_network', 'disaster_recovery'],
        '数据库基本信息': ['database', 'instance_name', 'charset', 'file_size', 'archive_path', 'archive_mode',
                          'version', 'patch_level', 'host_model', 'location', 'os_version', 'storage_platform',
                          'storage_mode', 'backup_policy_id', 'disaster_recovery', 'cluster_id'],
        '备份策略': ['backup_policy_id', 'backup_data_type', 'backup_content', 'backup_window', 'backup_method',
                     'backup_frequency', 'backup_retention', 'backup_owner', 'backup_policy_version', 'backup_change_reference'],
    }
    table_keys = {'集群节点': 'cluster_nodes', '集群参数': 'cluster_parameters', '数据库参数': 'database_parameters', '逻辑存储空间': 'tablespaces',
                  '用户账号': 'accounts', '角色成员': 'role_members', '权限明细': 'permissions', '备份策略': 'backup_policies'}
    for name in {s for t in options['deliverables'] for s in DELIVERABLES[t][2]} | {'备份执行'}:
        rows = []
        for aid, asset in assets.items():
            before_count = len(rows)
            base = business_row(asset)
            if name in register_fields:
                values = {label(k): asset['payload'].get(k) for k in register_fields[name]}
                rows.append({**base, **values, '来源': '当前身份关联，历史字段待核对' if aid in missing_history else '已确认台账',
                             '状态': '历史台账版本缺失' if aid in missing_history else '待核对', '登记时间': asset['updated_at']})
            table = table_keys.get(name)
            if table:
                value = asset['payload'].get('tables', {}).get(table, asset['payload'].get(table))
                for item in detail_rows(value):
                    rows.append({**base, **{label(k): v for k, v in item.items()}, '来源': '已确认台账'})
            run = latest.get(aid)
            found = False
            if run:
                for key, source in run['payload'].get('source_results', {}).items():
                    if not isinstance(source, dict) or not source_matches(key, name):
                        continue
                    found = True
                    common = {**business_row(asset, run), '观测时间': source.get('collected_at') or run['updated_at'],
                              '来源查询': key, '采集范围': source.get('scope', '未声明'),
                              '状态': '部分采集' if source.get('truncated') else STATUS.get(source.get('status'), source.get('status')),
                              '原因': source.get('reason', ''), '完整性': source.get('completeness', '范围待核对')}
                    for item in source.get('rows') or [{}]:
                        row = {**common, **{label(k): v for k, v in item.items()}}
                        if name == '数据库参数':
                            parameter = item.get('name') or item.get('variable_name')
                            observed = item.get('value_in_use', item.get('value', item.get('variable_value')))
                            expected = asset['payload'].get('approved_parameters', {}).get(parameter)
                            row.update({'批准基线': expected, '基线差异': '待登记' if expected is None else
                                        '一致' if str(expected) == str(observed) else '不一致'})
                        rows.append(row)
            if len(rows) == before_count and name in {'资产底账', '数据库基本信息', '备份策略', '数据库参数', '逻辑存储空间', '存储文件', '用户账号', '权限明细', '角色成员', '集群节点', '集群参数', '慢SQL', '索引观测'}:
                rows.append({**base, '状态': '未采集', '原因': '缺少对应范围的采集或登记明细'})
        put(name, rows)

    check_rows = []
    for r in checks:
        p = r['payload']
        check_rows.append({**business_row(assets[r['asset_id']], r), '检查项编号': r['item_id'],
                           '检查项': items[r['item_id']]['title'], '检查域': items[r['item_id']]['key'],
                           '状态': STATUS.get(p.get('status'), p.get('status')), '原始状态': p.get('status'),
                           '原因': p.get('reason'), '待补字段': p.get('missing', []),
                           '实测内容': p.get('measurements', p.get('details')), '采集时间': p.get('collected_at')})
    put('检查明细', check_rows)
    put('健康检查项', check_rows)
    put('专项巡检', [r for r in check_rows if r['检查域'] in {'file_integrity', 'statistics', 'growth', 'file_status', 'deep_logs'}])
    daily = []
    for aid, asset in assets.items():
        day = start.astimezone(TZ).date()
        active_from = asset['payload'].get('service_start_date') or asset['payload'].get('online_date')
        if active_from:
            day = max(day, datetime.fromisoformat(str(active_from)).date())
        last = min(end - timedelta(microseconds=1), cutoff).astimezone(TZ).date()
        if asset['payload'].get('offline_date'):
            last = min(last, datetime.fromisoformat(asset['payload']['offline_date']).date() - timedelta(days=1))
        while day <= last and datetime.combine(day, time.min, TZ) < end:
            samples = [r for r in in_period if r['asset_id'] == aid and
                       timestamp(r['payload'].get('collected_at') or r['updated_at']).astimezone(TZ).date() == day]
            daily.append({**business_row(asset), '日期': day.isoformat(), '样本数': len(samples),
                          '状态': '有采集记录' if samples else '未采集',
                          '采集批次': [r['payload'].get('run_id') for r in samples],
                          '异常检查项数': sum(r['原始状态'] in {'fail', 'error', 'blocked'} for r in check_rows
                                            if r['资产编号'] == aid and timestamp(r.get('采集时间') or r['记录时间']).astimezone(TZ).date() == day)})
            day += timedelta(days=1)
    put('日运行监控', daily)
    summaries = []
    for aid, asset in assets.items():
        days = [r for r in daily if r['资产编号'] == aid]
        summaries.append({**business_row(asset), '周期起': options['start'], '周期止（不含）': options['end'],
                          '观测天数': sum(bool(r['样本数']) for r in days), '应覆盖天数': len(days),
                          '采集批次数': sum(r['样本数'] for r in days), '数据缺口天数': sum(not r['样本数'] for r in days),
                          '说明': '连续可用性与故障时长需监控事件证据'})
    for name in ['月运行汇总', '年度运行概况', '季度运行概况']:
        put(name, summaries)

    grouped = {k: [] for k in WORKFLOW_KEYS}
    for r in workflows:
        p = r['payload']
        details = p.get('details') or {}
        key = items.get(r['item_id'], {}).get('key', 'remediation' if r['item_id'] == 'inspection-alert' else '')
        row = {**business_row(assets[r['asset_id']], r), '需求项': key, '标题': p.get('title'),
               '状态': STATUS.get(p.get('state'), p.get('state')), '原始状态': p.get('state'),
               **{label(k): v for k, v in details.items()},
               '发现时间': p.get('detected_at'), '故障等级': p.get('priority'),
               '关联故障': details.get('incident_reference') or p.get('incident_reference'),
               '重大故障': bool(details.get('major_incident') or p.get('major_incident')),
               '审批人': p.get('approved_by'), '执行人': p.get('executor'),
               '验证证据': p.get('verification_reference') or details.get('verification_reference'),
               '闭环时间': next((v['time'] for v in reversed(p.get('timeline', [])) if v.get('state') == 'closed'), None),
               '期初转入': timestamp(p.get('detected_at') or r['created_at']) < start,
               '逾期': bool(details.get('deadline') and deadline_time(details['deadline']) < min(end, cutoff) and p.get('state') != 'closed')}
        if key in WORKFLOW_KEYS['incident'] and p.get('detected_at'):
            try:
                row['故障时效核对'] = sla_result({**details, **p}, now=effective_cutoff)
            except ValueError:
                row['故障时效核对'] = '事件时间或分级待核对'
        for group, keys in WORKFLOW_KEYS.items():
            if key in keys:
                grouped[group].append(row)
    major_references = {r['记录编号'] for r in grouped['incident'] if r['重大故障']}
    major_references.update(r['关联故障'] for r in grouped['incident'] if r['重大故障'] and r['关联故障'])
    for rows in grouped.values():
        for row in rows:
            if row['关联故障'] in major_references:
                row['重大故障'] = True
    assignments = {
        'maintenance': ['维护计划与执行', '运维分析'],
        'change': ['变更记录', '审批执行', '前后对比', '回滚验证', '策略变更'],
        'drill': ['演练概况', '恢复步骤', '校验结果', 'RPO与RTO'],
        'incident': ['故障记录', '故障分析', '影响分析', '故障时间线', '根因证据', '处置评估', '恢复验证', '预防措施', '重大事项'],
        'performance': ['基线对比', '瓶颈分析', '优化效果', '后续计划', '治理记录', '效果验证', '索引建议', '变更验证', '参数调优', '负载与验证', '优化计划'],
        'architecture': ['现状与目标', '方案评估', '容量测算', '实施回退', '验证结果'],
    }
    for group, names in assignments.items():
        for name in names:
            values = grouped[group]
            if name in {'索引建议', '变更验证'}:
                values = [r for r in values if r['需求项'] == 'index_change']
            elif name in {'参数调优', '负载与验证'}:
                values = [r for r in values if r['需求项'] == 'parameter_change']
            elif name in {'治理记录', '效果验证'}:
                values = [r for r in values if r['需求项'] == 'sql_optimization']
            elif name == '故障时间线':
                values = [{**business_row(assets[r['asset_id']], r), **v,
                           '重大故障': r['id'] in major_references or r['payload'].get('details', {}).get('incident_reference') in major_references}
                          for r in workflows
                          if items.get(r['item_id'], {}).get('key') in WORKFLOW_KEYS['incident']
                          for v in r['payload'].get('timeline', [])]
            put(name, values)
    issues = grouped['remediation']
    for name in ['问题清单', '异常整改', '风险清单', '整改过程', '整改闭环', '复测与闭环', '改进闭环']:
        put(name, issues)
    put('风险清单', [*issues, *[r for r in check_rows if r['原始状态'] in {'fail', 'error', 'blocked'}]])
    native_issues = [{**business_row(assets[r['asset_id']], r), **v, '来源': '巡检智能分析', '根因确认': '待分析'}
                     for r in in_period for v in r['payload'].get('source_results', {}).get('inspection_issues', {}).get('rows', [])]
    sheets['风险清单'].extend(native_issues)
    put('遗留问题', [r for r in issues if r['原始状态'] != 'closed'])
    put('验证记录', [r for r in grouped['maintenance'] if r['验证证据']])
    # Preserve each sample rather than summing cumulative counters or averaging percentiles.
    observations = []
    for r in in_period:
        for key, source in r['payload'].get('source_results', {}).items():
            if isinstance(source, dict) and source_matches(key, '性能观测'):
                for value in source.get('rows') or [{}]:
                    observations.append({**business_row(assets[r['asset_id']], r), '指标组': key,
                                         '观测时间': source.get('collected_at') or r['updated_at'],
                                         '采集状态': source.get('status'), '原始指标与单位': value})
    put('性能观测', observations)
    put('资源趋势', observations)
    sql_groups = {}
    for run in in_period:
        asset = assets[run['asset_id']]
        for key, source in run['payload'].get('source_results', {}).items():
            if not isinstance(source, dict) or not source_matches(key, '慢SQL'):
                continue
            for item in source.get('rows', []):
                identity = item.get('sql_id') or item.get('sql_fingerprint') or item.get('queryid') or item.get('sql_handle') or item.get('plan_handle')
                if identity is None:
                    identity = digest(canonical(item).encode())
                fingerprint = digest(canonical([run['asset_id'], asset['payload'].get('database'), identity]).encode())
                row = sql_groups.setdefault(fingerprint, {**business_row(asset), 'SQL指纹': fingerprint,
                                                          '首次观测': run['updated_at'], '观测次数': 0, '采集批次': []})
                row.update({label(k): v for k, v in item.items()})
                row['最近观测'] = run['updated_at']
                row['观测次数'] += 1
                row['采集批次'].append(run['payload'].get('run_id'))
                row['统计说明'] = '原始指标保留最近观测值，累计计数需结合重置与区间证据分析'
    if sql_groups:
        put('慢SQL', list(sql_groups.values()))
    monitor_rows = []
    for r in in_period:
        for key, source in r['payload'].get('source_results', {}).items():
            if not isinstance(source, dict):
                continue
            for value in source.get('rows') or [{}]:
                monitor_rows.append({**business_row(assets[r['asset_id']], r), '指标组': key,
                                     '观测时间': source.get('collected_at') or r['updated_at'],
                                     '采集状态': source.get('status'), '部分采集': source.get('truncated', False),
                                     '原因': source.get('reason'), **{label(k): v for k, v in value.items()}})
    put('监控采集明细', monitor_rows)
    projections = {
        '审批执行': ['标题', '状态', '审批人', '执行人', '审批记录', '执行记录', '维护窗口', '验证证据'],
        '前后对比': ['标题', '变更前状态', '变更后状态', '变更前指标', '变更后指标', '验证证据'],
        '回滚验证': ['标题', '回滚方案', '回退结果', '验证证据', '闭环时间'],
        '恢复步骤': ['标题', '验证场景', '备份引用', '隔离恢复目标', '恢复开始时间', '恢复结束时间', '处置步骤'],
        '校验结果': ['标题', '备份SHA256', '校验和', '校验记录', '业务验证', '数据库验证', '验证证据'],
        'RPO与RTO': ['标题', 'RPO（分钟）', 'RTO（分钟）', '恢复开始时间', '恢复结束时间', '校验记录'],
        '影响分析': ['标题', '重大故障', '影响范围', '故障等级', '发现时间', '恢复登记时间'],
        '根因证据': ['标题', '重大故障', '问题根因', '证据引用', '排查过程', '复盘记录'],
        '处置评估': ['标题', '重大故障', '处置步骤', '响应时间', '介入时间', '恢复登记时间', '处理结果', '复盘记录'],
        '预防措施': ['标题', '预防措施', '运维责任人', '整改时限', '验证证据'],
        '基线对比': ['标题', 'SQL指纹', '基线引用', '变更前指标', '变更后指标', '业务负载依据'],
        '优化效果': ['标题', 'SQL指纹', '优化方案', '变更后指标', '验证证据'],
        '效果验证': ['标题', 'SQL指纹', '基线引用', '变更后指标', '验证证据'],
        '负载与验证': ['标题', '参数名称', '变更前取值', '拟定参数值', '业务负载依据', '变更后指标', '验证证据'],
        '方案评估': ['标题', '风险评估', '可行性论证引用', '兼容性验证引用', '审批记录'],
        '容量测算': ['标题', '数据增长分析', '容量扩展方案', '目标范围'],
        '实施回退': ['标题', '变更前拓扑', '变更后拓扑', '执行记录', '回滚方案', '回退结果'],
        '验证结果': ['标题', '处理结果', '验证证据', '业务验证', '数据库验证', '闭环时间'],
    }
    for name, fields in projections.items():
        sheets[name] = [{**{k: r.get(k) for k in ['资产编号', '资产名称', '记录编号', '需求项', '重大故障', '关联故障']},
                         **{k: r.get(k) for k in fields}} for r in sheets.get(name, [])]
    reviews = []
    changes = []
    for aid, asset in assets.items():
        review_records = [r for r in workflows if r['asset_id'] == aid and
                          items.get(r['item_id'], {}).get('key') == 'asset_review']
        review = review_records[-1] if review_records else None
        review_state = review['payload'].get('state') if review else None
        explicit_inventory_review = bool(review and review['payload'].get('provenance') == 'report_event' and
                                         review['payload'].get('details', {}).get('audit_reference') and
                                         review['payload'].get('details', {}).get('verification_reference'))
        reviewed = bool(review and (review_state == 'closed' or (review_state == 'verified' and explicit_inventory_review)) and
                        review['payload'].get('asset_version') == asset['version'] and
                        review['payload'].get('details', {}).get('reconciliation_result') == 'pass')
        reviews.append({**business_row(asset), '核对状态': '已核对' if reviewed else '待全量核对',
                        '核对记录': review['id'] if review else None,
                        '核对依据与差异': review['payload'].get('details') if review else None})
        history = [v for v in services.asset_history(actor, aid) if timestamp(v['recorded_at']) <= effective_cutoff]
        for before, after in zip(history, history[1:]):
            for field in set(before['details']) | set(after['details']):
                old, new = before['details'].get(field), after['details'].get(field)
                if old != new:
                    changes.append({**business_row(asset), '字段': label(field), '变更前': old, '变更后': new,
                                    '原版本': before['version'], '新版本': after['version'],
                                    '登记时间': after['recorded_at'], '变更引用': after['details'].get('change_reference')})
    put('季度核对记录', reviews)
    put('台账变更记录', changes)
    scores = []
    for aid, asset in assets.items():
        candidates = [r for r in checks if r['asset_id'] == aid and r['payload'].get('asset_version') == asset['version']]
        last_run = candidates[-1]['payload'].get('run_id') if candidates else None
        current = {r['item_id']: r['payload'] for r in candidates if r['payload'].get('run_id') == last_run}
        required = {k for k, item in items.items() if item['kind'] == 'inspection'}
        statuses = [current[k]['status'] for k in required if k in current]
        ready = bool(required) and required <= current.keys() and all(s in {'pass', 'fail', 'not_applicable'} for s in statuses)
        gates = {k for k in required if items[k]['key'] in {'file_integrity', 'backup_integrity', 'file_status'}}
        safe = all(current.get(k, {}).get('status') in {'pass', 'not_applicable'} for k in gates)
        valid = [s for s in statuses if s != 'not_applicable']
        score = round(100 * valid.count('pass') / len(valid), 1) if ready and safe and valid else None
        scores.append({**business_row(asset), '健康评分': score, '评分规则': 'service_health_v1',
                       '采集批次': last_run, '应检项': len(required), '有效评分项': len(valid),
                       '原因': '完整覆盖等权评分' if score is not None else '检查覆盖、适用性或安全门槛尚未满足'})
    put('健康评分', scores)
    snapshot = {'start': options['start'], 'end': options['end'], 'cutoff': options['cutoff'],
                'assets': list(assets.values()), 'sheets': sheets, 'sources': source_refs,
                'catalog_sha256': catalog.get('source_sha256'), 'source_manifest_sha256': digest(canonical(source_refs).encode()),
                'history_notes': {'missing_asset_versions': missing_history, 'legacy_mapping': legacy_notes},
                'coverage': {'assets': len(assets), 'runs': len(in_period), 'checks': len(checks),
                             'uncollected_days': sum(not r['样本数'] for r in daily),
                             'workflows': len(workflows), 'open_issues': len(sheets['遗留问题'])}}
    snapshot['coverage_by_source'] = {identity: {
        'assets': 1, 'runs': sum(r['asset_id'] == identity for r in in_period),
        'checks': sum(r['asset_id'] == identity for r in checks),
        'uncollected_days': sum(r['资产编号'] == identity and not r['样本数'] for r in daily),
        'workflows': sum(r['asset_id'] == identity for r in workflows),
        'open_issues': sum(r['资产编号'] == identity for r in sheets['遗留问题'])} for identity in assets}
    return redact(snapshot, mask_identity=True, preserve_network=True)


def deadline_time(value):
    raw = str(value)
    if len(raw) == 10:
        return datetime.combine(datetime.fromisoformat(raw).date(), time.max, TZ)
    return timestamp(raw)


def legacy_observations(assets, start, cutoff, database=None):
    """Use historical context only when its stable data-source ID matches uniquely."""
    from modules.core import paths
    path = Path(database or paths.DATA_DIR / 'history.db')
    if not path.is_file():
        return [], '历史快照库未提供，使用已归档采集'
    mappings = {}
    for asset_id, asset in assets.items():
        instance_id = asset['payload'].get('instance_id')
        if instance_id:
            mappings.setdefault(instance_id, []).append(asset_id)
    if not mappings:
        return [], '所选资产没有稳定数据源引用'
    result = []
    unmatched = 0
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshots'").fetchone()
        if not exists:
            return [], '历史快照结构尚未建立'
        for row_id, stamp, encoded in db.execute('SELECT id,ts,context_json FROM snapshots WHERE ts>=? AND ts<=? ORDER BY ts,id',
                (start.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S'), cutoff.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S'))):
            try:
                context = json.loads(encoded)
                targets = mappings.get(context.get('_instance_id'), [])
                if not targets:
                    continue
                if len(targets) != 1:
                    unmatched += 1
                    continue
                observed = datetime.fromisoformat(stamp)
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=TZ)
                if not start <= observed <= cutoff:
                    continue
                sources = sources_from_context(context)
                sources['inspection_issues'] = {'status': 'collected', 'rows': context.get('auto_analyze') or []}
                result.append({'id': 'history-' + str(row_id), 'asset_id': targets[0], 'version': 1,
                               'created_at': observed.isoformat(), 'updated_at': observed.isoformat(),
                               'payload': {'run_id': 'history-' + str(row_id), 'source_results': sources,
                                           'native_task_id': context.get('_report_task_id'),
                                           'collected_at': observed.isoformat(), 'provenance': 'legacy_history_exact_instance_id'}})
            except (ValueError, TypeError, AttributeError):
                continue
    return result, {'matched': len(result), 'unmatched_or_ambiguous': unmatched,
                    'legacy_timezone': 'Asia/Shanghai', 'mapping': 'exact_instance_id'}
