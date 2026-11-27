#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
MySQL 数据库巡检模块 - 基于 BaseInspectionEngine 重构版本

使用方式：
    from modules.entrypoints.main_mysql import MySQLInspector
    inspector = MySQLInspector(host, port, user, password, database, ssh_info)
    ok, ver = inspector.connect()
    if ok:
        inspector.collect_data()
        inspector.generate_report(output_file, inspector_name)
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import os
from modules.inspection.engine import BaseInspectionEngine
from modules.core import entry


class MySQLInspector(BaseInspectionEngine):
    """
    MySQL 数据库巡检器 - 继承 BaseInspectionEngine
    
    只需实现 connect() 方法，其他逻辑全部在基类中！
    """
    
    def __init__(self, host, port, user, password, database=None, ssh_info=None, template_id=None, driver_version=''):
        """
        初始化 MySQL 巡检器

        :param host: MySQL 服务器 IP 地址或主机名
        :param port: MySQL 服务端口
        :param user: MySQL 登录用户名
        :param password: MySQL 登录密码
        :param database: 要连接的数据库名（可选）
        :param ssh_info: SSH 连接信息字典（可选）
        :param template_id: 巡检模板 ID（可选，指定后使用对应模板的 SQL）
        :param driver_version: JDBC 驱动版本（可选，驱动管理登记；空=激活版本）
        """
        super().__init__(host, port, user, password, database, ssh_info, template_id)
        self.db_type = 'mysql'
        self.driver_version = driver_version

    def connect(self):
        """
        连接 MySQL 数据库（统一 JDBC 优先，回退 pymysql）。

        返回:
            (ok, version) - ok 为 True 时 version 是版本号，否则是错误信息
        """
        try:
            from modules.jdbc_connector import open_jdbc_connection
            _conn, _meta = open_jdbc_connection(
                'mysql', self.host, int(self.port),
                user=self.user, password=self.password,
                database=self.database or 'mysql',
                driver_version=self.driver_version,
            )
            if _conn is None:
                return self._connect_native((_meta or {}).get('error') or 'JDBC 连接失败')
            self.conn = _conn
            self.cursor = self.conn.cursor()
            self.cursor.execute("SELECT VERSION()")
            ver = self.cursor.fetchone()[0]
            return True, ver
        except Exception as e:
            return self._connect_native(str(e))

    def _connect_native(self, reason=''):
        """回退：pymysql 原生连接（JDBC 驱动缺失/JVM 异常时）。

        巡检子进程未 gevent monkey-patch，pymysql 原生调用安全。
        """
        try:
            import pymysql
            self.conn = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database or 'mysql',
                charset='utf8mb4',
                connect_timeout=10,
                read_timeout=60
            )
            self.cursor = self.conn.cursor()
            self.cursor.execute("SELECT VERSION()")
            ver = self.cursor.fetchone()[0]
            print(f"[MySQL] pymysql 回退连接成功（JDBC: {reason[:80]}...）" if reason else "[MySQL] pymysql 连接成功")
            return True, ver
        except Exception as e2:
            return False, (f'{reason}；pymysql 回退也失败: {e2}' if reason else str(e2))

    def _resolve_innodb_table(self, modern, legacy):
        """解析 MySQL information_schema 的 InnoDB 表名。

        INNODB_TABLESPACES / INNODB_DATAFILES 仅 MySQL 8.0.21+ 存在；
        5.7 / 8.0 早期版本为 INNODB_SYS_TABLESPACES / INNODB_SYS_DATAFILES。
        以「能否真正 SELECT」做运行时探测，比查 information_schema.TABLES 目录可靠；
        两者皆不可用时返回 None（调用方降级为空结果，章节不再报 Unknown table）。

        :param modern: 8.0.21+ 表名，如 'INNODB_TABLESPACES'
        :param legacy: 旧版本表名，如 'INNODB_SYS_TABLESPACES'
        :return: 实际可查询的表名；两者皆不可用时返回 None
        """
        conn = getattr(self, 'conn', None)
        if conn is None:
            return modern
        for tbl in (modern, legacy):
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM information_schema.%s LIMIT 0" % tbl)
                cur.fetchall()
                cur.close()
                return tbl
            except Exception:
                try:
                    cur.close()
                except Exception:
                    pass
        return None

    def _customize_queries(self, sql_dict):
        """覆盖基类空实现：MySQL 单库巡检时，把相关查询过滤到指定 schema；
        并对 InnoDB 表空间/数据文件表名做版本探测（8.0.21+ 与旧版表名不同）。"""
        from modules.inspection.engine import scope_mysql_schema
        scope_mysql_schema(sql_dict, self.database)

        # InnoDB 表空间/数据文件：表名随 MySQL 版本变化（8.0.21+ 才有
        # INNODB_TABLESPACES / INNODB_DATAFILES，更早为 INNODB_SYS_*），
        # 运行时探测；两者皆不可用时降级为空结果，避免 Unknown table 报错。
        df_tbl = self._resolve_innodb_table('INNODB_DATAFILES', 'INNODB_SYS_DATAFILES')
        ts_tbl = self._resolve_innodb_table('INNODB_TABLESPACES', 'INNODB_SYS_TABLESPACES')
        if 'innodb_datafiles' in sql_dict:
            sql_dict['innodb_datafiles'] = (
                "SELECT * FROM information_schema.%s;" % df_tbl
                if df_tbl else
                "SELECT 'INNODB_DATAFILES_UNAVAILABLE' AS note WHERE 1=0;"
            )
        if 'innodb_tablespaces' in sql_dict:
            sql_dict['innodb_tablespaces'] = (
                "SELECT * FROM information_schema.%s;" % ts_tbl
                if ts_tbl else
                "SELECT 'INNODB_TABLESPACES_UNAVAILABLE' AS note WHERE 1=0;"
            )


