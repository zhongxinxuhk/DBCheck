#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
SQL Server 数据库巡检模块 - 基于 BaseInspectionEngine 重构版本

使用方式：
    from modules.entrypoints.main_sqlserver import SQLServerInspector
    inspector = SQLServerInspector(host, port, user, password, database, ssh_info)
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


class SQLServerInspector(BaseInspectionEngine):
    """
    SQL Server 数据库巡检器 - 继承 BaseInspectionEngine
    
    只需实现 connect() 方法，其他逻辑全部在基类中！
    """
    
    def __init__(self, host, port, user, password, database=None, ssh_info=None, template_id=None):
        """
        初始化 SQL Server 巡检器

        :param host: SQL Server 服务器 IP 地址或主机名
        :param port: SQL Server 服务端口（默认 1433）
        :param user: SQL Server 登录用户名
        :param password: SQL Server 登录密码
        :param database: 要连接的数据库名（可选）
        :param ssh_info: SSH 连接信息字典（可选）
        :param template_id: 巡检模板 ID（可选，指定后使用对应模板的 SQL）
        """
        super().__init__(host, port, user, password, database, ssh_info, template_id)
        self.db_type = 'sqlserver'
    
    def collect_data(self,sql_templates=''):
        if self._template_id is None:
            ok,version=self.connect()
            if not ok:raise ValueError(version)
            self._template_id=self.get_template_id()
            if self._template_id is None:
                raise ValueError('巡检模板尚未初始化，请刷新巡检指标配置')
        return super().collect_data(sql_templates)

    def connect(self):
        if self.conn is not None:
            return True,getattr(self,'_server_version','SQL Server')
        try:
            from modules.db_types.compatibility import connect_sqlserver
            self.conn=connect_sqlserver({**self.ssh_info,'host':self.host,'port':self.port,
                                        'user':self.user,'password':self.password,'database':self.database})
            self.cursor=self.conn.cursor()
            self.cursor.execute("SELECT SERVERPROPERTY('ProductVersion'), @@VERSION")
            row=self.cursor.fetchone()
            self._db_version_major=int(str(row[0]).split('.')[0])
            self._server_version=str(row[1])
            return True,self._server_version
        except Exception as error:
            if self.conn:
                self.conn.close();self.conn=None
            from modules.core.data_security import redact
            return False,str(redact(str(error).replace(self.password,'[REDACTED]') if self.password else str(error)))

    def get_template_id(self):
        if self._template_id is not None:
            return self._template_id
        version={9:'2005',10:'2008',12:'2014'}.get(getattr(self,'_db_version_major',None))
        if version:
            import sqlite3
            from modules.core.paths import INSPECTION_DB
            with sqlite3.connect(str(INSPECTION_DB)) as database:
                row=database.execute("SELECT id FROM inspection_template WHERE db_type='sqlserver' AND version=? ORDER BY id LIMIT 1",(version,)).fetchone()
            if row:return row[0]
        return super().get_template_id()


# ── 保留原有 API 兼容性（供 web_ui.py 旧代码调用）────────────────────
def getData(ip, port, user, password, database=None, ssh_info=None, label=None, template_id=None):
    """
    原有 API - 创建 SQLServerInspector 实例
    
    注意：这个函数在重构过程中保留，用于兼容 web_ui.py 中的旧代码。
    新代码应该直接使用 SQLServerInspector 类。
    """
    inspector = SQLServerInspector(ip, port, user, password, database, ssh_info, template_id)
    ok, ver = inspector.connect()
    if not ok:
        return None
    # 为了兼容旧代码，返回一个对象，其中包含 conn_db2 属性
    class CompatWrapper:
        def __init__(self, inspector):
            self.inspector = inspector
            self.conn_db2 = inspector.conn
        def checkdb(self, sqlfile=''):
            self.inspector.collect_data()
            return self.inspector.context
        def generate_report(self, output_file, inspector_name="Jack"):
            return self.inspector.generate_report(output_file, inspector_name)
    return CompatWrapper(inspector)

def main():
    """SQL Server 巡检 CLI 入口"""
    entry.ensure_bootstrapped('sqlserver')
    import getpass

    print(u"SQL Server 数据库巡检")
    print(u"=" * 50)

    host = input(u"主机地址 [localhost]: ") or "localhost"
    port = int(input(u"端口 [1433]: ") or 1433)
    user = input(u"用户名: ")
    if not user:
        print(u"用户名不能为空"); return
    password = getpass.getpass(u"密码: ")
    database = input(u"数据库名 [master]: ") or "master"

    inspector = SQLServerInspector(host, port, user, password, database)
    ok, ver = inspector.connect()
    if not ok:
        print(u"连接失败: {}".format(ver)); return
    print(u"连接成功: {}".format(ver))

    inspector.collect_data()
    name = "{}_{}".format(host, port)
    output = "SQLServer_Inspection_Report_{}.docx".format(name)
    inspector.generate_report(output, name)
    print(u"报告已生成: {}".format(output))


if __name__ == '__main__':
    main()
