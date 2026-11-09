# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Bounded JDBC subprocess collection for explicit Oracle and DM cluster members."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import json
import os
import re
import subprocess
import sys
import time
from modules.core.paths import PROJECT_ROOT
from modules.core.data_security import redact
from modules.driver_registry import db_family
from .model import numeric


def queries(kind):
    from modules.monitor.screen_metrics import SCREEN_SQLS
    family = 'oracle' if kind.startswith('oracle_') else 'dm'
    result = {key: SCREEN_SQLS[family][key] for key in ('stat','tbs','repl','locks')}
    if family == 'oracle':
        result.update({
            'identity': SCREEN_SQLS['oracle']['identity'].replace('SELECT database_role','SELECT dbid, database_role'),
            'instance': "SELECT instance_name,host_name,version,status,instance_number FROM v$instance",
            'sessions': "SELECT COUNT(*) AS sessions FROM v$session WHERE type='USER'",
            'parameters': "SELECT name,value FROM v$parameter WHERE name IN ('cluster_database','db_block_size','compatible','open_cursors','processes')",
        })
        if kind == 'oracle_rac':
            result.pop('repl')
            result['members'] = 'SELECT inst_id,instance_name,host_name,status FROM gv$instance'
        else:
            result['destinations'] = SCREEN_SQLS['oracle']['dest'].replace('db_unique_name, status', 'db_unique_name, status, error')
    else:
        result.update({
            'identity': 'SELECT * FROM V$DATABASE',
            'instance': 'SELECT * FROM V$INSTANCE',
            'version': 'SELECT BANNER FROM V$VERSION',
            'sessions': 'SELECT COUNT(*) AS sessions FROM V$SESSIONS',
            'parameters': "SELECT PARA_NAME,PARA_VALUE FROM V$DM_INI WHERE PARA_NAME IN ('MAL_INI','ARCH_INI','DW_INACTIVE_INTERVAL','DW_ERROR_TIME','MAX_SESSIONS')",
            'mal': 'SELECT * FROM V$DM_MAL_INI',
        })
        if kind == 'dm_dsc':
            result.pop('repl')
            result['members'] = 'SELECT * FROM V$DSC_EP_INFO'
        else:
            result['repl'] = 'SELECT * FROM V$RAPPLY_STAT'
            result['replay_system'] = 'SELECT * FROM V$RAPPLY_SYS'
            result['replay_queue'] = 'SELECT COUNT(*) AS pending_tasks FROM V$RAPPLY_LOG_TASK'
            result['redo'] = 'SELECT * FROM V$RLOG'
            result['watcher'] = 'SELECT * FROM V$DMWATCHER'
            result['destinations'] = 'SELECT * FROM V$ARCH_STATUS'
            result['archive_config'] = 'SELECT * FROM V$DM_ARCH_INI'
    return result


def role_of(value):
    text = str(value or '').strip().upper()
    if 'STANDBY' in text or text in {'备库','备机'}:
        return 'standby'
    if 'PRIMARY' in text or text in {'主库','主机'}:
        return 'primary'
    return 'unknown'


