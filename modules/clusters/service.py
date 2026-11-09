# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Continuous cluster monitoring and bounded fan-out to existing inspection tasks."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import copy
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import g, session
from modules.core import paths
from modules.core.data_security import redact
from .collector import collect_node
from .model import aggregate, transition_events
from .store import ClusterStore
from .templates import ensure_template

_log = logging.getLogger('dbcheck.clusters')
_service_lock = threading.Lock()


def visible_instance(principal, instance_id, decrypted=False):
    from modules.access import get_owner, visible_to
    from modules.pro import get_instance_manager
    if not visible_to(principal,get_owner('instance',instance_id)):
        raise PermissionError('无权访问集群成员数据源')
    manager = get_instance_manager()
    value = manager.get_instance_decrypted(instance_id) if decrypted else manager.get_instance(instance_id)
    if not value:
        raise ValueError('集群成员数据源已被删除')
    return value


class ClusterService:
    def __init__(self, app, store=None, collect=collect_node):
        self.app = app
        self.store = store or ClusterStore(paths.DATA_DIR/'clusters.db')
        self.collect_node = collect
        self.runtime = str(uuid.uuid4())
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.store.register_runtime(self.runtime)
        self.store.clear_dead_monitor_leases()
        self.store.interrupt_previous_runs(self.runtime)

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._loop,name='cluster-monitor',daemon=True)
            self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.store.interrupt_previous_runs(self.runtime)
                for record in self.store.list():
                    for run in self.store.runs(record['id']):
                        if run['runtime']==self.runtime and run['status']=='unconfirmed':
                            self.reconcile_run(run)
                    if self.stop_event.is_set():
                        break
                    if not record['spec']['monitor_enabled']:
                        continue
                    previous = self.store.latest(record['id'])
                    if not previous or previous.get('config_version') != record['version'] or time.time()-previous['ts'] >= record['spec']['interval_seconds']:
                        self.refresh(record)
            except Exception:
                _log.exception('集群监控轮次失败')
            self.stop_event.wait(3)

    def _collect_member(self, record, iid, previous):
        from modules.access import get_principal
        try:
            principal = get_principal(record['owner'])
            if principal is None or principal.tenant_id != record['tenant']:
                raise PermissionError('集群拥有者身份已失效')
            instance = visible_instance(principal,iid,decrypted=True)
            result = self.collect_node(instance,record['spec']['kind'],previous)
            result['instance_id'] = iid
            return result
        except Exception as error:
            # Store only a redacted error, never connection configuration or credentials.
            message = str(error)
            for field in ('password','ssh_password','ssh_key_password'):
                secret = locals().get('instance',{}).get(field)
                if secret:
                    message = message.replace(str(secret),'[REDACTED]')
            return {'instance_id':iid,'ts':time.time(),'available':False,'identity':None,
                    'role':'unknown','errors':{'collection':str(redact(message))[:300]}}

    def refresh(self, record):
        token = self.store.claim_monitor(record,self.runtime)
        if token is None:
            return False
        try:
            previous = self.store.latest(record['id'])
            old_nodes = (previous or {}).get('nodes',{})
            ids = [member['instance_id'] for member in record['spec']['members'] if member['enabled']]
            service_id = record['spec']['service_instance_id']
            collect_ids = list(dict.fromkeys(ids+([service_id] if service_id else [])))
            samples = {}
            with ThreadPoolExecutor(max_workers=min(4,len(collect_ids)),thread_name_prefix='cluster-node') as pool:
                pending = {pool.submit(self._collect_member,record,iid,old_nodes.get(iid)):iid for iid in collect_ids}
                for future in as_completed(pending):
                    samples[pending[future]] = future.result()
            observation = aggregate(record,{iid:samples[iid] for iid in ids},
                                    samples.get(service_id))
            return self.store.finish_monitor(record,token,observation,transition_events(previous,observation))
        finally:
            self.store.release_monitor(record['id'],token)

    def refresh_async(self, record):
        thread = threading.Thread(target=self.refresh,args=(record,),name='cluster-refresh',daemon=True)
        thread.start()

    def start_run(self, record, request_key):
        self.store.interrupt_previous_runs(self.runtime)
        run,created = self.store.create_run(record,request_key,self.runtime)
        if created:
            thread = threading.Thread(target=self._run_inspections,args=(run,),name='cluster-inspection',daemon=True)
            thread.start()
        return run

    def _start_node_inspection(self, record, member, template_id):
        from modules.access import get_principal
        principal = get_principal(record['owner'])
        if principal is None or principal.tenant_id != record['tenant']:
            raise PermissionError('集群拥有者身份已失效')
        instance = visible_instance(principal,member['instance_id'])
        member['name'] = instance.get('name') or member['instance_id']
        payload = {'db_type':instance['db_type'],'datasource_id':member['instance_id'],
                   'template_id':template_id,'inspector_name':record['spec']['name']+' 集群巡检','desensitize':True}
        # Call the normal inspection entry point with the same verified owner and selected datasource.
        with self.app.test_request_context('/api/start_inspection',method='POST',json=payload):
            g.security_principal = principal
            session.update(user_id=principal.id,username=principal.username,role=next(iter(principal.roles),'user'),
                           user_roles=list(principal.roles),is_admin=principal.is_admin)
            response = self.app.make_response(self.app.view_functions['api_start_inspection']())
            result = response.get_json()
        if response.status_code >= 400 or not result or not result.get('ok'):
            raise ValueError((result or {}).get('msg') or (result or {}).get('error') or '节点巡检启动失败')
        return result['task_id']

    def task(self, task_id):
        supplied = self.app.config.get('CLUSTER_TASK_LOOKUP')
        if supplied:
            return supplied(task_id)
        from modules.web.app import tasks
        return tasks.get(task_id)

    def _run_inspections(self, run):
        payload = run['payload']
        record = payload['cluster']
        try:
            with self.app.app_context():
                template_id = ensure_template(record['spec']['kind'])
                payload['template_id'] = template_id
                self.store.update_run(run['id'],payload,'running')
                # At most two JVM-backed node inspections run simultaneously.
                for offset in range(0,len(payload['members']),2):
                    batch = payload['members'][offset:offset+2]
                    for member in batch:
                        try:
                            task_id = self._start_node_inspection(record,member,template_id)
                            member.update(task_id=task_id,status='running')
                        except Exception as error:
                            member.update(status='error',error=str(redact(str(error)))[:300])
                        self.store.update_run(run['id'],payload,'running')
                    deadline = time.monotonic()+900
                    while any(member['status']=='running' for member in batch):
                        for member in batch:
                            if member['status']!='running':
                                continue
                            task = self.task(member['task_id'])
                            if task is None:
                                member.update(status='interrupted',error='节点任务状态已丢失，请复核执行结果')
                            elif task.get('status') in {'done','error','failed'}:
                                member['status'] = 'done' if task.get('status')=='done' else 'error'
                                member['report_file'] = task.get('report_file','')
                                member['report_name'] = task.get('report_name','')
                                if member['status']=='error':
                                    member['error'] = str(redact(str(task.get('error') or '节点巡检失败，请复核节点连接与巡检日志')))[:300]
                                result = task.get('result') or {}
                                member['result'] = {key:result.get(key) for key in ('health_score','health_status','risk_count','risk_level','issues')}
                                logs = task.get('log') or []
                                member['collection_warnings'] = [str(line)[:500] for line in logs if '查询失败' in str(line) or '已跳过' in str(line) or '未采集' in str(line)][-100:]
                            elif time.monotonic()>=deadline:
                                # Keep its real task ID so a later refresh can reconcile completion.
                                member.update(status='unconfirmed',error='等待节点结果超过 15 分钟，任务仍需复核')
                        self.store.update_run(run['id'],payload,'running')
                        if any(member['status']=='running' for member in batch):
                            self.stop_event.wait(1)
                    if any(member['status']=='unconfirmed' for member in batch):
                        for member in payload['members'][offset+2:]:
                            member.update(status='not_started',error='先前节点任务尚待复核，暂停后续节点调度')
                        break
                terminal = self.run_status(payload['members'])
                payload['monitor'] = self.store.latest(record['id'])
                if payload['monitor'] and payload['monitor']['config_version'] != record['version']:
                    payload['monitor'] = None
                self.store.update_run(run['id'],payload,terminal)
        except Exception as error:
            payload['error'] = str(redact(str(error)))[:300]
            for member in payload['members']:
                if member['status'] in {'pending','running'}:
                    member.update(status='interrupted',error='集群调度中断，请复核节点任务')
            self.store.update_run(run['id'],payload,'interrupted')
            _log.exception('集群巡检调度失败')

    @staticmethod
    def run_status(members):
        if any(member['status']=='unconfirmed' for member in members):
            return 'unconfirmed'
        if all(member['status']=='done' for member in members):
            return 'done'
        if any(member['status']=='done' for member in members):
            return 'partial_failed'
        return 'failed'

    def reconcile_run(self, run):
        if run['status']!='unconfirmed' or run['runtime']!=self.runtime:
            return run
        payload = run['payload']
        for member in payload['members']:
            if member['status']!='unconfirmed':
                continue
            task = self.task(member['task_id'])
            if task and task.get('status') in {'done','error','failed'}:
                member.update(status='done' if task['status']=='done' else 'error',
                              report_file=task.get('report_file',''),report_name=task.get('report_name',''))
                member.pop('error',None)
        self.store.update_run(run['id'],payload,self.run_status(payload['members']))
        return self.store.get_run(run['id'])

    def public_run(self, run):
        value = copy.deepcopy(run)
        for member in value['payload']['members']:
            member.pop('report_file',None)
        value.pop('runtime',None)
        return value


def get_service(app):
    with _service_lock:
        service = app.extensions.get('cluster_service')
        if service is None:
            service = ClusterService(app,store=app.config.get('CLUSTER_STORE'))
            app.extensions['cluster_service'] = service
        return service


def start_cluster_monitor(app):
    service = get_service(app)
    if not app.config.get('CLUSTER_MONITOR_DISABLED'):
        service.start()
    return service
