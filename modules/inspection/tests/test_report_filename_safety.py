# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""报告文件名安全化与报告失败原因回传的回归测试。"""


import unittest
from unittest.mock import patch

from modules.core.filename_safety import (
    build_report_filename,
    sanitize_filename_component,
)
from modules.inspection.engine import BaseInspectionEngine


class ReportFilenameTest(unittest.TestCase):
    def test_datasource_name_with_windows_illegal_chars_is_sanitized(self):
        name = build_report_filename(
            'SQL Server (JDBC)巡检报告_{ip}_{name}_{ts}',
            ip='10.60.6.216', name='10.60.6.216:1433/主库',
            timestamp='20261119_145535', suffix='0f1e2d3c',
            extension='.docx',
        )
        for character in '<>:"/\\|?*':
            self.assertNotIn(character, name)
        self.assertTrue(name.endswith('.docx'))
        self.assertIn('10.60.6.216', name)

    def test_reserved_device_name_and_trailing_dots(self):
        self.assertEqual(sanitize_filename_component('CON'), '_CON')
        self.assertEqual(sanitize_filename_component('nul.txt'), '_nul.txt')
        self.assertEqual(sanitize_filename_component('  report...  '), 'report')

    def test_empty_and_overlong_values_fall_back_safely(self):
        self.assertEqual(sanitize_filename_component('', fallback='db'), 'db')
        long_name = build_report_filename(
            '{name}_{ts}', name='x' * 500, timestamp='20261119_145535',
            suffix='task', extension='.docx')
        self.assertLessEqual(len(long_name), 140 + 1 + 64 + len('.docx'))
        self.assertTrue(long_name.endswith('.docx'))

    def test_broken_template_still_produces_a_filename(self):
        name = build_report_filename('{unknown}_{ts}', ip='10.0.0.1',
                                     timestamp='20261119_145535')
        self.assertTrue(name.endswith('.docx'))
        self.assertIn('20261119_145535', name)


class ReportFailureDetailTest(unittest.TestCase):
    def test_generate_report_records_real_exception(self):
        engine = BaseInspectionEngine('10.0.0.1', 1433, 'user', 'secret')
        engine.db_type = 'sqlserver_jdbc'
        engine.context = {}
        with patch.object(engine, '_load_word_template',
                          side_effect=OSError(22, 'Invalid argument')):
            self.assertIsNone(engine.generate_report('/tmp/dbcheck-nope.docx'))
        self.assertIn('OSError', engine.last_report_error)
        self.assertIn('Invalid argument', engine.last_report_error)

    def test_successful_start_resets_previous_failure(self):
        engine = BaseInspectionEngine('10.0.0.1', 1433, 'user', 'secret')
        engine.db_type = 'sqlserver_jdbc'
        engine.context = {}
        engine.last_report_error = 'old failure'
        engine._render_context = lambda output, name: False
        engine.generate_report('/tmp/dbcheck-nope.docx')
        # 渲染失败会被记录为当前失败原因，而不是沿用上一轮的旧文本。
        self.assertNotEqual(engine.last_report_error, 'old failure')


if __name__ == '__main__':
    unittest.main()


class CliReportHelpersTest(unittest.TestCase):
    def test_cli_report_filename_is_safe(self):
        from modules.inspection import run
        name = run._report_filename(
            'MySQL巡检报告', {'label': '10.60.6.216:1433/主库'},
            '20261119_145535')
        for character in '<>:"/\\|?*':
            self.assertNotIn(character, name)
        self.assertTrue(name.endswith('.docx'))
        self.assertIn('20261119_145535', name)

    def test_cli_failure_includes_engine_reason(self):
        from modules.inspection import run

        class Target:
            last_report_error = 'OSError: [Errno 22] Invalid argument'

        with self.assertRaises(RuntimeError) as caught:
            run._raise_report_failure(Target())
        self.assertIn('OSError', str(caught.exception))
