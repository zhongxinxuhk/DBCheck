# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Authenticated cluster inventory, live health and unified inspection reports."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import io
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
import time
from pathlib import Path
from flask import Blueprint, current_app, g, jsonify, render_template, request, send_file
from modules.core import paths
from modules.driver_registry import db_family
from .model import KINDS, validate_spec
from .service import get_service, visible_instance
from .store import Conflict

bp = Blueprint('clusters',__name__)


def principal(write=False):
    actor = getattr(g,'security_principal',None)
    if actor is None or actor.is_anonymous:
        raise PermissionError('请先登录')
    if write and not (actor.is_admin or 'operator' in actor.roles):
        raise PermissionError('当前角色无集群变更权限')
    return actor


def request_data():
    value = request.get_json(silent=True)
    if value is None:
        return {}
    if not isinstance(value,dict):
        raise ValueError('请求内容须为 JSON 对象')
    return value


def service():
    return get_service(current_app._get_current_object())


def visible_record(cluster_id, include_deleted=False):
    actor = principal()
    record = service().store.get(cluster_id,actor,include_deleted=include_deleted)
    ids = [member['instance_id'] for member in record['spec']['members']]
    if record['spec']['service_instance_id']:
        ids.append(record['spec']['service_instance_id'])
    for iid in set(ids):
        visible_instance(actor,iid)
    return record


def public_record(record):
    observation = service().store.latest(record['id'])
    # Configuration edits and expired samples cannot keep a healthy badge.
    if observation and (observation['config_version'] != record['version'] or
                        time.time()-observation['ts'] > max(90,record['spec']['interval_seconds']*3)):
        observation = dict(observation,status='stale')
    return {**record,'monitor':observation}


@bp.errorhandler(PermissionError)
def forbidden(error):
    return jsonify(ok=False,error=str(error)),403


@bp.errorhandler(LookupError)
def missing(error):
    return jsonify(ok=False,error=str(error)),404


@bp.errorhandler(Conflict)
def conflict(error):
    return jsonify(ok=False,error=str(error)),409


@bp.errorhandler(sqlite3.IntegrityError)
def duplicate(error):
    return jsonify(ok=False,error='集群名称或请求标识已存在'),409


@bp.errorhandler(ValueError)
def invalid(error):
    return jsonify(ok=False,error=str(error)),400


@bp.route('/clusters')
def page():
    principal()
    return render_template('clusters.html')


@bp.route('/api/clusters/options')
def options():
    actor = principal()
    from modules.access import filter_visible
    from modules.pro import get_instance_manager
    instances = filter_visible(actor,get_instance_manager().get_all_instances(),'instance')
    safe = [{key:instance.get(key) for key in ('id','name','db_type','host','port','service_name','database')}
            for instance in instances if db_family(instance.get('db_type')) in {'oracle','dm'}]
    return jsonify(ok=True,kinds=KINDS,datasources=safe)


@bp.route('/api/clusters',methods=['GET','POST'])
def clusters():
    actor = principal(write=request.method=='POST')
    if request.method=='POST':
        data = request_data()
        spec = validate_spec(data,lambda iid:visible_instance(actor,iid))
        record = service().store.save(actor,spec)
        if spec['monitor_enabled']:
            service().refresh_async(record)
        service().start()
        return jsonify(ok=True,cluster=public_record(record)),201
    records = []
    for record in service().store.list(actor):
        try:
            visible_record(record['id'])
            records.append(public_record(record))
        except (PermissionError,ValueError):
            # An owner can remove a revoked binding through the configuration update API.
            records.append({'id':record['id'],'name':record['name'],'version':record['version'],
                            'restricted':True,'spec':{'kind':record['spec']['kind']},'monitor':None})
    return jsonify(ok=True,clusters=records)


@bp.route('/api/clusters/<cluster_id>',methods=['GET','PUT','DELETE'])
def cluster(cluster_id):
    actor = principal(write=request.method!='GET')
    if request.method=='GET':
        return jsonify(ok=True,cluster=public_record(visible_record(cluster_id)))
    data = request_data()
    current = service().store.get(cluster_id,actor)
    if request.method=='DELETE':
        service().store.delete(cluster_id,actor,data.get('version'))
        return jsonify(ok=True)
    spec = validate_spec(data,lambda iid:visible_instance(actor,iid))
    record = service().store.save(actor,spec,cluster_id=current['id'],version=data.get('version'))
    if spec['monitor_enabled']:
        service().refresh_async(record)
    return jsonify(ok=True,cluster=public_record(record))


@bp.route('/api/clusters/<cluster_id>/refresh',methods=['POST'])
def refresh(cluster_id):
    principal(write=True)
    record = visible_record(cluster_id)
    service().refresh_async(record)
    return jsonify(ok=True,status='queued'),202


@bp.route('/api/clusters/<cluster_id>/history')
def history(cluster_id):
    visible_record(cluster_id)
    return jsonify(ok=True,samples=service().store.history(cluster_id),events=service().store.history(cluster_id,events=True))


@bp.route('/api/clusters/<cluster_id>/inspections',methods=['GET','POST'])
def inspections(cluster_id):
    principal(write=request.method=='POST')
    record = visible_record(cluster_id)
    if request.method=='GET':
        return jsonify(ok=True,runs=[service().public_run(run) for run in service().store.runs(cluster_id)])
    data = request_data()
    key = data.get('request_key')
    if not isinstance(key,str) or not 8 <= len(key) <= 100:
        raise ValueError('请提供有效的巡检请求标识')
    run = service().start_run(record,key)
    return jsonify(ok=True,run=service().public_run(run)),202


def authorized_run(run_id):
    run = service().store.get_run(run_id)
    # A run retains its original members even if the current cluster configuration changes.
    record = run['payload']['cluster']
    actor = principal()
    if record['tenant'] != actor.tenant_id or record['owner'] != actor.id and not actor.is_admin:
        raise PermissionError('无权访问此集群巡检')
    for member in record['spec']['members']:
        visible_instance(actor,member['instance_id'])
    service_id = record['spec']['service_instance_id']
    if service_id:
        visible_instance(actor,service_id)
    return run


@bp.route('/api/cluster-inspections/<run_id>')
def run_status(run_id):
    return jsonify(ok=True,run=service().public_run(service().reconcile_run(authorized_run(run_id))))


@bp.route('/cluster-inspections/<run_id>/report')
def report(run_id):
    run = authorized_run(run_id)
    html = render_template('cluster_report.html',run=service().public_run(run),kinds=KINDS,
                           created_at=datetime.fromtimestamp(run['created'],ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'))
    if request.args.get('download')=='1':
        return send_file(io.BytesIO(html.encode('utf-8')),mimetype='text/html',as_attachment=True,
                         download_name='cluster-'+run['id']+'.html')
    return html


@bp.route('/api/cluster-inspections/<run_id>/members/<instance_id>/report')
def member_report(run_id, instance_id):
    run = authorized_run(run_id)
    member = next((row for row in run['payload']['members'] if row['instance_id']==instance_id),None)
    if not member or not member.get('report_file'):
        raise LookupError('节点报告尚未生成')
    report_path = Path(member['report_file']).resolve()
    if not report_path.is_relative_to(paths.REPORTS_DIR.resolve()) or not report_path.is_file():
        raise LookupError('节点报告文件不存在')
    return send_file(report_path,as_attachment=True,download_name=report_path.name)
