#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Isolated regressions for cluster identity, partial failures and ownership."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import copy
import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from flask import Flask, g, jsonify, request
from modules.access import Principal
from modules.clusters.store import ClusterStore, Conflict
from modules.clusters.model import aggregate, transition_events, validate_spec
from modules.clusters.collector import queries, normalize
from modules.clusters.service import ClusterService
from modules.clusters.routes import bp
from modules.core.data_security import assert_readonly_sql

ADMIN = Principal(id=1,username='fixture-admin',tenant_id=1,roles=['admin'])
USER = Principal(id=2,tenant_id=1,roles=['operator'])
OTHER = Principal(id=3,tenant_id=2,roles=['admin'])
SOURCES = {
 'one':dict(id='one',name='one',db_type='oracle_jdbc',host='node1',port=1521,service_name='RAC1'),
 'two':dict(id='two',name='two',db_type='oracle',host='node2',port=1521,service_name='RAC2'),
 'dm':dict(id='dm',name='dm',db_type='dm',host='dm1',port=5236),
}


def spec(kind='oracle_rac', ids=('one','two')):
    return {'name':'fixture-cluster','kind':kind,'expected_nodes':2,'interval_seconds':30,
            'lag_alert_seconds':30,'monitor_enabled':False,'service_instance_id':'',
            'members':[{'instance_id':iid,'expected_role':'member','enabled':True} for iid in ids]}


def node(iid, role='primary', **updates):
    value = {'instance_id':iid,'available':True,'ts':time.time(),'identity':iid,'database_identity':'RACDB',
             'role':role,'version':'11.2.0.4','parameters':{'db_block_size':'8192'},'errors':{},
             'instance_status':'OPEN','repl_lag_s':0,'discovered_members':[{'name':'one'},{'name':'two'}],
             'archive_destinations':[{'db_unique_name':'standby','status':'VALID'}]}
    value.update(updates)
    return value


class ClusterChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ClusterStore(Path(self.temp.name)/'clusters.db')
        self.record = self.store.save(ADMIN,validate_spec(spec(),lambda iid:SOURCES[iid]))

    def tearDown(self):
        self.temp.cleanup()

    def test_validation_rejects_mixed_family_and_duplicate_endpoint(self):
        with self.assertRaises(ValueError):
            validate_spec(spec(ids=('one','dm')),lambda iid:SOURCES[iid])
        with self.assertRaises(ValueError):
            validate_spec(spec(ids=('one','one')),lambda iid:SOURCES[iid])
        same = dict(SOURCES['one'],id='two')
        with self.assertRaises(ValueError):
            validate_spec(spec(),lambda iid:SOURCES['one'] if iid=='one' else same)
        for field,value in [('expected_nodes',True),('expected_nodes',1),('interval_seconds',0),('monitor_enabled','false')]:
            data=spec();data[field]=value
            with self.assertRaises(ValueError):validate_spec(data,lambda iid:SOURCES[iid])

    def test_no_credentials_saved_and_ownership_isolated(self):
        data=spec();data['password']='not-a-real-secret';data['members'][0]['password']='ignored'
        clean=validate_spec(data,lambda iid:SOURCES[iid])
        self.assertNotIn('password',json.dumps(clean))
        with self.assertRaises(PermissionError):self.store.get(self.record['id'],OTHER)
        self.assertEqual(self.store.list(USER),[])
        with self.assertRaises(PermissionError):self.store.save(USER,clean,self.record['id'],1)

    def test_optimistic_revision_and_stale_collection(self):
        token=self.store.claim_monitor(self.record)
        self.assertIsNotNone(token)
        self.assertIsNone(self.store.claim_monitor(self.record))
        updated=self.store.save(ADMIN,spec(),self.record['id'],1)
        with self.assertRaises(Conflict):self.store.save(ADMIN,spec(),self.record['id'],1)
        observation=aggregate(self.record,{'one':node('one'),'two':node('two')})
        self.assertFalse(self.store.finish_monitor(self.record,token,observation,[]))
        self.assertIsNone(self.store.latest(updated['id']))

    def test_full_coverage_missing_down_and_stale_are_distinct(self):
        complete=aggregate(self.record,{'one':node('one'),'two':node('two')})
        self.assertEqual(complete['status'],'healthy')
        self.assertEqual(complete['coverage_pct'],100)
        partial=aggregate(self.record,{'one':node('one')})
        self.assertEqual(partial['status'],'incomplete')
        self.assertEqual(partial['coverage_pct'],50)
        down=aggregate(self.record,{'one':node('one'),'two':node('two',available=False)})
        self.assertEqual(down['status'],'critical')
        stale=aggregate(self.record,{'one':node('one',ts=time.time()-1000),'two':node('two')})
        self.assertEqual(stale['status'],'incomplete')

    def test_unrelated_instances_do_not_establish_cluster_membership(self):
        result=aggregate(self.record,{'one':node('one',discovered_members=[]),
                                      'two':node('two',discovered_members=[])})
        self.assertEqual(result['status'],'incomplete')
        self.assertIn('topology_unconfirmed',[row['code'] for row in result['issues']])
        record=copy.deepcopy(self.record);record['spec']['kind']='oracle_dataguard'
        result=aggregate(record,{'one':node('one',database_identity='P',archive_destinations=[]),
                                 'two':node('two','standby',database_identity='S')})
        self.assertEqual(result['status'],'incomplete')
        self.assertIn('replication_link_unknown',[row['code'] for row in result['issues']])

    def test_duplicate_identity_never_counts_twice(self):
        result=aggregate(self.record,{'one':node('one'),'two':node('two',identity='one')})
        self.assertEqual(result['observed_nodes'],1)
        self.assertEqual(result['status'],'critical')
        self.assertIn('duplicate_identity',[row['code'] for row in result['issues']])

    def test_primary_cardinality_lag_and_role_change_events(self):
        record=copy.deepcopy(self.record);record['spec']['kind']='oracle_dataguard'
        before=aggregate(record,{'one':node('one',database_identity='primary'),
                                 'two':node('two','standby',database_identity='standby')})
        self.assertEqual(before['status'],'healthy')
        after=aggregate(record,{'one':node('one','standby',database_identity='primary',repl_lag_s=40),
                                'two':node('two','primary',database_identity='standby')})
        self.assertEqual(after['status'],'critical')
        events=transition_events(before,after)
        self.assertEqual(sum(row['kind']=='role_changed' for row in events),2)
        dual=aggregate(record,{'one':node('one',database_identity='A'),'two':node('two',database_identity='B')})
        self.assertIn('primary_cardinality',[row['code'] for row in dual['issues']])

    def test_drift_missing_optional_evidence_and_service_endpoint(self):
        record=copy.deepcopy(self.record);record['spec']['service_instance_id']='one'
        result=aggregate(record,{'one':node('one'),'two':node('two',version='19.0',parameters={'db_block_size':'16384'},errors={'members':'not visible'})})
        codes={row['code'] for row in result['issues']}
        self.assertTrue({'parameter_drift','version_drift','evidence_missing','service_unavailable'}<=codes)

    def test_idempotent_run_reservation_and_active_run_lock(self):
        def create(_):return self.store.create_run(self.record,'request-fixture','runtime')
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(create,range(4)))
        self.assertEqual(sum(created for _,created in results),1)
        self.assertEqual(len({run['id'] for run,_ in results}),1)
        with self.assertRaises(Conflict):self.store.create_run(self.record,'another-request','runtime')
        with self.assertRaises(Conflict):self.store.delete(self.record['id'],ADMIN,1)

    def test_restart_preserves_completed_members_and_recovers_lost_tasks(self):
        run,_=self.store.create_run(self.record,'request-fixture','dead-runtime')
        run['payload']['members'][0]['status']='done'
        run['payload']['members'][1]['status']='running'
        self.store.update_run(run['id'],run['payload'],'running')
        self.store.interrupt_previous_runs('new-runtime')
        recovered=self.store.get_run(run['id'])
        self.assertEqual(recovered['status'],'interrupted')
        self.assertEqual([m['status'] for m in recovered['payload']['members']],['done','interrupted'])

    def test_live_second_process_runtime_does_not_interrupt_active_run(self):
        self.store.register_runtime('live-runtime')
        run,_=self.store.create_run(self.record,'request-fixture','live-runtime')
        self.store.interrupt_previous_runs('another-runtime')
        self.assertEqual(self.store.get_run(run['id'])['status'],'dispatching')

    def test_fixed_queries_pass_readonly_guard_and_rate_reset_is_unknown(self):
        for kind in ('oracle_rac','oracle_dataguard','dm_dsc','dm_guard'):
            for sql in queries(kind).values():assert_readonly_sql(sql)
        result={'identity':{'rows':[{'db_unique_name':'RACDB','database_role':'PRIMARY'}]},
                'instance':{'rows':[{'instance_name':'RAC1','host_name':'node1','status':'OPEN'}]},
                'stat':{'rows':[{'name':'execute count','value':10}]}}
        current=normalize(SOURCES['one'],'oracle_rac',result)
        old=dict(current,ts=current['ts']-10,counters={'executions':100,'transactions':20})
        reset=normalize(SOURCES['one'],'oracle_rac',result,old)
        self.assertIsNone(reset['qps'])
        self.assertIsNone(reset['repl_lag_s'])

    def test_dm_delay_uses_microseconds_and_missing_delay_stays_unknown(self):
        result={'identity':{'rows':[{'name':'DAMENG'}]},
                'instance':{'rows':[{'instance_name':'DM2','host_name':'node2','mode$':'STANDBY'}]},
                'repl':{'rows':[{'pkg_receive_delay':2000000,'pkg_apply_delay':3500000}]}}
        value=normalize(SOURCES['dm'],'dm_guard',result)
        self.assertEqual(value['repl_lag_s'],5.5)
        self.assertEqual(value['role'],'standby')
        result['repl']['rows'][0]['pkg_receive_delay']=None
        self.assertIsNone(normalize(SOURCES['dm'],'dm_guard',result)['repl_lag_s'])

    def test_dead_collector_lease_is_recovered_without_clearing_live_lease(self):
        self.store.register_runtime('live')
        self.assertIsNotNone(self.store.claim_monitor(self.record,'live'))
        self.store.clear_dead_monitor_leases()
        self.assertIsNone(self.store.claim_monitor(self.record,'new'))
        with self.store.connection(write=True) as db:
            db.execute('UPDATE clusters SET lease_token=? WHERE id=?',('dead:fixture',self.record['id']))
        self.store.clear_dead_monitor_leases()
        self.assertIsNotNone(self.store.claim_monitor(self.record,'new'))

    def test_timed_out_batch_blocks_new_dispatch_until_reconciled(self):
        app=self.make_app()
        app.config['CLUSTER_TASK_LOOKUP']=lambda iid:{'status':'running'}
        service=ClusterService(app,store=self.store)
        record=copy.deepcopy(self.record)
        record['spec']['members'].append({'instance_id':'three','expected_role':'member','enabled':True})
        run,_=self.store.create_run(record,'timeout-fixture',service.runtime)
        with patch.object(service,'_start_node_inspection',side_effect=lambda rec,member,tid:member['instance_id']) as start,patch('modules.clusters.service.ensure_template',return_value=1),patch('modules.clusters.service.time.monotonic',side_effect=[0,901,901]):
            service._run_inspections(run)
        self.assertEqual(start.call_count,2)
        result=self.store.get_run(run['id'])
        self.assertEqual(result['status'],'unconfirmed')
        self.assertEqual(result['payload']['members'][2]['status'],'not_started')
        with self.assertRaises(Conflict):self.store.create_run(self.record,'another-timeout-fixture',service.runtime)
        app.config['CLUSTER_TASK_LOOKUP']=lambda iid:{'status':'done'}
        service.reconcile_run(result)
        self.assertEqual(self.store.get_run(run['id'])['status'],'partial_failed')

    def make_app(self):
        app=Flask(__name__,template_folder=str(ROOT/'web_templates'))
        app.secret_key='isolated-cluster-fixture'
        app.config.update(CLUSTER_STORE=self.store,CLUSTER_MONITOR_DISABLED=True)
        app.register_blueprint(bp)
        @app.before_request
        def actor():g.security_principal=ADMIN
        @app.route('/api/start_inspection',methods=['POST'])
        def api_start_inspection():
            self.assertEqual(g.security_principal.id,ADMIN.id)
            iid=request.json['datasource_id']
            return jsonify(ok=True,task_id=iid)
        return app

    def test_run_uses_normal_node_entrypoint_and_retains_partial_failure(self):
        app=self.make_app()
        app.config['CLUSTER_TASK_LOOKUP']=lambda iid:dict(status='done' if iid=='one' else 'error',report_file='',result={})
        service=ClusterService(app,store=self.store)
        run,_=self.store.create_run(self.record,'fixture-normal-path',service.runtime)
        with patch('modules.clusters.service.visible_instance',side_effect=lambda actor,iid,decrypted=False:SOURCES[iid]),patch('modules.clusters.service.ensure_template',return_value=123):
            service._run_inspections(run)
        result=self.store.get_run(run['id'])
        self.assertEqual(result['status'],'partial_failed')
        self.assertEqual([m['task_id'] for m in result['payload']['members']],['one','two'])
        self.assertEqual([m['status'] for m in result['payload']['members']],['done','error'])

    def test_routes_version_conflict_history_report_and_revoked_member(self):
        app=self.make_app();service=ClusterService(app,store=self.store)
        app.extensions['cluster_service']=service
        resolve=lambda actor,iid,decrypted=False:SOURCES[iid]
        with patch('modules.clusters.routes.visible_instance',side_effect=resolve):
            client=app.test_client()
            response=client.get('/api/clusters');self.assertEqual(response.status_code,200)
            body=dict(spec(),version=99)
            self.assertEqual(client.put('/api/clusters/'+self.record['id'],json=body).status_code,409)
            self.assertEqual(client.get('/api/clusters/'+self.record['id']+'/history').status_code,200)
            run,_=self.store.create_run(self.record,'report-fixture',service.runtime)
            run['payload']['members'][0].update(status='done',report_name='fixture.docx',report_file='/private/not-public')
            self.store.update_run(run['id'],run['payload'],'partial_failed')
            report=client.get('/cluster-inspections/'+run['id']+'/report')
            self.assertEqual(report.status_code,200)
            self.assertIn('DBPulse',report.text)
            api=client.get('/api/cluster-inspections/'+run['id']).json
            self.assertNotIn('report_file',api['run']['payload']['members'][0])
            self.assertEqual(client.get('/api/cluster-inspections/'+run['id']+'/members/one/report').status_code,404)
        with patch('modules.clusters.routes.visible_instance',side_effect=PermissionError('revoked')):
            self.assertEqual(app.test_client().get('/api/clusters/'+self.record['id']).status_code,403)


if __name__=='__main__':
    unittest.main(verbosity=2)
