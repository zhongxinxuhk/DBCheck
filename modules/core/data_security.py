# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Fail-closed inspection SQL validation and recursive disclosure controls."""



import re
from collections.abc import Mapping
from typing import Any

_SECRET = re.compile(r'password|passwd|pwd|secret|token|private.?key|credential', re.I)
_ASSIGNMENT = re.compile(r'(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*(?:\{[^}]*}|\"[^\"]*\"|\'[^\']*\'|[^\s;,]+)')
_IPV4 = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
_EMAIL = re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b')
_DENIED = set(
    (
        'INSERT UPDATE DELETE DROP ALTER TRUNCATE CREATE REPLACE MERGE GRANT REVOKE COMMENT RENAME  '
        'SET EXEC EXECUTE BEGIN COMMIT ROLLBACK SAVEPOINT CALL LOCK UNLOCK FLUSH PURGE ANALYZE  '
        'ANALYSE VACUUM INTO OUTFILE DUMPFILE ATTACH DETACH COPY LOAD_FILE SLEEP BENCHMARK WAITFOR  '
        'NEXTVAL SETVAL PG_SLEEP PG_READ_FILE PG_WRITE_FILE LO_IMPORT LO_EXPORT GET_LOCK  '
        'RELEASE_LOCK OPENROWSET OPENDATASOURCE DBLINK_EXEC '
    ).split()
)
_READ_FUNCTIONS = set(
    (
        'AGENT_DATETIME DM_IO_VIRTUAL_FILE_STATS DATEPART CURRENT_USER REGEXP_LIKE SUSER_NAME SYSTEM_USER ORIGINAL_LOGIN APP_NAME HOST_NAME LOGINPROPERTY USER DATABASE FN_VIRTUALFILESTATS FN_VIRTUAL_FILE_STATS FN_AGENT_DATETIME PARSENAME IS_MEMBER COUNT SUM MIN MAX AVG ROUND FLOOR CEIL CEILING ABS MOD TRUNC COALESCE NULLIF NVL  '
        'NVL2 DECODE CAST CONVERT TRY_CONVERT TO_CHAR TO_DATE TO_NUMBER TO_TIMESTAMP UPPER LOWER  '
        'LENGTH CHAR_LENGTH SUBSTR SUBSTRING LEFT RIGHT REPLACE CONCAT CONCAT_WS TRIM LTRIM RTRIM  '
        'REVERSE INSTR LOCATE LPAD RPAD REGEXP_REPLACE REGEXP_SUBSTR EXTRACT DATE_PART DATE_TRUNC  '
        'DATEADD DATEDIFF GETDATE GETUTCDATE CURRENT_TIMESTAMP NOW VERSION SERVERPROPERTY  '
        'STATS_DATE IS_SRVROLEMEMBER OBJECTPROPERTY OBJECTPROPERTYEX DATABASEPROPERTYEX OBJECT_NAME  '
        'OBJECT_ID DB_NAME DB_ID SCHEMA_NAME USER_NAME SUSER_SNAME FILEPROPERTY  '
        'SQL_VARIANT_PROPERTY ISNULL IIF FORMAT ROW_NUMBER RANK DENSE_RANK LAG LEAD FIRST_VALUE  '
        'LAST_VALUE PERCENTILE_CONT PERCENTILE_DISC GROUPING GROUPING_ID POWER SQRT LOG LN EXP  '
        'PERCENT_RANK CUME_DIST SYS_CONTEXT NVL DECODE IN OVER AS FROM WHERE HAVING WITH VALUES  '
        'EXISTS PARTITION SETS GROUP BY ROLLUP CUBE SELECT DISTINCT TABLE PG_SIZE_PRETTY  '
        'PG_DATABASE_SIZE PG_TABLE_SIZE PG_TOTAL_RELATION_SIZE PG_RELATION_SIZE  '
        'PG_STAT_GET_BACKEND_PID CURRENT_SETTING HAS_TABLE_PRIVILEGE HAS_DATABASE_PRIVILEGE  '
        'HAS_SCHEMA_PRIVILEGE HAS_FUNCTION_PRIVILEGE HAS_COLUMN_PRIVILEGE ARRAY_AGG STRING_AGG  '
        'LISTAGG XMLAGG XMLPARSE XMLSERIALIZE GREATEST LEAST NTILE BITAND HASHBYTES CHECKSUM  '
        'CHECKSUM_AGG TIMESTAMPDIFF PG_POSTMASTER_START_TIME PG_TABLESPACE_SIZE PG_CURRENT_WAL_LSN '
        'PG_IS_IN_RECOVERY PG_WALFILE_NAME PG_WAL_LSN_DIFF PG_LAST_WAL_RECEIVE_LSN AGE FILTER '
        'PG_LS_DIR JOIN AND OR DATALENGTH DM_DB_INDEX_PHYSICAL_STATS DM_EXEC_SQL_TEXT NVARCHAR DBINFO '
        'DECIMAL FLOAT INTEGER SMALLINT BIGINT VARCHAR CHAR DATE TIME TIMESTAMP SYSDATE '
        'TODAY YESTERDAY TO_DAYS TO_SECONDS IFNULL NOW CURDATE CURTIME SYSTEM UUID '
        'ROWCOUNT PIPE ADD_MONTHS MONTHS_BETWEEN LAST_DAY NEXT_DAY TRUNC'
    ).split()
)


