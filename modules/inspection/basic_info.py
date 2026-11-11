# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""数据库核心属性采集（16 项台账字段 + 版本/补丁）。

背景
----
专项业务巡检报告此前只有 SQL 查询结果，缺少《数据库基本信息表》要求的 16 项
核心属性，尤其是「数据库版本及补丁号」从未真正读取（只取到版本号，补丁级别
为空）。本模块负责：

1. 从数据库实时读取：数据库名、实例名、字符集、库大小、归档路径/模式、
   版本与补丁号、存储模式、备份策略、容灾状态、集群属性；
2. 从巡检上下文与宿主机采集：OS 版本、主机型号；
3. 从资产台账镜像 ``data/basic_info.json`` 读取/覆盖：配置项编号、设备位置、
   存储平台、备份策略、容灾状态、集群属性等静态登记项；
4. 保证 16 个字段**始终有值**（采集不到时显式标注「未采集 / 未登记」），
   避免报告缺行，也避免把「没采到」伪装成「没有」。

设计约束
--------
- 所有 SQL 单条失败只影响该字段，不影响巡检主流程；
- 不猜测：多候选无法唯一确定时如实写「未采集」；
- 与库型方言解耦：按 db_type 分派，未覆盖的库型回退到上下文与台账。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import json
import os
import platform
import threading
from pathlib import Path

from modules.core import paths

# 字段顺序即报告中的展示顺序，与《数据库基本信息表》(D-04) 一致
FIELD_ORDER = [
    ('configuration_id', '配置项编号'),
    ('db_name', '数据库名称'),
    ('instance_name', '实例名'),
    ('charset', '字符集'),
    ('db_size', '数据库文件大小'),
    ('archive_path', '归档路径'),
    ('archive_mode', '归档模式'),
    ('host_model', '主机型号'),
    ('location', '设备位置'),
    ('os_version', 'OS 版本'),
    ('storage_platform', '存储平台'),
    ('version_patch', '数据库版本及补丁号'),
    ('storage_mode', '存储模式'),
    ('backup_policy', '备份策略'),
    ('dr_status', '容灾状态'),
    ('cluster_attrs', '集群属性'),
]

LABELS_EN = {
    'configuration_id': 'Configuration ID',
    'db_name': 'Database Name',
    'instance_name': 'Instance Name',
    'charset': 'Character Set',
    'db_size': 'Database Size',
    'archive_path': 'Archive Path',
    'archive_mode': 'Archive Mode',
    'host_model': 'Host Model',
    'location': 'Location',
    'os_version': 'OS Version',
    'storage_platform': 'Storage Platform',
    'version_patch': 'Version / Patch Level',
    'storage_mode': 'Storage Mode',
    'backup_policy': 'Backup Policy',
    'dr_status': 'DR Status',
    'cluster_attrs': 'Cluster Attributes',
}

# 资产台账镜像：静态登记项（设备位置、存储平台等）无法从数据库读到，
# 但报告必须体现。放在 data/ 下，随实例迁移，不进版本库。
OVERRIDE_FILE = Path(paths.DATA_DIR) / 'basic_info.json'

DB_UNKNOWN = '未采集'
ASSET_UNKNOWN = '未登记'

_cache_lock = threading.Lock()
_cache = {'mtime': None, 'data': {}}


def field_labels(lang='zh'):
    """返回 [(key, label)]，供报告渲染复用同一顺序。"""
    if lang == 'en':
        return [(k, LABELS_EN.get(k, k)) for k, _ in FIELD_ORDER]
    return list(FIELD_ORDER)


# ── 基础工具 ────────────────────────────────────────────────────────────

def _rows(cursor, sql):
    cursor.execute(sql)
    cols = [str(d[0]) for d in (cursor.description or [])]
    out = []
    for row in cursor.fetchall() or []:
        if isinstance(row, dict):
            out.append(row)
        else:
            out.append(dict(zip(cols, row)))
    return out


def _try(cursor, sql):
    """单条探测失败返回 None，绝不向上抛（权限不足/版本不支持都很常见）。"""
    try:
        return _rows(cursor, sql)
    except Exception:
        return None


def _get(row, *names):
    if not isinstance(row, dict):
        return None
    lower = {str(k).lower(): v for k, v in row.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ''):
            return v
    return None