def normalize(instance, kind, result, previous=None):
    from modules.monitor.screen_metrics import _parse_dg_lag
    now = time.time()
    states = {}
    data = {}
    for key,value in result.items():
        rows = [{str(k).lower():v for k,v in row.items()} for row in value.get('rows',[])[:200]]
        data[key] = rows
        states[key] = {'status':'error' if value.get('error') else 'collected' if rows else 'empty',
                       'rows':len(rows),'error':value.get('error')}
    identity = (data.get('identity') or [{}])[0]
    inst = (data.get('instance') or [{}])[0]
    family = db_family(instance['db_type'])
    role = role_of(identity.get('database_role') or inst.get('mode$'))
    database = identity.get('db_unique_name') or identity.get('name')
    instance_name = inst.get('instance_name') or inst.get('name')
    host = inst.get('host_name') or inst.get('host')
    unique = '|'.join(str(value or '') for value in (database,host,instance_name)) if database and instance_name else None
    lag = None
    transport_lag = None
    apply_lag = None
    for row in data.get('repl',[]):
        if family == 'dm':
            receive = numeric(row.get('pkg_receive_delay'))
            apply = numeric(row.get('pkg_apply_delay'))
            if receive is not None and receive >= 0:
                transport_lag = max(transport_lag or 0,receive/1000000)
            if apply is not None and apply >= 0:
                apply_lag = max(apply_lag or 0,apply/1000000)
        else:
            candidate = _parse_dg_lag(row.get('value'))
            if candidate is not None:
                if 'transport' in str(row.get('name')):
                    transport_lag = candidate
                elif 'apply' in str(row.get('name')):
                    apply_lag = candidate
    if family == 'dm' and transport_lag is not None and apply_lag is not None:
        lag = transport_lag+apply_lag
    elif family == 'oracle':
        observed_lags = [value for value in (transport_lag,apply_lag) if value is not None]
        lag = max(observed_lags) if observed_lags else None
    parameters = {str(row.get('name') or row.get('para_name')):row.get('value',row.get('para_value'))
                  for row in data.get('parameters',[])}
    counters = {str(row.get('name') or '').lower():numeric(row.get('value')) for row in data.get('stat',[])}
    execute = counters.get('execute count') if family=='oracle' else counters.get('sql executed count')
    commit = counters.get('user commits') if family=='oracle' else counters.get('transaction commit count')
    rollback = counters.get('user rollbacks') if family=='oracle' else counters.get('transaction rollback count')
    totals = {'executions':execute,'transactions':commit+rollback if commit is not None and rollback is not None else None}
    rates = {'qps':None,'tps':None}
    if previous and previous.get('identity') == unique:
        elapsed = now-previous.get('ts',now)
        if elapsed>0:
            for rate,key in (('qps','executions'),('tps','transactions')):
                old = previous.get('counters',{}).get(key)
                current = totals[key]
                if old is not None and current is not None and current>=old:
                    rates[rate] = round((current-old)/elapsed,3)
    version = inst.get('version')
    if not version and data.get('version'):
        version = ' / '.join(str(row.get('banner') or '') for row in data['version'])
    errors = {key:value['error'] for key,value in states.items() if value['status']=='error'}
    available = bool(data.get('instance')) and not states.get('instance',{}).get('error')
    return {'instance_id':instance['id'],'name':instance.get('name',instance['id']),'ts':now,
            'available':available,'identity':unique,'database_identity':database,'instance_name':instance_name,
            'role':role,'version':version,'database_id':identity.get('dbid'),
            'replay_pending_tasks':numeric((data.get('replay_queue') or [{}])[0].get('pending_tasks')),'instance_status':inst.get('status') or inst.get('status$'),
            'repl_lag_s':lag,'transport_lag_s':transport_lag,'apply_lag_s':apply_lag,'sessions':numeric((data.get('sessions') or [{}])[0].get('sessions')),
            'qps':rates['qps'],'tps':rates['tps'],'counters':totals,'parameters':parameters,
            'tablespaces':data.get('tbs',[]),'archive_destinations':data.get('destinations',[]),
            'discovered_members':data.get('members',[]),
            'replication_primary_names':[str(row['primary_name']) for row in data.get('replay_system',[]) if row.get('primary_name')],
            'query_status':states,'evidence':data,'errors':errors}


def collect_node(instance, kind, previous=None):
    from modules.ssh import resolve_db_endpoint
    from modules.core.data_security import assert_readonly_sql
    sqls = queries(kind)
    for sql in sqls.values():
        assert_readonly_sql(sql)
    payload = {key:instance.get(key) for key in ('host','port','user','password','database','service_name',
                                               'sid','use_sid','sysdba','driver_version','jdbc_url')}
    payload['db_type'] = 'oracle_jdbc' if db_family(instance['db_type'])=='oracle' else 'dm'
    host,port,tunnel = resolve_db_endpoint(instance,log=lambda *args:None)
    payload.update(host=host,port=port,queries=list(sqls.values()))
    url = str(payload.get('jdbc_url') or '')
    if url.startswith('(') and payload['db_type']=='oracle_jdbc':
        url = 'jdbc:oracle:thin:@'+url
    if tunnel and url:
        # Avoid silently bypassing SSH with a complete, original remote URL.
        if payload['db_type']=='oracle_jdbc' and re.fullmatch(r'jdbc:oracle:thin:@//[^/:]+:\d+/[^;?]+',url):
            url = re.sub(r'(?<=@)//[^/:]+:\d+',f'//{host}:{port}',url)
        else:
            raise ValueError('SSH 集群节点请使用主机、端口和服务名连接，或使用单地址 Oracle EZConnect URL')
    payload['jdbc_url'] = url
    command = [sys.executable,'--jdbc-collect-cli'] if getattr(sys,'frozen',False) else [
        sys.executable,str(PROJECT_ROOT/'modules/monitor/jdbc_collect_cli.py')]
    environment = dict(os.environ,DBCheck_NO_GEVENT_PATCH='1')
    try:
        process = subprocess.run(command,input=json.dumps(payload),capture_output=True,text=True,
                                 timeout=35,env=environment)
        # Existing CLI prints the structured batch on its final JSON line.
        lines = [line for line in process.stdout.splitlines() if line.strip().startswith('{')]
        if process.returncode or not lines:
            raise ValueError('集群节点采集进程未返回有效结果')
        batch = json.loads(lines[-1])
        if not all('q'+str(index) in batch for index in range(len(sqls))):
            raise ValueError('集群节点采集结果不完整')
        mapped = {key:batch['q'+str(index)] for index,key in enumerate(sqls)}
        result = normalize(instance,kind,mapped,previous)
        return redact(result)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError('集群节点采集超过 35 秒') from error
