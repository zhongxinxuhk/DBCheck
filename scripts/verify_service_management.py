#!/usr/bin/env python3
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Local isolated regression checks. No customer database or external network."""



import io
import os
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from cryptography.fernet import Fernet
from flask import Flask
from modules.core.data_security import assert_readonly_sql,redact
from modules.core.web_security import install_security
from modules.service_management.catalog import load_catalog,all_items,FIELDS
from modules.service_management.store import ServiceStore
from modules.service_management.service import overview,period_bounds,first_business_day,business_deadline,sla_result,evidence_result,run_inspection
from modules.service_management.inspection import QUERIES,SOURCES,collect,assess,connect_secure
from modules.service_management.report import render_docx,render_html
from modules.access import Principal

# CI always uses a synthetic fixture; private acceptance is explicitly opt-in.
CATALOG=Path(os.environ.get('DBPULSE_TEST_CATALOG',ROOT/'scripts/fixtures/service_catalog.json'))
OWNER={'id':11,'tenant_id':1,'admin':False,'approver':False}
ADMIN={'id':12,'tenant_id':1,'admin':True,'approver':True}
OTHER={'id':21,'tenant_id':2,'admin':True,'approver':True}


class Services(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='dbpulse-services-')
        self.addCleanup(self.temp.cleanup)
        self.store=ServiceStore(Path(self.temp.name)/'services.db',Fernet.generate_key())
        self.catalog=load_catalog(CATALOG)
        self.payload={'configuration_id':'CI-TEST','asset_name':'isolated fixture','db_type':'oracle','version':'11g',
                      'owner':'fixture','business':'fixture','system':'fixture','lifecycle':'active','host':'192.0.2.123'}
        self.asset=self.store.save(OWNER,'asset',self.payload)

    def test_source_traceability_and_all_declared_fields(self):
        self.assertEqual(len(self.catalog['modules']),6)
        self.assertEqual(len(self.catalog['requirements']),37)
        self.assertEqual({r['source']['row'] for r in self.catalog['requirements']},set(range(3,40)))
        self.assertEqual(len(all_items(self.catalog)),75)
        self.assertEqual({e['family'] for e in self.catalog['equipment']},{'oracle','dm','sqlserver','gbase8a'})
        for item in all_items(self.catalog).values():
            if item['kind']=='inspection':self.assertIn(item['key'],SOURCES)
            else:self.assertTrue(FIELDS[item['key']])

    def test_fixed_queries_are_readonly(self):
        for family,queries in QUERIES.items():
            for name,sql in queries.items():
                with self.subTest(family=family,query=name):assert_readonly_sql(sql)

    def test_sql_side_effects_and_bypasses_rejected(self):
        denied=["SELECT 1; DELETE FROM users", "SELECT * INTO stolen FROM users", "SELECT 1 INTO OUTFILE '/tmp/x'",
                "PRAGMA writable_schema=1", "SELECT LOAD_FILE('/etc/passwd')", "SELECT SLEEP(9)",
                "SELECT nextval('x')", "SELECT pg_notify('x','secret')", "SELECT dangerous_udf()",
                'SELECT "dangerous"()', "SELECT 1 /*! INTO OUTFILE '/tmp/x' */", "EXPLAIN ANALYZE DELETE FROM x",
                "WITH x AS (DELETE FROM a RETURNING *) SELECT * FROM x", "SELECT 1--x; DELETE FROM a"]
        for sql in denied:
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):assert_readonly_sql(sql)
        assert_readonly_sql("SELECT 'DELETE; password=x' AS value FROM SYS.OBJECTS;")

    def test_recursive_redaction_preserves_source_and_attribution(self):
        value={'password':'secret-value','nested':[{'sql_text':"SELECT * FROM t WHERE phone='13812345678' AND id=123",'host':'192.0.2.123'}],
               'setting':{'name':'ssl_password','value':'private-passphrase'},'author':'sdfiyon@gmail.com'}
        clean=redact(value,mask_identity=True)
        encoded=json.dumps(clean)
        self.assertNotIn('secret-value',encoded)
        self.assertNotIn('private-passphrase',encoded)
        self.assertNotIn('13812345678',encoded)
        self.assertNotIn('192.0.2.123',encoded)
        self.assertEqual(value['password'],'secret-value')
        self.assertIn('sdfiyon@gmail.com',encoded)

    def test_ciphertext_scope_permissions_and_optimistic_updates(self):
        raw=self.store.path.read_bytes()
        self.assertNotIn(b'192.0.2.123',raw)
        self.assertNotIn(b'isolated fixture',raw)
        self.assertEqual(self.store.path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.store.list(OTHER),[])
        with self.assertRaises(PermissionError):self.store.get(OTHER,self.asset['id'])
        with self.assertRaises(ValueError):self.store.save(OWNER,'asset',self.payload,record_id=self.asset['id'],version=99)
        result=self.store.save(OWNER,'asset',{**self.payload,'version':'11.2'},record_id=self.asset['id'],version=1)
        self.assertEqual(result['version'],2)

    def test_scope_tampering_and_audit_tampering_fail_closed(self):
        with self.store.connection() as db:
            db.execute('UPDATE service_records SET tenant=2 WHERE id=?',(self.asset['id'],))
        with self.assertRaises(ValueError):self.store.get(OTHER,self.asset['id'])
        with self.store.connection() as db:
            db.execute('UPDATE service_audit SET event=? WHERE seq=1',('{}',))
        with self.assertRaises(ValueError):self.store.list(OWNER)
        with self.assertRaises(ValueError):self.store.save(OWNER,'asset',self.payload)

    def test_evidence_is_append_only(self):
        record=self.store.save(OWNER,'evidence',{'status':'not_collected'},asset_id=self.asset['id'],item_id='R01.01')
        with self.assertRaises(ValueError):self.store.save(OWNER,'evidence',{'status':'pass'},record_id=record['id'],version=1)

    def test_maker_checker_state_machine_and_protected_fields(self):
        record=self.store.save(OWNER,'workflow',{'risk_assessment':'fixture','rollback_plan':'fixture','state':'closed','approved_by':11},asset_id=self.asset['id'],item_id='R07.01')
        self.assertEqual(record['payload']['state'],'draft')
        self.assertNotIn('approved_by',record['payload'])
        with self.assertRaises(ValueError):self.store.transition(OWNER,record['id'],'closed',1,{})
        record=self.store.transition(OWNER,record['id'],'submitted',1,{})
        with self.assertRaises(PermissionError):self.store.transition(OWNER,record['id'],'approved',2,{})
        with self.assertRaises(ValueError):self.store.transition(ADMIN,record['id'],'approved',2,{'approved_by':11})
        record=self.store.transition(ADMIN,record['id'],'approved',2,{})
        record=self.store.transition(OWNER,record['id'],'executing',3,{'execution_reference':'fixture-execution'})
        with self.assertRaises(ValueError):self.store.transition(OWNER,record['id'],'verified',4,{})
        record=self.store.transition(OWNER,record['id'],'verified',4,{'verification_reference':'fixture-validation'})
        with self.assertRaises(PermissionError):self.store.transition(OWNER,record['id'],'closed',5,{'verification_reference':'fixture-validation'})
        record=self.store.transition(ADMIN,record['id'],'closed',5,{'verification_reference':'fixture-validation'})
        self.assertEqual(record['payload']['state'],'closed')

    def test_every_manual_requirement_rejects_missing_evidence(self):
        for item in all_items(self.catalog).values():
            if item['kind']=='inspection':continue
            result=evidence_result(item,{})
            self.assertNotEqual(result['status'],'pass',item['id'])
            self.assertTrue(result['missing'],item['id'])
            data={key:'fixture' for key in item['fields']}
            try:
                result=evidence_result(item,data)
            except ValueError:
                continue
            if item['kind']=='workflow':self.assertNotEqual(result['status'],'pass')

    def test_failed_validations_are_not_green(self):
        item={'key':'incident_verification','kind':'workflow'}
        data={k:'fixture' for k in FIELDS[item['key']]}
        data['business_validation']='fail'
        data['verified_at']='2026-11-09T00:00:00+00:00'
        self.assertEqual(evidence_result(item,data,{'state':'closed'})['status'],'fail')

    def test_missing_inspection_evidence_is_never_healthy(self):
        for key in SOURCES:
            self.assertNotEqual(assess(key,{})['status'],'pass')
            self.assertEqual(assess(key,{'connection':{'status':'blocked','reason':'fixture'}})['status'],'blocked')
        result=overview(self.store,OWNER,self.catalog)
        self.assertGreater(result['counts']['not_collected'],0)
        self.assertIsNone(result['risk_discovery_rate'])
        self.assertLess(result['completion_rate'],100)

    def test_empty_population_has_no_false_100_percent(self):
        result=overview(self.store,OTHER,self.catalog)
        self.assertIsNone(result['completion_rate'])

    def test_calendar_and_sla_boundaries(self):
        self.assertEqual(first_business_day(2026,11,['2026-11-02']),'2026-11-03')
        self.assertEqual(first_business_day(2026,11,[],['2026-11-01']),'2026-11-01')
        with self.assertRaises(ValueError):first_business_day(2026,11,None)
        calendar={'timezone':'Asia/Shanghai','holidays':[],'intervals':[['09:00','12:00'],['13:00','18:00']]}
        self.assertEqual(business_deadline('2026-11-06T17:00:00+08:00',240,calendar),'2026-11-09T12:00:00+08:00')
        data={'detected_at':'2026-11-09T00:00:00+00:00','priority':'P1','responded_at':'2026-11-09T00:30:00+00:00'}
        self.assertEqual(sla_result(data,datetime(2026,11,9,0,31,tzinfo=timezone.utc))['responded_at']['status'],'met')
        data['responded_at']='2026-11-09T00:31:00+00:00'
        self.assertEqual(sla_result(data,datetime(2026,11,9,0,32,tzinfo=timezone.utc))['responded_at']['status'],'breached')
        self.assertEqual(period_bounds('quarter','2026-12-31')[1].date().isoformat(),'2027-01-01')

    def test_secure_transport_prerequisites_block_before_connect(self):
        for family in QUERIES:
            with self.subTest(family=family):
                with self.assertRaises(ValueError):connect_secure({'user':'SYSDBA'}, {'db_type':family})
        with self.assertRaises(ValueError):connect_secure({'user':'readonly'}, {'db_type':'sqlserver','version':'2005','least_privilege_reference':'fixture'})

    def test_reports_cover_all_requirements_and_redact(self):
        snapshot=overview(self.store,OWNER,self.catalog)
        html=render_html(self.catalog,snapshot,'隔离验证报告')
        content=render_docx(self.catalog,snapshot,'隔离验证报告')
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            document=archive.read('word/document.xml').decode()
        for req in self.catalog['requirements']:
            self.assertIn(req['id'],html)
            self.assertIn(req['id'],document)
        self.assertNotIn('192.0.2.123',html)
        self.assertNotIn('192.0.2.123',document)
        self.assertNotIn('基于 RaccoonX / DBCheck',document)
        self.assertNotIn('DBPulse 衍生维护',document)
        self.assertIn('未采集',document)

    def test_schedule_lease_prevents_duplicate_collection(self):
        now='2026-11-09T00:00:00+00:00'
        next_at='2026-11-09T00:05:00+00:00'
        self.assertTrue(self.store.claim_schedule(OWNER,self.asset['id'],now,next_at))
        self.assertFalse(self.store.claim_schedule(OWNER,self.asset['id'],now,next_at))

    def test_credential_crypto_failure_and_missing_key_fail_closed(self):
        from modules.pro import instance_manager as manager
        with patch.object(manager,'_FERNET_AVAILABLE',False):
            with self.assertRaises(RuntimeError):manager._encrypt_pwd('fixture-secret')
        key=Fernet.generate_key()
        with patch.object(manager,'_get_fernet',return_value=Fernet(key)):
            encrypted=manager._encrypt_pwd('fixture-secret')
            self.assertNotIn('fixture-secret',encrypted)
            self.assertEqual(manager._decrypt_pwd(encrypted),'fixture-secret')
            with self.assertRaises(ValueError):manager._decrypt_pwd('old-plaintext')
        import sqlite3
        directory=Path(self.temp.name)/'old-store'
        directory.mkdir()
        with sqlite3.connect(directory/'instances.db') as db:
            db.execute('CREATE TABLE instances(password TEXT,ssh_password TEXT)')
            db.execute('INSERT INTO instances VALUES(?,?)',(encrypted,''))
        missing_key=directory/'missing.key'
        with patch.object(manager.paths,'PRO_DATA_DIR',directory), patch.object(manager.paths,'DB_KEY_PATH',missing_key), patch.object(manager.paths,'PROJECT_ROOT',directory):
            with self.assertRaises(RuntimeError):manager._get_fernet()
        self.assertFalse(missing_key.exists())

    def test_outbound_data_is_local_by_default(self):
        from modules.core.data_security import ensure_egress_allowed
        with patch.dict('os.environ',{'DBPULSE_ALLOWED_EGRESS_HOSTS':''}):
            ensure_egress_allowed('http://127.0.0.1:11434')
            with self.assertRaises(ValueError):ensure_egress_allowed('https://external.example/collect')
            with self.assertRaises(ValueError):ensure_egress_allowed('http://localhost.evil.example')
            with self.assertRaises(ValueError):ensure_egress_allowed('file:///etc/passwd')
        with patch.dict('os.environ',{'DBPULSE_ALLOWED_EGRESS_HOSTS':'approved.example'}):
            ensure_egress_allowed('https://approved.example/api')
            with self.assertRaises(ValueError):ensure_egress_allowed('http://approved.example/api')

    def test_file_integrity_has_positive_and_negative_assessments(self):
        evidence={'target_file_hashes':{'status':'collected','rows':[{'status':'pass'}]},'target_file_permissions':{'status':'collected'}}
        self.assertEqual(assess('file_integrity',evidence,{'baseline_approved':True})['status'],'pass')
        evidence['target_file_hashes']['rows'][0]['status']='fail'
        self.assertEqual(assess('file_integrity',evidence)['status'],'fail')

    def test_fixed_collector_closes_and_bounds_mock_connection(self):
        class Cursor:
            description=[('PRODUCT_VERSION',)]
            def execute(self,sql):self.sql=sql
            def fetchmany(self,limit):return [('11g',)]
            def close(self):pass
        class Connection:
            closed=False
            def cursor(self):return Cursor()
            def rollback(self):pass
            def close(self):self.closed=True
        connection=Connection()
        result=collect({}, {'db_type':'oracle'},lambda *_:connection)
        self.assertEqual(result['version']['status'],'collected')
        self.assertTrue(connection.closed)
        self.assertNotEqual(assess('backup_integrity',result)['status'],'pass')

    def test_legacy_report_has_no_fabricated_disk_default(self):
        source=(ROOT/'modules/inspection/engine.py').read_text()
        self.assertNotIn("'total_gb': 50.0, 'used_gb': 25.0",source)
        self.assertIn("'disk_collection_status'] = 'not_collected'",source)

    def test_asset_history_and_stale_snapshot_rejection(self):
        item=next(i for i in all_items(self.catalog).values() if i['kind']=='inspection')
        self.store.save(OWNER,'inspection',{'status':'pass','missing':[]},asset_id=self.asset['id'],item_id=item['id'])
        updated=self.store.save(OWNER,'asset',{**self.payload,'host':'198.51.100.17'},record_id=self.asset['id'],version=1)
        versions=self.store.asset_history(OWNER,self.asset['id'])
        self.assertEqual([v['version'] for v in versions],[1,2])
        self.assertEqual(versions[0]['details']['host'],'192.0.2.123')
        row=next(r for r in overview(self.store,OWNER,self.catalog)['rows'] if r['item_id']==item['id'])
        self.assertEqual(row['status'],'not_collected')

    def test_configuration_identifiers_are_unique_within_tenant(self):
        another={**OWNER,'id':13}
        with self.assertRaises(ValueError):self.store.save(another,'asset',self.payload)
        self.store.save(OTHER,'asset',self.payload)

    def test_explicit_reviewer_read_scope_and_revocation(self):
        reviewer={**OWNER,'id':13,'approver':True}
        updated=self.store.save(OWNER,'asset',{**self.payload,'reviewers':[13]},record_id=self.asset['id'],version=1)
        record=self.store.save(OWNER,'inspection',{'status':'needs_review','missing':[]},asset_id=self.asset['id'],item_id='R01.01')
        self.assertEqual(self.store.get(reviewer,record['id'])['id'],record['id'])
        with self.assertRaises(PermissionError):self.store.save(reviewer,'asset',self.payload,record_id=self.asset['id'],version=2)
        self.store.save(OWNER,'asset',self.payload,record_id=self.asset['id'],version=2)
        with self.assertRaises(PermissionError):self.store.get(reviewer,record['id'])

    def test_service_event_timestamps_cannot_be_overwritten(self):
        record=self.store.save(OWNER,'workflow',{},asset_id=self.asset['id'],item_id='R01.01')
        record=self.store.record_event(OWNER,record['id'],1,'responded','fixture-ack')
        self.assertTrue(record['payload']['responded_at'])
        with self.assertRaises(ValueError):self.store.record_event(OWNER,record['id'],2,'responded','rewrite')

    def test_health_score_requires_complete_measured_coverage(self):
        snapshot=overview(self.store,OWNER,self.catalog)
        self.assertIsNone(snapshot['health_score'])
        item=next(i for i in all_items(self.catalog).values() if i['key']=='file_integrity')
        self.store.save(OWNER,'inspection',{'status':'fail','missing':[]},asset_id=self.asset['id'],item_id=item['id'])
        snapshot=overview(self.store,OWNER,self.catalog)
        self.assertEqual(snapshot['safety_gate'],'blocked')
        self.assertIsNone(snapshot['health_score'])

    def test_periodic_plans_are_idempotent_and_calendar_explicit(self):
        from modules.service_management.scheduler import plan_periodic_services
        asset=self.store.save(OWNER,'asset',{**self.payload,'service_schedule_enabled':True,'service_start_date':'2026-11-01'},record_id=self.asset['id'],version=1)
        now=datetime(2026,11,9,tzinfo=timezone.utc)
        self.assertEqual(plan_periodic_services(self.store,self.catalog,OWNER,asset,now),4)
        self.assertEqual(plan_periodic_services(self.store,self.catalog,OWNER,asset,now),0)
        monthly=next(r for r in self.store.list(OWNER,'workflow') if r['payload']['title'].startswith('month '))
        self.assertEqual(monthly['payload']['reason'],'客户工作日日历尚未配置')

    def test_asset_change_invalidates_pending_approval(self):
        record=self.store.save(OWNER,'workflow',{'risk_assessment':'fixture','rollback_plan':'fixture'},asset_id=self.asset['id'],item_id='R01.01')
        record=self.store.transition(OWNER,record['id'],'submitted',1,{})
        self.store.save(OWNER,'asset',{**self.payload,'version':'11.2'},record_id=self.asset['id'],version=1)
        with self.assertRaises(ValueError):self.store.transition(ADMIN,record['id'],'approved',2,{})


class DisclosureBoundaries(unittest.TestCase):
    def test_egress_redirects_and_insecure_tls_are_rejected(self):
        import ssl,urllib.request,urllib.error
        from modules.core.data_security import egress_urlopen
        from unittest.mock import MagicMock
        request=urllib.request.Request('https://approved.example/api',data=b'fixture')
        with patch.dict('os.environ',{'DBPULSE_ALLOWED_EGRESS_HOSTS':'approved.example'}),patch('urllib.request.build_opener') as build:
            egress_urlopen(request)
            handler=build.call_args.args[0]
            with self.assertRaises(urllib.error.HTTPError):handler.redirect_request(request,None,307,'redirect',{},'https://other.example')
            insecure=ssl._create_unverified_context()
            with self.assertRaises(ValueError):egress_urlopen(request,context=insecure)

    def test_webhook_redaction_and_no_crypto_downgrade(self):
        from modules.notify import WebhookNotifier,_encrypt_password
        from unittest.mock import MagicMock
        with patch('modules.notify.HAS_CRYPTOGRAPHY',False):
            with self.assertRaises(ValueError):_encrypt_password('fixture-secret')
        notifier=WebhookNotifier({'url':'https://hooks.example/endpoint','verify_ssl':True})
        response=MagicMock();response.__enter__.return_value.read.return_value=b'{}'
        with patch.dict('os.environ',{'DBPULSE_ALLOWED_EGRESS_HOSTS':'hooks.example'}),patch('modules.core.data_security.egress_urlopen',return_value=response) as send:
            notifier._send_webhook({'message':'password=fixture-secret 192.0.2.123'})
            payload=send.call_args.args[0].data.decode()
            self.assertNotIn('fixture-secret',payload)
            self.assertNotIn('192.0.2.123',payload)
        with patch.dict('os.environ',{'DBPULSE_ALLOWED_EGRESS_HOSTS':''}),patch('modules.core.data_security.egress_urlopen') as send:
            with self.assertRaises(ValueError):notifier._send_webhook({'message':'fixture'})
            send.assert_not_called()

    def test_email_attachment_and_body_are_sanitized_before_send(self):
        from modules.notify import EmailNotifier
        from docx import Document
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture.docx'
            document=Document();document.add_paragraph('password=fixture-secret 192.0.2.123');document.save(path)
            notifier=EmailNotifier({'host':'smtp.example','port':465,'recipients':['fixture@example.invalid']})
            with patch.dict('os.environ',{'DBPULSE_ALLOWED_EGRESS_HOSTS':'smtp.example'}),patch.object(notifier,'_send_smtp',return_value=True) as send:
                notifier.send_report('192.0.2.123','oracle',str(path),custom_msg='password=fixture-secret')
                message=send.call_args.args[0]
                attachment=next(part for part in message.walk() if part.get_content_maintype()=='application')
                with zipfile.ZipFile(io.BytesIO(attachment.get_payload(decode=True))) as archive:
                    xml=archive.read('word/document.xml').decode()
                self.assertNotIn('fixture-secret',xml);self.assertNotIn('192.0.2.123',xml)
                self.assertNotIn('fixture-secret',message.get_payload(0).get_payload(decode=True).decode())


class IntegratedHistoryMetrics(unittest.TestCase):
    def engines(self):
        from modules.inspection.db_history import SQLiteHistoryManager
        import importlib.util
        spec=importlib.util.spec_from_file_location('standalone_history_fixture',ROOT/'modules/skill/dbcheck/scripts/db_history.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for cls in (SQLiteHistoryManager,module.SQLiteHistoryManager):
            yield cls.__new__(cls)

    def test_oracle_dm_tablespace_scalars_and_unknown_limits(self):
        for engine in self.engines():
            oracle=engine._extract_metrics('oracle',{'ora_sessions':[{'TOTAL_SESSIONS':7}], 'ora_tablespace':[{'USED_PCT':0},{'USED_PCT_WITH_MAXEXT':72.5}]})
            self.assertEqual(oracle['max_tablespace_pct'],72.5)
            self.assertNotIn('max_connections',oracle)
            dm=engine._extract_metrics('dm',{'dm_sessions':[{'TOTAL_SESSIONS':7}],'dm_tablespace':[{'USED_PCT':'81.5'}]})
            self.assertEqual(dm['max_tablespace_pct'],81.5)
            self.assertNotIn('max_connections',dm)

    def test_partial_host_metadata_does_not_fabricate_zero_metrics(self):
        for engine in self.engines():
            missing=engine._extract_metrics('pg',{'system_info':{'hostname':'fixture'}})
            for key in ('cpu_usage','mem_usage','disk_usage_max'):self.assertIsNone(missing[key])
            zero=engine._extract_metrics('pg',{'system_info':{'cpu':{'usage_percent':0},'memory':{'usage_percent':0},'disk_list':[{'usage_percent':0}]}})
            for key in ('cpu_usage','mem_usage','disk_usage_max'):self.assertEqual(zero[key],0)


class ExistingSystemIntegration(unittest.TestCase):
    def test_builtin_inspection_queries_remain_readable(self):
        from modules.inspection import init_db
        for name,chapters in vars(init_db).items():
            if not name.endswith('_CHAPTERS') or not isinstance(chapters,list):continue
            for chapter in chapters:
                for query in chapter.get('queries',[]):
                    with self.subTest(template=name,key=query['key']):assert_readonly_sql(query['sql'])
        for family,queries in init_db.COMPATIBILITY_QUERIES.items():
            for sql in queries.values():assert_readonly_sql(sql)

    def test_db_type_registered_and_separate_from_8s(self):
        from modules.db_types.dbtype_registry import load_builtin_types
        types={row.db_type:row for row in load_builtin_types()}
        self.assertEqual(types['gbase8a'].port,5258)
        self.assertEqual(types['gbase8a'].connect_test,'gbase8a')
        self.assertEqual(types['gbase'].port,9088)
        self.assertIn('2005',types['sqlserver'].description)
        self.assertIn('11g',types['oracle'].description)

    def test_metrics_seed_is_idempotent_and_preserves_custom_queries(self):
        import contextlib,sqlite3
        from modules.inspection.dal import init_database,create_template,create_chapter,create_query
        from modules.inspection.init_db import init_default_templates
        from modules.entrypoints.main_sqlserver import SQLServerInspector
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'inspection.db'
            init_database(str(path))
            custom=create_template('sqlserver','客户自定义保留',db_path=str(path))
            chapter=create_chapter(custom,1,'用户指标',db_path=str(path))
            create_query(chapter,'user_marker','SELECT 17 AS KEEP_VALUE',db_path=str(path))
            with sqlite3.connect(path) as database:
                database.execute("UPDATE inspection_baseline SET expected_value='keep-user-value' WHERE db_type='gbase'")
            from modules.inspection.dal import init_default_baselines
            with contextlib.redirect_stdout(io.StringIO()):
                init_default_templates(str(path));init_default_templates(str(path));init_default_baselines(str(path))
            with sqlite3.connect(path) as database:
                self.assertEqual(database.execute("SELECT COUNT(*) FROM inspection_baseline WHERE db_type='gbase' AND expected_value!='keep-user-value'").fetchone()[0],0)
            with sqlite3.connect(path) as database:
                self.assertEqual(database.execute("SELECT query_sql FROM inspection_query WHERE query_key='user_marker'").fetchone()[0],'SELECT 17 AS KEEP_VALUE')
                for family,version,count in [('gbase8a','8a',14),('sqlserver','2005',17),('sqlserver','2008',17),('sqlserver','2014',17)]:
                    result=database.execute('SELECT t.id,COUNT(q.id) FROM inspection_template t JOIN inspection_chapter ch ON ch.template_id=t.id JOIN inspection_query q ON q.chapter_id=ch.id WHERE t.db_type=? AND t.version=? AND t.template_name_zh NOT LIKE ? GROUP BY t.id',(family,version,'%_业务专项巡检_%')).fetchall()
                    self.assertEqual(len(result),1);self.assertEqual(result[0][1],count)
                    if family=='sqlserver':
                        inspector=SQLServerInspector('localhost',1433,'reader','fixture')
                        inspector._db_version_major={'2005':9,'2008':10,'2014':12}[version]
                        with patch('modules.core.paths.INSPECTION_DB',path):self.assertEqual(inspector.get_template_id(),result[0][0])
                query=database.execute("SELECT q.query_sql FROM inspection_query q JOIN inspection_chapter ch ON ch.id=q.chapter_id JOIN inspection_template t ON t.id=ch.template_id WHERE t.db_type='sqlserver' AND t.version='2014' AND q.query_key='sqlserver_shared_pool'").fetchone()[0]
                self.assertIn('PAGES_KB',query);self.assertNotIn('SINGLE_PAGES_KB',query)

    def test_gbase8a_native_engine_verifies_family(self):
        from unittest.mock import MagicMock
        from modules.entrypoints.main_gbase8a import GBase8aInspector
        for banner,expected in [('GBase 8a MPP Cluster',True),('MySQL 8.0',False)]:
            connection=MagicMock();connection.cursor.return_value.fetchone.return_value=(banner,'')
            with patch('modules.entrypoints.main_gbase8a.connect_gbase8a',return_value=connection):
                inspector=GBase8aInspector('fixture',5258,'reader','fixture')
                self.assertEqual(inspector.connect()[0],expected)
                if not expected:connection.close.assert_called_once()

    def test_native_transports_do_not_downgrade(self):
        from modules.db_types.compatibility import connect_gbase8a,connect_sqlserver
        from unittest.mock import MagicMock
        data={'host':'fixture','port':5258,'user':'reader','password':'fixture-secret'}
        with patch('pymysql.connect') as mysql:
            with self.assertRaises(ValueError):connect_gbase8a(data)
            mysql.assert_not_called()
        odbc_module=MagicMock()
        odbc_module.drivers.return_value=['ODBC Driver 18 for SQL Server']
        with patch.dict(sys.modules,{'pyodbc':odbc_module}):
            odbc=odbc_module.connect
            connect_sqlserver({**data,'port':1433})
            value=odbc.call_args.args[0]
            self.assertIn('Encrypt=yes;',value)
            self.assertIn('TrustServerCertificate=no;',value)
        source=(ROOT/'modules/web/app.py').read_text(encoding='utf-8-sig')
        self.assertIn("register_connection_tester('gbase8a',_ct_gbase8a)",source)
        self.assertIn("module_name='main_gbase8a'",source)
        self.assertNotIn('CONTROLLED_COLLECTION_REQUIRED',source)


class DriverCompatibility(unittest.TestCase):
    def test_oracle_11g_initializes_thick_before_connect(self):
        from unittest.mock import Mock
        from modules.db_types.compatibility import oracle11g_client
        driver=Mock()
        driver.is_thin_mode.return_value=True
        driver.clientversion.return_value=(19,3,0,0,0)
        oracle11g_client(driver,{'version':'11g'})
        driver.init_oracle_client.assert_called_once_with()
        driver.clientversion.return_value=(23,0,0,0,0)
        with self.assertRaises(ValueError):oracle11g_client(driver,{'version':'11g'})
        driver.clientversion.return_value=(19,3,0,0,0)
        with self.assertRaises(ValueError):oracle11g_client(driver,{'version':'11.1.0.7'})

    def test_sqlserver_tds_versions_and_verified_tunnel(self):
        from unittest.mock import Mock, MagicMock
        import pytds
        from modules.service_management.inspection import connect_isolated
        instance={'host':'db.internal','port':1433,'user':'reader','password':'fixture',
                  'ssh_enabled':True,'ssh_host':'db.internal','ssh_user':'collector'}
        asset={'db_type':'sqlserver','ssh_host_role':'database_host'}
        for version,tds in [('2005',pytds.tds_base.TDS72),('2008',pytds.tds_base.TDS73),('2014',pytds.tds_base.TDS74)]:
            tunnel=MagicMock(local_port=12345)
            with patch('modules.ssh.SSHTunnel',return_value=tunnel),patch('pytds.connect') as connect:
                conn=connect_isolated(instance,{**asset,'version':version})
                self.assertEqual(connect.call_args.kwargs['server'],'127.0.0.1')
                self.assertEqual(connect.call_args.kwargs['tds_version'],tds)
                self.assertFalse(connect.call_args.kwargs['autocommit'])
                conn.close();tunnel.close.assert_called_once()
        with patch('modules.ssh.SSHTunnel') as tunnel:
            with self.assertRaises(ValueError):connect_isolated({**instance,'ssh_host':'other.internal'},asset)
            tunnel.assert_not_called()

    def test_dm_jdbc_requires_verified_tunnel_and_cleans_up(self):
        from unittest.mock import Mock, MagicMock
        from modules.service_management.inspection import connect_isolated
        tunnel=MagicMock(local_port=12345)
        instance={'host':'db.internal','port':5236,'user':'reader','password':'fixture',
                  'ssh_enabled':True,'ssh_host':'db.internal','ssh_user':'collector'}
        with patch('modules.ssh.SSHTunnel',return_value=tunnel),patch('modules.db_types.compatibility.jdbc_dm') as connect:
            conn=connect_isolated(instance,{'db_type':'dm','ssh_host_role':'database_host'})
            self.assertEqual(connect.call_args.args[0]['port'],12345)
            conn.close();tunnel.close.assert_called_once()
        with patch('modules.ssh.SSHTunnel',return_value=tunnel),patch('modules.db_types.compatibility.jdbc_dm',side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):connect_isolated(instance,{'db_type':'dm','ssh_host_role':'database_host'})
            self.assertEqual(tunnel.close.call_count,2)

    def test_driver_class_identity_and_no_false_live_validation(self):
        from modules.db_types.compatibility import jar_valid,readiness
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'wrong.jar'
            with zipfile.ZipFile(path,'w') as archive:archive.writestr('com/gbasedbt/jdbc/Driver.class',b'fixture')
            self.assertFalse(jar_valid(path,'com.gbase.jdbc.Driver'))
            self.assertTrue(jar_valid(path,'com.gbasedbt.jdbc.Driver'))
        rows=readiness()['drivers']
        self.assertEqual(len(rows),6)
        self.assertTrue(all(row['connection_verified'] is False for row in rows))

    def test_worker_uses_stdin_and_times_out_without_secret_in_error(self):
        import subprocess
        from modules.service_management.driver_worker import isolated_collect
        with patch('subprocess.run',side_effect=subprocess.TimeoutExpired('fixture',100)) as run:
            result=isolated_collect({'password':'fixture-secret'},{'db_type':'dm'})
            self.assertEqual(result['connection']['status'],'blocked')
            self.assertNotIn('fixture-secret',str(result))
            self.assertNotIn('fixture-secret',str(run.call_args.args[0]))
            self.assertIn('fixture-secret',run.call_args.kwargs['input'])
        result=isolated_collect({'user':'SYSDBA','password':'fixture-secret'},{'db_type':'dm'})
        self.assertEqual(result['connection']['status'],'blocked')
        self.assertNotIn('fixture-secret',str(result))


class WebSecurity(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='dbpulse-service-api-')
        self.addCleanup(self.temp.cleanup)
        self.app=Flask(__name__,template_folder=str(ROOT/'web_templates'),static_folder=str(ROOT/'assets/web'),static_url_path='/')
        self.app.secret_key='isolated-regression-only'
        self.app.config.update(TESTING=True,SERVICE_STORE=ServiceStore(Path(self.temp.name)/'services.db',Fernet.generate_key()),SERVICE_CATALOG_PATH=str(CATALOG))
        install_security(self.app)
        from modules.service_management.routes import bp
        self.app.register_blueprint(bp)
        from modules.core.brand import PRODUCT_NAME_EN,PRODUCT_FULL_NAME
        self.app.context_processor(lambda:dict(product_name=PRODUCT_NAME_EN,product_full_name=PRODUCT_FULL_NAME))
        @self.app.route('/api/auth/login',methods=['POST'])
        def fixture_login():
            from flask import session,jsonify
            session['user_id']=11
            return jsonify(ok=True)
        @self.app.route('/api/auth/logout',methods=['POST'])
        def fixture_logout():
            from flask import session,jsonify
            session.clear()
            return jsonify(ok=True)
        self.client=self.app.test_client()
        self.principal=Principal(id=11,username='fixture',tenant_id=1,roles=['operator'])
        self.patcher=patch('modules.access.get_principal',side_effect=lambda uid:self.principal if uid==11 else None)
        self.patcher.start();self.addCleanup(self.patcher.stop)

    def login(self):
        with self.client.session_transaction() as session:
            session['user_id']=11;session['_csrf']='fixture-csrf'

    def test_auth_csrf_fresh_identity_and_tenant_catalog(self):
        self.assertEqual(self.client.get('/api/services/catalog').status_code,401)
        self.login()
        self.assertEqual(self.client.get('/api/services/catalog').status_code,200)
        self.assertEqual(self.client.post('/api/services/assets',json={}).status_code,403)
        self.assertEqual(self.client.post('/api/services/assets',json={},headers={'Authorization':'Bearer invalid'}).status_code,403)
        self.principal.tenant_id=2
        self.assertEqual(self.client.get('/api/services/catalog').status_code,403)

    def test_page_coverage_and_forbidden_manual_inspection_pass(self):
        self.login()
        result=self.client.get('/services')
        self.assertEqual(result.status_code,302)
        self.assertEqual(result.headers['Location'],'/#services')
        self.assertEqual(result.headers['Cache-Control'],'no-store, private')
        item=next(i for i in all_items(load_catalog(CATALOG)).values() if i['kind']=='inspection')
        result=self.client.post('/api/services/evidence',json={'item_id':item['id'],'status':'pass'},headers={'X-CSRF-Token':'fixture-csrf'})
        self.assertEqual(result.status_code,400)

    def test_catalog_does_not_disable_existing_inspection_routes(self):
        # The catalog is data, not a global switch to disable existing features.
        self.login()
        self.app.add_url_rule('/api/start_inspection','fixture_inspect',lambda: {'ok':True},methods=['POST'])
        self.app.add_url_rule('/api/start_config_baseline','fixture_baseline',lambda: {'ok':True},methods=['POST'])
        self.app.add_url_rule('/api/start_index_health','fixture_indexes',lambda: {'ok':True},methods=['POST'])
        for route in ('/api/start_inspection','/api/start_config_baseline','/api/start_index_health'):
            response=self.client.post(route,json={},headers={'X-CSRF-Token':'fixture-csrf'})
            self.assertEqual(response.status_code,200)
        self.assertNotIn(b'customer-data-mode',self.client.get('/services').data)
        # 强制改密闸门已移除：不再因为「仍使用默认口令」把接口打成 428
        self.assertEqual(self.client.get('/api/services/catalog').status_code,200)

    def test_forced_password_rotation_is_removed(self):
        """强制改密这套东西已按产品要求整条移除。

        它此前会把所有页面 302 到 /security/rotate-password、把所有接口打成
        428 PASSWORD_ROTATION_REQUIRED，还要求一个「本机激活码」才能改密——
        拦住的只有自己人，没有实际安全收益。这里锁定移除结果，防止被重新加回。
        """
        self.login()
        # 直接断言路由表里不再有这两条规则：夹具应用挂了 static_url_path='/'
        # 的兜底静态路由，用状态码判断会被 405/404 的混响掩盖真实情况。
        rules = {str(rule) for rule in self.app.url_map.iter_rules()}
        self.assertEqual([r for r in rules if 'rotate-password' in r], [])
        self.assertEqual([r for r in rules if 'skip-password-rotation' in r], [])
        # 正常业务接口不再被 428 PASSWORD_ROTATION_REQUIRED 拦截
        self.assertEqual(self.client.get('/api/services/catalog').status_code,200)


if __name__=='__main__':
    unittest.main(verbosity=2)
