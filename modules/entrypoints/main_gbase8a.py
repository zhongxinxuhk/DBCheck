# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""GBase 8a uses the existing inspection engine, metric catalog and report renderer."""


from modules.inspection.engine import BaseInspectionEngine
from modules.inspection import gbase_compat
from modules.db_types.compatibility import connect_gbase8a
from modules.core.data_security import redact


class GBase8aInspector(BaseInspectionEngine):
    # GBase 8a 的 information_schema.TABLES 在大集群上可能远超普通章节的
    # 15 秒预算。这两条查询单独用长预算连接执行，并按“专项采集”处理，
    # 避免一次超时把主连接置为 aborted 后连带拖垮 W07 等后续查询。
    _CAPACITY_QUERY_KEYS = ('W05_table_size_top', 'W06_schema_space')
    _CAPACITY_QUERY_TIMEOUT = 90

    def __init__(self,host,port,user,password,database=None,ssh_info=None,template_id=None,**kwargs):
        super().__init__(host,port,user,password,database,ssh_info,template_id)
        self.db_type='gbase8a'
        self._server_version=''

    def _customize_queries(self, sql_dict):
        # 先收集 W07 等轻量章节，再把可能很慢的容量查询排到最后；
        # 即使容量查询超时，其他章节也已经在主连接上采集完成。
        for key in self._CAPACITY_QUERY_KEYS:
            if key in sql_dict:
                sql_dict[key] = sql_dict.pop(key)
        super()._customize_queries(sql_dict)

    @staticmethod
    def _capacity_number(row, column):
        """按列名大小写不敏感地取数值，兼容 GBase/MySQL 驱动差异。"""
        target = column.upper()
        for key, value in row.items():
            if str(key).upper() != target:
                continue
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _update_capacity_metrics(self, item_name, data):
        """把容量明细回填成规则引擎需要的标量，避免重复扫描大表。"""
        if item_name == 'W05_table_size_top':
            values = [self._capacity_number(row, 'DATA_MB') for row in data]
            self.context['gbase8a_max_table_mb'] = round(max(values), 2) if values else 0
        elif item_name == 'W06_schema_space':
            values = [self._capacity_number(row, 'data_mb') for row in data]
            self.context['gbase8a_total_data_mb'] = round(sum(values), 2)

    def _mark_capacity_metric_unavailable(self, item_name):
        # 0 不是真实容量，但能让 fill_missing_metrics 不再用同一条重查询
        # 反复施压；章节本身会显示为“不适用/专项采集未完成”。
        if item_name == 'W05_table_size_top':
            self.context.setdefault('gbase8a_max_table_mb', 0)
        elif item_name == 'W06_schema_space':
            self.context.setdefault('gbase8a_total_data_mb', 0)

    def _execute_capacity_query(self, item_name, sql):
        """用独立连接执行容量查询，超时只影响当前章节。"""
        from modules.core.data_security import assert_readonly_sql
        from modules.core.dbapi_compat import fetch_rows
        from modules.inspection.sql_safety import guarded_connection

        raw = None
        conn = None
        try:
            assert_readonly_sql(sql)
            raw = connect_gbase8a({
                **self.ssh_info,
                'host': self.host,
                'port': self.port,
                'user': self.user,
                'password': self.password,
                'database': self.database,
                'read_timeout': self._CAPACITY_QUERY_TIMEOUT + 30,
                'write_timeout': self._CAPACITY_QUERY_TIMEOUT + 30,
            })
            conn = guarded_connection(
                raw, 'gbase8a', total_timeout=self._CAPACITY_QUERY_TIMEOUT + 30)
            conn.query_timeout = self._CAPACITY_QUERY_TIMEOUT
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [str(col[0]) for col in cursor.description]
            rows = fetch_rows(cursor, 1001)
            data = [dict(zip(columns, row)) for row in rows[:1000]]
            truncated = len(rows) > 1000 or getattr(cursor, 'truncated', False) is True
            if truncated:
                if not hasattr(self, '_safe_errors'):
                    self._safe_errors = []
                self._safe_errors.append('GBase 8a 容量查询结果超出安全返回范围，需要缩小范围后复核')
            self.context.setdefault('_query_status', {})[item_name] = {
                'status': 'collected', 'rows': len(data), 'truncated': truncated}
            self._update_capacity_metrics(item_name, data)
            return {'columns': columns, 'data': data, '_truncated': truncated}
        except Exception as error:
            reason = 'GBase 8a 容量查询超过专项预算已跳过（不影响其他章节）：%s' % str(error)[:180]
            self.context.setdefault('_query_status', {})[item_name] = {
                'status': 'not_applicable', 'reason': reason}
            self.context[item_name] = []
            self._mark_capacity_metric_unavailable(item_name)
            print('[WARN] %s: %s' % (item_name, reason))
            return {'columns': [], 'data': [], '_unavailable': reason}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            elif raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass

    def collect_data(self,sql_templates=''):
        if self._template_id is None:
            ok,version=self.connect()
            if not ok:raise ValueError(version)
            self._template_id=self.get_template_id()
            if self._template_id is None:
                raise ValueError('巡检模板尚未初始化，请刷新巡检指标配置')
        return super().collect_data(sql_templates)

    def _execute_query_safe(self, cursor, sql, item_name=""):
        if item_name in self._CAPACITY_QUERY_KEYS:
            return self._execute_capacity_query(item_name, sql)
        # Coordinator metadata commands can lose their VC session context.
        # Rebind the configured VC before node-scoped reads, including reused sessions.
        import re
        scoped = re.search(r'\bSHOW\s+(?:GCLUSTER\s+)?NODES\b|\bINFORMATION_SCHEMA\.(?:GNODES_|CLUSTER_MONIT_INFO)', sql, re.I)
        if scoped and self.database:
            vc = str(self.database).strip()
            if not re.fullmatch(r'[A-Za-z0-9_]+', vc):
                raise ValueError('GBase 8a 虚拟集群名格式无效')
            select_vc = getattr(cursor, 'select_virtual_cluster', None)
            if callable(select_vc):
                select_vc(vc)
            else:
                cursor.execute('USE VC ' + vc)
        return super()._execute_query_safe(cursor, sql, item_name=item_name)

    def connect(self):
        if self.conn is not None:
            return True,self._server_version
        try:
            self.conn=connect_gbase8a({**self.ssh_info,'host':self.host,'port':self.port,
                                      'user':self.user,'password':self.password,'database':self.database})
            self.cursor=self.conn.cursor()
            # 版本探测按候选链执行：不同 8a 版本对 VERSION_COMMENT 的支持不一致，
            # 组合查询一旦含不支持的系统变量会连带丢掉 VERSION()。
            self._server_version, parsed = gbase_compat.fetch_gbase8a_version(self.cursor)
            if not self._server_version or 'gbase' not in self._server_version.lower():
                raise ValueError('服务端未返回GBase 8a版本标识，请核对类型和端口')
            self._db_version_major = parsed.major or None
            self.context['gbase8a_version_detail'] = [{
                'VERSION': self._server_version,
                'FAMILY': parsed.family,
                'BUILD': parsed.build,
            }]
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