def _first(rows, *names):
    for r in rows or []:
        v = _get(r, *names)
        if v not in (None, ''):
            return v
    return None


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gb(value):
    n = _num(value)
    if n is None:
        return None
    if n >= 1024:
        return '%.2f TB' % (n / 1024.0)
    return '%.2f GB' % n


def _mb_to_gb(value):
    n = _num(value)
    return None if n is None else _gb(n / 1024.0)


def _join(values, sep=' / ', limit=6):
    items = []
    for v in values or []:
        if v not in (None, ''):
            s = str(v).strip()
            if s and s not in items:
                items.append(s)
    if not items:
        return None
    if len(items) > limit:
        items = items[:limit] + ['…']
    return sep.join(items)


# ── 各库型探测 ──────────────────────────────────────────────────────────

def _oracle(cursor):
    out = {}
    ver = _first(_try(cursor, 'SELECT version_full AS v FROM v$instance'), 'v')
    if not ver:
        ver = _first(_try(cursor, 'SELECT version AS v FROM v$instance'), 'v')
    if not ver:
        banner = _try(cursor, 'SELECT banner AS b FROM v$version') or []
        ver = _join([_get(r, 'b') for r in banner], limit=1)
    patch_rows = _try(cursor, (
        'SELECT * FROM (SELECT action_time, action, namespace, version, id, comments '
        'FROM dba_registry_history ORDER BY action_time DESC) WHERE ROWNUM <= 5')) or []
    if not patch_rows:
        # 12c+ 的 RU/PSU 记录在 dba_registry_sqlpatch（部分 11g 无此视图，失败即跳过）
        patch_rows = _try(cursor, (
            'SELECT * FROM (SELECT action_time, action, description AS comments, '
            'patch_id AS id, version FROM dba_registry_sqlpatch '
            'ORDER BY action_time DESC) WHERE ROWNUM <= 5')) or []
    patches = []
    for r in patch_rows:
        version = _get(r, 'version')
        pid = _get(r, 'id')
        comments = _get(r, 'comments')
        piece = ' '.join(str(x) for x in (version, comments) if x)
        if pid and str(pid) not in piece:
            piece = ('%s (ID %s)' % (piece, pid)).strip()
        if piece:
            patches.append(piece)
    if ver:
        out['version_patch'] = str(ver).strip() + (
            '；补丁：' + _join(patches, ' | ', limit=3) if patches else '；补丁：未读取到')
    elif patches:
        out['version_patch'] = '补丁：' + _join(patches, ' | ', limit=3)

    out['db_name'] = _first(_try(cursor, "SELECT name AS v FROM v$database"), 'v') \
        or _first(_try(cursor, "SELECT SYS_CONTEXT('USERENV','DB_NAME') AS v FROM dual"), 'v')
    out['instance_name'] = _first(
        _try(cursor, "SELECT instance_name AS v FROM v$instance"), 'v') \
        or _first(_try(cursor, "SELECT SYS_CONTEXT('USERENV','INSTANCE_NAME') AS v FROM dual"), 'v')
    out['charset'] = _first(_try(
        cursor, "SELECT value AS v FROM nls_database_parameters "
                "WHERE parameter='NLS_CHARACTERSET'"), 'v')

    size = _try(cursor, (
        "SELECT (SELECT NVL(SUM(bytes),0) FROM dba_data_files) + "
        "(SELECT NVL(SUM(bytes),0) FROM dba_temp_files) AS total_bytes FROM dual"))
    total = _num(_first(size, 'total_bytes'))
    if total:
        out['db_size'] = _gb(total / 1024.0 ** 3)

    out['archive_mode'] = _first(_try(cursor, 'SELECT log_mode AS v FROM v$database'), 'v')
    dests = _try(cursor, (
        "SELECT destination AS v FROM v$archive_dest "
        "WHERE status='VALID' AND destination IS NOT NULL")) or []
    out['archive_path'] = _join([_get(r, 'v') for r in dests])

    asm = _try(cursor, "SELECT COUNT(*) AS c FROM v$datafile WHERE name LIKE '+%'")
    cnt = _num(_first(asm, 'c'))
    if cnt is not None and cnt > 0:
        out['storage_mode'] = 'ASM（%d 个数据文件位于磁盘组）' % int(cnt)
    elif cnt == 0:
        out['storage_mode'] = '本地/共享文件系统'

    rman = _try(cursor, 'SELECT name AS n, value AS v FROM v$rman_configuration') or []
    if rman:
        out['backup_policy'] = _join(
            ['%s=%s' % (_get(r, 'n'), _get(r, 'v')) for r in rman], '; ', limit=4)
    elif _try(cursor, 'SELECT 1 FROM v$backup_job WHERE ROWNUM = 1') is not None:
        out['backup_policy'] = '未配置 RMAN 持久参数（按作业执行，需在备份平台核对）'

    role = _first(_try(cursor, 'SELECT database_role AS v FROM v$database'), 'v')
    dg = _try(cursor, "SELECT name AS n, value AS v FROM v$dataguard_stats") or []
    lag = ', '.join('%s=%s' % (_get(r, 'n'), _get(r, 'v')) for r in dg[:3])
    if role:
        out['dr_status'] = str(role) + (('；' + lag) if lag else '')

    insts = _try(cursor, 'SELECT instance_name AS n, host_name AS h FROM gv$instance') or []
    names = ['%s@%s' % (_get(r, 'n'), _get(r, 'h')) for r in insts]
    if len(insts) > 1:
        out['cluster_attrs'] = 'RAC（%d 节点）：%s' % (len(insts), _join(names))
    elif insts:
        out['cluster_attrs'] = '单实例（%s）' % (_join(names) or '-')
    return out


