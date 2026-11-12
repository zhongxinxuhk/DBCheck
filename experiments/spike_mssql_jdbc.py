#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
M1 spike 实验脚本 —— 验证 JPype1 + mssql-jdbc-13.4.0.jre11.jar 链路。

目标（与 architecture §7 T-015 验收对齐）：
  1. 启动 JVM，加载 drivers/sqlserver/mssql-jdbc-13.4.0.jre11.jar
  2. Class.forName 触发 SQLServerDriver 静态初始化
  3. DriverManager.getConnection 建立连接
  4. 执行 3 个最小验证查询：
     a) SELECT @@VERSION          （版本）
     b) SELECT name FROM sys.databases   （库清单）
     c) SELECT TOP 5 name FROM sys.tables （表清单）
  5. 打印结构化结果 + 错误保护

用法：
  python experiments/spike_mssql_jdbc.py <host> <port> <user> <password> [database]
  例：
  python experiments/spike_mssql_jdbc.py 127.0.0.1 1433 sa YourStrong!Passw0rd master

环境要求：
  - default-jre-headless（Dockerfile L91 已装）
  - JPype1>=1.5.0（requirements-docker.txt L24-25 已装）
  - drivers/sqlserver/mssql-jdbc-13.4.0.jre11.jar（由 Jack 从 Microsoft Learn 下载）

返回码：
  0 全部成功
  1 JVM 启动失败
  2 驱动类加载失败
  3 连接失败
  4 查询失败
"""

import glob
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _PROJECT_ROOT)


def _find_mssql_jar() -> str:
    """定位 mssql-jdbc-*.jar 绝对路径（取第一个匹配）。"""
    candidates = glob.glob(
        os.path.join(_PROJECT_ROOT, "drivers", "sqlserver", "**", "mssql-jdbc-*.jar"),
        recursive=True,
    )
    if not candidates:
        raise FileNotFoundError(
            "未找到 mssql-jdbc-*.jar；请将 MS JDBC 13.4 放入 drivers/sqlserver/\n"
            "  下载：https://learn.microsoft.com/sql/connect/jdbc/download-microsoft-jdbc-driver-for-sql-server"
        )
    return sorted(candidates)[0]


def main():
    if len(sys.argv) < 5:
        print("用法: python spike_mssql_jdbc.py <host> <port> <user> <password> [database]")
        sys.exit(1)

    host = sys.argv[1]
    port = int(sys.argv[2])
    user = sys.argv[3]
    password = sys.argv[4]
    database = sys.argv[5] if len(sys.argv) > 5 else "master"

    print("=" * 70)
    print("MSSQL-JDBC M1 SPIKE  ── JPype1 + mssql-jdbc-13.4.0.jre11.jar")
    print("=" * 70)
    print(f"host={host}  port={port}  user={user}  database={database}")

    # 1. 定位 jar
    try:
        jar = _find_mssql_jar()
    except FileNotFoundError as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)
    print(f"\n[1/5] jar 定位: {jar}")
    print(f"      size = {os.path.getsize(jar) / 1024:.1f} KB")

    # 2. 启动 JVM
    print(f"\n[2/5] 启动 JVM ...")
    t0 = time.time()
    import jpype
    try:
        if not jpype.isJVMStarted():
            jpype.startJVM(classpath=[jar], convertStrings=True)
            print(f"      OK（耗时 {time.time() - t0:.2f}s）")
        else:
            print("      JVM 已由其它组件启动（OK）")
    except (RuntimeError, OSError) as e:
        print(f"      FAIL: {e}")
        sys.exit(1)

    # 3. 注册驱动
    print(f"\n[3/5] 注册驱动 Class.forName ...")
    try:
        jpype.JClass("java.lang.Class").forName(
            "com.microsoft.sqlserver.jdbc.SQLServerDriver"
        )
        print("      OK（com.microsoft.sqlserver.jdbc.SQLServerDriver）")
    except (jpype.JException, TypeError) as e:
        print(f"      FAIL: {e}")
        sys.exit(2)

    # 4. 建立连接
    print(f"\n[4/5] DriverManager.getConnection ...")
    # 不用 ``from java.sql import ...``：``java`` 是 JPype 运行时注入的虚拟包，
    # 静态分析阶段无法解析（会报“未解析的引用”）；JClass 是等价写法且可被 IDE 识别。
    driver_manager = jpype.JClass("java.sql.DriverManager")
    properties_cls = jpype.JClass("java.util.Properties")

    url = (
        f"jdbc:sqlserver://{host}:{port};"
        f"databaseName={database};"
        f"encrypt=true;trustServerCertificate=true;"
        f"loginTimeout=10;applicationName=DBCheck-Spike"
    )
    props = properties_cls()
    props.setProperty("user", user)
    props.setProperty("password", password)
    try:
        conn = driver_manager.getConnection(url, props)
        print(f"      OK  url={url}")
    except jpype.JException as e:
        print(f"      FAIL: {e}")
        sys.exit(3)

    # 5. 执行 3 个最小验证查询
    print(f"\n[5/5] 执行 3 个验证查询 ...")
    queries = [
        ("SELECT @@VERSION", "@@VERSION"),
        ("SELECT name FROM sys.databases ORDER BY name", "sys.databases"),
        ("SELECT TOP 5 name, type_desc FROM sys.tables ORDER BY name", "sys.tables (TOP 5)"),
    ]
    failures = 0
    try:
        for sql, label in queries:
            try:
                stmt = conn.createStatement()
                rs = stmt.executeQuery(sql)
                meta = rs.getMetaData()
                cols = [meta.getColumnName(i + 1) for i in range(meta.getColumnCount())]
                print(f"\n  ── {label} ──")
                print(f"  SQL: {sql}")
                print(f"  columns: {cols}")
                rows = []
                while rs.next():
                    row = []
                    for i in range(meta.getColumnCount()):
                        obj = rs.getObject(i + 1)
                        row.append(str(obj)[:80] if obj is not None else "")
                    rows.append(row)
                if not rows:
                    print("  (无数据)")
                else:
                    # 简单表格打印
                    widths = [
                        max(len(str(r[i])) for r in [row] + rows)
                        for i, row in enumerate(rows[:1])
                    ]
                    for i, c in enumerate(cols):
                        widths[i] = max(widths[i], len(c))
                    print("  | " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)) + " |")
                    print("  | " + " | ".join("-" * w for w in widths) + " |")
                    for row in rows:
                        print("  | " + " | ".join(r.ljust(widths[i]) for i, r in enumerate(row)) + " |")
                stmt.close()
            except jpype.JException as e:
                print(f"  FAIL  ({label}): {e}")
                failures += 1
    finally:
        try:
            conn.close()
        except jpype.JException:
            pass

    print("\n" + "=" * 70)
    if failures == 0:
        print("M1 SPIKE PASS — 3/3 查询成功")
        sys.exit(0)
    else:
        print(f"M1 SPIKE PARTIAL — {failures} 个查询失败")
        sys.exit(4)


if __name__ == "__main__":
    main()
