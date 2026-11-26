# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Read-only inspection grant suggestions. The inspection engine never executes them."""

import re

from modules.driver_registry import db_family


GUIDE_URL = "https://zhongxinxuhk.github.io/DBCheck/docs/inspection-permissions/"
_OBJECT = re.compile(r"\b(?:from|join)\s+([a-z][\w$#]*(?:\.[a-z][\w$#]*){0,2})\b", re.I | re.ASCII)
_SQL_LITERAL = re.compile(r"'(?:''|[^'])*'|/\*.*?\*/|--[^\r\n]*", re.S)
_SQLSERVER_PERMISSIONS = (
    "VIEW SERVER PERFORMANCE STATE", "VIEW SERVER SECURITY STATE", "VIEW SERVER STATE",
    "VIEW DATABASE PERFORMANCE STATE", "VIEW DATABASE SECURITY STATE", "VIEW DATABASE STATE",
    "VIEW ANY ERROR LOG", "VIEW ANY DEFINITION", "VIEW ANY DATABASE",
)
_DENIAL = re.compile(
    r"insufficient privileges|permission(?:s)? denied|access denied|command denied|"
    r"not authorized|lacks permission|permission was denied|权限不足|没有权限|没有[^\r\n]{0,180}对象的查询权限|无访问权限|权限被拒绝|"
    r"(?<!\d)42501(?!\d)|ora-01031\b|ora-01045\b|(?<!\d)-5515(?!\d)",
    re.I,
)
_ORACLE_VIEW = re.compile(r"(?:SYS\.)?((?:G?V\$|DBA_|CDB_)[A-Z0-9_$#]+)\Z")
_DM_VIEW = re.compile(r"(?:SYS\.)?((?:V\$|DBA_)[A-Z0-9_$#]+)\Z")
_SQLSERVER_OBJECT = re.compile(
    r"(?:MSDB\.DBO\.(?:BACKUPSET|BACKUPMEDIAFAMILY|RESTOREHISTORY|SYSJOBS|SYSJOBHISTORY|SYSJOBSTEPS|SUSPECT_PAGES|LOG_SHIPPING_MONITOR_PRIMARY)"
    r"|SYS\.(?:DM_[A-Z0-9_]+|CONFIGURATIONS|DATABASES|MASTER_FILES|DATABASE_FILES))\Z"
)


def _safe_objects(sql):
    return list(dict.fromkeys(
        match.group(1).upper()
        for match in _OBJECT.finditer(_SQL_LITERAL.sub(" ", sql))
    ))[:20]


