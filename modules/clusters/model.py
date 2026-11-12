# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Cluster architecture validation and evidence-based health aggregation."""


import math
import time
from modules.driver_registry import db_family

KINDS = {
    'oracle_rac': {'label': 'Oracle RAC', 'family': 'oracle'},
    'oracle_dataguard': {'label': 'Oracle Data Guard', 'family': 'oracle'},
    'dm_dsc': {'label': '达梦 DMDSC', 'family': 'dm'},
    'dm_guard': {'label': '达梦数据守护', 'family': 'dm'},
}
ROLES = {'member', 'primary', 'standby', 'unknown'}


def bounded_integer(value, low, high, label):
    if isinstance(value, bool):
        raise ValueError(label+'格式无效')
    try:
        number = int(value)
    except (ValueError, TypeError) as error:
        raise ValueError(label+'格式无效') from error
    if str(number) != str(value).strip() or not low <= number <= high:
        raise ValueError(label+'超出允许范围')
    return number


def validate_spec(data, resolve):
    if not isinstance(data, dict):
        raise ValueError('集群配置须为对象')
    name = str(data.get('name') or '').strip()
    kind = data.get('kind')
    if not name or len(name) > 120 or kind not in KINDS:
        raise ValueError('请填写集群名称并选择受支持的架构')
    members = data.get('members')
    if not isinstance(members, list) or not 1 <= len(members) <= 32:
        raise ValueError('每个集群支持 1 至 32 个已登记成员，预期节点数至少为 2')
    seen = set()
    endpoints = set()
    normalized = []
    for member in members:
        if not isinstance(member, dict):
            raise ValueError('成员配置格式无效')
        instance_id = str(member.get('instance_id') or '')
        if not instance_id or instance_id in seen:
            raise ValueError('成员数据源不能为空或重复')
        instance = resolve(instance_id)
        if db_family(instance.get('db_type')) != KINDS[kind]['family']:
            raise ValueError('成员数据源与集群架构所属数据库类型不一致')
        endpoint = (str(instance.get('host') or '').lower(), instance.get('port'),
                    instance.get('service_name') or '', instance.get('database') or '', instance.get('jdbc_url') or '')
        if endpoint in endpoints:
            raise ValueError('同一个数据库连接端点被重复登记，请核对节点数据源')
        endpoints.add(endpoint)
        seen.add(instance_id)
        role = member.get('expected_role', 'member')
        if role not in ROLES:
            raise ValueError('成员角色格式无效')
        enabled = member.get('enabled', True)
        if not isinstance(enabled, bool):
            raise ValueError('成员启用标志须为布尔值')
        normalized.append({'instance_id': instance_id, 'expected_role': role, 'enabled': enabled})
    if not any(member['enabled'] for member in normalized):
        raise ValueError('至少启用一个成员')
    expected = bounded_integer(data.get('expected_nodes', max(2,len(normalized))),2,64,'预期节点数')
    if expected < len(normalized):
        raise ValueError('预期节点数不能小于登记成员数')
    service_id = str(data.get('service_instance_id') or '').strip()
    if service_id and db_family(resolve(service_id).get('db_type')) != KINDS[kind]['family']:
        raise ValueError('业务入口数据源与集群类型不一致')
    enabled = data.get('monitor_enabled', True)
    if not isinstance(enabled,bool):
        raise ValueError('监控启用标志须为布尔值')
    return {'name': name, 'kind': kind, 'members': normalized, 'expected_nodes': expected,
            'service_instance_id': service_id, 'monitor_enabled': enabled,
            'interval_seconds': bounded_integer(data.get('interval_seconds',30),15,3600,'采集周期'),
            'lag_alert_seconds': bounded_integer(data.get('lag_alert_seconds',30),1,86400,'复制延迟阈值')}


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):
        return None


