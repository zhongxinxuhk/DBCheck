# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Evidence-derived requirement status, calendars, SLA and service delivery."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .catalog import all_items, FIELDS
from .inspection import assess, collect
from .store import utcnow

STATUS_NAMES = {'pass':'通过','fail':'不符合','blocked':'安全阻断','error':'采集失败',
                'not_collected':'未采集','needs_review':'待复核','not_applicable':'不适用'}
FAMILIES = {'oracle','sqlserver','dm','gbase8a'}


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return result


def period_bounds(kind, anchor=None):
    today = date.fromisoformat(anchor) if anchor else datetime.now(ZoneInfo('Asia/Shanghai')).date()
    if kind == 'week':
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    elif kind == 'year':
        start, end = date(today.year,1,1), date(today.year+1,1,1)
    elif kind in {'month','quarter'}:
        month = today.month if kind == 'month' else (today.month-1)//3*3+1
        start = date(today.year, month,1)
        offset = 1 if kind == 'month' else 3
        index = today.year * 12 + month - 1 + offset
        end = date(index//12,index%12+1,1)
    else:
        raise ValueError('周期须为 week、month、quarter 或 year')
    tz = ZoneInfo('Asia/Shanghai')
    return datetime.combine(start,datetime.min.time(),tz), datetime.combine(end,datetime.min.time(),tz)


def first_business_day(year, month, holidays, working_days=None):
    if holidays is None:
        raise ValueError('请配置客户工作日日历')
    day = date(year,month,1)
    excluded = {date.fromisoformat(v) for v in holidays}
    overrides = {date.fromisoformat(v) for v in (working_days or [])}
    while day not in overrides and (day.weekday() >= 5 or day in excluded):
        day += timedelta(days=1)
        if day.month != month:
            raise ValueError('本月日历没有可用工作日')
    return day.isoformat()


def business_deadline(start, minutes, calendar):
    if not calendar or not calendar.get('intervals') or 'holidays' not in calendar:
        raise ValueError('缺少客户工作日、节假日或工作时段配置')
    zone = ZoneInfo(calendar.get('timezone','Asia/Shanghai'))
    current = timestamp(start).astimezone(zone)
    remaining = int(minutes)
    holidays = set(calendar['holidays'])
    overrides = set(calendar.get('working_days',[]))
    for _ in range(730):
        day = current.date()
        if day.isoformat() in overrides or (day.weekday()<5 and day.isoformat() not in holidays):
            for begin,end in calendar['intervals']:
                left = datetime.combine(day,datetime.strptime(begin,'%H:%M').time(),zone)
                right = datetime.combine(day,datetime.strptime(end,'%H:%M').time(),zone)
                if right<=left:
                    raise ValueError('工作时段结束须晚于开始')
                current = max(current,left)
                available = max(0,int((right-current).total_seconds()//60))
                if remaining<=available:
                    return (current+timedelta(minutes=remaining)).isoformat()
                remaining-=available
        current = datetime.combine(day+timedelta(days=1),datetime.min.time(),zone)
    raise ValueError('工作日日历无法计算响应时限')


def sla_result(data, now=None):
    now = now or datetime.now(timezone.utc)
    detected = timestamp(data['detected_at'])
    if detected > now:
        raise ValueError('故障发生时间不能晚于当前时间')
    priority = str(data.get('priority','')).upper()
    if priority not in {'P1','P2','NORMAL'}:
        raise ValueError('故障等级须为 P1、P2 或 NORMAL')
    limits = {'responded_at':30 if priority=='P1' else 60 if priority=='P2' else 240,
              'plan_at':60 if priority=='P1' else None,
              'intervened_at':120 if priority=='P2' else None,
              'resolved_at':240}
    result = {}
    for field, minutes in limits.items():
        if minutes is None:
            continue
        deadline = detected + timedelta(minutes=minutes)
        actual = timestamp(data[field]) if data.get(field) else None
        if actual is not None and (actual < detected or actual > now):
            raise ValueError('故障时间线无效')
        result[field] = {'deadline':deadline.isoformat(),'actual':actual.isoformat() if actual else None,
                         'status':'met' if actual and actual <= deadline else
                                  'breached' if actual else 'overdue_unverified' if now > deadline else 'pending'}
    result['escalation_required'] = now > detected + timedelta(hours=4) and not data.get('resolved_at')
    return result


def validate_asset(data):
    if data.get('db_type') not in FAMILIES and not (data.get('scope_kind')=='temporary' and str(data.get('db_type','')).replace('_','').isalnum()):
        raise ValueError('请明确选择 Oracle、SQL Server、DM8 或 GBase 8a')
    required = ('configuration_id','asset_name','db_type','version','owner','business','system','lifecycle')
    missing = [field for field in required if not str(data.get(field,'')).strip()]
    if missing:
        raise ValueError('资产必填字段缺失 '+', '.join(missing))
    for field in ('password','ssh_password','ssl_pwd','private_key','api_key'):
        if field in data:
            raise ValueError('资产台账只允许引用加密凭据，禁止提交口令或私钥')
    if data.get('transport',data.get('legacy_transport','tls')) not in {'tls','verified_ssh_loopback'}:
        raise ValueError('传输方式须为TLS或已验证SSH隧道')
    if data.get('lifecycle') not in {'planned','active','maintenance','offline'}:
        raise ValueError('生命周期状态无效')
    if data.get('schedule_seconds'):
        value = int(data['schedule_seconds'])
        if value < 60 or value > 86400:
            raise ValueError('采集周期须在60秒到86400秒之间')
    if 'monitoring_enabled' in data and not isinstance(data['monitoring_enabled'],bool):
        raise ValueError('监控启用状态须为布尔值')
    if data.get('monitoring_enabled') and not data.get('instance_id'):
        raise ValueError('启用监控前须绑定加密数据源')
    if data.get('service_schedule_enabled') and not data.get('service_start_date'):
        raise ValueError('周期服务须登记实际开始日期')
    if data.get('statistics_max_age_days') is not None and not 1 <= int(data['statistics_max_age_days']) <= 365:
        raise ValueError('统计信息时效基线须为1到365天')
    if data.get('node_count') is not None and (isinstance(data['node_count'],bool) or int(data['node_count'])<1):
        raise ValueError('节点数量须为正整数')


def validate_details(data):
    if not isinstance(data,dict):
        raise ValueError('服务证据须为结构化对象')
    for key,value in data.items():
        if value in (None,'',[],{}):
            continue
        if key.endswith('_at') or key in {'restore_started','restore_finished'}:
            timestamp(value)
        if key.endswith('_date'):
            date.fromisoformat(str(value).split('T')[0])
        if key in {'rpo_minutes','rto_minutes'} and (isinstance(value,bool) or float(value)<0):
            raise ValueError('RPO/RTO须为非负分钟数')
        if key in {'backup_sha256','artifact_sha256'} and (not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdefABCDEF' for c in value)):
            raise ValueError('SHA256须为64位十六进制')


def evidence_result(item, data, workflow=None):
    validate_details(data)
    fields = FIELDS.get(item['key'], [])
    missing = [field for field in fields if data.get(field) in (None,'',[],{})]
    if missing:
        return {'status':'not_collected','missing':missing,'reason':'证据字段不完整'}
    if item['kind'] == 'workflow':
        if not workflow or workflow.get('state') != 'closed':
            return {'status':'needs_review','missing':[],'reason':'等待独立审批、执行记录及闭环复核'}
    for field in ('business_validation','database_validation','reconciliation_result'):
        if field in data and data[field] != 'pass':
            return {'status':'fail','missing':[],'reason':field+' 未通过'}
    if item['key'] == 'tool_verification' and not isinstance(data['artifact_sha256'],str):
        raise ValueError('工具 SHA256 无效')
    if item['key'] == 'tool_verification' and (len(data['artifact_sha256']) != 64 or any(c not in '0123456789abcdefABCDEF' for c in data['artifact_sha256'])):
        raise ValueError('工具 SHA256 须为64位十六进制')
    if item['key'] in {'incident_sla','incident_escalation'}:
        sla = sla_result(data)
        failed = any(isinstance(v,dict) and v.get('status')=='breached' for v in sla.values())
        unverified = any(isinstance(v,dict) and v.get('status') not in {'met','breached'} for v in sla.values())
        return {'status':'fail' if failed else 'needs_review' if unverified else 'pass', 'missing':[], 'sla':sla}
    if item['key'] in {'restore_validation','recovery_drill'}:
        if timestamp(data['restore_finished']) < timestamp(data['restore_started']):
            raise ValueError('恢复结束时间早于开始时间')
        if not data.get('isolated_target'):
            raise ValueError('恢复验证须使用隔离目标')
    return {'status':'pass','missing':[], 'reason':'结构化证据完整且已复核' if workflow else '台账必填字段完整'}


def run_inspection(store, actor, asset_id, catalog, instance, factory=None):
    asset = store.get(actor, asset_id)
    if asset['kind'] != 'asset':
        raise ValueError('目标不是数据库资产')
    results = collect(instance, asset['payload'], factory)
    if factory is None:
        from .host import collect_host
        results.update(collect_host(instance,asset['payload']))
    history = store.list(actor,asset_id=asset_id)
    expected = asset['payload'].get('approved_parameters') or {}
    actual = {}
    for row in results.get('parameters',{}).get('rows',[]):
        name = row.get('name') or row.get('variable_name')
        if name:
            actual[str(name).lower()] = row.get('value_in_use',row.get('value',row.get('variable_value')))
    if expected:
        mismatches = []
        unknown = []
        for name,value in expected.items():
            observed = actual.get(name.lower(),'[MISSING]')
            if '[REDACTED]' in str(observed) or '[REDACTED]' in str(value) or observed=='[MISSING]':
                unknown.append(name)
            elif str(observed)!=str(value):
                mismatches.append({'name':name,'expected':value,'observed':observed})
        results['parameter_drift'] = {'status':'collected','mismatches':mismatches,'unknown':unknown,'provenance':'approved_parameter_comparison'}
    prior = next((r for r in history if r['kind']=='inspection' and r['item_id']=='run-evidence'),None)
    if prior:
        for key,source in (('prior_storage_snapshot','storage'),('prior_backup_snapshot','backups')):
            previous = prior['payload'].get('source_results',{}).get(source)
            if previous:
                results[key] = {**previous,'observed_at':prior['updated_at'],'provenance':'prior_immutable_snapshot'}
        results['monitor_heartbeat'] = {'status':'collected','previous_at':prior['updated_at'],
                                       'current_at':utcnow(),'provenance':'persisted_collection_history'}
    items_by_id = all_items(catalog)
    baseline_approved = any(r['kind']=='workflow' and items_by_id.get(r['item_id'],{}).get('key')=='asset_review'
                            and r['payload'].get('state')=='closed' and r['payload'].get('asset_version')==asset['version']
                            and r['payload'].get('details',{}).get('reconciliation_result')=='pass' for r in history)
    for record in history:
        if record['kind']!='workflow' or record['payload'].get('state')!='closed':
            continue
        item = items_by_id.get(record['item_id'],{})
        details = record['payload'].get('details',{})
        if item.get('key') in {'restore_validation','recovery_drill'}:
            results['restore_validation'] = {'status':'collected','record_id':record['id'],'provenance':'independently_reviewed_drill'}
            if details.get('backup_sha256'):
                results['backup_checksum'] = {'status':'collected','sha256':details['backup_sha256'],'record_id':record['id'],'provenance':'reviewed_backup_evidence'}
        if item.get('key') == 'parameter_change' and details.get('workload_reference'):
            results['workload_reference'] = {'status':'collected','reference':details['workload_reference'],'provenance':'reviewed_workload'}
        if item.get('key') == 'index_change':
            results['query_plan_review'] = {'status':'collected','reference':record['id'],'provenance':'reviewed_index_plan'}
    incidents = [r['id'] for r in history if r['kind']=='workflow' and r['payload'].get('detected_at')]
    maintenance = [r['id'] for r in history if r['kind']=='workflow' and r['payload'].get('state')=='closed']
    if incidents:
        results['incident_history'] = {'status':'collected','record_ids':incidents,'provenance':'service_history'}
    if maintenance:
        results['maintenance_history'] = {'status':'collected','record_ids':maintenance,'provenance':'reviewed_service_history'}
    run_id = hashlib.sha256((asset_id+utcnow()).encode()).hexdigest()[:24]
    saved = []
    store.save(actor,'inspection',{'run_id':run_id,'status':'needs_review',
               'source_results':results,'provenance':'database_metadata','collected_at':utcnow()},
               asset_id=asset_id,item_id='run-evidence')
    for item in all_items(catalog).values():
        if item['kind'] != 'inspection':
            continue
        outcome = assess(item['key'], results, {**asset['payload'],'baseline_approved':baseline_approved})
        body = {'run_id':run_id,**outcome,'provenance':'database_metadata',
                'source_reference':run_id,'collected_at':utcnow()}
        saved.append(store.save(actor,'inspection',body,asset_id=asset_id,item_id=item['id']))
    problems = [r for r in saved if r['payload']['status'] in {'fail','error','blocked'}]
    if problems:
        database_ticket = {'title':'巡检异常待研判','state':'draft','detected_at':utcnow(),
                           'priority':'NORMAL','run_id':run_id,'details':{'finding_reference':run_id},
                           'reason':'存在采集失败或安全阻断，需研判影响及故障等级'}
        store.save(actor,'workflow',database_ticket,asset_id=asset_id,item_id='inspection-alert')
    return {'run_id':run_id,'records':len(saved),'results':results,'alerts':len(problems)}


def overview(store, actor, catalog, *, period='month', anchor=None):
    start,end = period_bounds(period,anchor)
    assets = store.list(actor,'asset')
    records = store.list(actor)
    items = all_items(catalog)
    latest = {}
    for record in records:
        if record['kind'] in {'evidence','inspection'} and start <= timestamp(record['updated_at']) < end:
            latest.setdefault((record['asset_id'],record['item_id']),record)
    rows = []
    review_items = {item['id'] for item in items.values() if item['key']=='asset_review'}
    for asset in assets:
        for item in items.values():
            record = latest.get((asset['id'],item['id']))
            if item['kind'] == 'asset':
                result = evidence_result(item, asset['payload'])
                reviewed = any(r['kind']=='workflow' and r['asset_id']==asset['id']
                               and r['item_id'] in review_items and r['payload'].get('state')=='closed'
                               and r['payload'].get('asset_version')==asset['version']
                               and r['payload'].get('details',{}).get('reconciliation_result')=='pass'
                               for r in records)
                if result['status']=='pass' and not reviewed:
                    result = {'status':'needs_review','missing':[],'reason':'字段完整，等待当前台账版本的独立盘点复核'}
            elif record:
                result = record['payload'] if record['payload'].get('asset_version')==asset['version'] else {'status':'not_collected','missing':[], 'reason':'台账版本已变更，须重采或复核'}
            else:
                result = {'status':'not_collected','missing':item.get('fields',[]),'reason':'周期内没有验收证据'}
            rows.append({'asset_id':asset['id'],'asset_name':asset['payload']['asset_name'],
                         'item_id':item['id'],'title':item['title'],'module_id':item['module_id'],
                         'requirement_id':item['requirement_id'],'status':result['status'],
                         'reason':result.get('reason',''),'missing':result.get('missing',[]),
                         'evidence_id':record['id'] if record else None,
                         'source':record['payload'].get('provenance') if record else 'asset_register'})
    counts = {status:sum(row['status']==status for row in rows) for status in STATUS_NAMES}
    inspection_ids = {item['id'] for item in items.values() if item['kind']=='inspection'}
    inspection_rows = [row for row in rows if row['item_id'] in inspection_ids]
    score_ready = bool(inspection_rows) and all(row['status'] in {'pass','fail','not_applicable'} for row in inspection_rows)
    scored = [row for row in inspection_rows if row['status']!='not_applicable']
    health_score = round(sum(row['status']=='pass' for row in scored)/len(scored)*100,1) if score_ready and scored else None
    gate_ids = {item['id'] for item in items.values() if item['key'] in {'file_integrity','backup_integrity','file_status'}}
    safety_blocked = any(row['item_id'] in gate_ids and row['status'] in {'fail','blocked','error'} for row in rows)
    if safety_blocked:
        health_score = None
    incident = []
    response_sla = []
    for record in records:
        data = record['payload']
        if record['kind']=='workflow' and data.get('detected_at'):
            incident.append({'id':record['id'],**sla_result(data)})
        if record['kind']=='workflow' and data.get('module_id') in {'M1','M3','M4','M6'} and data.get('provenance')!='periodic_service_plan':
            if not data.get('requested_at') or not data.get('request_reference'):
                response_sla.append({'id':record['id'],'status':'request_time_unverified'})
                continue
            asset = next((a for a in assets if a['id']==record['asset_id']),None)
            calendar = asset['payload'].get('customer_calendar') if asset else None
            try:
                deadline = business_deadline(data['requested_at'],240,calendar)
                actual = data.get('responded_at')
                state = 'met' if actual and timestamp(actual)<=timestamp(deadline) else 'breached' if actual else 'pending'
                response_sla.append({'id':record['id'],'deadline':deadline,'actual':actual,'status':state})
            except ValueError:
                response_sla.append({'id':record['id'],'status':'calendar_unconfigured'})
    return {'period':period,'period_start':start.isoformat(),'period_end':end.isoformat(),
            'asset_count':len(assets),'required_checks':len(rows),'counts':counts,'rows':rows,
            'health_score':health_score,'safety_gate':'blocked' if safety_blocked else 'pending' if not score_ready else 'assessed',
            'health_score_policy':'完整覆盖后按有效检查项等权评分，数据安全门槛失败时停止评分',
            'completion_rate':round(counts['pass']/len(rows)*100,2) if rows else None,
            'risk_discovery_rate':None,'risk_discovery_reason':'需要独立已知风险基准集才能计算发现率',
            'asset_accuracy_rate':None,'asset_accuracy_reason':'准确率需独立盘点样本及差异核对证据',
            'incident_sla':incident,'response_sla':response_sla,'source_sha256':catalog['source_sha256'],
            'equipment_scope':catalog['equipment'],'generated_at':utcnow(),
            'asset_register':[r['payload'] for r in assets],
            'asset_change_history':{r['id']:store.asset_history(actor,r['id']) for r in assets},
            'service_evidence':[{'item_id':r['item_id'],'asset_id':r['asset_id'],
                                 'created_at':r['created_at'],'details':r['payload'].get('details',{}),
                                 'state':r['payload'].get('state'), 'id':r['id']}
                                for r in records if r['kind'] in {'evidence','workflow'}
                                and start <= timestamp(r['updated_at']) < end]}