def _dm(cursor):
    out = {}
    vrows = _try(cursor, 'SELECT * FROM V$VERSION') or []
    if vrows:
        out['version_patch'] = _join(
            [str(x) for r in vrows for x in r.values() if x not in (None, '')],
            ' | ', limit=3)
    out['db_name'] = _first(_try(cursor, 'SELECT NAME AS v FROM V$DATABASE'), 'v')
    out['instance_name'] = _first(_try(cursor, 'SELECT INSTANCE_NAME AS v FROM V$INSTANCE'), 'v')
    cs = _try(cursor, "SELECT PARA_VALUE AS v FROM V$DM_INI "
                      "WHERE PARA_NAME='CHARSET'") or []
    if not cs:
        cs = _try(cursor, "SELECT PARA_VALUE AS v FROM V$DM_INI "
                          "WHERE PARA_NAME='UNICODE_FLAG'") or []
    out['charset'] = _join([_get(r, 'v') for r in cs])
    size = _try(cursor, 'SELECT SUM(BYTES) AS b FROM DBA_DATA_FILES')
    total = _num(_first(size, 'b'))
    if total:
        out['db_size'] = _gb(total / 1024.0 ** 3)
    arch = _try(cursor, 'SELECT ARCH_TYPE AS t, ARCH_DEST AS d FROM V$DM_ARCH_INI') or []
    if arch:
        out['archive_mode'] = _join([_get(r, 't') for r in arch])
        out['archive_path'] = _join([_get(r, 'd') for r in arch])
    files = _try(cursor, 'SELECT FILE_NAME AS f FROM DBA_DATA_FILES') or []
    plus = [f for f in (_get(r, 'f') for r in files) if f and str(f).startswith('+')]
    if files:
        out['storage_mode'] = ('ASM/共享存储（%d 个文件）' % len(plus)) if plus else '文件系统'
    bak = _try(cursor, "SELECT PARA_VALUE AS v FROM V$DM_INI "
                       "WHERE PARA_NAME LIKE '%BACKUP%' OR PARA_NAME LIKE '%BAK%'") or []
    if bak:
        out['backup_policy'] = _join([_get(r, 'v') for r in bak], limit=3)
    ep = _try(cursor, 'SELECT * FROM V$DSC_EP_INFO') or []
    if ep:
        out['cluster_attrs'] = 'DMDSC（%d 节点）' % len(ep)
    else:
        out['cluster_attrs'] = '单实例'
    return out


