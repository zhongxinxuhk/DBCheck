# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""巡检报告的网络地址显示与系统字典字段说明回归验证。"""


import copy
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from modules.core.data_security import redact
from modules.inspection.engine import BaseInspectionEngine
from modules.intelligence.schema_knowledge import get_knowledge, report_field_labels
from modules.service_management.report import sanitize_document


class ReportFieldsTest(unittest.TestCase):
    def test_network_option_preserves_addresses_and_masks_other_identities(self):
        source = {'host': '192.0.2.10', 'details': ['192.0.2.10:55432'],
                  'password': 'fixture-secret', 'username': 'fixture-user',
                  'contact': 'fixture@example.org'}
        clean = redact(source, mask_identity=True, preserve_network=True)
        self.assertEqual(clean['host'], source['host'])
        self.assertEqual(clean['details'], source['details'])
        self.assertEqual(clean['password'], '[REDACTED]')
        self.assertEqual(clean['username'], '[MASKED]')
        self.assertEqual(clean['contact'], '[EMAIL]')
        self.assertEqual(source['password'], 'fixture-secret')
        self.assertEqual(redact(source, mask_identity=True)['host'], '[MASKED]')
        self.assertEqual(redact(source, mask_identity=True)['details'], ['[IP]:55432'])

    def test_document_sanitizer_network_option(self):
        doc = Document()
        doc.add_paragraph('192.0.2.10:55432 password=fixture-secret')
        doc.sections[0].header.paragraphs[0].text = '192.0.2.10'
        sanitize_document(doc, preserve_network=True)
        self.assertEqual(doc.paragraphs[0].text, '192.0.2.10:55432 password=[REDACTED]')
        self.assertEqual(doc.sections[0].header.paragraphs[0].text, '192.0.2.10')
        sanitize_document(doc)
        self.assertIn('[IP]:55432', doc.paragraphs[0].text)


    def test_unknown_field_never_uses_placeholder(self):
        for field in ('FUTURE_FIELD_WITHOUT_TRANSLATION', 'XYZ_ABC'):
            label = report_field_labels('mysql', '', [field])[0]
            self.assertNotIn('待维护', label)
            self.assertEqual(label, field)
        self.assertEqual(report_field_labels('mysql', '', ['MONGODB_SERVER_STATUS'])[0],
                         'MONGODB_SERVER_STATUS（MongoDB服务器状态）')


    def test_every_template_projection_has_chinese_label(self):
        """所有库种的模板显式列都必须有中文说明，防止新增模板重新引入占位。"""
        import sqlite3
        from modules.core.paths import PROJECT_ROOT
        db_path = Path(str(PROJECT_ROOT)) / 'data' / 'inspection.db'
        if not db_path.exists():
            self.skipTest('inspection.db 不存在')
        rows = sqlite3.connect(str(db_path)).execute(
            "SELECT t.db_type, q.query_key, q.query_sql FROM inspection_template t "
            "JOIN inspection_chapter c ON c.template_id=t.id "
            "JOIN inspection_query q ON q.chapter_id=c.id WHERE q.enabled=1").fetchall()

        def split_top_level(text):
            parts, buf, depth, quote = [], [], 0, None
            index = 0
            while index < len(text):
                char = text[index]
                if quote:
                    buf.append(char)
                    if char == quote:
                        if index + 1 < len(text) and text[index + 1] == quote:
                            buf.append(text[index + 1])
                            index += 1
                        else:
                            quote = None
                elif char in "'\"`":
                    quote = char
                    buf.append(char)
                elif char == '(':
                    depth += 1
                    buf.append(char)
                elif char == ')':
                    depth -= 1
                    buf.append(char)
                elif char == ',' and depth == 0:
                    parts.append(''.join(buf))
                    buf = []
                else:
                    buf.append(char)
                index += 1
            if buf:
                parts.append(''.join(buf))
            return parts

        def projection(sql):
            text = re.sub(r'/\*.*?\*/|--[^\n]*', ' ', sql or '', flags=re.S)
            text = text.strip().rstrip(';').strip()
            if re.match(r'(?is)^show\s+(global\s+|session\s+)?(variables|status)\b', text):
                return ['Variable_name', 'Value']
            if not re.match(r'(?is)^(select|with)\b', text):
                return []
            start = 0
            if text.lower().startswith('with'):
                start = text.lower().rfind('select')
            match = re.match(r'(?is)^\s*select\s+', text[start:])
            if not match:
                return []
            body_start = start + match.end()
            depth, quote, from_at = 0, None, None
            index = body_start
            while index < len(text):
                char = text[index]
                if quote:
                    if char == quote:
                        if index + 1 < len(text) and text[index + 1] == quote:
                            index += 1
                        else:
                            quote = None
                elif char in "'\"`":
                    quote = char
                elif char == '(':
                    depth += 1
                elif char == ')':
                    depth -= 1
                elif depth == 0 and re.match(r'(?i)from\b', text[index:]):
                    from_at = index
                    break
                index += 1
            body = text[body_start:from_at if from_at is not None else len(text)]
            body = re.sub(r'(?is)^\s*distinct\s+', '', body)
            columns = []
            for expr in split_top_level(body):
                expr = expr.strip()
                if not expr or expr == '*' or expr.endswith('.*'):
                    continue
                alias = re.search(r'(?is)\s+as\s+[`"\[]?([\w$#]+)[`"\]]?\s*$', expr)
                if alias:
                    columns.append(alias.group(1))
                    continue
                plain = re.fullmatch(r'[\w$#.`"\[\]]+', expr)
                if plain:
                    columns.append(expr.split('.')[-1].strip('`"[]'))
                    continue
                trailing = re.search(r'(?is)^.*\S\s+([`"\[]?[A-Za-z_][\w$#]*[`"\]]?)$', expr)
                if trailing and not re.search(r'[+\-*/<>=]\s*$', expr):
                    columns.append(trailing.group(1).strip('`"[]'))
            return columns

        checked = 0
        for db_type, query_key, sql in rows:
            for column in projection(sql):
                with self.subTest(db_type=db_type, query_key=query_key, column=column):
                    label = report_field_labels(db_type, sql, [column])[0]
                    self.assertNotIn('待维护', label)
                    self.assertRegex(label, r'[\u4e00-\u9fff]', f'{db_type}/{query_key}/{column}')
                    checked += 1
        self.assertGreater(checked, 900)

    def test_dictionary_updates_feed_report_labels(self):
        knowledge = copy.deepcopy(get_knowledge())
        knowledge['databases']['dm']['views']['V$DATABASE']['fields']['NAME']['desc'] = '自定义库名说明'
        with patch('modules.intelligence.schema_knowledge._load_knowledge', return_value=knowledge):
            labels = report_field_labels('dm_jdbc', 'select NAME, STATUS$ from v$database;', ['NAME', 'STATUS$', 'UNKNOWN'])
        self.assertEqual(labels[0], 'NAME（自定义库名说明）')
        self.assertIn('数据库状态', labels[1])
        self.assertEqual(labels[2], 'UNKNOWN（未知）')

    def test_aliases_joins_and_missing_views_use_chinese_labels(self):
        labels = report_field_labels('dm8', 'SELECT NAME AS CUSTOM_DB FROM V$DATABASE', ['CUSTOM_DB'])
        self.assertEqual(labels, ['CUSTOM_DB（数据库名称）'])
        labels = report_field_labels('dm', 'SELECT * FROM missing_view', ['TOTAL_SIZE'])
        self.assertEqual(labels, ['TOTAL_SIZE（总大小）'])
        labels = report_field_labels('oracle', 'SELECT * FROM V$DATABASE', ['DSC_NODES'])
        self.assertIn('DSC集群实例总数', labels[0])
        labels = report_field_labels('mysql', "SELECT 'FROM V$DATABASE' AS VALUE", ['VALUE'])
        self.assertEqual(labels, ['VALUE（值）'])
        self.assertEqual(report_field_labels('dm', '', ['中文名称', 'NAME（名称）', '123']),
                         ['中文名称', 'NAME（名称）', '123'])

    def test_common_dictionary_has_chinese_for_every_registered_field(self):
        fields = get_knowledge()['report_fields']['fields']
        self.assertGreater(len(fields), 1000)
        for field, description in fields.items():
            with self.subTest(field=field):
                self.assertRegex(description, r'[\u4e00-\u9fff]')
                label = report_field_labels('mysql', '', [field])[0]
                self.assertNotIn('待维护', label)
                self.assertTrue(label.startswith(field + '（'))

    def test_all_database_families_generate_translated_template_and_native_reports(self):
        from modules.driver_registry import DB_TYPE_CATALOG
        db_types = sorted({item['key'] for item in DB_TYPE_CATALOG} | {
            'dm8', 'oracle_jdbc', 'sqlserver_jdbc', 'redis-cluster', 'pg'})
        row = {'NAME': 'fixture-db', 'TOTAL_MB': 123, 'STATUS': 'OK', 'SQL_ID': 'abc'}
        for db_type in db_types:
            for with_template in (False, True):
                with self.subTest(db_type=db_type, template=with_template), tempfile.TemporaryDirectory() as temp:
                    engine = BaseInspectionEngine('192.0.2.10', 55432, 'fixture-user', 'fixture-secret')
                    engine.db_type = db_type
                    engine._lang = 'zh'
                    engine._t = lambda key, **kwargs: kwargs.get('default', key)
                    engine.context = {'fixture_data': [copy.deepcopy(row)]}
                    query = {'query_key': 'fixture_data', 'query_sql': 'SELECT * FROM fixture_view'}
                    output = Path(temp) / 'report.docx'
                    with patch.object(engine, '_load_word_template', return_value='fixture-template'), \
                         patch.object(engine, 'get_template_id', return_value=1), \
                         patch.object(engine, '_check_baselines'), \
                         patch.object(engine, '_append_chapters'), \
                         patch('modules.inspection.dal.get_template', return_value={'id': 1} if with_template else None), \
                         patch('modules.inspection.dal.get_chapters_by_template', return_value=[{'id': 1}]), \
                         patch('modules.inspection.dal.get_queries_by_chapter', return_value=[query]):
                        self.assertEqual(engine.generate_report(str(output)), str(output))
                    doc = Document(output)
                    headers = [c.text for c in doc.tables[-1].rows[0].cells]
                    self.assertEqual(headers, report_field_labels(db_type, '', list(row)))
                    self.assertTrue(all(re.search(r'[\u4e00-\u9fff]', h) for h in headers))
                    self.assertEqual([c.text for c in doc.tables[-1].rows[1].cells], list(map(str, row.values())))

    def test_oracle_full_word_html_and_legacy_tables(self):
        from modules.entrypoints import main_oracle_full as oracle
        chapters = [{'chapter_title_zh': '测试章节', 'chapter_number': 1, 'queries_results': [
            {'query_key': 'fixture', 'columns': ['SQL_ID', 'TABLESPACE_NAME', 'TOTAL_MB'],
             'data': [['abc', 'USERS', 123]]}]}]
        html = oracle.build_html_report({}, {}, {}, 'fixture-version', chapter_results=chapters)
        self.assertIn('SQL_ID（SQL标识）', html)
        self.assertIn('TABLESPACE_NAME（表空间名称）', html)
        self.assertIn('hostname（主机名）', html)
        for lang in ('zh', 'en'):
            doc = oracle.build_word_report({}, {}, {}, 'fixture-version', lang=lang, chapter_results=chapters)
            self.assertIsNotNone(doc)
            text = '\n'.join(c.text for t in doc.tables for row in t.rows for c in row.cells)
            self.assertIn('abc', text)
            self.assertIn('SQL_ID（SQL标识）' if lang == 'zh' else 'SQL_ID', text)
            if lang == 'en':
                self.assertNotIn('SQL_ID（SQL标识）', text)
        for headers in oracle._ORACLE_COLUMNS_MAP.values():
            translated = report_field_labels('oracle', '', headers)
            self.assertTrue(all('待维护' not in h for h in translated), translated)

    def test_report_dictionary_api_permissions_validation_and_failed_save(self):
        from flask import Flask
        from modules.intelligence.views import intelligence_bp
        app = Flask(__name__)
        app.secret_key = 'fixture-only'
        app.register_blueprint(intelligence_bp)
        client = app.test_client()
        original = copy.deepcopy(get_knowledge())
        self.assertIn('TOTAL_MB', client.get('/api/intelligence/report-fields').get_json()['fields'])
        payload = {'field': 'fixture_extra', 'description': '测试扩展字段'}
        self.assertEqual(client.put('/api/intelligence/report-fields', json=payload).status_code, 403)
        with client.session_transaction() as session:
            session['user_roles'] = ['operator']
        with patch('modules.intelligence.schema_knowledge.get_knowledge', return_value=original), \
             patch('modules.intelligence.schema_knowledge.save_knowledge', return_value=True) as save:
            response = client.put('/api/intelligence/report-fields', json=payload)
            self.assertEqual(response.status_code, 200)
            saved = save.call_args.args[0]
            self.assertEqual(saved['report_fields']['fields']['FIXTURE_EXTRA'], '测试扩展字段')
            self.assertEqual(saved['databases'], original['databases'])
            self.assertNotIn('FIXTURE_EXTRA', original['report_fields']['fields'])
            self.assertEqual(client.put('/api/intelligence/report-fields', json={
                'field': 'EXTRA', 'description': 'English only'}).status_code, 400)
        with patch('modules.intelligence.schema_knowledge.save_knowledge', return_value=False):
            self.assertEqual(client.put('/api/intelligence/report-fields', json=payload).status_code, 500)
        self.assertNotIn('FIXTURE_EXTRA', get_knowledge()['report_fields']['fields'])

    def test_generate_report_tables_and_cards_in_both_languages(self):
        fields = get_knowledge()['databases']['dm']['views']['V$DATABASE']['fields']
        for lang, wide in (('zh', False), ('zh', True), ('en', False)):
            with self.subTest(lang=lang, wide=wide), tempfile.TemporaryDirectory() as temp:
                engine = BaseInspectionEngine('192.0.2.10', 55432, 'fixture-user', 'fixture-secret')
                engine.db_type = 'dm'
                engine._lang = lang
                engine._t = lambda key, **kwargs: kwargs.get('default', key)
                row = {name: str(index) for index, name in enumerate(fields)}
                row['NAME'] = 'fixture-db'
                if wide:
                    row['UNKNOWN'] = 'future-field-value'
                engine.context = {'database_info': [row], 'host': engine.host,
                                  'password': 'fixture-secret'}
                query = {'query_key': 'database_info', 'query_sql': 'SELECT * FROM V$DATABASE',
                         'query_description_zh': '数据库信息', 'query_description_en': 'Database info'}
                output = Path(temp) / 'report.docx'
                with patch.object(engine, '_load_word_template', return_value='fixture-template'), \
                     patch.object(engine, 'get_template_id', return_value=1), \
                     patch.object(engine, '_check_baselines'), \
                     patch.object(engine, '_append_chapters'), \
                     patch('modules.inspection.dal.get_template', return_value={'id': 1}), \
                     patch('modules.inspection.dal.get_chapters_by_template', return_value=[{'id': 1}]), \
                     patch('modules.inspection.dal.get_queries_by_chapter', return_value=[query]):
                    self.assertEqual(engine.generate_report(str(output)), str(output))
                doc = Document(output)
                text = '\n'.join(cell.text for table in doc.tables for r in table.rows for cell in r.cells)
                self.assertIn('192.0.2.10:55432', text)
                self.assertNotIn('[IP]', text)
                self.assertNotIn('fixture-secret', text)
                self.assertIn('fixture-db', text)
                for name, info in fields.items():
                    self.assertIn(f"{name}（{info['desc']}）" if lang == 'zh' else name, text)
                self.assertEqual(row['NAME'], 'fixture-db')
                if wide:
                    self.assertIn('future-field-value', text)
                if lang == 'en':
                    self.assertNotIn('数据库最大大小', text)


if __name__ == '__main__':
    unittest.main()