_READ_FUNCTIONS.update({'CURRENT_DATABASE','IF','DB_MEMBERS','MON_GET_PKG_CACHE_STMT','INET_SERVER_ADDR','INET_SERVER_PORT','ARRAY_TO_STRING','PG_ENCODING_TO_CHAR','FORMAT_TYPE','OBJECT_SCHEMA_NAME','TIDB_VERSION','ENV_GET_INST_INFO','MON_GET_DATABASE','MON_GET_ACTIVITY','SF_GET_PARAMLINK_VALUE','SF_GET_SESSION_SQL'})

def assert_readonly_sql(sql):
    """Accept one read statement, rejecting executable comments and known side effects.

    This is an additional guard. Dedicated least-privilege accounts and fixed
    metadata query catalogs remain required for customer inspection collection.
    """
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 100000:
        raise ValueError('巡检 SQL 为空或超出长度限制')
    if '/*!' in sql or any(ord(c) < 32 and c not in '\r\n\t' for c in sql):
        raise ValueError('禁止可执行注释或控制字符')
    # Only these fixed SQL Server diagnostics are read-only; never permit arbitrary EXEC/DBCC.
    if re.fullmatch(r"\s*(?:DBCC\s+LOGINFO|DBCC\s+SQLPERF\s*\(\s*LOGSPACE\s*\)|EXEC\s+(?:master\.\.)?xp_readerrorlog\s+0\s*,\s*1|EXEC\s+master\.dbo\.sp_readerrorlog\s+0\s*,\s*1\s*,\s*N'Error')\s*;?\s*", sql, re.I):
        return
    tokens = []
    i = 0
    semicolon = False
    while i < len(sql):
        char = sql[i]
        if char.isspace():
            i += 1
            continue
        if sql.startswith('--', i) or char == '#':
            if sql.startswith('--', i) and i + 2 < len(sql) and not sql[i + 2].isspace():
                raise ValueError('拒绝存在方言歧义的行注释')
            end = sql.find('\n', i)
            i = len(sql) if end < 0 else end + 1
            continue
        if sql.startswith('/*', i):
            end = sql.find('*/', i + 2)
            if end < 0:
                raise ValueError('SQL 注释未闭合')
            i = end + 2
            continue
        if semicolon:
            raise ValueError('巡检只允许单条 SQL')
        if char in "'\"`[":
            end_char = ']' if char == '[' else char
            i += 1
            while i < len(sql):
                if sql[i] == '\\' and char == "'":
                    i += 2
                    continue
                if sql[i] == end_char:
                    if i + 1 < len(sql) and sql[i + 1] == end_char:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise ValueError('SQL 引号未闭合')
            if sql[i:].lstrip().startswith('(') and char != "'":
                raise ValueError('禁止带引号的未知函数调用')
            continue
        if char == ';':
            semicolon = True
            i += 1
            continue
        match = re.match(r'[A-Za-z_][A-Za-z_0-9$]*', sql[i:])
        if match:
            token = match.group().upper()
            if sql[i+len(match.group()):].lstrip().startswith('(') and token not in _READ_FUNCTIONS:
                raise ValueError('未知函数须经只读审查后加入白名单: ' + token)
            tokens.append(token)
            i += len(match.group())
        else:
            i += 1
    if not tokens or tokens[0] not in {'SELECT', 'WITH', 'SHOW', 'DESC', 'DESCRIBE', 'EXPLAIN'}:
        raise ValueError('巡检仅允许只读查询')
    if any(t in _DENIED or t.startswith(('UTL_', 'DBMS_', 'XP_')) for t in tokens):
        raise ValueError('巡检 SQL 包含写入、文件、外联或执行类操作')
    if tokens[0] == 'EXPLAIN' and 'PLAN' in tokens:
        raise ValueError('禁止写入执行计划表')


