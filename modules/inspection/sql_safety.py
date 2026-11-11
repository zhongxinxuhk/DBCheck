# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""巡检只读查询、资源边界与同端点执行互斥。"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import re
import threading
import time
from modules.core.data_security import assert_readonly_sql
from modules.core.dbapi_compat import fetch_rows
from modules.driver_registry import db_family, normalize_db_type

QUERY_TIMEOUT_SECONDS = 15
ROW_LIMIT = 1000
LEASE_WAIT_SECONDS = 30


class UnsafeInspectionSQL(ValueError):
    pass


def _plain(sql):
    text = re.sub(r"'(?:''|[^'])*'", "''", sql)
    text = (
        re.sub(r"/\*.*?\*/|--[^\n]*", " ", text, flags=re.S)
        .replace('"', "")
        .replace("`", "")
        .replace("[", "")
        .replace("]", "")
        .upper()
    )
    return re.sub(r"\s*\.\s*", ".", text)


def metadata_sources(sql):
    """遍历 FROM、JOIN 与逗号连接，记录源对象和 CTE 名称。"""
    text = _plain(sql)
    ctes = set(re.findall(r"(?:\bWITH|,)\s*([\w$]+)\s+AS\s*\(", text))
    sources = []
    depth = 0
    from_depths = set()
    expected = False
    previous = ""
    grammar_functions = set()
    for token in re.findall(r"[A-Z_][A-Z0-9_$#.]*|[(),]", text):
        if token == "(":
            depth += 1
            if previous in {"EXTRACT", "SUBSTRING", "TRIM", "POSITION", "OVERLAY"}:
                grammar_functions.add(depth)
            expected = False
        elif token == ")":
            grammar_functions.discard(depth)
            from_depths.discard(depth)
            depth -= 1
        elif token in {"FROM", "JOIN"} and depth not in grammar_functions:

            from_depths.add(depth)
            expected = True
        elif token in {
            "WHERE",
            "GROUP",
            "ORDER",
            "HAVING",
            "UNION",
            "EXCEPT",
            "INTERSECT",
            "LIMIT",
            "FETCH",
        }:
            from_depths.discard(depth)
            expected = False
        elif token == "," and depth in from_depths:
            expected = True
        elif expected and token != ",":
            sources.append(token)
            expected = False
        previous = token
    return sources, ctes


