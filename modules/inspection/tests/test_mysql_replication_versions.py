# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""MySQL 系复制状态语句的跨版本适配回归。"""

import unittest

from modules.inspection import replication_sql as repl
from modules.inspection.engine import BaseInspectionEngine


class FakeCursor:
    def __init__(self, failed_sql, rows=None, columns=None):
        self.failed_sql = {sql.upper() for sql in failed_sql}
        self.rows = rows or [('mysql-bin.000001', 123)]
        self.columns = columns or [('File',), ('Position',)]
        self.description = None
        self.executed = []

    def execute(self, sql):
        self.executed.append(sql)
        if sql.strip().rstrip(';').upper() in self.failed_sql:
            raise RuntimeError(
                "java.sql.SQLSyntaxErrorException: You have an error in your SQL syntax; check the manual")
        self.description = [(name,) for name, in self.columns]

    def fetchmany(self, size):
        return list(self.rows)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class ReplicationVersionTest(unittest.TestCase):
    def test_mysql_version_matrix(self):
        cases = {
            '5.5.62': ('SHOW MASTER STATUS', 'SHOW SLAVE STATUS'),
            '5.6.51-log': ('SHOW MASTER STATUS', 'SHOW SLAVE STATUS'),
            '5.7.44-log': ('SHOW MASTER STATUS', 'SHOW SLAVE STATUS'),
            '8.0.21': ('SHOW MASTER STATUS', 'SHOW SLAVE STATUS'),
            '8.0.22': ('SHOW MASTER STATUS', 'SHOW REPLICA STATUS'),
            '8.0.36': ('SHOW MASTER STATUS', 'SHOW REPLICA STATUS'),
            '8.4.0': ('SHOW BINARY LOG STATUS', 'SHOW REPLICA STATUS'),
            '8.4.6': ('SHOW BINARY LOG STATUS', 'SHOW REPLICA STATUS'),
            '9.1.0': ('SHOW BINARY LOG STATUS', 'SHOW REPLICA STATUS'),
        }
        for version, (master, replica) in cases.items():
            with self.subTest(version=version):
                self.assertEqual(repl.replication_sql_for('mysql', version, 'master_status'), master)
                self.assertEqual(repl.replication_sql_for('mysql', version, 'slave_status'), replica)
                self.assertEqual(repl.replication_sql_for('mysql', version, 'replication_lag'), replica)
                self.assertEqual(repl.replication_sql_for('mysql', version, 'slave_io_running'), replica)

    def test_mariadb_and_percona_matrix(self):
        for version in ('10.3.39-MariaDB', '10.5.25-MariaDB', '10.11.6-MariaDB',
                        '11.4.2-MariaDB', '5.7.44-29.1-Percona'):
            with self.subTest(version=version):
                self.assertEqual(repl.replication_sql_for('mariadb', version, 'master_status'),
                                 'SHOW MASTER STATUS')
                self.assertEqual(repl.replication_sql_for('mariadb', version, 'replication_lag'),
                                 'SHOW SLAVE STATUS')
        self.assertEqual(repl.replication_sql_for('mysql', '8.0.36-28', 'slave_status'),
                         'SHOW REPLICA STATUS')

    def test_binlog_disabled_returns_explanation_row(self):
        sql = repl.replication_sql_for('mysql', '8.4.0', 'master_status', log_bin_enabled=False)
        self.assertIn('log_bin=OFF', sql)
        self.assertIn('说明', sql)

    def test_candidates_include_both_syntaxes_and_skip_failed(self):
        candidates = repl.replication_query_candidates(
            'mysql', '8.4.0', 'master_status', 'SHOW MASTER STATUS')
        self.assertNotIn('SHOW MASTER STATUS', candidates)
        self.assertIn('SHOW BINARY LOG STATUS', candidates)
        candidates = repl.replication_query_candidates(
            'mysql', '5.7.44', 'slave_status', 'SHOW SLAVE STATUS')
        self.assertIn('SHOW REPLICA STATUS', candidates)

    def test_replica_field_aliases(self):
        row = {'Source_Host': '10.0.0.1', 'Replica_IO_Running': 'Yes',
               'Replica_SQL_Running': 'Yes', 'Seconds_Behind_Source': '7'}
        self.assertEqual(repl.replica_field(row, 'Master_Host'), '10.0.0.1')
        self.assertEqual(repl.replication_lag_seconds(row), 7)
        self.assertEqual(repl.replication_threads(row), ('Yes', 'Yes'))
        old = {'Master_Host': '10.0.0.2', 'Slave_IO_Running': 'No',
               'Slave_SQL_Running': 'Yes', 'Seconds_Behind_Master': None}
        self.assertEqual(repl.replication_lag_seconds(old), None)
        self.assertEqual(repl.replication_threads(old), ('No', 'Yes'))

    def test_engine_rewrites_template_by_server_version(self):
        engine = BaseInspectionEngine('192.0.2.10', 3306, 'fixture', 'fixture')
        engine.db_type = 'mysql'
        engine.context['version'] = [{'VERSION': '8.4.0'}]
        queries = {'master_status': 'SHOW MASTER STATUS;',
                   'replication_lag': 'SHOW SLAVE STATUS',
                   'slave_io_running': 'SHOW SLAVE STATUS',
                   'slave_status': 'SHOW SLAVE STATUS',
                   'binlog_status': 'SHOW BINARY LOGS;'}
        engine._adapt_replication_queries(queries)
        self.assertEqual(queries['master_status'], 'SHOW BINARY LOG STATUS')
        self.assertEqual(queries['replication_lag'], 'SHOW REPLICA STATUS')
        self.assertEqual(queries['binlog_status'], 'SHOW BINARY LOGS;')

    def test_engine_keeps_tidb_non_replication_placeholder(self):
        engine = BaseInspectionEngine('192.0.2.10', 4000, 'fixture', 'fixture')
        engine.db_type = 'tidb'
        engine.context['version'] = [{'VERSION': '8.0.11-TiDB-v8.1.0'}]
        queries = {'master_status': "SELECT 'TiDB has no master-slave replication' AS info;"}
        engine._adapt_replication_queries(queries)
        self.assertIn('TiDB has no', queries['master_status'])

    def test_execute_query_safe_retries_syntax_error(self):
        engine = BaseInspectionEngine('192.0.2.10', 3306, 'fixture', 'fixture')
        engine.db_type = 'mysql'
        engine.context['version'] = [{'VERSION': '8.4.0'}]
        cursor = FakeCursor({'SHOW MASTER STATUS'})
        result = engine._execute_query_safe(cursor, 'SHOW MASTER STATUS', 'master_status')
        self.assertNotIn('_error', result)
        self.assertEqual(result['data'][0]['Position'], 123)
        self.assertEqual(engine.context['_query_sql_used']['master_status'],
                         'SHOW BINARY LOG STATUS')
        self.assertEqual(cursor.executed[0], 'SHOW MASTER STATUS')
        self.assertEqual(cursor.executed[1], 'SHOW BINARY LOG STATUS')

    def test_execute_query_safe_does_not_retry_non_replication_error(self):
        engine = BaseInspectionEngine('192.0.2.10', 3306, 'fixture', 'fixture')
        engine.db_type = 'mysql'
        engine.context['version'] = [{'VERSION': '8.4.0'}]
        cursor = FakeCursor({'SHOW TABLES'})
        result = engine._execute_query_safe(cursor, 'SHOW TABLES', 'tables')
        self.assertIn('_error', result)
        self.assertEqual(cursor.executed, ['SHOW TABLES'])


if __name__ == '__main__':
    unittest.main()
