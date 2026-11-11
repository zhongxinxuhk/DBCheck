# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Authenticated service management API and tenant-scoped delivery exports."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


import io
from pathlib import Path
from flask import Blueprint, g, jsonify, request, send_file, current_app, redirect
from modules.core import paths
from modules.core.data_security import redact
from .catalog import all_items, load_catalog
from .store import ServiceStore
from .service import validate_asset, validate_details, evidence_result, overview, run_inspection

bp = Blueprint('service_management',__name__)


def actor():
    principal = getattr(g,'security_principal',None)
    if principal is None or principal.is_anonymous:
        raise PermissionError('登录身份不可用')
    return {'id':int(principal.id),'tenant_id':int(principal.tenant_id),
            'admin':principal.is_admin,'approver':principal.is_admin or 'operator' in principal.roles}


def store():
    supplied = current_app.config.get('SERVICE_STORE')
    if supplied is not None:
        return supplied
    from modules.pro.instance_manager import _get_fernet
    _get_fernet()
    return ServiceStore(paths.DATA_DIR/'service_management'/'services.db',paths.DB_KEY_PATH.read_bytes())


def catalog():
    result = load_catalog(current_app.config.get('SERVICE_CATALOG_PATH'))
    if int(result.get('tenant_id',1)) != actor()['tenant_id']:
        raise PermissionError('当前租户未导入此需求目录')
    return result


def safe_record(record):
    return {key:redact(value) for key,value in record.items() if key != 'payload' or isinstance(value,dict)}


@bp.errorhandler(PermissionError)
def forbidden(error):
    return jsonify(ok=False,error=str(error)),403


@bp.errorhandler(ValueError)
def invalid(error):
    return jsonify(ok=False,error=str(error)),400


@bp.route('/services')
def page():
    actor()
    return redirect('/#services')


@bp.route('/api/services/catalog')
def get_catalog():
    actor()
    return jsonify(ok=True,catalog=catalog())


@bp.route('/api/services/overview')
def get_overview():
    result = overview(store(),actor(),catalog(),period=request.args.get('period','month'),anchor=request.args.get('date'))
    return jsonify(ok=True,overview=redact(result))


@bp.route('/api/services/assets',methods=['GET','POST'])
def assets():
    identity = actor()
    database = store()
    if request.method == 'GET':
        return jsonify(ok=True,assets=[safe_record(v) for v in database.list(identity,'asset')])
    data = request.get_json(silent=True) or {}
    payload = data.get('payload') or {}
    validate_asset(payload)
    from modules.access import get_principal
    for reviewer_id in payload.get('reviewers',[]):
        principal = get_principal(reviewer_id)
        if principal is None or principal.tenant_id!=identity['tenant_id']:
            raise ValueError('资产复核人须为当前租户有效用户')
    if payload.get('instance_id'):
        visible_instance(identity,payload['instance_id'])
    records = database.list(identity,'asset')
    if any(v['payload']['configuration_id']==payload['configuration_id'] and v['id']!=data.get('id') for v in records):
        raise ValueError('配置项编号已存在')
    result = database.save(identity,'asset',payload,record_id=data.get('id'),version=data.get('version'))
    return jsonify(ok=True,asset=safe_record(result))


def visible_instance(identity, instance_id):
    from modules.pro import get_instance_manager
    from modules.access import get_owner, visible_to, Principal
    principal = getattr(g,'security_principal',None)
    if not visible_to(principal,get_owner('instance',instance_id)):
        raise PermissionError('无权访问绑定数据源')
    instance = get_instance_manager().get_instance_decrypted(instance_id)
    if not instance:
        raise ValueError('绑定数据源不存在')
    return instance


@bp.route('/api/services/assets/<asset_id>/inspect',methods=['POST'])
def inspect(asset_id):
    identity = actor()
    database = store()
    asset = database.get(identity,asset_id)['payload']
    if not asset.get('instance_id'):
        raise ValueError('请先绑定已有加密数据源')
    instance = visible_instance(identity,asset['instance_id'])
    result = run_inspection(database,identity,asset_id,catalog(),instance)
    return jsonify(ok=True,result=redact(result,mask_identity=True))


@bp.route('/api/services/evidence',methods=['POST'])
def evidence():
    identity = actor()
    database = store()
    data = request.get_json(silent=True) or {}
    item = all_items(catalog()).get(data.get('item_id'))
    if not item or item['kind']=='inspection':
        raise ValueError('自动采集项由受控巡检生成，禁止手动勾选通过')
    asset_id = data.get('asset_id','')
    asset = database.get(identity,asset_id)
    workflow = None
    payload = data.get('details') or {}
    if item['kind']=='workflow':
        related = database.get(identity,data.get('workflow_id',''))
        if related['kind']!='workflow' or related['asset_id']!=asset_id or related['item_id']!=item['id']:
            raise ValueError('闭环记录与资产或需求不匹配')
        workflow = related['payload']
        if workflow.get('asset_version')!=asset['version']:
            raise ValueError('台账版本已变化，闭环记录须重新复核')
        payload = workflow.get('details',{})
    result = evidence_result(item,payload,workflow)
    saved = database.save(identity,'evidence',{**result,'details':payload,'provenance':'reviewed_service_record' if workflow else 'asset_attestation'},asset_id=asset_id,item_id=item['id'])
    return jsonify(ok=True,record=safe_record(saved))