def _mysql(cursor, db_type='mysql'):
    out = {}
    ver = _first(_try(cursor, 'SELECT VERSION() AS v'), 'v')
    if db_type == 'tidb':
        tv = _first(_try(cursor, 'SELECT TIDB_VERSION() AS v'), 'v')
        ver = tv or ver
    if db_type == 'oceanbase':
        ov = _first(_try(cursor, 'SELECT @@version_comment AS v'), 'v')
        ver = ('%s %s' % (ver, ov)).strip() if ov else ver
    if ver:
        out['version_patch'] = str(ver).strip()
    out['db_name'] = _first(_try(cursor, 'SELECT DATABASE() AS v'), 'v')
    out['instance_name'] = _first(_try(
        cursor, "SELECT CONCAT(@@hostname, ':', @@port) AS v"), 'v') \
        or _first(_try(cursor, 'SELECT @@hostname AS v'), 'v')
    cs = _try(cursor, "SHOW VARIABLES LIKE 'character_set_server'") or []
    out['charset'] = _first(cs, 'Value', 'value')
    size = _try(cursor, (
        'SELECT ROUND(SUM(COALESCE(data_length,0)+COALESCE(index_length,0))'
        '/1024/1024/1024, 2) AS g FROM information_schema.tables'))
    out['db_size'] = _gb(_first(size, 'g'))
    binlog = _try(cursor, "SHOW VARIABLES LIKE 'log_bin'") or []
    base = _try(cursor, "SHOW VARIABLES LIKE 'log_bin_basename'") or []
    log_bin = str(_first(binlog, 'Value', 'value') or '').upper()
    out['archive_mode'] = 'binlog 开启' if log_bin in ('ON', '1') else 'binlog 关闭（无归档）'
    out['archive_path'] = _first(base, 'Value', 'value')
    datadir = _first(_try(cursor, "SHOW VARIABLES LIKE 'datadir'") or [], 'Value', 'value')
    out['storage_mode'] = '本地文件系统' + ('（%s）' % datadir if datadir else '')

    slave = _try(cursor, 'SHOW SLAVE STATUS') or []
    if not slave:
        slave = _try(cursor, 'SHOW REPLICA STATUS') or []
    if slave:
        r = slave[0]
        io = _get(r, 'Slave_IO_Running', 'Replica_IO_Running')
        sql = _get(r, 'Slave_SQL_Running', 'Replica_SQL_Running')
        lag = _get(r, 'Seconds_Behind_Master', 'Seconds_Behind_Source')
        out['dr_status'] = '主从/副本：IO=%s SQL=%s 延迟=%ss' % (io, sql, lag)
        out['cluster_attrs'] = '主从复制（1 主 ≥1 备）'
    gr = _try(cursor, (
        "SELECT COUNT(*) AS c FROM performance_schema.replication_group_members")) or []
    grc = _num(_first(gr, 'c'))
    if grc:
        out['cluster_attrs'] = 'MGR（%d 节点）' % int(grc)
        out['dr_status'] = out.get('dr_status') or 'MGR 多副本'
    return out


def _pg(cursor):
    out = {}
    ver = _first(_try(cursor, 'SHOW server_version'), 'server_version')
    if not ver:
        ver = _first(_try(cursor, 'SELECT version() AS v'), 'v')
    if ver:
        out['version_patch'] = str(ver).strip()
    out['db_name'] = _first(_try(cursor, 'SELECT current_database() AS v'), 'v')
    out['instance_name'] = _first(_try(cursor, 'SHOW cluster_name'), 'cluster_name') \
        or _first(_try(cursor, 'SELECT inet_server_addr()::text AS v'), 'v')
    out['charset'] = _first(_try(cursor, 'SHOW server_encoding'), 'server_encoding')
    size = _first(_try(cursor, 'SELECT pg_database_size(current_database()) AS b'), 'b')
    out['db_size'] = _gb((_num(size) or 0) / 1024.0 ** 3) if size else None
    am = _first(_try(cursor, 'SHOW archive_mode'), 'archive_mode')
    ac = _first(_try(cursor, 'SHOW archive_command'), 'archive_command')
    if am:
        out['archive_mode'] = am
    if ac:
        out['archive_path'] = ac
    dd = _first(_try(cursor, 'SHOW data_directory'), 'data_directory')
    out['storage_mode'] = '本地文件系统' + ('（%s）' % dd if dd else '')
    rec = _first(_try(cursor, 'SELECT pg_is_in_recovery() AS v'), 'v')
    reps = _try(cursor, (
        'SELECT client_addr::text AS c, state AS s FROM pg_stat_replication')) or []
    if reps:
        out['dr_status'] = '流复制（%d 备库）：%s' % (
            len(reps), _join(['%s/%s' % (_get(r, 'c'), _get(r, 's')) for r in reps]))
        out['cluster_attrs'] = '主备流复制（%d 节点）' % (len(reps) + 1)
    elif rec in (True, 't', 'true'):
        out['dr_status'] = '当前为备库（pg_is_in_recovery=true）'
        out['cluster_attrs'] = '主备流复制'
    return out


