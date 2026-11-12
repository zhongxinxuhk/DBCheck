# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Bounded, fixed metadata collectors for the four customer database families.

Queries do not read application rows or execute maintenance. A collection is
reported separately from a verified finding. Missing privileges and unsupported
views stay explicit and never become a healthy zero.
"""



import hashlib
import re
import ssl
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from modules.core.data_security import assert_readonly_sql, redact

from modules.inspection.init_db import COMPATIBILITY_QUERIES as QUERIES

# Every item lists all required evidence sources. Host/log/hash evidence must
# come from the target host or a signed collection record, never this workstation.
SOURCES = {
 'availability': ('instance','monitor_heartbeat'),
 'instance_sessions': ('instance','sessions','client_connectivity'),
 'alerts': ('database_alert_log','cluster_alert_log'),
 'backup_jobs': ('backups','backup_job_log'),
 'capacity_cluster': ('storage','files','target_filesystems','cluster_nodes'),
 'file_integrity': ('target_file_hashes','target_file_permissions'),
 'statistics': ('statistics',), 'growth': ('storage','prior_storage_snapshot'),
 'health_assessment': ('instance','storage','backups','objects','waits','target_cpu'),
 'process_health': ('instance','target_processes','target_network_services'),
 'deep_logs': ('database_alert_log','target_core_dumps','target_mail_alerts'),
 'file_status': ('files','archive','target_file_hashes'), 'objects': ('objects','indexes'),
 'resource_parameters': ('parameters','sessions','storage','target_filesystems'),
 'performance': ('waits','cache','target_cpu','shared_pool'),
 'backup_trend': ('backups','prior_backup_snapshot','restore_validation'),
 'backup_integrity': ('backups','backup_job_log','backup_checksum'),
 'slow_sql': ('slow_sql',), 'indexes': ('indexes','index_usage','query_plan_review'),
 'tuning_parameters': ('parameters','workload_reference'),
 'operations_analysis': ('instance','sessions','storage','waits','incident_history','maintenance_history'),
}


def connect_secure(instance, asset):
    family = asset['db_type']
    if family not in QUERIES:
        raise ValueError('未支持的数据库类型，GBase 8a 与 8s 须分别登记')
    user = str(instance.get('user','')).upper()
    if user in {'SYS','SYSTEM','SYSDBA','SYSSSO','SYSAUDITOR','SA','ROOT'} or instance.get('sysdba'):
        raise ValueError('请配置专用最小权限巡检账号')
    if not asset.get('least_privilege_reference'):
        raise ValueError('缺少巡检账号授权清单及最小权限复核证据')
    transport = asset.get('transport') or asset.get('legacy_transport') or 'tls'
    if transport == 'verified_ssh_loopback':
        return connect_isolated(instance,asset)
    if transport != 'tls':
        raise ValueError('传输方式须为TLS或已验证SSH隧道')
    if family == 'sqlserver' and ('2005' in str(asset.get('version')) or str(asset.get('version','')).startswith('9.')):
        raise ValueError('SQL Server 2005 请明确配置已验证SSH隔离采集，禁止自动降低TLS')
    host = str(instance.get('host') or '')
    if not re.fullmatch(r'[A-Za-z0-9.:-]+', host):
        raise ValueError('数据库主机格式无效')
    port = int(instance['port'])
    if not 0 < port < 65536:
        raise ValueError('数据库端口无效')
    ca = Path(asset.get('tls_ca_file') or '')
    if family != 'sqlserver' and (not asset.get('tls_ca_file') or not ca.exists()):
        raise ValueError('缺少可信 CA 或钱包配置')
    if family == 'gbase8a':
        import pymysql
        if not ca.is_file():
            raise ValueError('GBase8a须配置明确的可信CA文件')
        context = ssl.create_default_context(cafile=str(ca))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = pymysql.Connection(host=host,port=port,user=instance['user'],password=instance['password'],
                                       database='information_schema',ssl=context,connect_timeout=5,
                                       read_timeout=15,write_timeout=15,autocommit=False,local_infile=False,
                                       defer_connect=True)
        if not getattr(connection,'_ssl_required',False):
            connection.close()
            raise ValueError('当前MySQL协议驱动不能强制TLS，已在认证前阻断')
        connection.connect()
        return connection
    if family == 'sqlserver':
        import pyodbc
        def quote(value):
            return '{' + str(value).replace('}', '}}') + '}'
        connection = ('DRIVER={ODBC Driver 18 for SQL Server};SERVER=' + quote(f'{host},{port}') +
                      ';UID=' + quote(instance['user']) + ';PWD=' + quote(instance['password']) +
                      ';DATABASE=' + quote(instance.get('database') or 'master') +
                      ';Encrypt=yes;TrustServerCertificate=no;Connection Timeout=5;APP=DBPulse;')
        conn = pyodbc.connect(connection,timeout=5,autocommit=False)
        conn.timeout = 15
        return conn
    if family == 'dm':
        import dmPython
        if not ca.is_dir() or not (ca / 'ca-cert.pem').is_file():
            raise ValueError('DM8 需配置经验证的客户端证书目录')
        return dmPython.connect(user=instance['user'],password=instance['password'],server=host,port=port,
                                ssl_path=str(ca),login_timeout=5000,connection_timeout=15,
                                autoCommit=False,app_name='DBPulse-readonly')
    import oracledb
    from modules.db_types.compatibility import oracle11g_client
    oracle11g_client(oracledb,asset)
    service = str(instance.get('service_name') or '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', service):
        raise ValueError('Oracle 需明确且合法的服务名')
    if not oracledb.is_thin_mode():
        dn = str(asset.get('oracle_server_dn') or '')
        wallet = str(ca.resolve())
        if not dn or any(char in dn+wallet for char in '()\"\n\r'):
            raise ValueError('Oracle Thick TCPS需要明确的服务端证书DN及合法钱包路径')
        dsn = (f'(DESCRIPTION=(CONNECT_TIMEOUT=5)(TRANSPORT_CONNECT_TIMEOUT=5)'
               f'(ADDRESS=(PROTOCOL=TCPS)(HOST={host})(PORT={port}))'
               f'(CONNECT_DATA=(SERVICE_NAME={service}))'
               f'(SECURITY=(SSL_SERVER_DN_MATCH=YES)(SSL_SERVER_CERT_DN=\"{dn}\")'
               f'(MY_WALLET_DIRECTORY=\"{wallet}\")))')
    else:
        dsn = f'tcps://{host}:{port}/{service}'
    conn = oracledb.connect(user=instance['user'],password=instance['password'],
                           dsn=dsn,ssl_server_dn_match=True,wallet_location=str(ca),
                           tcp_connect_timeout=5,ssl_version=ssl.TLSVersion.TLSv1_2)
    conn.call_timeout = 15000
    return conn


def collect(instance, asset, factory=None, _worker=False):
    if factory is None and not _worker:
        from .driver_worker import isolated_collect
        return isolated_collect(instance,asset)
    family = asset['db_type']
    results = {}
    conn = None
    started = time.monotonic()
    try:
        conn = (factory or connect_secure)(instance, asset)
        if factory is None:
            from modules.inspection.sql_safety import guarded_connection
            conn = guarded_connection(conn, family)
        results['client_connectivity'] = {'status':'collected','provenance':'authenticated_driver_connection','collected_at':datetime.now(timezone.utc).isoformat()}
        if factory is None:
            privileges = conn.cursor()
            try:
                privileges.execute(QUERIES[family]['privileges'])
                roles = privileges.fetchmany(101)
                if family == 'sqlserver':
                    elevated = any(value not in (0,False) for row in roles for value in row)
                elif family == 'gbase8a':
                    elevated = bool(re.search(r'\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|CREATE|DROP|EXECUTE|FILE|SUPER|GRANT OPTION)\b',str(roles),re.I))
                else:
                    elevated = any(str(value).upper() in {'DBA','SYSDBA','RESOURCE','SYSOPER'} for row in roles for value in row)
                if elevated or not roles:
                    raise ValueError('账号权限超出只读范围或权限无法确认')
            finally:
                privileges.close()
        queries = dict(QUERIES[family])
        if family=='sqlserver' and ('2014' in str(asset.get('version')) or str(asset.get('version','')).startswith('12.')):
            queries['shared_pool'] = 'SELECT TOP 100 TYPE,PAGES_KB FROM SYS.DM_OS_MEMORY_CLERKS'
        for key, sql in queries.items():
            cursor = None
            try:
                if time.monotonic() - started > 90:
                    raise ValueError('本次采集已达到90秒预算')
                assert_readonly_sql(sql)
                cursor = conn.cursor()
                warning_count = len(getattr(conn, 'warnings', []))
                cursor.execute(sql)
                columns = [str(c[0]).lower() for c in cursor.description or []]
                rows = cursor.fetchmany(201)
                data = [dict(zip(columns,[v if isinstance(v,(int,float,bool,type(None))) else str(v)[:512] for v in row])) for row in rows[:200]]
                if key == 'version' and family == 'gbase8a' and not any('gbase' in str(value).lower() for row in data for value in row.values()):
                    raise ValueError('当前服务端未返回 GBase 身份证据，禁止按8a版本推断')
                warnings = getattr(conn, 'warnings', [])[warning_count:]
                if warnings: raise ValueError('; '.join(warnings))
                truncated = len(rows)>200
                while len(json.dumps(data,ensure_ascii=False).encode()) > 30000:
                    data.pop()
                    truncated = True
                results[key] = {'status':'collected','rows':redact(data),'truncated':truncated,
                                'query_sha256':hashlib.sha256(sql.encode()).hexdigest(),
                                'collected_at':datetime.now(timezone.utc).isoformat(),
                                'scope':'current_connection','provenance':'database_metadata'}
            except Exception as error:
                results[key] = {'status':'error','reason':_safe_error(error,instance)}
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                if cursor is not None:
                    cursor.close()
        if factory is None and time.monotonic()-started<60:
            try:
                from modules.inspection.index_health import get_index_health
                guarded = _MetadataConnection(conn,started+90)
                analysis = get_index_health('mysql' if family=='gbase8a' else family,guarded)
                if guarded.failures or analysis is None:
                    results['index_usage'] = {'status':'error','reason':'索引分析存在缺权限、不兼容或预算不足','details':guarded.failures}
                else:
                    results['index_usage'] = {'status':'collected','analysis':redact(analysis),
                                              'provenance':'existing_index_health_with_readonly_guard'}
            except Exception:
                results['index_usage'] = {'status':'error','reason':'索引分析未完成'}
    except Exception as error:
        return {'connection':{'status':'blocked','reason':_safe_error(error,instance)}}
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
    return results


def assess(key, collected, policy=None):
    policy = policy or {}
    required = SOURCES[key]
    missing = [name for name in required if name not in collected]
    errors = [name for name in required if collected.get(name,{}).get('status') in {'error','blocked'}]
    if collected.get('connection',{}).get('status') == 'blocked':
        return {'status':'blocked','missing':list(required),'reason':collected['connection']['reason']}
    if key=='file_integrity' and any(row.get('status')=='fail' for row in collected.get('target_file_hashes',{}).get('rows',[])):
        return {'status':'fail','missing':missing,'reason':'核心文件与登记基线不一致，须复核与整改'}
    if key in {'resource_parameters','tuning_parameters'}:
        drift = collected.get('parameter_drift',{})
        if drift.get('mismatches'):
            return {'status':'fail','missing':missing,'reason':'运行参数与登记基线不一致'}
    if errors:
        return {'status':'error','missing':missing,'reason':'采集失败 '+', '.join(errors)}
    if missing:
        return {'status':'not_collected','missing':missing,'reason':'缺少独立证据 '+', '.join(missing)}
    if any(collected[name].get('truncated') for name in required):
        return {'status':'needs_review','missing':[],'reason':'结果截断，需要缩小范围后复核'}
    if key == 'file_integrity':
        rows = collected['target_file_hashes'].get('rows',[])
        if any(row.get('status')=='fail' for row in rows):
            return {'status':'fail','missing':[],'reason':'核心文件哈希或权限与批准基线不一致'}
        if rows and all(row.get('status')=='pass' for row in rows):
            return _approved_result('核心文件哈希与权限匹配批准基线',policy)
    if key == 'instance_sessions':
        rows = collected['instance'].get('rows',[])
        states = [str(row.get('status',row.get('status$',''))).upper() for row in rows]
        if any(value in {'CLOSED','FAILED','ABNORMAL'} for value in states):
            return {'status':'fail','missing':[],'reason':'实例状态异常'}
        if rows and collected['client_connectivity'].get('status')=='collected':
            return _approved_result('客户端连接与实例查询成功，会话数已采集',policy)
    # Metadata collection alone cannot prove file integrity, backup recoverability,
    # SQL improvement or index effectiveness. Those require measured baselines.
    if key == 'statistics' and policy.get('statistics_max_age_days') and policy.get('database_timezone'):
        from zoneinfo import ZoneInfo
        rows = collected['statistics'].get('rows',[])
        cutoff = datetime.now(timezone.utc).timestamp() - int(policy['statistics_max_age_days'])*86400
        unknown = False
        for row in rows:
            value = row.get('last_analyzed')
            if not value:
                unknown = True
                continue
            observed = datetime.fromisoformat(str(value))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=ZoneInfo(policy['database_timezone']))
            if observed.timestamp()<cutoff or str(row.get('stale_stats','')).upper()=='YES':
                return {'status':'fail','missing':[],'reason':'统计信息超过批准的时效基线'}
        if rows and not unknown:
            return _approved_result('已采集对象的统计信息满足时效基线',policy)
    if key == 'statistics':
        return {'status':'needs_review','missing':[],'reason':'已采集统计信息，需按批准的时效基线复核'}
    return {'status':'needs_review','missing':[],'reason':'已采集元数据，等待阈值、基线或复核证据'}


def _approved_result(reason, policy):
    if not policy.get('baseline_approved'):
        return {'status':'needs_review','missing':[],'reason':'已完成采集，当前台账与基线等待独立批准'}
    return {'status':'pass','missing':[],'reason':reason}


class _TunnelConnection:
    def __init__(self, connection, tunnel):
        self.connection = connection
        self.tunnel = tunnel

    def __getattr__(self, name):
        return getattr(self.connection,name)

    def close(self):
        try:
            self.connection.close()
        finally:
            self.tunnel.close()


def connect_isolated(instance, asset):
    """Encryption remains end-to-end between verified SSH endpoints, no downgrade."""
    if not instance.get('ssh_enabled'):
        raise ValueError('隔离采集须启用SSH')
    if asset.get('ssh_host_role') != 'database_host' or instance.get('ssh_host') != instance.get('host'):
        raise ValueError('隔离采集须由数据库主机自身的已验证SSH端点承载')
    from modules.ssh import SSHTunnel
    tunnel = SSHTunnel(ssh_host=instance['ssh_host'],ssh_port=int(instance.get('ssh_port',22)),
                       ssh_user=instance['ssh_user'],ssh_password=instance.get('ssh_password'),
                       ssh_key=instance.get('ssh_key_file'),remote_host='127.0.0.1',remote_port=int(instance['port']))
    try:
        tunnel.__enter__()
        family=asset['db_type']
        local={**instance,'port':tunnel.local_port}
        if family=='dm':
            from modules.db_types.compatibility import jdbc_dm
            connection=jdbc_dm(local)
        elif family=='gbase8a':
            import pymysql
            connection=pymysql.connect(host='127.0.0.1',port=tunnel.local_port,user=instance['user'],
                                       password=instance['password'],database='information_schema',
                                       connect_timeout=5,read_timeout=15,write_timeout=15,
                                       local_infile=False,autocommit=False)
        elif family=='sqlserver':
            import pytds
            version=str(asset.get('version',''))
            tds_version=pytds.tds_base.TDS72 if '2005' in version or version.startswith('9.') else pytds.tds_base.TDS73 if '2008' in version or version.startswith('10.') else pytds.tds_base.TDS74
            connection=pytds.connect(server='127.0.0.1',port=tunnel.local_port,
                                     user=instance['user'],password=instance['password'],
                                     database=instance.get('database') or 'master',tds_version=tds_version,
                                     login_timeout=5,timeout=15,autocommit=False,use_mars=False)
        else:
            raise ValueError('此数据库类型请使用验证证书的TLS采集')
        return _TunnelConnection(connection,tunnel)
    except Exception:
        tunnel.close()
        raise


class _MetadataCursor:
    def __init__(self, cursor, failures, deadline):
        self.cursor = cursor
        self.failures = failures
        self.deadline = deadline

    def __getattr__(self, name):
        return getattr(self.cursor,name)

    def execute(self, sql, *parameters):
        try:
            if time.monotonic()>self.deadline:
                raise ValueError('元数据分析预算耗尽')
            assert_readonly_sql(sql)
            relations = re.findall(r'\b(?:FROM|JOIN)\s+([A-Za-z_$][A-Za-z0-9_$.]*)',sql,re.I)
            for relation in relations:
                if not relation.upper().startswith(('SYS.','INFORMATION_SCHEMA.','PERFORMANCE_SCHEMA.','DBA_','ALL_','USER_','V$','GV$','DUAL')):
                    raise ValueError('分析仅允许系统元数据关系')
            self.cursor.execute(sql,*parameters)
            return self
        except Exception as error:
            self.failures.append(redact(str(error),mask_identity=True)[:160])
            raise

    def fetchall(self):
        rows = self.cursor.fetchmany(201)
        if len(rows)>200:
            self.failures.append('分析结果超过200行预算')
            raise ValueError('结果超出安全预算，需缩小分析范围')
        return rows

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        self.cursor.close()


class _MetadataConnection:
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline
        self.failures = []

    def __getattr__(self, name):
        return getattr(self.connection,name)

    def cursor(self,*args,**kwargs):
        return _MetadataCursor(self.connection.cursor(*args,**kwargs),self.failures,self.deadline)

    def commit(self):
        raise ValueError('元数据分析禁止提交事务')


def _safe_error(error, instance):
    text = str(error)
    for key in ('password','ssh_password','ssh_key_password'):
        if instance.get(key):
            text = text.replace(str(instance[key]),'[REDACTED]')
    return redact(text,mask_identity=True)[:240]
