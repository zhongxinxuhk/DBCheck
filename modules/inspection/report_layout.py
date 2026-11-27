# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Shared assessment and document structure for database inspection reports."""

from collections import Counter

from modules.inspection.report_insights import (
    COMMAND_DIAGNOSTICS, DIAGNOSTICS, GUIDANCE, PRODUCT_REFERENCES, RULE_CATEGORIES,
    build_rule_index, diagnostic_sql,
    guidance_for, issue_state, issue_title, localized, matching_issue_states, metric_state, state_label,
)


def metric_reading(doc, key, title, value, state, rules, lang, issues=()):
    outcome = metric_state(value, state, rules, matching_issue_states(issues, key, title))
    guidance = GUIDANCE[guidance_for(key, title)]
    lines = [
        (localized(lang, '判定', 'Assessment'), state_label(outcome, lang)),
        (localized(lang, '指标用途', 'What it measures'), localized(lang, *guidance.meaning)),
        (localized(lang, '业务影响', 'Potential impact'), localized(lang, *guidance.impact)),
    ]
    relevant = [row for row in rules if row['triggered']] or [row for row in rules if row['assessed']]
    if relevant:
        lines.append((localized(lang, '判定依据', 'Rule evidence'), ' · '.join(
            row['title'] if lang.startswith('zh') else row['title_en'] for row in relevant[:3])))
    if state.get('truncated'):
        lines.append((localized(lang, '采集范围', 'Collection scope'),
                      localized(lang, '结果已截断，判断仅覆盖已取得的样本。',
                                'The result was truncated. Assessment covers the retrieved sample.')))
    if state.get('reason'):
        lines.append((localized(lang, '采集说明', 'Collection note'), str(state['reason'])[:360]))
    if outcome in {'critical', 'warning', 'notice', 'partial', 'unavailable'}:
        lines.append((localized(lang, '建议', 'Next action'), localized(lang, *guidance.action)))
    paragraph = doc.add_paragraph(style='Caption')
    for index, (label, text) in enumerate(lines):
        if index:
            paragraph.add_run().add_break()
        paragraph.add_run(label + '  ').bold = True
        paragraph.add_run(text)
    paragraph.paragraph_format.keep_with_next = True


def _heading_styles(doc):
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    for name in ('Normal', 'Caption', 'Subtitle'):
        style = doc.styles[name]
        style.font.name = 'Calibri'
        style.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
        if name == 'Normal':
            style.paragraph_format.space_after = Pt(4)
            style.paragraph_format.line_spacing = 1.15
        if name == 'Caption':
            style.font.color.rgb = RGBColor(64, 64, 64)
            style.font.italic = False
    for name, size in (('Title', 22), ('Heading 1', 16), ('Heading 2', 12), ('Heading 3', 11)):
        style = doc.styles[name]
        style.font.name = 'Calibri'
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.size = Pt(size)
        style.font.bold = True
        style.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.keep_together = True
        style.paragraph_format.space_before = Pt(12 if name == 'Heading 1' else 8)
        style.paragraph_format.space_after = Pt(5)
    title_style = doc.styles['Title'].element.get_or_add_pPr()
    for border in title_style.findall(qn('w:pBdr')):
        title_style.remove(border)
    # Existing report sources apply direct styles, so normalize those headings too.
    for paragraph in doc.paragraphs:
        if paragraph.style.name not in ('Title', 'Heading 1', 'Heading 2', 'Heading 3'):
            continue
        paragraph.paragraph_format.keep_together = True
        paragraph.paragraph_format.keep_with_next = True
        for run in paragraph.runs:
            run.font.color.rgb = RGBColor(0, 0, 0)
            run.font.size = paragraph.style.font.size

    for index, section in enumerate(doc.sections):
        if section.orientation == WD_ORIENT.PORTRAIT:
            section.page_width = Cm(21)
            section.page_height = Cm(29.7)
            section.left_margin = section.right_margin = Cm(2.5)
            section.top_margin = section.bottom_margin = Cm(2.3)
        if index and section.footer.is_linked_to_previous:
            continue
        footer = section.footer.paragraphs[0]
        if footer.text or list(footer._p.iter(qn('w:fldSimple'))):
            continue
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        page = OxmlElement('w:fldSimple')
        page.set(qn('w:instr'), 'PAGE')
        footer._p.append(page)