def _sqlserver(cursor):
    out = {}
    ver = _try(cursor, (
        "SELECT SERVERPROPERTY('ProductVersion') AS v, "
        "SERVERPROPERTY('ProductLevel') AS lvl, "
        "SERVERPROPERTY('Edition') AS ed, "
        "SERVERPROPERTY('ProductUpdateLevel') AS upd"))
    if ver:
        r = ver[0]
        v = _get(r, 'v')
        lvl = _get(r, 'lvl')
        upd = _get(r, 'upd')
        ed = _get(r, 'ed')
        if v:
            out['version_patch'] = '%s%s%s%s' % (
                v,
                (' (%s)' % lvl) if lvl else '',
                (' 补丁 %s' % upd) if upd else '',
                (' · %s' % ed) if ed else '')
    out['db_name'] = _first(_try(cursor, 'SELECT DB_NAME() AS v'), 'v')
    out['instance_name'] = _first(_try(cursor, "SELECT SERVERPROPERTY('InstanceName') AS v"), 'v') \
        or _first(_try(cursor, 'SELECT @@SERVERNAME AS v'), 'v')
    out['charset'] = _first(_try(cursor, "SELECT SERVERPROPERTY('Collation') AS v"), 'v')
    size = _first(_try(cursor, 'SELECT SUM(size)*8.0/1024/1024 AS g FROM sys.database_files'), 'g')
    out['db_size'] = _gb(size)
    rec = _first(_try(cursor, (
        'SELECT recovery_model_desc AS v FROM sys.databases WHERE name=DB_NAME()')), 'v')
    if rec:
        out['archive_mode'] = '恢复模式 %s（日志备份=%s）' % (
            rec, '必需' if str(rec).upper() == 'FULL' else '简单模式无日志链')
    out['archive_path'] = _join([
        _get(r, 'physical_device_name') for r in
        (_try(cursor, 'SELECT TOP 5 physical_device_name FROM msdb.dbo.backupmediafamily') or [])
    ])
    fn = _first(_try(cursor, "SELECT physical_name AS v FROM sys.database_files WHERE type=0"), 'v')
    out['storage_mode'] = '文件系统' + ('（%s）' % fn if fn else '')
    ag = _try(cursor, (
        "SELECT ag.name AS n, ar.replica_server_name AS s, ars.role_desc AS r "
        "FROM sys.availability_groups ag "
        "JOIN sys.availability_replicas ar ON ag.group_id=ar.group_id "
        "JOIN sys.dm_hadr_availability_replica_states ars ON ar.replica_id=ars.replica_id"))
    if ag:
        out['cluster_attrs'] = 'AlwaysOn（%d 副本）' % len(ag)
        out['dr_status'] = _join(['%s/%s' % (_get(r, 's'), _get(r, 'r')) for r in ag])
    else:
        out['cluster_attrs'] = '单实例'
    return out


def _db2(cursor):
    out = {}
    lvl = _first(_try(cursor, (
        'SELECT SERVICE_LEVEL AS v FROM TABLE(SYSPROC.ENV_GET_INST_INFO())')), 'v')
    ver = _first(_try(cursor, 'SELECT INSTANCE_NAME AS i, VERSION AS v FROM TABLE(SYSPROC.ENV_GET_INST_INFO())'), 'v')
    if ver or lvl:
        out['version_patch'] = ' '.join(x for x in (str(ver or '').strip(), str(lvl or '').strip()) if x)
    out['db_name'] = _first(_try(cursor, 'VALUES CURRENT SERVER'), '1')
    out['instance_name'] = _first(_try(cursor, 'VALUES CURRENT SERVER'), '1')
    page = _num(_first(_try(cursor, (
        'SELECT SUM(TOTAL_PAGES)*PAGE_SIZE AS b FROM TABLE(SYSPROC.MON_GET_DATABASE(-2))')), 'b'))
    if page:
        out['db_size'] = _gb(page / 1024.0 ** 3)
    out['archive_mode'] = _first(_try(cursor, (
        "SELECT VALUE AS v FROM SYSIBMADM.DBCFG WHERE NAME='logarchmeth1'")), 'v')
    return out


