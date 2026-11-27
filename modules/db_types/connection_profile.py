# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Connection fields shared by saved sources and connection consumers."""

from modules.db_types.compatibility import plaintext_allowed

BOOLEAN_FIELDS = frozenset({
    'sysdba', 'use_sid', 'tls', 'tls_allow_invalid_certs', 'encrypt',
    'trust_server_certificate', 'ssh_enabled', 'ssh_tunnel', 'enabled',
    'gbase8a_allow_plaintext',
})
FILE_FIELDS = ('ssh_key_file', 'tls_ca_file', 'tls_cert_key_file')
CONNECTION_FIELDS = BOOLEAN_FIELDS | frozenset({
    'id', 'name', 'label', 'db_type', 'host', 'ip', 'port', 'user', 'password',
    'database', 'service_name', 'sid', 'instance_name', 'tenant', 'jdbc_url',
    'connection_mode', 'driver_version', 'gbase_server_name', 'virtual_cluster',
    'connect_mode', 'auth_source', 'auth_mechanism', 'replica_set', 'seed_nodes',
    'ssh_host', 'ssh_port', 'ssh_user', 'ssh_password', 'ssh_key_password',
    'ssh_ebpf', 'ssl', 'updated_at', *FILE_FIELDS,
})


def normalize_source_fields(values):
    """Validate supplied fields without changing omitted legacy values."""
    result = dict(values)
    for field in BOOLEAN_FIELDS.intersection(result):
        value = result[field]
        if isinstance(value, str):
            if value.strip().lower() not in {'', '0', 'false', 'no', 'off',
                                             '1', 'true', 'yes', 'on'}:
                raise ValueError(f'{field} 须为布尔值')
        elif value not in (None, True, False, 0, 1):
            raise ValueError(f'{field} 须为布尔值')
        result[field] = plaintext_allowed(value)
    for field in ('port', 'ssh_port'):
        if field not in result:
            continue
        value = result[field]
        try:
            port = int(value)
            if isinstance(value, bool) or (isinstance(value, float) and value != port):
                raise ValueError
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError('端口须为1到65535的整数') from error
        result[field] = port
    if 'tags' in result and (not isinstance(result['tags'], list)
                             or any(not isinstance(tag, str) for tag in result['tags'])):
        raise ValueError('标签须为文本列表')
    if result.get('db_type'):
        kind = result['db_type'].strip().lower()
        result['db_type'] = {'postgresql': 'pg', 'dm8': 'dm',
                             'oracle_full': 'oracle', 'mssql': 'sqlserver'}.get(kind, kind)
    return result


def connection_profile(source, db_type=None):
    """Project connection inputs before passing them to drivers or workers."""
    profile = {key: value for key, value in source.items() if key in CONNECTION_FIELDS}
    for field in ('port', 'ssh_port'):
        if profile.get(field) in (None, ''):
            profile.pop(field, None)
    if db_type is not None:
        profile['db_type'] = db_type
    profile = normalize_source_fields(profile)
    host = str(profile.get('host') or profile.get('ip') or '').strip()
    profile.update(host=host, ip=host)
    if profile.get('ssh_tunnel') and not str(profile.get('ssh_host') or '').strip():
        raise ValueError('已启用 SSH 隧道，但未配置 SSH 主机地址')
    return profile


def sqlserver_mode(source):
    """Resolve the configured SQL Server backend, preserving explicit auto mode."""
    from modules.core import paths

    if paths.PORTABLE_ROOT is not None:
        return 'jdbc'
    default = 'jdbc' if source.get('db_type') == 'sqlserver_jdbc' else 'odbc'
    return str(source.get('connection_mode') or default).strip().lower()


def resolve_sqlserver_mode(source):
    mode = sqlserver_mode(source)
    if mode != 'auto':
        return mode
    from modules.entrypoints.main_sqlserver_dual import jdbc_unavailable_reason

    return 'jdbc' if jdbc_unavailable_reason() is None else 'odbc'


def monitor_jdbc_driver(source, dialect):
    """Resolve JDBC for monitoring while retaining native protocol adapters."""
    from modules.core import paths

    kind = source.get('db_type')
    if dialect == 'sqlserver':
        return 'sqlserver_jdbc' if resolve_sqlserver_mode(source) == 'jdbc' else None
    if dialect == 'oracle':
        if (kind == 'oracle_jdbc' or paths.PORTABLE_ROOT is not None
                or str(source.get('jdbc_url') or '').strip().lower().startswith('jdbc:')):
            return 'oracle_jdbc'
    if dialect == 'dm':
        from modules.entrypoints.main_dm import _find_dm_jdbc_jar

        if paths.PORTABLE_ROOT is not None or source.get('driver_version') or _find_dm_jdbc_jar():
            return 'dm'
    if kind == 'uxdb_jdbc':
        return 'uxdb'
    if dialect in ('gbase', 'db2', 'clickhouse'):
        return dialect
    return None


def inspection_profile(source, db_type):
    from modules.db_types.dbtype_registry import get_db_meta

    profile = connection_profile(source, db_type)
    meta = get_db_meta(db_type)
    default = meta.default_database if meta else 'postgres'
    database = profile.get('database') or default
    if default == '' and database == 'postgres':
        database = ''
    profile.setdefault('port', meta.port if meta else 3306)
    profile.setdefault('user', '')
    profile.setdefault('password', '')
    profile['database'] = database
    if db_type == 'sqlserver_jdbc':
        profile['connection_mode'] = sqlserver_mode(profile)
    return profile