def _table(doc, headings, rows, widths):
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    table = doc.add_table(rows=1, cols=len(headings), style='Table Grid')
    table.autofit = False
    for column, width in zip(table.columns, widths):
        column.width = Cm(width)
    first = table.rows[0]
    first._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
    borders = OxmlElement('w:tblBorders')
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        line = OxmlElement(f'w:{edge}')
        for attribute, value in (('val', 'single'), ('sz', '4'), ('color', 'D9D9D9')):
            line.set(qn(f'w:{attribute}'), value)
        borders.append(line)
    table._tbl.tblPr.append(borders)
    for index, values in enumerate([headings, *rows]):
        row = first if index == 0 else table.add_row()
        row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        for cell, text, width in zip(row.cells, values, widths):
            cell.width = Cm(width)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.text = str(text)
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(4)
            paragraph.paragraph_format.space_before = Pt(4)
            for run in paragraph.runs:
                run.font.size = Pt(10)
                run.bold = index == 0
            if index == 0:
                fill = OxmlElement('w:shd')
                fill.set(qn('w:fill'), 'E7EEF6')
                cell._tc.get_or_add_tcPr().append(fill)
    return table


def _condition_text(row):
    data = {**row['inputs'], **row['thresholds']}
    details = ', '.join(f'{key}={value:.4g}' for key, value in data.items())
    return row['condition'] + (f'\n{details}' if details else '')


def _diagnostic_for(db_type, category):
    from modules.pro.rule_engine import load_metrics, normalize_db_type

    db_type = normalize_db_type(db_type)
    default = DIAGNOSTICS.get(db_type)
    if default and default[0] == category:
        return diagnostic_sql(default[1])
    if db_type in COMMAND_DIAGNOSTICS:
        command_category, command = COMMAND_DIAGNOSTICS[db_type]
        return command if command_category == category else ''
    for metric in load_metrics(db_type):
        if guidance_for(str(metric.get('key', ''))) == category:
            sql = diagnostic_sql(metric.get('sql'))
            if sql:
                return sql
    return ''


def _action_plan(doc, db_type, issues, rules, lang):
    tr = lambda zh, en: localized(lang, zh, en)
    if not issues:
        doc.add_paragraph(tr('按业务峰值复测采集不足或缺少判定依据的项目，补齐监测记录和实例基线。',
                             'Recheck unassessed or incomplete items at peak load and record the evidence and instance baseline.'))
        return

    categories = {}
    for issue in issues:
        evidence = rules.get(str(issue.get('_rule_id') or ''), {})
        category = evidence.get('category')
        category = RULE_CATEGORIES.get(category, category)
        if category not in GUIDANCE:
            category = guidance_for(issue_title(issue), str(issue.get('col3') or ''))
        categories.setdefault(category, []).append(issue)

    for category, items in categories.items():
        guidance = GUIDANCE[category]
        title = issue_title(items[0]) or tr('巡检发现', 'Inspection finding')
        if len(items) > 1:
            title += tr(f' 等 {len(items)} 项', f' and {len(items) - 1} related items')
        doc.add_heading(title, level=2)
        for issue in items:
            name = issue_title(issue) or tr('巡检发现', 'Inspection finding')
            description = str(issue.get('col3') or issue.get('message') or issue.get('description') or '')
            label = state_label(issue_state(issue), lang)
            owner = issue.get('col5') or issue.get('owner')
            text = f'{label} · {name}' + (tr('。', '. ') + description if description else '')
            if owner:
                text += tr('  建议负责角色 ', '  Suggested owner ') + str(owner)
            doc.add_paragraph(text)
        for label, text in (
            (tr('可能影响', 'Potential impact'), tr(*guidance.impact)),
            (tr('优化策略', 'Optimization'), tr(*guidance.action)),
            (tr('复测标准', 'Acceptance'), tr(*guidance.acceptance)),
        ):
            p = doc.add_paragraph()
            p.add_run(label + '  ').bold = True
            p.add_run(text)
        statements = list(dict.fromkeys(filter(None, (diagnostic_sql(item.get('fix_sql')) for item in items))))
        if not statements:
            candidate = _diagnostic_for(db_type, category)
            if candidate:
                statements = [candidate]
        if statements:
            doc.add_paragraph(tr('只读诊断示例  由 DBA 按产品版本和权限复核后使用',
                                  'Read-only diagnostics  Review the product version and privileges before use'))
            for sql in statements:
                p = doc.add_paragraph()
                p.add_run(sql).font.name = 'Consolas'
        if category in ('performance', 'locking'):
            _query_examples(doc, db_type, lang)