@bp.route('/api/services/workflows',methods=['GET','POST'])
def workflows():
    identity = actor()
    database = store()
    if request.method=='GET':
        return jsonify(ok=True,records=[safe_record(v) for v in database.list(identity,'workflow')])
    data = request.get_json(silent=True) or {}
    item = all_items(catalog()).get(data.get('item_id'))
    if item is None or item['kind']!='workflow':
        raise ValueError('工作流需求编号无效')
    details = data.get('details') or {}
    validate_details(details)
    from .store import utcnow
    payload = {'title':str(data.get('title') or item['title'])[:200], 'details':details,
               'module_id':item['module_id'],'requested_at':details.get('requested_at'),
               'request_reference':details.get('request_reference'),
               'risk_assessment':details.get('risk_assessment') or data.get('risk_assessment'),
               'rollback_plan':details.get('rollback_plan') or data.get('rollback_plan')}
    reviewers = data.get('reviewers',[])
    from modules.access import get_principal
    for user_id in reviewers:
        reviewer = get_principal(user_id)
        if reviewer is None or reviewer.tenant_id != identity['tenant_id']:
            raise ValueError('复核人员须为当前租户的有效用户')
    payload['reviewers'] = reviewers
    linked_asset = database.get(identity,data.get('asset_id',''))
    payload['asset_version'] = linked_asset['version']
    for field in ('detected_at','priority','incident_reference'):
        if field in details:
            payload[field]=details[field]
    result = database.save(identity,'workflow',payload,asset_id=data.get('asset_id',''),item_id=item['id'],
                           record_id=data.get('id'),version=data.get('version'))
    return jsonify(ok=True,record=safe_record(result))


@bp.route('/api/services/workflows/<record_id>/transition',methods=['POST'])
def transition(record_id):
    data = request.get_json(silent=True) or {}
    validate_details((data.get('evidence') or {}).get('details_update',{}))
    if data.get('state') == 'closed':
        record = store().get(actor(),record_id)
        item = all_items(catalog()).get(record['item_id'])
        details = {**record['payload'].get('details',{}),**(data.get('evidence') or {}).get('details_update',{})}
        if item and evidence_result(item,details,{**record['payload'],'state':'closed'}).get('missing'):
            raise ValueError('需求必填证据不完整，禁止闭环')
    result = store().transition(actor(),record_id,data.get('state'),data.get('version'),data.get('evidence') or {})
    return jsonify(ok=True,record=safe_record(result))


@bp.route('/api/services/records/<record_id>')
def get_record(record_id):
    return jsonify(ok=True,record=safe_record(store().get(actor(),record_id)))


@bp.route('/api/services/runs/<run_id>')
def run_evidence(run_id):
    for record in store().list(actor(),'inspection'):
        if record['item_id']=='run-evidence' and record['payload'].get('run_id')==run_id:
            return jsonify(ok=True,record=safe_record(record))
    raise PermissionError('采集证据不存在或无权访问')


@bp.route('/api/services/workflows/<record_id>/events',methods=['POST'])
def service_event(record_id):
    data = request.get_json(silent=True) or {}
    result = store().record_event(actor(),record_id,data.get('version'),data.get('event'),data.get('reference'))
    return jsonify(ok=True,record=safe_record(result))


@bp.route('/api/services/assets/<asset_id>/calendar')
def calendar_preview(asset_id):
    from datetime import date
    from .service import first_business_day,business_deadline
    asset = store().get(actor(),asset_id)['payload']
    calendar = asset.get('customer_calendar')
    if not calendar:
        raise ValueError('请配置客户工作日与工作时段')
    anchor = date.fromisoformat(request.args.get('date',date.today().isoformat()))
    return jsonify(ok=True,first_business_day=first_business_day(anchor.year,anchor.month,calendar.get('holidays'),calendar.get('working_days')))


@bp.route('/api/services/reviews',methods=['POST'])
def review_inspection():
    identity = actor()
    if not identity['approver']:
        raise PermissionError('需要复核权限')
    data = request.get_json(silent=True) or {}
    database = store()
    record = database.get(identity,data.get('record_id',''))
    if record['kind']!='inspection' or record['item_id'] not in all_items(catalog()):
        raise ValueError('复核目标不是检查结果')
    payload = record['payload']
    asset = database.get(identity,record['asset_id'])
    if payload.get('asset_version')!=asset['version']:
        raise ValueError('台账版本已变化，请重新采集')
    if record['owner']==identity['id']:
        raise PermissionError('请由独立复核人员审阅')
    if payload.get('status')!='needs_review' or payload.get('missing'):
        raise ValueError('仅可复核证据完整的待复核结果，失败和缺证据项目须整改后重采')
    if data.get('verdict') not in {'pass','fail'} or not data.get('baseline_reference') or not data.get('review_reason'):
        raise ValueError('复核须填写明确结论、基线引用与理由')
    result = database.save(identity,'evidence',{'status':data['verdict'],'source_record':record['id'],
             'baseline_reference':data['baseline_reference'],'review_reason':data['review_reason'],
             'reviewer_id':identity['id'],'provenance':'independent_review','missing':[],
             'reason':'依据完整采集证据与批准基线完成独立复核'},asset_id=record['asset_id'],item_id=record['item_id'])
    return jsonify(ok=True,record=safe_record(result))


@bp.route('/api/services/report')
def report():
    identity = actor()
    requirements = catalog()
    title = request.args.get('deliverable','数据库运维服务履约报告')
    choices = {'数据库运维服务履约报告'} | {name for m in requirements['modules'] for name in m['deliverables']}
    if title not in choices:
        raise ValueError('交付物名称无效')
    result = overview(store(),identity,requirements,period=request.args.get('period','month'),anchor=request.args.get('date'))
    from .report import render_docx, render_html
    if request.args.get('format','docx')=='html':
        response = current_app.response_class(render_html(requirements,result,title),mimetype='text/html')
        response.headers['Content-Security-Policy']="default-src 'none'; style-src 'self'; frame-ancestors 'none'"
        return response
    content = render_docx(requirements,result,title)
    return send_file(io.BytesIO(content),download_name='DBPulse-service-report.docx',as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
