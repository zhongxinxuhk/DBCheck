# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""巡检检出与健康评分的回归测试。

覆盖三个曾经导致“巡检未检出异常”的缺陷：
- 基线 FAIL/ERROR 被插件 auto_analyze 覆盖后丢失；
- 有风险项时评分仍沿用早期“优秀”状态而显示 100；
- 规则消息里的 fix_params 占位符未被求值。
"""

import unittest
from unittest.mock import patch

from modules.inspection.analyzer import collect_issues
from modules.intelligence.inspection_runner import _score
from modules.pro.rule_engine import RuleEngine


class DetectionRegressionTests(unittest.TestCase):
    def test_mysql_slow_query_normalizes_jdbc_values(self):
        from modules.inspection.slow_query import MySQLSlowQueryAnalyzer

        class JdbcString:
            def __str__(self):
                return 'SELECT  1'

        class JdbcInteger:
            def __str__(self):
                return '18446744073709551615'

        result = MySQLSlowQueryAnalyzer().normalize({'top_by_latency': [{
            'query_sample_text': JdbcString(), 'tmp_disk_tables': JdbcInteger(),
        }]})
        self.assertEqual(result.top_sql_by_latency[0]['query_text'], 'SELECT 1')
        self.assertTrue(result.summary['has_tmp_disk_spill'])
        self.assertEqual(result.top_sql_by_latency[0]['tmp_disk_tables'], 18446744073709551615)

    def test_gbase_scalar_query_does_not_use_derived_table(self):
        from unittest.mock import Mock
        from modules.inspection.sql_safety import InspectionConnection
        raw = Mock()
        cursor = raw.cursor.return_value
        cursor.stmt = None
        connection = InspectionConnection(raw, 'gbase8a')
        connection.configured = True
        connection.cursor().execute('SELECT VERSION() AS version')
        sql = cursor.execute.call_args.args[0]
        self.assertNotIn('DBPULSE_BOUNDED', sql)
        connection.cursor().execute('SELECT * FROM information_schema.TABLES')
        self.assertIn('DBPULSE_BOUNDED', cursor.execute.call_args.args[0])

    def test_baseline_issues_are_kept_when_auto_analyze_exists(self):
        context = {
            'auto_analyze': [{'col1': '插件问题', 'col2': '中风险'}],
            'baseline_issues': [{'col1': '基线问题', 'col2': '高风险'}],
        }
        issues = collect_issues('oracle', context)
        titles = [item.get('col1') for item in issues]
        self.assertIn('插件问题', titles)
        self.assertIn('基线问题', titles)

    def test_risk_count_never_scores_100(self):
        score, risk_count, risk_level, status = _score({
            'risk_count': 4,
            'health_status': '优秀',
        })
        self.assertEqual(score, 80)
        self.assertEqual(risk_count, 4)
        self.assertEqual(risk_level, 'low')
        self.assertNotEqual(status, '优秀')

    def test_rule_fix_params_format_message(self):
        engine = RuleEngine()
        rules = {rule.get('id'): rule for rule in engine.get_enabled_rules('oracle')}
        with patch('modules.pro.rule_engine.load_thresholds',
                   return_value={'db_oracle_buf_cache_warn_min_pct': 95.0}):
            issue = engine._run_rule(
                rules['oracle_sga_cache_hit_low'],
                'oracle',
                {'library_cache_hit_ratio': 74.42, '_lang': 'zh'},
            )
        self.assertIsNotNone(issue)
        self.assertEqual(issue['col3'], '库缓存命中率 74.4%，SQL 重复解析开销大')
        self.assertNotIn('{', issue['col3'])


if __name__ == '__main__':
    unittest.main()