def redact(value, *, mask_identity=False, preserve_network=False, key=''):
    """Redact nested credentials and identities without mutating the input.

    preserve_network keeps report IP addresses and host names visible while
    retaining credential and other identity redaction.
    """
    if _SECRET.search(str(key)) and str(key) not in {'credential_reference','password_policy'}:
        return '[REDACTED]'
    if isinstance(value, Mapping):
        output = {
            str(k): redact(v, mask_identity=mask_identity,
                           preserve_network=preserve_network, key=str(k))
            for k, v in value.items()
        }
        label = str(value.get('name') or value.get('NAME') or value.get('variable_name') or value.get('VARIABLE_NAME') or '')
        if _SECRET.search(label):
            for field in ('value','VALUE','variable_value','VARIABLE_VALUE','setting'):
                if field in output:
                    output[field] = '[REDACTED]'
        return output
    if isinstance(value, (list, tuple)):
        return [redact(v, mask_identity=mask_identity,
                       preserve_network=preserve_network, key=key) for v in value]
    if isinstance(value, str):
        text = _ASSIGNMENT.sub(lambda m: m.group(1) + '=[REDACTED]', value)
        text = re.sub(r'([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@', r'\1[REDACTED]@', text)
        text = re.sub(r'(?i)Bearer\s+[A-Za-z0-9._~+/-]+', 'Bearer [REDACTED]', text)
        if 'sql' in key.lower() or key.lower() in {'query','statement','info','query_text','query_sample_text','statement_text'}:
            text = re.sub(r"'(?:''|[^'])*'", "'?'", text)
            text = re.sub(r'\b\d+(?:\.\d+)?\b', '?', text)
        if mask_identity:
            if not preserve_network:
                text = _IPV4.sub('[IP]', text)
            public_authors = {'sdfiyon@gmail.com','xuzhongxin@sec.hn.cn'}
            text = _EMAIL.sub(lambda m: m.group() if m.group() in public_authors else '[EMAIL]', text)
            text = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[PHONE]', text)
            text = re.sub(r'(?<!\w)\d{17}[0-9Xx](?!\w)', '[IDENTITY]', text)
            if key.lower() in {'host', 'ip', 'hostname', 'user', 'username', 'db_user', 'ssh_user', 'owner', 'responsible_person'}:
                if not (preserve_network and key.lower() in {'host', 'ip', 'hostname'}):
                    text = '[MASKED]' if text else ''
        return text
    return value


# ── 运行时放行清单 ────────────────────────────────────────────────────────
# 由「自动允许在线 AI」（dbc_config.json → ai.auto_allow_egress）在进程内登记。
# 与 DBPULSE_ALLOWED_EGRESS_HOSTS 的区别：那个是运维在部署层写的静态清单，
# 这个是管理员在 AI 设置页点开关产生的会话级授权，进程重启即失效。
# 两者都只对 https 生效，且都不改变「默认 fail-closed」的基线行为。
_RUNTIME_ALLOWED_HOSTS = set()


def allow_egress_host(url_or_host):
    """登记一个运行时放行的外发主机（仅 https）。

    :return: ``(ok, host_or_reason)``；ok 为 False 时第二项是拒绝原因（中文）。
    """
    from urllib.parse import urlsplit
    s = (url_or_host or '').strip()
    if not s:
        return False, '地址为空'
    if '://' not in s:
        s = 'https://' + s
    try:
        parsed = urlsplit(s)
    except Exception:
        return False, '地址无效'
    if parsed.scheme != 'https':
        return False, '仅支持 https'
    host = (parsed.hostname or '').lower()
    if not host:
        return False, '地址无效'
    _RUNTIME_ALLOWED_HOSTS.add(host)
    return True, host


def runtime_allowed_hosts():
    """返回当前进程内已登记放行的主机（只读副本，供自检/日志使用）。"""
    return frozenset(_RUNTIME_ALLOWED_HOSTS)


def egress_policy(url):
    """判定外发目标属于哪种策略态，返回 ``('local'|'allowlisted'|'needs_approval', host)``。

    与 ``ensure_egress_allowed`` 的分工：
    - 非交互场景（通知、外部索引、后台任务）继续用 ``ensure_egress_allowed``，
      未批准主机直接失败——这些场景没有"人在旁边点头"的条件；
    - 交互场景（AI 助手对话）用本函数拿到 ``needs_approval`` 后走
      ``egress_consent`` 的人工审批闸门，不再一刀切报错。
    地址本身非法时抛 ``ValueError``（两种场景都不放行）。
    """
    import ipaddress
    import os
    from urllib.parse import urlsplit
    parsed = urlsplit(str(url))
    hostname = parsed.hostname
    if parsed.scheme not in {'http','https'} or not hostname or parsed.username or parsed.password:
        raise ValueError('外联地址无效')
    host = hostname.lower()
    local = host == 'localhost'
    try:
        local = local or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if local:
        return 'local', host
    allowed = {h.strip().lower() for h in os.environ.get('DBPULSE_ALLOWED_EGRESS_HOSTS','').split(',') if h.strip()}
    if parsed.scheme == 'https' and (host in allowed or host in _RUNTIME_ALLOWED_HOSTS):
        return 'allowlisted', host
    return 'needs_approval', host


def ensure_egress_allowed(url):
    """Customer data stays local unless the operator explicitly allowlists a TLS host."""
    state, _host = egress_policy(url)
    if state == 'needs_approval':
        raise ValueError('数据安全策略阻断未批准的外部数据发送')


def egress_urlopen(request, *, timeout=30, context=None, proxy=None):
    """Send approved data without forwarding bodies or credentials across redirects."""
    import ssl
    import urllib.request
    from urllib.error import HTTPError
    ensure_egress_allowed(request.full_url)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise HTTPError(req.full_url,code,'Data egress redirects are disabled',headers,fp)
    tls=context or ssl.create_default_context()
    if tls.verify_mode!=ssl.CERT_REQUIRED or not tls.check_hostname:
        raise ValueError('数据外发须验证TLS证书与主机名')
    handlers: list[Any] = [NoRedirect(), urllib.request.HTTPSHandler(context=tls)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    opener=urllib.request.build_opener(*handlers)
    return opener.open(request,timeout=timeout)