def aggregate(record, nodes, service=None, now=None):
    now = time.time() if now is None else now
    spec = record['spec']
    issues = []

    def issue(code, level, message, instance_id=None):
        issues.append({'code':code,'level':level,'message':message,'instance_id':instance_id})

    enabled = [member for member in spec['members'] if member['enabled']]
    fresh = []
    identities = set()
    for member in enabled:
        iid = member['instance_id']
        node = nodes.get(iid)
        if not node or now-node.get('ts',0) > max(90,spec['interval_seconds']*3):
            issue('sample_missing','unknown','节点尚未采集或样本已过期',iid)
            continue
        if node.get('available') is not True:
            issue('node_unavailable','critical','节点连接或实例状态采集失败',iid)
            continue
        fresh.append(node)
        identity = node.get('identity')
        if not identity:
            issue('identity_missing','unknown','节点身份采集缺失',iid)
        elif identity in identities:
            issue('duplicate_identity','critical','多个数据源实际连接到同一实例，请核对 SID、VIP 或负载均衡入口',iid)
        else:
            identities.add(identity)
        role = node.get('role')
        if member['expected_role'] in {'primary','standby'} and role != member['expected_role']:
            issue('role_mismatch','warning','实际主备角色与登记角色不一致',iid)
        if node.get('errors'):
            issue('evidence_missing','unknown','部分集群视图不可用，请查看节点采集明细',iid)
        state = str(node.get('instance_status') or '').upper()
        if state and state not in {'OPEN','ACTIVE','STARTED'}:
            issue('instance_state','warning','实例当前状态为 '+state,iid)
        for archive in node.get('archive_destinations',[]):
            status = str(archive.get('status') or archive.get('arch_status') or '').upper()
            if archive.get('error') or status in {'ERROR','INVALID','FAILED','CLOSED'}:
                issue('archive_destination','critical','归档目的端状态异常',iid)
        if role == 'standby' and spec['kind'] in {'oracle_dataguard','dm_guard'}:
            lag = numeric(node.get('repl_lag_s'))
            if lag is None:
                issue('replication_unknown','unknown','备库复制延迟缺少可用证据',iid)
            elif lag > spec['lag_alert_seconds']:
                issue('replication_lag','critical','复制延迟超过配置阈值',iid)
    coverage = len(identities)
    if coverage < spec['expected_nodes']:
        issue('coverage_incomplete','unknown',f"预期 {spec['expected_nodes']} 个节点，取得 {coverage} 个独立实例身份")
    if spec['kind'] in {'oracle_rac','dm_dsc'}:
        if not any(len(node.get('discovered_members',[])) >= spec['expected_nodes'] for node in fresh):
            issue('topology_unconfirmed','unknown','集群成员视图尚未证实预期数量的共享集群节点')
    if spec['kind'] in {'oracle_dataguard','dm_guard'}:
        roles = [node.get('role') for node in fresh]
        if len(fresh) != len(enabled) or any(role not in {'primary','standby'} for role in roles):
            issue('roles_unknown','unknown','主备角色证据不完整')
        else:
            primaries = {node.get('database_identity') or node['identity'] for node in fresh if node.get('role')=='primary'}
            if len(primaries) != 1:
                issue('primary_cardinality','critical','检测到的主库数量不符合单主架构，需要复核角色及隔离状态')
            if 'standby' not in roles:
                issue('standby_missing','unknown','未采集到备库角色')
            primary_nodes = [node for node in fresh if node.get('role')=='primary']
            primary_names = {str(node.get('instance_name') or '') for node in primary_nodes}
            destinations = {str(dest.get('db_unique_name') or dest.get('arch_dest') or '')
                            for node in primary_nodes for dest in node.get('archive_destinations',[])}
            for standby in (node for node in fresh if node.get('role')=='standby'):
                peer_names = {str(standby.get('database_identity') or ''),str(standby.get('instance_name') or '')}
                identified_primary = set(standby.get('replication_primary_names',[]))
                if not (destinations & peer_names or identified_primary & primary_names):
                    issue('replication_link_unknown','unknown','尚未取得主库与该备库的复制关联证据',standby['instance_id'])

    else:
        databases = {node.get('database_identity') for node in fresh if node.get('database_identity')}
        if len(databases)>1:
            issue('database_mismatch','critical','成员返回了不同的数据库身份')
    if spec['kind']=='oracle_rac' and len({node.get('database_id') for node in fresh if node.get('database_id')})>1:
        issue('database_id_mismatch','critical','RAC 成员返回不同的 DBID')
    versions = {node.get('version') for node in fresh if node.get('version')}
    if len(versions)>1:
        issue('version_drift','warning','成员数据库版本存在差异')
    if len(fresh)>1:
        shared = set.intersection(*(set(node.get('parameters',{})) for node in fresh))
        drift = [key for key in sorted(shared) if len({str(node['parameters'][key]) for node in fresh})>1]
        if drift:
            issue('parameter_drift','warning','节点参数存在差异，请复核 '+', '.join(drift))
    if spec['service_instance_id']:
        if not service or service.get('available') is not True:
            issue('service_unavailable','critical','业务服务入口连接失败或缺少采集结果')
    levels = {row['level'] for row in issues}
    status = 'healthy'
    if 'critical' in levels:
        status = 'critical'
    elif 'unknown' in levels:
        status = 'incomplete'
    elif 'warning' in levels:
        status = 'warning'
    return {'ts':now,'config_version':record['version'],'status':status,'expected_nodes':spec['expected_nodes'],
            'registered_nodes':len(spec['members']),'enabled_nodes':len(enabled),'online_nodes':len(fresh),
            'observed_nodes':coverage,'coverage_pct':round(coverage/spec['expected_nodes']*100,1),
            'nodes':nodes,'service':service,'issues':issues}


def transition_events(previous, current):
    if not previous:
        return [{'kind':'monitor_started','message':'首次完成集群采集'}]
    events = []
    if previous.get('status') != current['status']:
        events.append({'kind':'health_changed','before':previous.get('status'),'after':current['status']})
    for iid,node in current['nodes'].items():
        old = previous.get('nodes',{}).get(iid)
        if old and old.get('role') and node.get('role') and old['role'] != node['role']:
            events.append({'kind':'role_changed','instance_id':iid,'before':old['role'],'after':node['role']})
        if old and old.get('available') != node.get('available'):
            events.append({'kind':'availability_changed','instance_id':iid,'before':old.get('available'),'after':node.get('available')})
    old_service = previous.get('service') or {}
    service = current.get('service') or {}
    if old_service.get('identity') and service.get('identity') and old_service['identity'] != service['identity']:
        events.append({'kind':'service_target_changed','before':old_service['identity'],'after':service['identity']})
    old_codes = {(row['code'],row.get('instance_id')) for row in previous.get('issues',[])}
    new_codes = {(row['code'],row.get('instance_id')) for row in current['issues']}
    for code,iid in sorted(new_codes-old_codes,key=str):
        events.append({'kind':'alert_opened','code':code,'instance_id':iid})
    for code,iid in sorted(old_codes-new_codes,key=str):
        events.append({'kind':'alert_resolved','code':code,'instance_id':iid})
    return events