def assert_inspection_sql(sql, *, metadata_only=True):
    assert_readonly_sql(sql)
    text = _plain(sql)
    expensive = {
        r"\bDM_DB_INDEX_PHYSICAL_STATS\b": "索引物理扫描需在维护窗口专项执行",
        r"\b(?:PGSTATINDEX|PGSTATTUPLE|PG_CHECKSUMS|PG_PREWARM)\b": "物理页面扫描或预热不属于常规巡检",
        r"\b(?:XP_READERRORLOG|SP_READERRORLOG)\b": "未限定时间范围的错误日志读取需专项执行",
        r"\bWITH\s+RECURSIVE\b|\bCROSS\s+JOIN\b|\bJOIN\b.*?\bON\s+1\s*=\s*1": "递归或笛卡尔连接存在放大负载风险",
        r"\bCONNECT\s+BY\b.*?\bLEVEL\b": "层级数据生成存在放大负载风险",
        r"\b(?:PARALLEL_ENABLE|DBMS_STATS|DBMS_WORKLOAD_REPOSITORY)\b": "统计收集或快照维护不属于只读巡检",
    }
    for pattern, reason in expensive.items():
        if re.search(pattern, text, re.S):
            raise UnsafeInspectionSQL(reason)
    for schema, function in re.findall(r"([A-Z_][\w$]*)\.([A-Z_][\w$]*)\s*\(", text):
        if schema not in {"SYS", "PG_CATALOG", "SYSIBM", "SYSPROC"} and not (
            schema == "DBO"
            and function in {"AGENT_DATETIME", "FN_AGENT_DATETIME"}
            and "MSDB.DBO." + function in text
        ):
            raise UnsafeInspectionSQL(
                "常规巡检不调用业务模式函数 " + schema + "." + function
            )
    sources, ctes = metadata_sources(sql)
    for name in sources if metadata_only else []:
        allowed = (
            name in ctes
            or name
            in {
                "DUAL",
                "TABLE",
                "INFORMATION_SCHEMA",
                "NLS_DATABASE_PARAMETERS",
                "SESSION_ROLES",
                "GLOBAL_NAME",
                "DUALINSTANCE",
                "REGISTRY$HISTORY",
                "GBASE.USER",
                "DISTRIBUTION.DBO.MSPUBLICATIONS",
            }
            or name.startswith(
                (
                    "V$",
                    "GV$",
                    "DBA_",
                    "ALL_",
                    "USER_",
                    "CDB_",
                    "PG_",
                    "SYS",
                    "INFORMATION_SCHEMA.",
                    "PERFORMANCE_SCHEMA.",
                    "MYSQL.",
                    "SYSTEM.",
                    "UX_CATALOG.",
                    "PG_CATALOG.",
                    "OCEANBASE.",
                    "MASTER.SYS.",
                    "TEMPDB.SYS.",
                    "MSDB.DBO.",
                )
            )
        )
        if not allowed:
            raise UnsafeInspectionSQL(
                "常规巡检仅允许系统元数据，已拦截非目录对象 " + name
            )
    if re.search(r"\b(?:MYSQL\.SLOW_LOG|MYSQL\.GENERAL_LOG)\b", text):
        raise UnsafeInspectionSQL("系统日志表可能很大，常规巡检使用汇总统计")
    return True


