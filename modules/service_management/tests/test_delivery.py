# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Regressions for frozen reports, signed artifacts, period boundaries and ACLs."""

import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from flask import Flask, g
from openpyxl import load_workbook

from modules.access import Principal
from modules.service_management.catalog import load_catalog, all_items
from modules.service_management.store import ServiceStore
from modules.service_management.delivery_store import DeliveryStore, digest
from modules.service_management.delivery_catalog import DELIVERABLES
from modules.service_management.delivery_data import request_options, build_snapshot, sources_from_context
from modules.service_management.delivery_render import render_job, watermark, package, verify_package, content_diff
from modules.service_management.routes import bp

ROOT = Path(__file__).resolve().parents[3]
OWNER = {'id': 11, 'tenant_id': 1, 'admin': False, 'approver': True}
ADMIN = {'id': 12, 'tenant_id': 1, 'admin': True, 'approver': True}
OTHER = {'id': 21, 'tenant_id': 2, 'admin': True, 'approver': True}


class InventoryFixture:
    def __init__(self, ids=('native',)):
        now = datetime.now(timezone.utc).isoformat()
        self.records = {identity: {'id': identity, 'configuration_id': 'CI-' + identity, 'version': 1,
            'source': {'name': identity, 'db_type': 'oracle', 'host': '192.0.2.1', 'port': 1521,
                       'database': identity, 'created_at': now, 'updated_at': now},
            'metadata': {'system': 'fixture', 'owner': 'fixture', 'tables': {}}, 'revision': 'fixture'} for identity in ids}

    def get(self, identity):
        if identity not in self.records:
            raise LookupError('missing fixture')
        return self.records[identity]

    def list(self, ids):
        return [self.get(identity) for identity in ids]

    def history(self, actor, ids):
        return []


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.services = ServiceStore(Path(self.temp.name) / 'services.db', Fernet.generate_key())
        self.archive = DeliveryStore(self.services)
        self.catalog = load_catalog(ROOT / 'scripts/fixtures/service_catalog.json')
        self.payload = {'configuration_id': 'CI-TEST', 'asset_name': 'fixture', 'db_type': 'oracle',
                        'version': '11g', 'owner': 'fixture', 'business': 'fixture', 'system': 'fixture',
                        'lifecycle': 'active', 'approved_parameters': {'processes': 100},
                        'backup_frequency': 'daily', 'tables': {'accounts': [{'username': '=1+1', 'owner': 'fixture'}]}}
        self.asset = self.services.save(OWNER, 'asset', self.payload)
        self.now = datetime.now(timezone.utc)
        self.date = self.now.astimezone().date().isoformat()
        self.observation = self.archive.observe(OWNER, self.asset['id'], {
            'parameters': {'status': 'collected', 'rows': [{'name': 'processes', 'value': 120}],
                           'scope': 'instance', 'truncated': False},
            'storage': {'status': 'error', 'reason': 'fixture permission', 'rows': []},
            'slow_sql': {'status': 'collected', 'rows': [{'sql_id': 'q1', 'executions': 3}]}})

    def options(self, **extra):
        return request_options({'deliverables': ['D01', 'D03', 'D06', 'D09'],
                                'asset_ids': [self.asset['id']], 'date': self.date,
                                'formats': ['xlsx'], **extra}, self.services.list(OWNER, 'asset'))

    def job(self, **extra):
        options = self.options(**extra)
        snapshot = build_snapshot(self.services, self.archive, OWNER, self.catalog, options)
        job = self.archive.create(OWNER, options, snapshot)
        self.archive.claim(OWNER, job['id'])
        self.archive.finish(OWNER, job['id'], render_job(job))
        return self.archive.get(OWNER, job['id'])

    def test_all_24_deliverables_have_purpose_specific_sections(self):
        job = self.job(deliverables=list(DELIVERABLES)[:-1], formats=['xlsx', 'docx'])
        self.assertEqual(len(job['artifacts']), 48)
        names = set()
        for artifact in job['artifacts']:
            info, content = self.archive.artifact(OWNER, job['id'], artifact['artifact_id'])
            self.assertEqual(watermark(content)['artifact_id'], artifact['artifact_id'])
            self.assertEqual(digest(content), info['sha256'])
            if info['format'] == 'xlsx':
                workbook = load_workbook(io.BytesIO(content))
                self.assertEqual(workbook.sheetnames, ['报告说明', *DELIVERABLES[info['deliverable']][2]])
                names.add(tuple(workbook.sheetnames))
        self.assertGreater(len(names), 20)

    def test_typed_cells_and_formula_injection(self):
        job = self.job(deliverables=['D03', 'D06'])
        artifact = next(a for a in job['artifacts'] if a['deliverable'] == 'D06')
        _, content = self.archive.artifact(OWNER, job['id'], artifact['artifact_id'])
        workbook = load_workbook(io.BytesIO(content), data_only=False)
        values = [c for row in workbook['用户账号'] for c in row if c.value == '=1+1']
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].data_type, 's')
        params = job['snapshot']['sheets']['数据库参数']
        self.assertTrue(any(r.get('运行值', r.get('配置值')) == 120 for r in params))

    def test_combined_workbook(self):
        job = self.job(deliverables=['D01', 'D03', 'D06'], layout='combined')
        self.assertEqual(len(job['artifacts']), 1)
        artifact = job['artifacts'][0]
        self.assertEqual(artifact['deliverable'], 'COMBINED')
        _, content = self.archive.artifact(OWNER, job['id'], artifact['artifact_id'])
        workbook = load_workbook(io.BytesIO(content))
        self.assertIn('数据库参数', workbook.sheetnames)
        self.assertIn('用户账号', workbook.sheetnames)

    def test_signature_package_download_and_mutation(self):
        job = self.job(deliverables=['D01'])
        content = package(job, self.archive, OWNER)
        self.assertEqual(content, package(job, self.archive, OWNER))
        self.assertTrue(verify_package(content, job['manifest']['key_id'])['files_valid'])
        with self.assertRaises(ValueError):
            verify_package(content, '0' * 64)
        modified = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(modified, 'w') as target:
            for name in source.namelist():
                value = source.read(name)
                if name.endswith('.xlsx'):
                    value += b'changed'
                target.writestr(name, value)
        with self.assertRaises(ValueError):
            verify_package(modified.getvalue(), job['manifest']['key_id'])

    def test_edited_xlsx_diff_and_archive_original(self):
        job = self.job(deliverables=['D01'])
        info, content = self.archive.artifact(OWNER, job['id'], job['artifacts'][0]['artifact_id'])
        self.assertTrue(self.archive.verify(OWNER, job['id'], content)['signature_valid'])
        workbook = load_workbook(io.BytesIO(content))
        workbook['资产底账']['A3'] = 'changed'
        output = io.BytesIO()
        workbook.save(output)
        self.assertFalse(self.archive.verify(OWNER, job['id'], output.getvalue())['matches_original'])
        self.assertGreater(content_diff(content, output.getvalue(), 'xlsx')['total'], 0)
        self.assertEqual(self.archive.artifact(OWNER, job['id'], info['artifact_id'])[1], content)

    def test_acl_on_list_download_verify_and_revocation(self):
        job = self.job(deliverables=['D01'])
        self.assertEqual(self.archive.list(OTHER), [])
        for action in (lambda: self.archive.get(OTHER, job['id']),
                       lambda: self.archive.artifact(OTHER, job['id'], job['artifacts'][0]['artifact_id']),
                       lambda: self.archive.verify(OTHER, job['id'], b'x')):
            with self.assertRaises(PermissionError):
                action()
        with self.services.connection() as db:
            db.execute('UPDATE service_records SET owner=99 WHERE id=?', (self.asset['id'],))
        with self.assertRaises((PermissionError, ValueError)):
            self.archive.get(OWNER, job['id'])

    def test_published_bytes_frozen_and_revision(self):
        job = self.job(deliverables=['D01'])
        old_id = job['artifacts'][0]['artifact_id']
        original = self.archive.artifact(OWNER, job['id'], old_id)[1]
        published = self.archive.finish(ADMIN, job['id'], render_job(job, True), publish=True,
                                        expected_version=job['version'])
        self.assertEqual(published['state'], 'published')
        self.assertEqual(self.archive.artifact(OWNER, job['id'], old_id)[1], original)
        self.assertTrue(self.archive.verify(OWNER, job['id'], original)['matches_original'])
        revision = self.archive.create(OWNER, job['request'], job['snapshot'], previous_id=job['id'], reason='fixture correction')
        self.assertEqual(revision['revision'], 2)
        with self.assertRaises(ValueError):
            self.archive.finish(ADMIN, job['id'], [], publish=True)

    def test_historical_asset_and_workflow_versions(self):
        cutoff = datetime.now(timezone.utc).isoformat()
        workflow = self.services.save(OWNER, 'workflow', {'details': {'root_cause': 'original'}}, asset_id=self.asset['id'], item_id='fixture')
        before_workflow = datetime.now(timezone.utc).isoformat()
        self.services.save(OWNER, 'workflow', {'details': {'root_cause': 'later'}}, record_id=workflow['id'],
                           version=workflow['version'], asset_id=self.asset['id'], item_id='fixture')
        self.services.save(OWNER, 'asset', {**self.payload, 'version': 'new'}, record_id=self.asset['id'], version=1)
        rows = self.services.reporting_records(OWNER, cutoff)
        self.assertEqual(next(r for r in rows if r['id'] == self.asset['id'])['payload']['version'], '11g')
        old = self.services.reporting_records(OWNER, before_workflow)
        self.assertEqual(next(r for r in old if r['id'] == workflow['id'])['payload']['details']['root_cause'], 'original')

    def test_daily_gaps_and_missing_health_are_explicit(self):
        job = self.job(deliverables=['D09', 'D10'])
        self.assertTrue(any(r['状态'] == '未采集' for r in job['snapshot']['sheets']['日运行监控']) or self.now.day == 1)
        self.assertIsNone(job['snapshot']['sheets']['健康评分'][0]['健康评分'])
        self.assertEqual(self.services.get(OWNER, self.asset['id'])['version'], 1)

    def test_run_filter_and_context_truncation(self):
        job = self.job(deliverables=['RUN'], run_id=self.observation['id'])
        self.assertEqual(job['snapshot']['coverage']['runs'], 1)
        result = sources_from_context({'unused': [], 'parameters': [{'name': 'a'}],
                                       '_query_status': {'parameters': {'status': 'collected', 'truncated': True}}})
        self.assertNotIn('unused', result)
        self.assertTrue(result['parameters']['truncated'])
        with self.assertRaises(ValueError):
            self.job(deliverables=['RUN'], run_id='missing')

    def test_full_collection_paginates_beyond_200(self):
        from modules.service_management.inspection import collect
        class Cursor:
            description = [('NAME',), ('VALUE',)]
            def execute(self, sql): self.offset = 0
            def fetchmany(self, limit):
                size = min(limit, 2300 - self.offset)
                self.offset += size
                return [(str(i), i) for i in range(self.offset - size, self.offset)]
            def close(self): pass
        class Connection:
            def cursor(self): return Cursor()
            def rollback(self): pass
            def close(self): pass
        result = collect({}, {**self.payload, '_delivery_full_inventory': True}, factory=lambda *_: Connection())
        self.assertEqual(len(result['parameters']['rows']), 2300)
        self.assertFalse(result['parameters']['truncated'])

    def test_http_page_jobs_and_verification(self):
        app = Flask(__name__, template_folder=str(ROOT / 'web_templates'))
        principal = Principal(id=11, tenant_id=1, roles=['operator'])
        app.config.update(SERVICE_STORE=self.services, SERVICE_CATALOG_PATH=ROOT / 'scripts/fixtures/service_catalog.json',
                          DELIVERY_SYNC=True, DELIVERY_PRINCIPAL_RESOLVER=lambda _: principal,
                          REPORT_INVENTORY=InventoryFixture(), REPORT_VISIBLE_IDS=lambda _: ['native'])
        @app.before_request
        def identity(): g.security_principal = principal
        app.register_blueprint(bp)
        client = app.test_client()
        self.assertEqual(client.get('/reports/center').status_code, 302)
        self.assertEqual(client.get('/reports/center?run_id=fixture-run').headers['Location'], '/?run_id=fixture-run#report-center')
        response = client.post('/api/report-center/jobs', json={'source_ids': ['native'], 'deliverables': ['D01'], 'date': self.date})
        self.assertEqual(response.status_code, 202, response.json)
        job = response.json['job']
        self.assertEqual(job['state'], 'published', job.get('error'))
        artifact = job['artifacts'][0]
        downloaded = client.get('/api/services/deliverables/' + job['id'] + '/download?artifact_id=' + artifact['artifact_id'])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.headers['X-Content-SHA256'], digest(downloaded.data))
        verified = client.post('/api/services/deliverables/verify', data={
            'job_id': job['id'], 'file': (io.BytesIO(downloaded.data), 'fixture.xlsx')})
        self.assertEqual(verified.status_code, 200, verified.json)
        self.assertTrue(verified.json['result']['matches_original'])

    def test_verified_change_updates_asset_and_closes_atomically(self):
        updates = {'approved_parameters': {'processes': 150}}
        workflow = self.services.save(OWNER, 'workflow', {
            'details': {'asset_updates': updates}, 'risk_assessment': 'fixture', 'rollback_plan': 'fixture'},
            asset_id=self.asset['id'], item_id='fixture-change')
        for identity, target, evidence in [
            (OWNER, 'submitted', {}), (ADMIN, 'approved', {}),
            (OWNER, 'executing', {'execution_reference': 'fixture-exec'}),
            (OWNER, 'verified', {'verification_reference': 'fixture-verify', 'details_update': {'verified_asset_values': updates}}),
            (ADMIN, 'closed', {'verification_reference': 'fixture-final'})]:
            workflow = self.services.transition(identity, workflow['id'], target, workflow['version'], evidence)
        asset = self.services.get(OWNER, self.asset['id'])
        self.assertEqual(asset['version'], 2)
        self.assertEqual(workflow['payload']['state'], 'closed')
        self.assertEqual(workflow['payload']['asset_version'], 2)
        self.assertEqual(asset['payload']['approved_parameters'], updates['approved_parameters'])

    def test_finding_deduplication_keeps_workflow_and_evidence(self):
        item = next(i['id'] for i in all_items(self.catalog).values() if i['key'] == 'remediation')
        first = self.services.record_finding(OWNER, self.asset['id'], 'capacity', 'run1', 'evidence1', item)
        second = self.services.record_finding(OWNER, self.asset['id'], 'capacity', 'run2', 'evidence2', item)
        self.assertEqual(first, second)
        self.assertEqual(self.services.record_finding(OWNER, self.asset['id'], 'capacity', 'run2', 'evidence2', item), first)
        self.assertEqual(len(self.services.list(OWNER, 'workflow')), 1)

    def test_key_rotation_preserves_old_signatures(self):
        job = self.job(deliverables=['D01'])
        original = package(job, self.archive, OWNER)
        old_key = job['manifest']['key_id']
        keys = self.archive.keys(ADMIN, rotate=True)
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(next(k['key_id'] for k in keys if k['active']), old_key)
        self.assertTrue(verify_package(original, old_key)['signature_valid'])

    def test_period_boundary_excludes_later_observations(self):
        options = self.options(date='2026-01-15')
        snapshot = build_snapshot(self.services, self.archive, OWNER, self.catalog, options)
        self.assertEqual(snapshot['coverage']['runs'], 0)
        self.assertEqual(snapshot['history_notes']['missing_asset_versions'], [self.asset['id']])

    def test_invalid_request_shapes(self):
        for extra in [{'deliverables': [{}]}, {'deliverables': []}, {'formats': [{}]}, {'formats': []},
                      {'asset_ids': []}, {'source_ids': None}, {'collect_first': 'yes'},
                      {'sheets': [{}]}, {'asset_ids': ['foreign']}]:
            with self.assertRaises((ValueError, PermissionError)):
                self.options(**extra)

    def test_normal_inspection_capture_and_idempotent_callback(self):
        from modules.service_management.delivery_capture import capture_inspection
        principal = Principal(id=11, tenant_id=1, roles=['operator'])
        app = Flask(__name__)
        app.config.update(SERVICE_STORE=self.services, SERVICE_CATALOG_PATH=ROOT / 'scripts/fixtures/service_catalog.json')
        app.config.update(REPORT_INVENTORY=InventoryFixture(), REPORT_VISIBLE_IDS=lambda _: ['native'])
        task = {'datasource_id': 'native', 'owner_user_id': 11, 'owner_tenant_id': 1, 'id': 'native-run'}
        context = {'parameters': [{'name': 'processes', 'value': 120}], 'auto_analyze': [{'col1': 'fixture issue'}]}
        with app.app_context(), patch('modules.access.get_principal', return_value=principal):
            first = capture_inspection(task, context)
            second = capture_inspection(task, context)
        self.assertEqual(first, second)
        self.assertEqual(first['id'], 'native-run')
        self.assertEqual(len(self.services.list(OWNER, 'asset')), 1)

    def test_legacy_mapping_uses_instance_id_and_preserves_unknown_scope(self):
        import sqlite3
        from modules.service_management.delivery_data import legacy_observations, TZ
        database = Path(self.temp.name) / 'history.db'
        stamp = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE snapshots(id INTEGER,ts TEXT,context_json TEXT)')
            for row_id, instance in [(1, 'known'), (2, 'other')]:
                db.execute('INSERT INTO snapshots VALUES(?,?,?)', (row_id, stamp, json.dumps({
                    '_instance_id': instance, 'parameters': [{'name': 'processes', 'value': 100}]})))
        asset = {**self.asset, 'payload': {**self.payload, 'instance_id': 'known'}}
        runs, note = legacy_observations({asset['id']: asset}, datetime.now(timezone.utc)-timedelta(days=1),
                                         datetime.now(timezone.utc), database)
        self.assertEqual([r['id'] for r in runs], ['history-1'])
        self.assertEqual(note['matched'], 1)

    def test_major_review_and_specific_event_scope(self):
        items = all_items(self.catalog)
        incident_item = next(i['id'] for i in items.values() if i['key'] == 'incident_lifecycle')
        remediation_item = next(i['id'] for i in items.values() if i['key'] == 'remediation')
        major = self.services.save(OWNER, 'workflow', {'title': 'major fixture',
            'details': {'major_incident': True, 'incident_reference': 'INC-MAJOR'}},
            asset_id=self.asset['id'], item_id=incident_item)
        normal = self.services.save(OWNER, 'workflow', {'title': 'normal fixture',
            'details': {'incident_reference': 'INC-NORMAL'}}, asset_id=self.asset['id'], item_id=incident_item)
        for record in (major, normal):
            self.services.transition(OWNER, record['id'], 'submitted', record['version'], {})
        self.services.save(OWNER, 'workflow', {'title': 'major remediation', 'details': {'incident_reference': 'INC-MAJOR'}},
                           asset_id=self.asset['id'], item_id=remediation_item)
        self.services.save(OWNER, 'workflow', {'title': 'normal remediation', 'details': {'incident_reference': 'INC-NORMAL'}},
                           asset_id=self.asset['id'], item_id=remediation_item)
        job = self.job(deliverables=['D17'])
        _, content = self.archive.artifact(OWNER, job['id'], job['artifacts'][0]['artifact_id'])
        workbook = load_workbook(io.BytesIO(content))
        encoded = str([row for sheet in workbook for row in sheet.values])
        self.assertIn(major['id'], encoded)
        self.assertNotIn(normal['id'], encoded)
        self.assertIn('major remediation', encoded)
        self.assertNotIn('normal remediation', encoded)
        selected = self.job(deliverables=['D16'], workflow_ids=[normal['id']])
        rows = selected['snapshot']['sheets']['故障记录']
        self.assertEqual([r['记录编号'] for r in rows], [normal['id']])

    def test_direct_sources_need_no_service_registration_or_customer_catalog(self):
        app = Flask(__name__)
        principal = Principal(id=11, tenant_id=1, roles=['operator'])
        inventory = InventoryFixture(('one', 'two'))
        allowed = ['one', 'two']
        app.config.update(SERVICE_STORE=self.services, SERVICE_CATALOG_PATH=Path(self.temp.name)/'missing.json',
                          REPORT_INVENTORY=inventory, REPORT_VISIBLE_IDS=lambda _: allowed,
                          DELIVERY_SYNC=True, DELIVERY_PRINCIPAL_RESOLVER=lambda _: principal)
        @app.before_request
        def identity(): g.security_principal = principal
        app.register_blueprint(bp)
        client = app.test_client()
        choices = client.get('/api/report-center/sources').json['sources']
        self.assertEqual({row['id'] for row in choices}, set(allowed))
        self.assertNotIn('password', json.dumps(choices))
        self.assertEqual(client.post('/api/report-center/jobs', json={'deliverables':['D01']}).status_code, 400)
        created = client.post('/api/report-center/jobs', json={'source_ids':allowed,'deliverables':['D01','D04'],
                             'layout':'combined','split_sources':True})
        self.assertEqual(created.status_code,202,created.json)
        job=created.json['job']
        self.assertEqual(job['state'],'published',job.get('error'))
        self.assertEqual(len(job['artifacts']),2)
        self.assertEqual(len(self.services.list(OWNER,'asset')),1)
        self.assertEqual(set(job['request']['source_ids']),set(allowed))
        allowed.remove('two')
        self.assertEqual(client.get('/api/report-center/'+job['id']+'/download').status_code,403)

    def test_new_reports_read_current_inventory_and_link_legacy_observations(self):
        from modules.service_management.report_sources import ReportSources, report_catalog
        principal=Principal(id=11,tenant_id=1,roles=['operator'])
        inventory=InventoryFixture()
        self.services.save(OWNER,'asset',{**self.payload,'instance_id':'native'},record_id=self.asset['id'],version=1)
        records=ReportSources(self.services,inventory,principal,['native'])
        archive=DeliveryStore(records)
        observations=archive.observations(OWNER,['native'])
        self.assertEqual(observations[0]['asset_id'],'native')
        app=Flask(__name__)
        with app.app_context():
            options=request_options({'source_ids':['native'],'deliverables':['D03']},records.list(OWNER,'asset'))
            snapshot=build_snapshot(records,archive,OWNER,report_catalog(OWNER),options)
        self.assertTrue(any(row.get('配置值')==120 for row in snapshot['sheets']['数据库参数']))
        inventory.records['native']['metadata']['owner']='updated owner'
        self.assertEqual(records.get(OWNER,'native')['payload']['owner'],'updated owner')
        legacy = self.job(deliverables=['D01'])
        new_options={**legacy['request'],'source_ids':['native'],'asset_ids':['native']}
        revised=archive.create(OWNER,new_options,legacy['snapshot'],previous_id=legacy['id'],reason='source mapping')
        self.assertEqual(revised['revision'],2)

    def test_source_event_versions_are_cutoff_aware(self):
        from modules.service_management.report_sources import ReportSources
        records=ReportSources(self.services,InventoryFixture(),Principal(id=11,tenant_id=1,roles=['operator']),['native'])
        event=records.save_event(OWNER,{'source_id':'native','key':'incident_lifecycle','title':'initial','details':{}})
        cutoff=datetime.now(timezone.utc).isoformat()
        records.save_event(OWNER,{'id':event['id'],'version':event['version'],'source_id':'native',
                                 'key':'incident_lifecycle','title':'revised','details':{}})
        self.assertEqual(records.events(OWNER,cutoff)[0]['payload']['title'],'initial')
        self.assertEqual(records.events(OWNER)[0]['payload']['title'],'revised')
        with self.assertRaises(ValueError):
            records.save_event(OWNER,{'source_id':'native','key':'incident_lifecycle','state':'verified','details':{}})

    def test_collect_then_generate_reuses_batch_runner(self):
        from modules.service_management.report_sources import sources_for
        app=Flask(__name__)
        principal=Principal(id=11,tenant_id=1,roles=['operator'])
        app.config.update(SERVICE_STORE=self.services, REPORT_INVENTORY=InventoryFixture(),
                          REPORT_VISIBLE_IDS=lambda _: ['native'], DELIVERY_SYNC=True,
                          DELIVERY_PRINCIPAL_RESOLVER=lambda _: principal)
        @app.before_request
        def identity(): g.security_principal=principal
        app.register_blueprint(bp)
        class Batch:
            def __init__(batch):
                batch.store=batch
                batch.value={'id':'fixture-batch','payload':{'members':[{'instance_id':'native','status':'done'}]}}
            def create(batch,*args): return batch.value,True
            def get(batch,*args): return batch.value
            def run(batch,run):
                DeliveryStore(sources_for(OWNER)).observe(OWNER,'native',{'instance':{'status':'collected','rows':[{'status':'OPEN'}]}})
        batch=Batch()
        with patch('modules.operations.routes.prepare_batch',return_value=({},[])) as prepared, \
             patch('modules.operations.routes.resources',return_value=(None,batch,None)):
            response=app.test_client().post('/api/report-center/jobs',json={
                'source_ids':['native'],'deliverables':['RUN'],'collect_first':True})
        self.assertEqual(response.status_code,202,response.json)
        self.assertEqual(response.json['job']['state'],'published',response.json['job'].get('error'))
        self.assertEqual(response.json['job']['coverage']['runs'],1)
        self.assertEqual(response.json['job']['inspection_run_id'],'fixture-batch')
        self.assertEqual(prepared.call_count,1)

    def test_inventory_review_is_bound_to_its_source_version(self):
        from modules.service_management.report_sources import ReportSources, report_catalog
        inventory=InventoryFixture()
        records=ReportSources(self.services,inventory,Principal(id=11,tenant_id=1,roles=['operator']),['native'])
        records.save_event(OWNER,{'source_id':'native','key':'asset_review','title':'quarterly inventory',
            'state':'verified','details':{'inventory_date':self.date,'scope_reference':'fixture-list',
            'reconciliation_result':'pass','audit_reference':'fixture-audit','verification_reference':'fixture-verified'}})
        app=Flask(__name__)
        def snapshot():
            options=request_options({'source_ids':['native'],'deliverables':['D01']},records.list(OWNER,'asset'))
            return build_snapshot(records,DeliveryStore(records),OWNER,report_catalog(OWNER),options)
        with app.app_context():
            self.assertEqual(snapshot()['sheets']['季度核对记录'][0]['核对状态'],'已核对')
            inventory.records['native']['version']=2
            self.assertEqual(snapshot()['sheets']['季度核对记录'][0]['核对状态'],'待全量核对')


if __name__ == '__main__':
    unittest.main()
