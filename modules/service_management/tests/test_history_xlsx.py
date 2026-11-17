# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Exact historical-file exports, owner sheets and archived signature regressions."""

import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from docx import Document
from flask import Flask, g
from openpyxl import load_workbook

from modules.access import Principal
from modules.service_management.delivery_capture import capture_inspection
from modules.service_management.delivery_routes import archive
from modules.service_management.delivery_store import digest
from modules.service_management.history_xlsx import TABLES, HEADERS, extract_tables, owner_rows, topic
from modules.service_management.routes import bp
from modules.service_management.store import ServiceStore
from modules.service_management.tests.test_delivery import InventoryFixture


class HistoryXlsxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.reports=self.root/'reports'
        self.reports.mkdir()
        self.services=ServiceStore(self.root/'services.db',Fernet.generate_key())
        self.inventory=InventoryFixture()
        self.principal=Principal(id=11,tenant_id=1,roles=['admin'])
        self.identity={'id':11,'tenant_id':1,'admin':True,'approver':True}
        self.app=Flask(__name__)
        self.app.config.update(SERVICE_STORE=self.services,REPORT_INVENTORY=self.inventory,
            REPORT_VISIBLE_IDS=lambda _: ['native'],DELIVERY_PRINCIPAL_RESOLVER=lambda _:self.principal,
            DELIVERY_SYNC=True,HISTORY_REPORTS_DIR=self.reports,HISTORY_CONTEXT_DB=self.root/'history.db',
            HISTORY_PRO_DB=self.root/'pro_history.db',SERVICE_CATALOG_PATH=self.root/'no-customer-catalog.json')
        @self.app.before_request
        def identity(): g.security_principal=self.principal
        self.app.register_blueprint(bp)
        self.path=self.reports/'Oracle巡检报告_20261117_fixture.docx'
        self.document(self.path,parameters=True)

    def document(self,path,parameters=False):
        document=Document()
        def table(title,headers,rows):
            document.add_heading(title,1)
            grid=document.add_table(rows=1,cols=len(headers))
            for cell,value in zip(grid.rows[0].cells,headers): cell.text=value
            for row in rows:
                for cell,value in zip(grid.add_row().cells,row): cell.text=value
        table('数据库基本信息',['配置项','内容'],[['配置项编号','CI-ORIGINAL'],['数据库名称','ORIGINAL_DB']])
        if parameters:
            table('数据库参数',['参数名称','参数值'],[['processes','128'],['formula','=1+1']])
        table('集群参数',['节点名称','参数名称','参数值'],[['node1','mode','primary']])
        table('逻辑存储空间属性',['逻辑存储空间名称','存储大小'],[['DATA','20 GB']])
        table('用户及权限',['用户名','具体权限'],[['qa_user','SELECT']])
        table('备份策略',['备份方式','执行频率'],[['全量','每日']])
        table('风险清单',['问题','整改方案'],[['连接水位','核对容量']])
        document.save(path)

    def post(self,path=None):
        return self.app.test_client().post('/api/report-center/history-xlsx',json={'filename':(path or self.path).name})

    def workbook(self,job):
        result=self.app.test_client().get('/api/report-center/'+job['id']+'/download?artifact_id='+job['artifacts'][0]['artifact_id'])
        self.assertEqual(result.status_code,200)
        self.assertEqual(digest(result.data),result.headers['X-Content-SHA256'])
        return load_workbook(io.BytesIO(result.data)),result.data

    def test_existing_word_exports_seven_owner_tables_without_registration(self):
        response=self.post()
        self.assertEqual(response.status_code,202,response.json)
        job=response.json['job']
        self.assertEqual(job['state'],'published',job.get('error'))
        workbook,content=self.workbook(job)
        self.assertEqual(workbook.sheetnames[:8],['报告说明',*TABLES])
        for title in HEADERS:
            self.assertTrue(set(HEADERS[title])<=set(cell.value for cell in workbook[title][2]))
        self.assertIn('ORIGINAL_DB',str(list(workbook['数据库基本信息表'].values)))
        self.assertIn('128',str(list(workbook['数据库参数表'].values)))
        formula=[cell for row in workbook['数据库参数表'] for cell in row if cell.value=='=1+1'][0]
        self.assertEqual(formula.data_type,'s')
        self.assertEqual(len(self.services.list(self.identity,'asset')),0)
        repeated=self.post().json['job']
        self.assertEqual(repeated['id'],job['id'])
        self.assertEqual(self.workbook(repeated)[1],content)
        with self.app.test_request_context():
            g.security_principal=self.principal
            self.assertTrue(archive().verify(self.identity,job['id'],content)['signature_valid'])

    def test_fixed_capture_wins_over_later_inspections_and_inventory_changes(self):
        self.document(self.path,parameters=True)
        with self.app.app_context(),patch('modules.access.get_principal',return_value=self.principal), \
             patch('modules.core.paths.REPORTS_DIR',self.reports):
            captured=capture_inspection({'id':'original-run','datasource_id':'native','owner_user_id':11,'owner_tenant_id':1,
                                        'report_path':str(self.path)}, {'parameters':[{'name':'processes','value':128},
                                                                                 {'name':'extra_captured_parameter','value':600}]})
            database=archive(self.identity)
            database.observe(self.identity,'native',{'parameters':{'status':'collected','rows':[{'name':'processes','value':999}]}})
            self.inventory.records['native']['metadata']['owner']='LATER_OWNER'
        response=self.post()
        job=response.json['job']
        self.assertEqual(job['state'],'published',job.get('error'))
        workbook,_=self.workbook(job)
        values=str(list(workbook['数据库参数表'].values))
        self.assertIn('128',values)
        self.assertIn('extra_captured_parameter',values)
        self.assertNotIn('999',values)
        self.assertNotIn('LATER_OWNER',str(list(workbook['全品类数据库资产底账清单'].values)))
        self.assertIn('original-run',str(list(workbook['数据库参数表'].values)))
        original_bytes=self.workbook(job)[1]
        self.inventory.records.pop('native')
        self.assertEqual(self.post().json['job']['id'],job['id'])
        self.assertEqual(self.workbook(job)[1],original_bytes)
        self.document(self.path,parameters=True)
        edited=Document(self.path)
        edited.add_paragraph('changed original file')
        edited.save(self.path)
        revised=self.post().json['job']
        self.assertNotEqual(revised['id'],job['id'])
        self.assertIn('摘要不一致',str(list(self.workbook(revised)[0]['报告说明'].values)))

    def test_legacy_snapshot_requires_an_exact_task_match(self):
        task='d0ef0284-7cdf-42de-afb5-2cbdf18942c5'
        path=self.reports/('Oracle巡检报告_20261117_'+task+'.docx')
        self.document(path,parameters=False)
        with sqlite3.connect(self.root/'history.db') as db:
            db.execute('CREATE TABLE snapshots(id INTEGER,ts TEXT,context_json TEXT)')
            for identity,run,value in [(1,task,128),(2,'later-task',999)]:
                db.execute('INSERT INTO snapshots VALUES(?,?,?)',(identity,'2026-11-17 00:00:00',json.dumps({
                    '_report_task_id':run,'_instance_id':'native','parameters':[{'name':'processes','value':value}]})))
        job=self.post(path).json['job']
        self.assertEqual(job['state'],'published',job.get('error'))
        values=str(list(self.workbook(job)[0]['数据库参数表'].values))
        self.assertIn('128',values)
        self.assertNotIn('999',values)

    def test_history_access_and_paths_follow_the_existing_boundary(self):
        client=self.app.test_client()
        self.principal=Principal(id=11,tenant_id=1,roles=['operator'])
        self.assertEqual(self.post().status_code,403)
        self.principal=Principal(id=11,tenant_id=2,roles=['admin'])
        self.assertEqual(self.post().status_code,403)
        self.principal=Principal(id=11,tenant_id=1,roles=['admin'])
        for filename in ['../report.docx','/tmp/report.docx','report.pdf','missing.docx']:
            self.assertEqual(client.post('/api/report-center/history-xlsx',json={'filename':filename}).status_code,400)
        link=self.reports/'link.docx'
        link.symlink_to(self.path)
        self.assertEqual(self.post(link).status_code,400)

    def test_concurrent_requests_share_one_archive(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses=list(executor.map(lambda _:self.post(),range(2)))
        self.assertTrue(all(response.status_code==202 for response in responses))
        self.assertEqual(len({response.json['job']['id'] for response in responses}),1)
        with self.app.app_context():
            job=archive(self.identity).get(self.identity,responses[0].json['job']['id'])
        self.assertEqual(job['state'],'published',job.get('error'))

    def test_original_headers_and_sensitive_columns(self):
        self.assertEqual(topic('集群配置漂移核对：实例配置项',['name（名称）','value（值）','value_in_use（当前生效值）']),'数据库参数表')
        rows=owner_rows('数据库参数表',[{'name（名称）':'processes','value（值）':128,'value_in_use（当前生效值）':128}])
        self.assertEqual(rows[0]['参数名称'],'processes')
        self.assertEqual(rows[0]['运行值'],128)
        document=Document()
        document.add_heading('用户及权限',1)
        table=document.add_table(rows=2,cols=2)
        for cell,value in zip(table.rows[0].cells,['用户名','密码']):cell.text=value
        for cell,value in zip(table.rows[1].cells,['qa','fixture-private-password']):cell.text=value
        document.add_heading('数据库参数',1)
        table=document.add_table(rows=2,cols=2)
        for cell,value in zip(table.rows[0].cells,['参数名称','参数值']):cell.text=value
        for cell,value in zip(table.rows[1].cells,['ssl_password','fixture-private-key']):cell.text=value
        data=io.BytesIO()
        document.save(data)
        extracted=json.dumps(extract_tables(data.getvalue()),ensure_ascii=False)
        self.assertNotIn('fixture-private-password',extracted)
        self.assertNotIn('fixture-private-key',extracted)

    def test_matching_encrypted_inventory_observation_recovers_original_accounts(self):
        observed='2026-11-17T01:00:00+00:00'
        old_metadata={'configuration_id':'CI-TIME','owner':'ORIGINAL_TEAM',
                      'tables':{'accounts':[{'username':'audit@example.test','purpose':'audit','owner':'ACCOUNT_TEAM'}]}}
        self.inventory.records['native']['metadata']['owner']='CURRENT_TEAM'
        self.inventory.history=lambda actor,ids:[{'created':'2026-11-17T00:00:00+00:00',
            'details':{'before':None,'after':{'source':{'host':'192.0.2.10','port':1521,'db_type':'oracle','database':'original'},
                                          'metadata':old_metadata}}}]
        self.inventory.observation_for_report=lambda actor,name:{'id':17,'instance_id':'native','task_id':'original-inventory-run',
            'observed_at':observed,'data':{'auto_fields':{'charset':'UTF8'},'basic_info':{'db_name':'original'},
            'specialized':{'accounts':{'status':'collected','rows':[{'username':'audit@example.test','status':'OPEN'}]}}}}
        response=self.post()
        self.assertEqual(response.status_code,202,response.json)
        workbook,_=self.workbook(response.json['job'])
        users=str(list(workbook['用户及权限表'].values))
        assets=str(list(workbook['全品类数据库资产底账清单'].values))
        self.assertIn('audit@example.test',users)
        self.assertIn('ACCOUNT_TEAM',users)
        self.assertIn('ORIGINAL_TEAM',assets)
        self.assertNotIn('CURRENT_TEAM',assets)


if __name__=='__main__':
    unittest.main()