class InspectionCursor:
    def __init__(self, raw, connection):
        self.raw = raw
        self.connection = connection
        self.remaining = ROW_LIMIT + 1
        self.deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _cancel(self):
        for target in (
            self.raw,
            getattr(self.raw, "stmt", None),
            getattr(self.raw, "_prep", None),
            self.connection.raw,
        ):
            cancel = getattr(target, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                    return
                except Exception:
                    pass
        try:
            self.connection.raw.close()
        except Exception:
            pass

    def execute(self, sql, *args, **kwargs):
        if self.connection.aborted:
            raise TimeoutError("本连接前一条巡检查询已超时，停止后续查询以避免持续施压")
        try:
            assert_inspection_sql(sql, metadata_only=self.connection.metadata_only)
        except ValueError as error:
            self.connection.warn("巡检SQL已跳过：" + str(error))
            raise
        self.connection.configure(self.raw)
        statement = getattr(self.raw, "stmt", None)
        if statement is not None:
            statement.setQueryTimeout(QUERY_TIMEOUT_SECONDS)
            statement.setMaxRows(ROW_LIMIT + 1)
            try:
                statement.setFetchSize(100)
            except Exception:
                pass
        clean = re.sub(r"/\*.*?\*/|--[^\n]*", " ", sql, flags=re.S).strip().rstrip(";")
        if re.match(r"^(SELECT|WITH)\b", clean, re.I):
            if self.connection.family in {"oracle", "dm", "yashandb"}:
                clean = (
                    "SELECT * FROM ("
                    + clean
                    + ") DBPULSE_BOUNDED WHERE ROWNUM <= "
                    + str(ROW_LIMIT + 1)
                )
            elif self.connection.family in {"mysql", "postgresql", "clickhouse"}:
                clean = (
                    "SELECT * FROM ("
                    + clean
                    + ") AS DBPULSE_BOUNDED LIMIT "
                    + str(ROW_LIMIT + 1)
                )
            elif self.connection.family == "db2":
                clean = (
                    "SELECT * FROM ("
                    + clean
                    + ") AS DBPULSE_BOUNDED FETCH FIRST "
                    + str(ROW_LIMIT + 1)
                    + " ROWS ONLY"
                )
        if self.connection.family == "oracle":
            clean = re.sub(
                r"\bSELECT\b", "SELECT /*+ NO_PARALLEL */", clean, count=1, flags=re.I
            )
        elif self.connection.family == "mysql" and self.connection.kind != "mariadb":
            clean = re.sub(
                r"\bSELECT\b",
                f"SELECT /*+ MAX_EXECUTION_TIME({QUERY_TIMEOUT_SECONDS*1000}) */",
                clean,
                count=1,
                flags=re.I,
            )
        if (
            self.connection.family == "sqlserver"
            and re.match(r"^(SELECT|WITH)\b", clean, re.I)
            and not re.search(r"\bOPTION\b|\bFOR\s+(XML|JSON)\b", clean, re.I)
        ):
            clean += " OPTION (MAXDOP 1)"
        if self.connection.family in {"gbase", "gbase8s"} and re.match(
            r"^SELECT\s+(?!FIRST\b|SKIP\b)", clean, re.I
        ):
            clean = re.sub(
                r"^SELECT\s+",
                "SELECT FIRST " + str(ROW_LIMIT + 1) + " ",
                clean,
                count=1,
                flags=re.I,
            )
        self.remaining = ROW_LIMIT + 1
        self.deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
        try:
            result = self._run_bounded(lambda: self.raw.execute(clean, *args, **kwargs))
            return self if result is self.raw else result
        except Exception:
            try:
                self.connection.rollback()
            except Exception:
                pass
            raise

    def _run_bounded(self, operation):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self._cancel()
            raise TimeoutError("巡检查询超过15秒预算，已请求取消")
        expired = threading.Event()

        def expire():
            expired.set()
            self.connection.aborted = True
            self._cancel()

        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        try:
            result = operation()
            if expired.is_set():
                raise TimeoutError("巡检查询超过15秒预算，已请求取消")
            return result
        except Exception as error:
            self.connection.warn(
                ("巡检查询超时：" if expired.is_set() else "巡检查询未完成：")
                + str(error)[:300]
            )
            raise
        finally:
            timer.cancel()

    def fetchmany(self, size=None):
        size = min(int(size or 100), self.remaining)
        if size <= 0:
            return []
        try:
            rows = self._run_bounded(lambda: fetch_rows(self.raw, size, strict=True))
        except Exception:
            try:
                self.connection.rollback()
            except Exception:
                pass
            raise
        returned_before = ROW_LIMIT + 1 - self.remaining
        self.remaining -= len(rows)
        if self.remaining == 0:
            self.connection.warn("查询结果已截断：超过1000行，须缩小范围复核")
        return rows[: max(0, ROW_LIMIT - returned_before)]

    def fetchall(self):
        return self.fetchmany(self.remaining)

    def fetchone(self):
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def close(self):
        return self.raw.close()


class InspectionConnection:
    def __init__(self, raw, kind, metadata_only=True):
        self.raw = raw
        self.metadata_only = metadata_only
        self.kind = normalize_db_type(kind).lower()
        self.family = db_family(kind)
        if self.family in {"tidb", "oceanbase", "gbase8a", "tdsqlc_mysql"}:
            self.family = "mysql"
        self.warnings = []
        self.configured = False
        self.aborted = False

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)
            print("[WARN] " + message)

    def cursor(self, *args, **kwargs):
        return InspectionCursor(self.raw.cursor(*args, **kwargs), self)

    def configure(self, cursor):
        if self.configured:
            return
        self.configured = True
        for attribute, value in (
            ("call_timeout", QUERY_TIMEOUT_SECONDS * 1000),
            ("timeout", QUERY_TIMEOUT_SECONDS),
        ):
            if hasattr(self.raw, attribute):
                try:
                    setattr(self.raw, attribute, value)
                except Exception:
                    self.warn("驱动未能配置查询超时，使用取消守护补充保护")
        commands = []
        if self.family == "oracle":
            commands = ["ALTER SESSION DISABLE PARALLEL QUERY"]
        elif self.family == "postgresql":
            commands = [
                "SET statement_timeout = '15s'",
                "SET lock_timeout = '1s'",
                "SET max_parallel_workers_per_gather = 0",
                "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY",
            ]
        elif self.family == "mysql":
            commands = [
                (
                    "SET SESSION max_statement_time=15"
                    if self.kind == "mariadb"
                    else (
                        "SET SESSION ob_query_timeout=15000000"
                        if self.kind == "oceanbase"
                        else "SET SESSION max_execution_time=15000"
                    )
                ),
                "SET SESSION TRANSACTION READ ONLY",
            ]
        elif self.family == "sqlserver":
            commands = [
                "SET LOCK_TIMEOUT 1000",
                "SET DEADLOCK_PRIORITY LOW",
                "SET ROWCOUNT " + str(ROW_LIMIT + 1),
            ]
        elif self.family == "clickhouse":
            commands = ["SET max_execution_time=15", "SET max_threads=1"]
        for command in commands:
            try:
                cursor.execute(command)
                native = getattr(
                    self.raw, "jdbc_conn", getattr(self.raw, "jconn", None)
                )
                autocommit = getattr(self.raw, "autocommit", False) is True
                if native is not None and hasattr(native, "getAutoCommit"):
                    autocommit = bool(native.getAutoCommit())
                if not autocommit:
                    self.raw.commit()
            except Exception:
                try:
                    self.raw.rollback()
                except Exception:
                    pass
                self.warn("会话执行限制未完全启用，须核对该数据库版本或驱动的超时支持")

    def rollback(self):
        self.configured = False
        return self.raw.rollback()

    def close(self):
        return self.raw.close()