def _query_examples(doc, db_type, lang):
    from modules.pro.rule_engine import normalize_db_type

    tr = lambda zh, en: localized(lang, zh, en)
    kind = normalize_db_type(db_type)
    if kind in ('redis', 'redis-cluster'):
        doc.add_paragraph(tr('命令优化示例  使用 SCAN 分批遍历，按返回游标继续，并处理重复键。',
                             'Command rewrite  Scan incrementally, resume from the returned cursor and handle duplicates.'))
        doc.add_paragraph('SCAN 0 MATCH example:* COUNT 100')
        return
    if kind == 'mongodb':
        doc.add_paragraph(tr('查询优化示例  只投影业务需要的字段，并复核过滤条件是否命中索引。',
                             'Query rewrite  Project only required fields and check the filter for index use.'))
        doc.add_paragraph('db.example.find({status: "open"}, {_id: 0, orderId: 1}).explain("queryPlanner")')
        return
    doc.add_paragraph(tr('SQL 改写示例  若时间列上的函数阻碍索引访问，可改写为同一时区的半开区间。替换示例对象及绑定参数后核对结果一致性。',
                         'SQL rewrite example  If a function on the time column prevents index access, use a half-open range in the same time zone. Replace the example object and bind parameters, then verify result equivalence.'))
    doc.add_paragraph('SELECT order_id FROM example_orders\n'
                      'WHERE order_time >= :range_start AND order_time < :range_end;')
    if kind == 'sqlserver':
        doc.add_paragraph(tr('在 SSMS 查看估计执行计划，重点比较扫描、查找及估算行数。',
                              'Use the estimated execution plan in SSMS to review scans, seeks and estimated rows.'))
    elif kind in ('pg', 'mysql', 'mariadb', 'tidb', 'clickhouse'):
        doc.add_paragraph(tr('使用对应版本的 EXPLAIN 复核计划。',
                             'Validate the plan using the EXPLAIN syntax for the installed version.'))


