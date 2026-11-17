# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Owner-field mapping, joins, physical units and identity-preserving metadata tests."""

import unittest
from modules.core.data_security import assert_readonly_sql,redact
from modules.inspection.owner_details import BACKUP_QUERIES,collect_owner_details
from modules.service_management.history_xlsx import HEADERS,owner_rows,topic
from modules.service_management.owner_field_adaptation import adapt_tables,classify_missing,deduplicate,redact_owner_tables
from modules.service_management.delivery_data import sources_from_context,source_matches


def sheets(rows):
    result={title:owner_rows(title,rows.get(title,[])) for title in HEADERS}
    result['原始表格明细']=[]
    return result


class OwnerFieldAdaptationTests(unittest.TestCase):
    def test_duplicate_evidence_keeps_conflicts_serializable(self):
        import json
        first={'用户名':'qa','说明':'first'}
        second={'用户名':'qa','说明':'second','同次取值差异':{'来源说明':['old','new']}}
        rows=deduplicate([first,first,second],('用户名',))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['同次取值差异']['说明'],['first','second'])
        json.dumps(rows)

    def test_native_headers_and_same_inspection_identity(self):
        data=sheets({'数据库基本信息表':[{'配置项编号':'CI-ORIGINAL','数据库名称':'original',
            '实例名':'instance-1','字符集':'UTF8','服务器地址':'[2001:db8::1]:1521'}],
            '数据库参数表':[{'记录类型':'数据库参数','PARA_NAME（参数名称）':'MAX_SESSIONS','PARA_VALUE（参数值）':0}]})
        adapt_tables(data,{'db_type':'dm'},None,'2026-11-17T01:00:00+00:00')
        row=data['数据库参数表'][0]
        self.assertEqual(row['参数名称'],'MAX_SESSIONS')
        self.assertEqual(row['配置值'],0)
        self.assertEqual(row['运行值'],0)
        self.assertEqual(row['数据库名称'],'original')
        self.assertEqual(row['配置项编号'],'CI-ORIGINAL')
        self.assertTrue(row['参数版本'].startswith('sha256:'))
        self.assertEqual(topic('集群配置漂移核对',['PARA_NAME（参数名称）','PARA_VALUE（参数值）']),'数据库参数表')
        self.assertEqual(topic('数据库基础信息',['NAME','DATABASE_ROLE']),'数据库基本信息表')

    def test_storage_units_zero_growth_and_usage(self):
        data=sheets({'数据库基本信息表':[{'字符集':'UTF8'}],
            '逻辑存储空间属性表':[
                {'记录类型':'存储文件','name':'data','physical_name':'/qa/data.mdf','size':64,'growth':0,'is_percent_growth':False},
                {'记录类型':'逻辑存储空间','TABLESPACE_NAME':'DATA','TOTAL_MB':100,'FREE_MB':75},
                {'记录类型':'逻辑存储空间','TABLE_SCHEMA':'qa','DATA_MB':0}]})
        adapt_tables(data,{'db_type':'sqlserver'},None,None)
        file,space,schema=data['逻辑存储空间属性表']
        self.assertEqual(file['存储大小（字节）'],64*8192)
        self.assertEqual(file['自动扩展模式'],'关闭')
        self.assertEqual(file['文件路径'],'/qa/data.mdf')
        self.assertEqual(space['存储大小（字节）'],100*1024**2)
        self.assertEqual(space['当前使用率'],'25.00%')
        self.assertEqual(schema['存储大小（字节）'],0)
        self.assertEqual(schema['类型'],'数据库/Schema')

    def test_accounts_roles_denials_and_frozen_registration(self):
        data=sheets({'数据库基本信息表':[{'字符集':'UTF8'}], '用户及权限表':[
            {'记录类型':'用户账号','name':'qa'},
            {'记录类型':'角色成员','member_name':'qa','role_name':'db_reader'},
            {'记录类型':'权限明细','grantee':'qa','privilege':'SELECT','table_name':'T','owner':'APP','state_desc':'DENY'}]})
        asset={'payload':{'tables':{'accounts':[{'username':'qa','purpose':'query','owner':'account-team'}]}}}
        adapt_tables(data,{'db_type':'sqlserver'},asset,None)
        account=data['用户及权限表'][0]
        self.assertEqual(account['用户名'],'qa')
        self.assertEqual(account['角色'],'db_reader')
        self.assertEqual(account['权限'],'DENY SELECT ON APP.T')
        self.assertEqual(account['责任归属人'],'account-team')

    def test_node_record_fields_are_not_parameter_gaps(self):
        data=sheets({'数据库基本信息表':[{'字符集':'UTF8'}], '集群参数表':[
            {'记录类型':'集群节点','INSTANCE_NAME':'same','INST_ID':1,'HOST_NAME':'node-a'},
            {'记录类型':'集群节点','INSTANCE_NAME':'same','INST_ID':2,'HOST_NAME':'node-b'}]})
        adapt_tables(data,{'db_type':'oracle'},None,None)
        row=data['集群参数表'][0]
        classify_missing('集群参数表',row,HEADERS['集群参数表'])
        self.assertEqual(row['节点数量'],2)
        self.assertIn('参数名称',row['不适用字段'])
        self.assertNotIn('参数名称',row['未记录字段'])

    def test_backup_configuration_is_distinct_from_execution_history(self):
        data=sheets({'数据库基本信息表':[{'字符集':'UTF8'}], '备份策略表':[
            {'job_name':'backup-qa','repeat_interval':'FREQ=DAILY;BYHOUR=2','sql_text':'BACKUP DATABASE [QA]'},
            {'policy_parameter':'RETENTION POLICY','policy_value':'RECOVERY WINDOW OF 7 DAYS'}]})
        adapt_tables(data,{'db_type':'oracle'},None,None)
        first,retention=data['备份策略表']
        self.assertEqual(first['执行频率'],'FREQ=DAILY;BYHOUR=2')
        self.assertTrue(first['策略版本'].startswith('sha256:'))
        self.assertEqual(retention['保存周期'],'RECOVERY WINDOW OF 7 DAYS')
        self.assertEqual(topic('全量备份策略执行核查',['backup_start_date','backup_size']),'备份执行记录')

    def test_authorized_metadata_identifiers_preserve_secrets_boundary(self):
        result=redact_owner_tables({'用户名':'qa@example.test','password':'private-value',
                                   'sql_text':"SELECT 'private-business-value'"})
        self.assertEqual(result['用户名'],'qa@example.test')
        self.assertNotIn('private-value',str(result))
        self.assertNotIn('private-business-value',str(result))
        context={'users':[{'username':'ordinary-user'}], '_owner_details':{'accounts':{
            'status':'collected','rows':[{'username':'qa@example.test','password':'private-value'}]}}}
        sources=sources_from_context(context)
        self.assertEqual(sources['accounts']['rows'][0]['username'],'qa@example.test')
        self.assertEqual(sources['users']['rows'][0]['username'],'[MASKED]')
        self.assertEqual(redact({'para_name':'ssl_pwd','para_value':'private','display_value':'private'})['para_value'],'[REDACTED]')

    def test_readonly_owner_metadata_and_failure_isolation(self):
        for queries in BACKUP_QUERIES.values():
            for sql in queries.values():assert_readonly_sql(sql)
        class Cursor:
            description=[('NAME',),('VALUE',)]
            def execute(self,sql):
                if 'DBA_USERS' in sql:raise ValueError('fixture view denied')
                self.sql=sql
            def fetchmany(self,size):return [('p',0)]
        result=collect_owner_details(Cursor(),'oracle')
        self.assertEqual(result['accounts']['status'],'error')
        self.assertEqual(result['parameter_metadata']['status'],'collected')
        self.assertTrue(source_matches('oracle_dba_tab_privs','权限明细'))


if __name__=='__main__':
    unittest.main()