def guarded_connection(connection, kind):
    return (
        connection
        if isinstance(connection, InspectionConnection)
        else InspectionConnection(connection, kind)
    )


def inspection_endpoint(info, kind=None):
    import hashlib
    import json

    value = [
        db_family(kind or info.get("db_type") or info.get("_db_type") or ""),
        str(info.get("ssh_host") or "") if info.get("ssh_tunnel") else "",
        str(info.get("host") or info.get("ip") or "").strip().lower(),
        int(info.get("port") or 0),
    ]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


from contextlib import contextmanager


@contextmanager
def inspection_lease(info, kind):
    """同一配置端点仅运行一条巡检链，退出进程的租约可回收。"""
    import sqlite3
    import os
    import psutil
    import uuid
    import time
    from modules.core import paths

    key = inspection_endpoint(info, kind)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + LEASE_WAIT_SECONDS
    database = paths.DATA_DIR / "inspection_leases.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    while True:
        db = sqlite3.connect(database, timeout=5)
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS inspection_leases(endpoint TEXT PRIMARY KEY,pid INTEGER,started REAL,token TEXT)"
            )
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT pid,started FROM inspection_leases WHERE endpoint=?", (key,)
            ).fetchone()
            alive = False
            if current:
                try:
                    alive = (
                        abs(psutil.Process(current[0]).create_time() - current[1])
                        < 0.01
                    )
                except (psutil.Error, ValueError):
                    pass
            if not alive:
                db.execute(
                    "INSERT OR REPLACE INTO inspection_leases VALUES(?,?,?,?)",
                    (key, os.getpid(), psutil.Process().create_time(), token),
                )
                db.commit()
                break
            db.rollback()
        finally:
            db.close()
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "该数据库已有巡检占用，等待30秒后停止本次执行，请错峰重试"
            )
        time.sleep(0.5)
    try:
        yield
    finally:
        db = sqlite3.connect(database, timeout=5)
        try:
            db.execute(
                "DELETE FROM inspection_leases WHERE endpoint=? AND token=?",
                (key, token),
            )
            db.commit()
        finally:
            db.close()
