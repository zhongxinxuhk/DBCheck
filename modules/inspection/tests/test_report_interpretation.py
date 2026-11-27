# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from docx import Document

from modules.inspection.report_insights import diagnostic_sql, matching_issue_states, metric_state
from modules.inspection.report_layout import format_inspection_report, metric_reading
from modules.pro.rule_engine import report_rule_evidence


def _rule():
    return {
        'id': 'fixture_storage', 'name_zh': '表空间', 'severity': 'high',
        'params': {'usage': '${context.storage[0].pct}'},
        'condition': 'usage > tbs_limit',
    }


def test_threshold_and_recorded_evidence_determine_metric_health():
    context = {'storage': [{'pct': 90}]}
    alert = report_rule_evidence(_rule(), context, {'tbs_limit': 85})
    assert alert['triggered'] and alert['inputs'] == {'usage': 90}
    assert alert['thresholds'] == {'tbs_limit': 85}
    assert metric_state(context['storage'], {}, [alert]) == 'critical'
    normal = report_rule_evidence(_rule(), context, {'tbs_limit': 95})
    assert metric_state(context['storage'], {}, [normal]) == 'healthy'
    missing = report_rule_evidence(_rule(), {}, {'tbs_limit': 85})
    assert not missing['assessed']
    assert metric_state(None, {}, [missing]) == 'missing'
    assert metric_state([], {'status': 'collected'}, []) == 'observe'
    assert metric_state(None, {'status': 'not_applicable'}, [alert]) == 'not_applicable'
    assert metric_state(None, {'status': 'error'}, [alert]) == 'unavailable'
    assert metric_state([1], {'truncated': True}, []) == 'partial'
    matching = matching_issue_states([{'col1': 'Tablespace usage', 'col2': 'High Risk'}],
                                     'tbs_usage', 'tablespace usage')
    assert metric_state([90], {'status': 'collected'}, [], matching) == 'critical'


def test_shared_report_shows_findings_evidence_headings_and_verified_sql():
    doc = Document()
    doc.add_heading('巡检报告', level=0)
    doc.add_page_break()
    doc.add_heading('采集证据', level=1)
    metric_reading(doc, 'slow_query', '慢查询', [], {'status': 'timeout'}, [], 'zh')
    issue = {'col1': '慢查询', 'col2': '高风险', 'col3': '峰值延迟上升',
             'fix_sql': "ALTER SYSTEM KILL SESSION '1,1' IMMEDIATE;"}
    result = format_inspection_report(doc, 'oracle', {'_query_status': {
        'slow_query': {'status': 'error'}}}, [issue])
    assert result == 'critical'
    headings = [(p.style.name, p.text) for p in doc.paragraphs if p.style.name.startswith('Heading')]
    assert headings[0] == ('Heading 1', '巡检结论与处置优先级')
    assert ('Heading 2', '慢查询') in headings
    text = '\n'.join(p.text for p in doc.paragraphs)
    for value in ('业务影响', '采集失败', '优化策略', '复测标准', 'SQL 改写示例',
                  'GB/T 8567-2006', '本次'):
        assert value in text
    assert 'ALTER SYSTEM' not in text
    assert diagnostic_sql('SHOW PROCESSLIST;')
    assert diagnostic_sql('SELECT 1; DROP TABLE example_orders;') == ''
    assert diagnostic_sql('SELECT * FROM {table};') == ''


def test_report_does_not_call_unassessed_data_healthy():
    doc = Document()
    doc.add_heading('Evidence', level=1)
    state = format_inspection_report(doc, 'mongodb', {'_query_status': {
        'mongodb_connections': {'status': 'error'}}}, [], 'en')
    assert state == 'partial'
    text = '\n'.join(p.text for p in doc.paragraphs)
    assert 'Overall assessment  Partial collection' in text
    assert 'Healthy within assessed scope' not in text