_FAMILIES = {
    'oracle': _oracle, 'oracle_jdbc': _oracle,
    'dm': _dm, 'dm8': _dm,
    'mysql': _mysql, 'mariadb': _mysql, 'tidb': _mysql, 'oceanbase': _mysql,
    'gbase8a': _mysql, 'gbase': _mysql,
    'postgresql': _pg, 'pg': _pg, 'ivorysql': _pg, 'halodb': _pg,
    'vastbase': _pg, 'uxdb': _pg, 'kingbase': _pg,
    'sqlserver': _sqlserver, 'sqlserver_jdbc': _sqlserver,
    'db2': _db2,
}


def normalize_db_type(db_type):
    dt = str(db_type or '').strip().lower()
    return 'postgresql' if dt in ('pg', 'postgres') else dt


def collect_db(cursor, db_type, database=None):
    """在巡检查询阶段调用：用同一个游标读取数据库侧属性。

    返回的 dict 只包含成功读到的键；失败/不支持由上层补默认值。
    """
    out = {}
    if cursor is None:
        return out
    dt = normalize_db_type(db_type)
    fn = _FAMILIES.get(dt)
    if fn is None:
        return out
    try:
        part = fn(cursor, dt) if fn is _mysql else fn(cursor)
    except TypeError:
        part = fn(cursor)
    except Exception:
        return out
    for k, v in (part or {}).items():
        if v not in (None, '', []):
            out[k] = str(v).strip()
    if not out.get('db_name') and database:
        out['db_name'] = str(database)
    return out


# ── 台账覆盖与合并 ──────────────────────────────────────────────────────

def _override_doc():
    try:
        mtime = OVERRIDE_FILE.stat().st_mtime
    except OSError:
        return {}
    with _cache_lock:
        if _cache['mtime'] == mtime:
            return _cache['data']
        try:
            data = json.loads(OVERRIDE_FILE.read_text(encoding='utf-8')) or {}
        except Exception:
            data = {}
        _cache.update({'mtime': mtime, 'data': data})
    return data


def _engine_keys(engine):
    """实例匹配键：实例 ID、host:port、实例名，按优先级排列。"""
    keys = []
    iid = ''
    try:
        iid = str((getattr(engine, 'context', {}) or {}).get('_instance_id') or '')
    except Exception:
        iid = ''
    if iid:
        keys.append(iid)
    host = str(getattr(engine, 'host', '') or '').strip()
    port = str(getattr(engine, 'port', '') or '').strip()
    if host:
        keys.append('%s:%s' % (host, port) if port else host)
        keys.append(host)
    name = str(getattr(engine, 'instance_name', '') or '').strip()
    if name:
        keys.append(name)
    return keys


def load_overrides(engine):
    """读取 data/basic_info.json 中该实例的静态登记项（实例 > defaults）。"""
    doc = _override_doc()
    merged = {}
    defaults = doc.get('defaults')
    if isinstance(defaults, dict):
        merged.update({k: v for k, v in defaults.items() if v not in (None, '')})
    insts = doc.get('instances')
    if isinstance(insts, dict):
        for key in _engine_keys(engine):
            hit = insts.get(key)
            if isinstance(hit, dict):
                merged.update({k: v for k, v in hit.items() if v not in (None, '')})
                break
    try:
        from modules.operations.inventory import report_overrides
        merged.update(report_overrides(engine))
    except (OSError, ValueError, RuntimeError):
        pass
    return merged


def _ctx_rows(engine, key):
    rows = (getattr(engine, 'context', {}) or {}).get(key) or []
    return rows if isinstance(rows, list) else []