# ── 保留原有 API 兼容性（供 web_ui.py 旧代码调用）────────────────────
def getData(ip, port, user, password, ssh_info=None, template_id=None, database=None, driver_version=''):
    """
    原有 API - 创建 MySQLInspector 实例

    注意：这个函数在重构过程中保留，用于兼容 web_ui.py 中的旧代码。
    新代码应该直接使用 MySQLInspector 类。
    """
    inspector = MySQLInspector(ip, port, user, password, database, ssh_info, template_id, driver_version)
    ok, ver = inspector.connect()
    if not ok:
        # 不再吞掉真实错误：直接抛出，由上层（web_ui.run_inspection_task / run_inspection.py）捕获并展示真实原因
        raise ConnectionError("MySQL 连接失败: " + str(ver))
    # 为了兼容旧代码，返回一个对象，其中包含 conn_db2 属性
    class CompatWrapper:
        def __init__(self, inspector):
            self.inspector = inspector
            self.conn_db2 = inspector.conn
        def checkdb(self, sqlfile=''):
            self.inspector.collect_data()
            return self.inspector.context
        def generate_report(self, output_file, inspector_name="Jack"):
            """委托给 inspector.generate_report()"""
            return self.inspector.generate_report(output_file, inspector_name)
    return CompatWrapper(inspector)


def create_word_template(inspector_name):
    """原有 API - 创建 Word 模板"""
    import tempfile
    from docx import Document
    doc = Document()
    fd, path = tempfile.mkstemp(suffix='.docx')
    os.close(fd)
    doc.save(path)
    return path


def saveDoc(context, ofile, ifile, inspector_name):
    """原有 API - 保存 Word 报告（空壳，供极端旧版兼容）"""
    class CompatWrapper:
        def __init__(self, context, ofile):
            self.context = context
            self.ofile = ofile
        def contextsave(self):
            from docx import Document
            doc = Document()
            doc.save(self.ofile)
            return True
    return CompatWrapper(context, ofile)

def main():
    """MySQL 巡检 CLI 入口"""
    entry.ensure_bootstrapped('mysql')
    import getpass

    print(u"MySQL 数据库巡检")
    print(u"=" * 50)

    host = input(u"主机地址 [localhost]: ") or "localhost"
    port = int(input(u"端口 [3306]: ") or 3306)
    user = input(u"用户名: ")
    if not user:
        print(u"用户名不能为空"); return
    password = getpass.getpass(u"密码: ")
    database = input(u"数据库名 [mysql]: ") or "mysql"

    inspector = MySQLInspector(host, port, user, password, database)
    ok, ver = inspector.connect()
    if not ok:
        print(u"连接失败: {}".format(ver)); return
    print(u"连接成功: {}".format(ver))

    inspector.collect_data()
    name = "{}_{}".format(host, port)
    output = "MySQL_Inspection_Report_{}.docx".format(name)
    inspector.generate_report(output, name)
    print(u"报告已生成: {}".format(output))


if __name__ == '__main__':
    main()