def permission_advice(db_type, sql, error, *, version=""):
    """Describe a confirmed/permitted ambiguity without deriving arbitrary grants."""
    message = str(error)
    family = db_family(db_type)
    inaccessible = family == "oracle" and "ora-00942" in message.lower()
    upper = message.upper()
    if "ORA-01045" not in upper and any(marker in upper for marker in (
            "LOGIN FAILED", "AUTHENTICATION FAILED", "INVALID USERNAME/PASSWORD",
            "USING PASSWORD:", "ORA-01017", "PASSWORD EXPIRED")):
        return None
    explicit = next((item for item in _SQLSERVER_PERMISSIONS if item in upper), None)
    if not (_DENIAL.search(message) or inaccessible or (family == "sqlserver" and explicit)):
        # Login/transport, unsupported syntax and generic object-not-found errors have
        # separate remediation paths and must never produce a grant.
        return None

    objects = _safe_objects(str(sql or ""))
    statements = []
    scope = "由 DBA 在执行巡检的数据库或容器中复核后执行。"
    if family == "oracle":
        if "ORA-01045" in upper:
            statements.append("GRANT CREATE SESSION TO DBCHECK_RO;")
        for obj in objects:
            match = _ORACLE_VIEW.fullmatch(obj)
            if not match:
                continue
            target = match.group(1)
            if target.startswith(("V$", "GV$")):
                target = target.replace("$", "_$", 1)
            statements.append(f"GRANT SELECT ON SYS.{target} TO DBCHECK_RO;")
        if inaccessible:
            scope = "ORA-00942 也可能表示视图在本版本或当前 PDB 不存在。仅对 DBA 确认存在且不可读的对象授权。"
    elif family == "dm":
        statements.extend(
            f"GRANT SELECT ON {match.group(1)} TO DBCHECK_RO;"
            for obj in objects if (match := _DM_VIEW.fullmatch(obj))
        )
    elif family == "sqlserver":
        if explicit:
            statements.append(f"GRANT {explicit} TO [DBCHECK_RO];")
            scope = ("在目标用户数据库执行，先确保该库已为登录名创建同名用户。"
                     if explicit.startswith("VIEW DATABASE") else "在 master 中对服务器登录名执行。")
        elif any(obj.startswith("MSDB.DBO.") and _SQLSERVER_OBJECT.fullmatch(obj) for obj in objects):
            statements.extend(f"GRANT SELECT ON {obj.split('.', 1)[1]} TO [DBCHECK_RO];"
                              for obj in objects if obj.startswith("MSDB.DBO.") and _SQLSERVER_OBJECT.fullmatch(obj))
            scope = "在 msdb 中执行，先为巡检登录名创建同名数据库用户。"
        elif any(obj.startswith(("SYS.DM_OS_", "SYS.DM_EXEC_", "SYS.DM_IO_", "SYS.DM_HADR_")) for obj in objects):
            major = re.search(r"\b(20(?:08|12|14|16|17|19|22|25)|1[0-7])(?:\.|\b)", str(version))
            if major and int(major[1]) in (16, 17, 2022, 2025):
                statements.append("GRANT VIEW SERVER PERFORMANCE STATE TO [DBCHECK_RO];")
            elif major:
                statements.append("GRANT VIEW SERVER STATE TO [DBCHECK_RO];")
            else:
                statements.extend([
                    "-- SQL Server 2019 及更早版本",
                    "GRANT VIEW SERVER STATE TO [DBCHECK_RO];",
                    "-- SQL Server 2022 及更新版本，限性能 DMV",
                    "GRANT VIEW SERVER PERFORMANCE STATE TO [DBCHECK_RO];",
                ])
            scope = "在 master 中执行。按服务器版本与该 DMV 的权限要求选一项。"
    elif family == "postgresql" and any(obj.split(".")[-1] in {
            "PG_STAT_ACTIVITY", "PG_STAT_REPLICATION", "PG_STAT_STATEMENTS"} for obj in objects):
        statements.append("GRANT pg_read_all_stats TO dbcheck_ro;")
        scope = "PostgreSQL 10 及以上，适用于需要查看其他会话的完整统计。若扩展视图本身不可读，另由 DBA 核对扩展所在模式。"
    elif family in {"gbase8a", "mysql"} and "PROCESS" in upper and (
            "PRIVILEGE" in upper or "权限" in message):
        scope = "全局 PROCESS 权限会扩大其他会话的可见范围。请按版本和审批范围参照权限指南处理。"

    if inaccessible and not statements:
        return None
    return {
        "confirmed": not inaccessible,
        "objects": objects,
        "scope": scope,
        "statements": list(dict.fromkeys(statements)),
    }


def append_permission_advice(doc, findings, heading=None):
    if not findings:
        return
    (heading or doc.add_heading)('巡检账号权限与补授权 SQL', 2)
    doc.add_paragraph('请由 DBA 复核后执行。将 DBCHECK_RO 替换为巡检专用账号。下列 SQL 仅供参考，巡检不会执行授权。')
    printed = set()
    for name, advice in findings.items():
        status = '权限拒绝' if advice['confirmed'] else '视图存在性或权限待核实'
        doc.add_paragraph(f'{name or "查询"} · {status}')
        doc.add_paragraph(advice['scope'])
        statements = tuple(advice['statements'])
        if statements and statements not in printed:
            doc.add_paragraph('\n'.join(statements))
            printed.add(statements)
        elif not statements:
            doc.add_paragraph('请使用 DBA 账号复核该检查项所需的对象和权限。')
    doc.add_paragraph('巡检账号权限指南：' + GUIDE_URL)