def build(engine, db_part=None):
    """合并数据库探测、宿主机信息与台账覆盖，返回完整 16 字段（全部字符串）。"""
    ctx = getattr(engine, 'context', {}) or {}
    db_part = dict(db_part or ctx.get('basic_info_db') or {})
    over = load_overrides(engine)
    sysinfo = ctx.get('system_info') or {}
    if not isinstance(sysinfo, dict):
        sysinfo = {}

    info = {}
    for key, _label in FIELD_ORDER:
        info[key] = ''

    info.update({k: str(v) for k, v in db_part.items() if v not in (None, '')})

    # 版本/补丁：数据库探测优先；否则至少给出连接时取得的版本号
    if not info.get('version_patch'):
        ver = ''
        for row in _ctx_rows(engine, 'version'):
            if isinstance(row, dict):
                ver = _get(row, 'VERSION', 'version') or ver
        if ver:
            info['version_patch'] = str(ver) + '（补丁级别未读取到）'

    if not info.get('db_name'):
        info['db_name'] = str(getattr(engine, 'database', '') or '')
    if not info.get('db_name'):
        for row in _ctx_rows(engine, 'co_name'):
            if isinstance(row, dict):
                info['db_name'] = str(_get(row, 'DB_NAME', 'db_name') or '')
                if info['db_name']:
                    break
    if not info.get('instance_name'):
        info['instance_name'] = str(getattr(engine, 'instance_name', '') or '')
    if not info.get('db_name') and not info.get('instance_name'):
        host = str(getattr(engine, 'host', '') or '')
        port = str(getattr(engine, 'port', '') or '')
        if host:
            info['db_name'] = '%s:%s' % (host, port) if port else host

    # 宿主机信息（SSH 采集优先，其次本机兜底；本机仅在未配置目标服务器时出现）
    if not info.get('os_version'):
        info['os_version'] = str(
            sysinfo.get('os_version') or sysinfo.get('platform_text')
            or sysinfo.get('platform') or '')
    if not info.get('host_model'):
        info['host_model'] = str(sysinfo.get('host_model') or '')
    if not info.get('os_version'):
        info['os_version'] = '%s %s' % (platform.system(), platform.release())

    # 台账覆盖优先于自动采集（登记项以台账为准，例如设备位置/存储平台）
    for key in ('configuration_id', 'location', 'storage_platform', 'host_model',
                'backup_policy', 'dr_status', 'cluster_attrs', 'storage_mode',
                'archive_path', 'archive_mode', 'charset', 'version_patch'):
        v = over.get(key)
        if v not in (None, ''):
            info[key] = str(v)

    # 台账中显式给出的 16 字段统一生效（允许业务自定义 key）
    for key, _label in FIELD_ORDER:
        v = over.get(key)
        if v not in (None, ''):
            info[key] = str(v)

    asset_known = {'configuration_id', 'host_model', 'location', 'os_version',
                   'storage_platform', 'storage_mode', 'backup_policy',
                   'dr_status', 'cluster_attrs'}
    for key, _label in FIELD_ORDER:
        val = str(info.get(key) or '').strip()
        if val:
            info[key] = val
        else:
            info[key] = ASSET_UNKNOWN if key in asset_known else DB_UNKNOWN
    return info


def to_rows(info, lang='zh'):
    return [[label, str(info.get(key) or DB_UNKNOWN)]
            for key, label in field_labels(lang)]


def build_from_parts(*, host='', port=None, db_type='oracle', database=None,
                     instance_name='', db_part=None, system_info=None,
                     version_text='', context=None):
    """给「不走 BaseInspectionEngine」的报告构建器用的轻量入口。

    Oracle 全面巡检走 ``build_word_report``，没有 engine 对象；这里用等价的
    最小上下文构造，保证 16 字段与台账覆盖逻辑完全一致。
    """
    from types import SimpleNamespace
    ctx = dict(context or {})
    if system_info is not None:
        ctx['system_info'] = system_info
    if db_part:
        ctx['basic_info_db'] = dict(db_part)
    if version_text:
        ctx['version'] = [{'VERSION': str(version_text)}]
    eng = SimpleNamespace(host=host, port=port, db_type=db_type, database=database,
                          instance_name=instance_name, context=ctx)
    return build(eng)


def collect(engine, cursor=None):
    """一站式入口：探测 + 合并，返回 16 字段 dict。"""
    db_part = collect_db(cursor, getattr(engine, 'db_type', ''),
                         getattr(engine, 'database', None)) if cursor is not None else None
    return build(engine, db_part)
