#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Offline regression for business defaults, cursor contracts and version gates."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.inspection import dal
from modules.inspection.business_templates import ITEM_SQL, ensure_business_defaults
from modules.inspection.engine import BaseInspectionEngine
from modules.core.data_security import assert_readonly_sql
from modules.driver_registry import db_family
from modules.pro.rule_engine import RuleEngine


class CompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = cls.tmp.name + '/inspection.db'
        with contextlib.redirect_stdout(io.StringIO()):
            dal.init_database(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fresh_presets_and_aliases(self):
        for token in ('oracle_jdbc','sqlserver_jdbc','dm','gbase8a'):
            presets = dal.get_templates_by_db_type(token, db_path=self.db, include_family=True)
            specials = [r for r in presets if '业务专项巡检' in r['template_name']]
            self.assertEqual(len(specials), 12, token)
            self.assertTrue(all(r['is_preset'] for r in specials))
            default = dal.get_default_template(token, self.db, db_version_major=11)
            self.assertIn('周巡检', default['template_name'])
            self.assertTrue(dal.get_baselines_by_db_type(token, db_path=self.db, include_family=True))
        self.assertEqual(db_family('hgdb_jdbc'), 'postgresql')
        self.assertNotEqual(db_family('gbase8a'),db_family('gbase'))

    def test_seed_preserves_disabled_queries_and_user_sql(self):
        conn = dal.get_db_connection(self.db)
        try:
            row=conn.execute('SELECT id FROM inspection_baseline WHERE db_type=? LIMIT 1',('gbase8a',)).fetchone()
            conn.execute("UPDATE inspection_baseline SET enabled=0,expected_value='custom' WHERE id=?",(row['id'],))
            conn.commit()
            n=conn.execute('SELECT COUNT(*) FROM inspection_template').fetchone()[0]
            ensure_business_defaults(self.db)
            self.assertEqual(n,conn.execute('SELECT COUNT(*) FROM inspection_template').fetchone()[0])
            saved=conn.execute('SELECT enabled,expected_value FROM inspection_baseline WHERE id=?',(row['id'],)).fetchone()
            self.assertEqual(tuple(saved),(0,'custom'))
        finally:
            conn.close()

    def test_plugin_write_scope_remains_exact(self):
        self.assertEqual(dal.get_templates_by_db_type('oracle_jdbc',db_path=self.db), [])
        self.assertTrue(all(r['db_type']=='oracle_jdbc' for r in dal.get_baselines_by_db_type('oracle_jdbc',db_path=self.db)))
        with __import__('unittest').mock.patch('modules.jdbc_connector.resolve_driver_jars', return_value=['fixture.jar']):
            from modules.jdbc_test_cli import _find_dm_jdbc_jar
            self.assertEqual(_find_dm_jdbc_jar(), 'fixture.jar')

    def test_catalog_readonly_guard(self):
        for product,items in ITEM_SQL.items():
            for key,_,sql in items.values():
                with self.subTest(product=product,key=key):
                    assert_readonly_sql(sql)
        for sql in ("EXEC sp_configure 'xp_cmdshell',1",'DBCC CHECKDB WITH REPAIR_ALLOW_DATA_LOSS',"EXEC master.dbo.sp_readerrorlog 0,1,N'Error'; DROP TABLE x"):
            with self.assertRaises(ValueError):
                assert_readonly_sql(sql)

    def test_legacy_feature_gates(self):
        engine=BaseInspectionEngine('localhost',1433,'test','test')
        engine.db_type='sqlserver';engine._db_version_major=9
        cur=Mock()
        result=engine._execute_query_safe(cur,'SELECT * FROM sys.dm_hadr_availability_replica_states','ag')
        cur.execute.assert_not_called()
        self.assertIn('_unavailable',result)
        self.assertEqual(engine.context['_query_status']['ag']['status'],'not_applicable')

    def test_baseline_comparison(self):
        engine=BaseInspectionEngine('localhost',1433,'test','test')
        self.assertEqual(engine._compare_baseline('utf8mb4','like','utf8',None,None),'PASS')
        self.assertEqual(engine._compare_baseline('0.000000','=','0',None,None),'PASS')
        self.assertEqual(engine._compare_baseline('UNLIMITED','<=','180',None,None),'FAIL')

    def test_explicit_oracle_template_and_cursor_contract(self):
        path=ROOT/'plugins/enabled/oracle_jdbc/main_plugin.py'
        spec=importlib.util.spec_from_file_location('oracle_cursor_regression',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        inspector=module.OracleJdbcInspector('localhost',1521,'test','test',template_id=98765)
        self.assertEqual(inspector.get_template_id(),98765)
        conn=Mock();cur=module.JdbcCursorWrapper(conn)
        rs=Mock();cur.rs=rs
        rs.getMetaData().getColumnCount.return_value=1
        rs.next.side_effect=[True,True,True,False,False]
        rs.getObject.side_effect=[11,22,33]
        cur._convert_java_obj=lambda value:value
        self.assertEqual(cur.fetchmany(2),[(11,),(22,)])
        self.assertEqual(cur.fetchone(),(33,))
        self.assertEqual(cur.fetchmany(2),[])
        self.assertEqual(cur.fetchall(),[])
        cur.execute('DELETE FROM fixture')
        self.assertIsNone(cur.description)
        self.assertIsNone(cur.rs)

    def test_cluster_rule_fires_only_on_observed_offline_nodes(self):
        engine=RuleEngine()
        rule=next(r for r in engine.get_enabled_rules('gbase8a') if r['id']=='gbase8a_data_node_offline')
        bad=engine._run_rule(rule,'gbase8a',{'gbase8a_data_nodes':[{'status':'offline'}]})
        good=engine._run_rule(rule,'gbase8a',{'gbase8a_data_nodes':[{'status':'online'}]})
        self.assertTrue(bad)
        self.assertFalse(good)


if __name__ == '__main__':
    unittest.main(verbosity=2)