def format_inspection_report(doc, db_type, context, issues, lang='zh'):
    """Place a scope-aware decision summary ahead of the evidence tables."""
    from docx.oxml.ns import qn
    from modules.inspection.report_terms import VENDOR_REFERENCES, core_family

    tr = lambda zh, en: localized(lang, zh, en)
    by_key, by_id = build_rule_index(db_type, context)
    risks = ('critical', 'warning', 'notice')
    issues = sorted((issue for issue in issues if isinstance(issue, dict)),
                    key=lambda issue: risks.index(issue_state(issue)))
    counts = Counter(issue_state(issue) for issue in issues)
    states = {**(context.get('_metric_status') or {}), **(context.get('_query_status') or {})}
    pending = Counter(metric_state(context.get(key), state, by_key.get(key, ()))
                      for key, state in states.items())
    incomplete = (context.get('collection_status') == 'incomplete'
                  or any(state.get('truncated') for state in states.values())
                  or any(pending[status] for status in ('partial', 'unavailable', 'missing')))
    controls = sorted(by_id.values(), key=lambda row: (
        not row['triggered'], not row['assessed'], risks.index(row['state'])))
    outcome = next((risk for risk in risks if counts[risk] or any(
        row['triggered'] and row['state'] == risk for row in controls)), None)
    if outcome is None:
        outcome = 'partial' if incomplete else ('healthy' if any(row['assessed'] for row in controls) else 'missing')

    original = list(doc.element.body)
    anchor = next((p._p for p in doc.paragraphs if p.style.name == 'Heading 1'), None)
    if anchor is None:
        anchor = next((item for item in original if item.tag == qn('w:tbl')), original[-1])
    doc.add_heading(tr('巡检结论与处置优先级', 'Inspection assessment and priorities'), level=1)
    doc.add_heading(tr('总体判定与采集覆盖', 'Overall assessment and coverage'), level=2)
    overall = tr('已评估项健康', 'Healthy within assessed scope') if outcome == 'healthy' else state_label(outcome, lang)
    if doc.tables:
        for row in doc.tables[0].rows:
            cells = row.cells
            if len(cells) == 2 and cells[0].text in ('巡检结果', 'Status'):
                cells[1].text = overall
                break
    doc.add_paragraph(tr('总体判定', 'Overall assessment') + '  ' + overall)
    doc.add_paragraph(tr('已记录发现', 'Recorded findings') + '  ' +
                      ' · '.join(f'{state_label(risk, lang)} {counts[risk]}' for risk in risks))
    if states:
        doc.add_paragraph(tr('采集记录', 'Collection records') + f'  {len(states)} · ' +
                          ' · '.join(f'{state_label(state, lang)} {count}' for state, count in pending.items()))
    if incomplete:
        doc.add_paragraph(tr('采集范围有缺口，未取得的数据需复核后再下结论。',
                              'Review unavailable evidence before reaching a conclusion on the affected scope.'))
    doc.add_paragraph(tr('健康表示已评估规则在本次范围内未触发。趋势、未采集项和没有阈值的指标仍需按业务基线复核。',
                          'Healthy means the evaluated rules did not trigger within this scope. Review trends, missing data and unthresholded metrics against the business baseline.'))
    assessed = [row for row in controls if row['assessed']]
    if assessed:
        doc.add_heading(tr('关键规则与判定依据', 'Key rules and evidence'), level=2)
        rows = [[row['title'] if lang.startswith('zh') else row['title_en'],
                 state_label(row['state'], lang) if row['triggered'] else tr('未触发', 'Not triggered'),
                 _condition_text(row)] for row in assessed[:10]]
        _table(doc, tr(['检查项', '状态', '触发条件与实测输入'],
                       ['Check', 'Status', 'Trigger and recorded inputs']), rows, [4.0, 2.0, 9.0])
        if len(assessed) > 10:
            doc.add_paragraph(tr(f'此处展示 {len(rows)} 条重点规则，本次共评估 {len(assessed)} 条。',
                                  f'Showing {len(rows)} priority checks of {len(assessed)} assessed.'))
    doc.add_heading(tr('优化策略与复测', 'Optimization and follow-up'), level=1)
    _action_plan(doc, db_type, issues, by_id, lang)
    doc.add_paragraph(tr('配置、索引和 SQL 改写先在受控环境验证，记录执行计划、回退方案和审批结果。',
                          'Validate configuration, index and SQL changes in a controlled environment. Record the query plan, rollback and approvals.'))
    for element in list(doc.element.body)[len(original) - 1:-1]:
        anchor.addprevious(element)

    doc.add_heading(tr('编制依据与判定口径', 'Reporting references'), level=1)
    doc.add_paragraph(tr('文档结构参考 GB/T 8567-2006 与 ISO/IEC/IEEE 15289:2019。指标的单位和可用范围以对应版本的厂商文档为准，告警阈值使用实际生效的巡检规则和实例基线。',
                          'Document structure references GB/T 8567-2006 and ISO/IEC/IEEE 15289:2019. Use the vendor manual for metric units and availability. Alert limits follow the active inspection rules and instance baseline.'))
    doc.add_paragraph('GB/T 8567-2006  https://std.samr.gov.cn/gb/search/gbDetailedCNF?id=71F772D7FDE1D3A7E05397BE0A0AB82A')
    doc.add_paragraph('ISO/IEC/IEEE 15289:2019  https://www.iso.org/standard/74909.html')
    from modules.pro.rule_engine import normalize_db_type
    reference = VENDOR_REFERENCES.get(core_family(db_type)) or PRODUCT_REFERENCES.get(normalize_db_type(db_type), ())
    for item in reference:
        doc.add_paragraph(item)
    _heading_styles(doc)
    return outcome
