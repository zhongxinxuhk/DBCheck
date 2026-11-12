# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""GBase 8a uses the existing inspection engine, metric catalog and report renderer."""


from modules.inspection.engine import BaseInspectionEngine
from modules.db_types.compatibility import connect_gbase8a
from modules.core.data_security import redact


class GBase8aInspector(BaseInspectionEngine):
    def __init__(self,host,port,user,password,database=None,ssh_info=None,template_id=None,**kwargs):
        super().__init__(host,port,user,password,database,ssh_info,template_id)
        self.db_type='gbase8a'
        self._server_version=''

    def collect_data(self,sql_templates=''):
        if self._template_id is None:
            ok,version=self.connect()
            if not ok:raise ValueError(version)
            self._template_id=self.get_template_id()
            if self._template_id is None:
                raise ValueError('巡检模板尚未初始化，请刷新巡检指标配置')
        return super().collect_data(sql_templates)

    def _execute_query_safe(self, cursor, sql, item_name=""):
        # Coordinator metadata commands can lose their VC session context.
        # Rebind the configured VC before node-scoped reads, including reused sessions.
        import re
        scoped = re.search(r'\bSHOW\s+(?:GCLUSTER\s+)?NODES\b|\bINFORMATION_SCHEMA\.(?:GNODES_|CLUSTER_MONIT_INFO)', sql, re.I)
        if scoped and self.database:
            vc = str(self.database).strip()
            if not re.fullmatch(r'[A-Za-z0-9_]+', vc):
                raise ValueError('GBase 8a 虚拟集群名格式无效')
            cursor.execute('USE VC ' + vc)
        return super()._execute_query_safe(cursor, sql, item_name=item_name)

    def connect(self):
        if self.conn is not None:
            return True,self._server_version
        try:
            self.conn=connect_gbase8a({**self.ssh_info,'host':self.host,'port':self.port,
                                      'user':self.user,'password':self.password,'database':self.database})
            self.cursor=self.conn.cursor()
            self.cursor.execute('SELECT VERSION(), @@VERSION_COMMENT')
            row=self.cursor.fetchone()
            self._server_version=' '.join(str(value) for value in row or ())
            if 'gbase' not in self._server_version.lower():
                raise ValueError('服务端未返回GBase 8a版本标识，请核对类型和端口')
            return True,self._server_version
        except Exception as error:
            if self.conn:
                self.conn.close();self.conn=None
            return False,str(redact(str(error).replace(self.password,'[REDACTED]') if self.password else str(error)))


def getData(ip,port,user,password,ssh_info=None,template_id=None,database=None,**kwargs):
    inspector=GBase8aInspector(ip,port,user,password,database,ssh_info,template_id)
    ok,_=inspector.connect()
    if not ok:return None
    class CompatWrapper:
        def __init__(self):
            self.inspector=inspector
            self.conn_db2=inspector.conn
        def checkdb(self,sqlfile=''):
            inspector.collect_data()
            return inspector.context
        def generate_report(self,output_file,inspector_name='Jack'):
            return inspector.generate_report(output_file,inspector_name)
    return CompatWrapper()
