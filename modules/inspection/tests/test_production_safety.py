# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Regression coverage for bounded production inspection execution."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from modules.inspection import sql_safety


class ProductionSafetyTests(unittest.TestCase):
    def test_comment_removal_preserves_quoted_sql(self):
        sql = "SELECT '2026--11', '/*literal*/', [a--b] FROM SYS.DATABASES -- comment\n"
        cleaned = sql_safety.strip_sql_comments(sql)
        self.assertIn("'2026--11'", cleaned)
        self.assertIn("'/*literal*/'", cleaned)
        self.assertIn('[a--b]', cleaned)
        self.assertNotIn('-- comment', cleaned)

    def test_business_sources_lock_hints_and_parallelism_are_rejected(self):
        for sql in [
            'SELECT COUNT(*) FROM SYSBUSINESS',
            'SELECT * FROM SYS.DATABASES WITH (UPDLOCK)',
            'SELECT * FROM SYS.DATABASES WITH (TABLOCKX)',
            'SELECT * FROM SYS.DATABASES OPTION (MAXDOP 8)',
            'SELECT * FROM SYS.DATABASES OPTION (MAXDOP 0)',
            'SELECT * FROM information_schema.tables FOR SHARE',
        ]:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                sql_safety.assert_inspection_sql(sql)
        sql_safety.assert_inspection_sql('SELECT * FROM SYS.DATABASES')

    def test_prepared_statement_limits_precede_execution(self):
        statement = Mock()
        native = Mock()
        native.prepareStatement.return_value = statement
        bounded = sql_safety._BoundedJDBC(native, 1000, 15)
        prepared = bounded.prepareStatement('SELECT * FROM SYS.DATABASES')
        prepared.execute()
        self.assertEqual(statement.mock_calls[:5], [
            unittest.mock.call.setQueryTimeout(15),
            unittest.mock.call.setMaxRows(1001),
            unittest.mock.call.setFetchSize(100),
            unittest.mock.call.setMaxFieldSize(16384),
            unittest.mock.call.execute(),
        ])

    def test_billion_row_source_is_fetched_with_a_fixed_bound(self):
        raw = Mock()
        cursor = raw.cursor.return_value
        cursor.stmt = None
        cursor.fetchmany.side_effect = lambda size: [(index,) for index in range(min(size, 1_000_000_000))]
        connection = sql_safety.InspectionConnection(raw, 'sqlserver')
        connection.configured = True
        guarded = connection.cursor()
        guarded.execute('SELECT * FROM SYS.DATABASES')
        self.assertEqual(len(guarded.fetchall()), 1000)
        self.assertTrue(guarded.truncated)
        self.assertEqual(guarded.fetchall(), [])
        cursor.fetchmany.assert_called_once_with(1001)

    def test_collection_deadline_aborts_later_queries(self):
        raw = Mock()
        raw.cursor.return_value.stmt = None
        connection = sql_safety.InspectionConnection(raw, 'oracle', total_timeout=-1)
        connection.configured = True
        with self.assertRaises(TimeoutError):
            connection.cursor().execute('SELECT 1 FROM DUAL')
        self.assertTrue(connection.aborted)
        raw.cursor.return_value.execute.assert_not_called()

    def test_session_configuration_has_a_cancellation_budget(self):
        released = threading.Event()
        raw = Mock()
        cursor = raw.cursor.return_value
        cursor.stmt = None
        cursor.execute.side_effect = lambda sql: released.wait(0.5)
        cursor.cancel.side_effect = released.set
        with patch.object(sql_safety, 'QUERY_TIMEOUT_SECONDS', 0.02):
            connection = sql_safety.InspectionConnection(raw, 'oracle')
            with self.assertRaises(TimeoutError):
                connection.cursor().execute('SELECT 1 FROM DUAL')
        self.assertTrue(connection.aborted)
        self.assertEqual(cursor.execute.call_count, 1)

    def test_nested_endpoint_leases_release_after_exception(self):
        with tempfile.TemporaryDirectory() as folder, patch('modules.core.paths.DATA_DIR', Path(folder)):
            endpoint = {'host': '192.0.2.8', 'port': 1521}
            with self.assertRaisesRegex(ValueError, 'fixture'):
                with sql_safety.inspection_lease(endpoint, 'oracle'):
                    with sql_safety.inspection_lease(endpoint, 'oracle'):
                        raise ValueError('fixture')
            with sql_safety.inspection_lease(endpoint, 'oracle'):
                pass

    def test_endpoint_aliases_share_a_lease_identity(self):
        self.assertEqual(
            sql_safety.inspection_endpoint({'host':'localhost','port':1521},'oracle'),
            sql_safety.inspection_endpoint({'ip':'127.0.0.1','port':1521},'oracle_jdbc'))

    def test_preset_sql_migration_preserves_user_changes(self):
        import sqlite3
        from modules.inspection import dal
        from modules.inspection.init_db import ORACLE_DEFAULT_CHAPTERS
        current = next(query['sql'] for chapter in ORACLE_DEFAULT_CHAPTERS
                       for query in chapter['queries'] if query['key']=='temp_space')
        old = current.replace('SUM(bytes_used + bytes_free)','SUM(bytes)').replace(
            'SUM(bytes_used)','SUM(NVL(blocks,0))*8192').replace('v$temp_space_header','dba_temp_files')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'metadata.db'
            def connect(*args):
                connection=sqlite3.connect(path)
                connection.row_factory=sqlite3.Row
                return connection
            with connect() as connection:
                connection.executescript('CREATE TABLE inspection_template(id,db_type,is_preset);'
                    'CREATE TABLE inspection_chapter(id,template_id);'
                    'CREATE TABLE inspection_query(id,chapter_id,query_key,query_sql);'
                    "INSERT INTO inspection_template VALUES(1,'oracle',1),(2,'oracle',0);"
                    'INSERT INTO inspection_chapter VALUES(1,1),(2,2);')
                connection.executemany('INSERT INTO inspection_query VALUES(?,?,?,?)',[
                    (1,1,'temp_space',old),(2,1,'temp_space',old+' -- edited'),(3,2,'temp_space',old)])
            with patch.object(dal,'get_db_connection',side_effect=connect):
                self.assertEqual(dal.ensure_production_query_fixes(),1)
                self.assertEqual(dal.ensure_production_query_fixes(),0)
            with connect() as connection:
                values=connection.execute('SELECT query_sql FROM inspection_query ORDER BY id').fetchall()
            self.assertEqual(values[0][0],current)
            self.assertEqual(values[1][0],old+' -- edited')
            self.assertEqual(values[2][0],old)

    def test_driver_fallback_respects_version_directories(self):
        from modules.jdbc_connector import _sort_jars_by_version, resolve_driver_jars
        from modules import jdbc_connector
        import zipfile
        self.assertEqual(_sort_jars_by_version([
            '/drivers/sqlserver/4.2/sqljdbc42.jar',
            '/drivers/sqlserver/13.4.0/mssql-jdbc-13.4.0.jar'])[0],
            '/drivers/sqlserver/13.4.0/mssql-jdbc-13.4.0.jar')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            jar=root/'drivers/oracle/6/ojdbc6.jar'
            jar.parent.mkdir(parents=True)
            with zipfile.ZipFile(jar,'w') as archive:
                archive.writestr('oracle/jdbc/driver/OracleDriver.class',b'fixture')
            with patch('modules.core.paths.PROJECT_ROOT',root), \
                 patch.object(jdbc_connector,'resolve_jdbc_driver_jars',return_value=None), \
                 patch.object(jdbc_connector,'_order_jars_for_jvm',side_effect=lambda jars:jars), \
                 patch.object(jdbc_connector,'_append_driver_deps',side_effect=lambda kind,jars:jars):
                self.assertEqual(resolve_driver_jars('oracle_jdbc','6'),[str(jar)])
                self.assertIsNone(resolve_driver_jars('oracle_jdbc','19'))

    def test_dm_recursive_fallback_reaches_driver_resolver(self):
        from modules import jdbc_connector
        with patch.object(jdbc_connector,'resolve_driver_jars',return_value=None) as resolve:
            connection,_=jdbc_connector.open_jdbc_connection('dm','localhost',5236,'fixture','fixture',
                fallback_dirs=['/fixture/drivers/dm'],recursive=True)
        self.assertIsNone(connection)
        self.assertTrue(resolve.call_args.kwargs['recursive'])

    def test_oracle_preflight_and_execution_share_connection_options(self):
        import importlib.util
        root=Path(__file__).resolve().parents[3]
        for directory in ['enabled','available']:
            spec=importlib.util.spec_from_file_location('audit_oracle_'+directory,
                root/'plugins'/directory/'oracle_jdbc/main_plugin.py')
            plugin=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(plugin)
            config=plugin.get_task_config()
            info={'ip':'192.0.2.1','port':1521,'user':'fixture','password':'fixture',
                  'sid':'LEGACY','use_sid':True,'driver_version':'6','jdbc_url':'jdbc:oracle:thin:@fixture'}
            arguments=config['connect_test_args'](info)
            self.assertEqual(arguments[4:],[info['sid'],False,info['jdbc_url'],'6',True])
            _,options=config['getdata_args'](info)
            self.assertEqual(options['service_name'],'LEGACY')
            self.assertEqual(options['jdbc_url'],info['jdbc_url'])
            self.assertTrue(options['use_sid'])

    def test_gbase_vc_selection_is_bounded_and_restricted(self):
        raw=Mock()
        raw.cursor.return_value.stmt=None
        connection=sql_safety.InspectionConnection(raw,'gbase8a')
        cursor=connection.cursor()
        cursor.select_virtual_cluster('vc1')
        raw.cursor.return_value.execute.assert_called_once_with('USE VC vc1')
        with self.assertRaises(ValueError):
            cursor.select_virtual_cluster('vc1; DROP DATABASE prod')
        with self.assertRaises(ValueError):
            cursor.execute('USE VC vc1')

    def test_large_metadata_cells_are_bounded_and_marked_partial(self):
        raw=Mock()
        raw.cursor.return_value.stmt=None
        raw.cursor.return_value.fetchmany.return_value=[('x'*100000,7)]
        connection=sql_safety.InspectionConnection(raw,'oracle')
        connection.configured=True
        cursor=connection.cursor()
        cursor.execute('SELECT * FROM V$SQL')
        rows=cursor.fetchall()
        self.assertEqual(len(rows[0][0]),16384)
        self.assertEqual(rows[0][1],7)
        self.assertTrue(cursor.truncated)

    def test_partial_cluster_visibility_is_not_labeled_standalone(self):
        from modules.inspection.tests.test_basic_info import FakeCursor
        from modules.inspection.basic_info import collect_db
        cursor=FakeCursor([
            ('from gv$instance',['N','H'],[('node1','host1')]),
            ("name='cluster_database'",['V'],[('TRUE',)])])
        value=collect_db(cursor,'oracle')
        self.assertIn('待核对',value['cluster_attrs'])
        self.assertNotIn('单实例',value['cluster_attrs'])
        value=collect_db(FakeCursor([('from v$dsc_ep_info',['NAME'],[])]),'dm')
        self.assertNotEqual(value.get('cluster_attrs'),'单实例')

    def test_oracle_sid_is_used_by_cluster_jdbc_url(self):
        from modules.jdbc_connector import build_jdbc_url
        self.assertEqual(build_jdbc_url('oracle_jdbc','localhost',1521,use_sid=True,sid='LEGACY'),
                         'jdbc:oracle:thin:@localhost:1521:LEGACY')

    def test_engine_keeps_guard_truncation_status(self):
        from modules.inspection.engine import BaseInspectionEngine
        raw = Mock()
        raw.cursor.return_value.stmt = None
        raw.cursor.return_value.description = [('VALUE',)]
        raw.cursor.return_value.fetchmany.return_value = [(index,) for index in range(1001)]
        connection = sql_safety.InspectionConnection(raw, 'oracle')
        connection.configured = True
        engine = SimpleNamespace(context={}, _safe_errors=[], _query_errors={}, conn=connection,
                                 _query_unavailable_reason=lambda cursor, sql: None)
        result = BaseInspectionEngine._execute_query_safe(engine, connection.cursor(), 'SELECT 1 FROM DUAL', 'fixture')
        self.assertEqual(len(result['data']), 1000)
        self.assertTrue(result['_truncated'])
        self.assertTrue(engine.context['_query_status']['fixture']['truncated'])


if __name__ == '__main__':
    unittest.main()
