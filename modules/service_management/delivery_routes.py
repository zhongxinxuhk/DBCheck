# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Report center routes with current asset ACL checks at every entry point."""

import io
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

from flask import current_app, jsonify, request, send_file, redirect

from .delivery_catalog import choices, COMBINATIONS
from .delivery_data import request_options, build_snapshot
from .delivery_render import render_job, package, watermark, content_diff, verify_package
from .delivery_store import DeliveryStore, digest
from .routes import bp, actor, store, catalog, visible_instance
from .report_sources import sources_for, report_catalog
from .catalog import FIELDS
from .labels import FIELD_LABELS

_lock = threading.Lock()


def archive(identity=None):
    return DeliveryStore(sources_for(identity or actor()))


def summary(job):
    source_ids = job['request'].get('source_ids')
    if source_ids is None:
        sources = sources_for(actor())
        source_ids = [sources.get(actor(), identity)['payload'].get('instance_id') for identity in job['request']['asset_ids']]
    return {k: v for k, v in job.items() if k not in {'snapshot', 'public_key', 'generations'}} | {
        'source_ids': [identity for identity in source_ids if identity],
        'source_mapping_status': 'mapped' if source_ids and all(source_ids) else 'needs_review',
        'coverage': job['snapshot']['coverage'],
        'source_manifest_sha256': job['snapshot']['source_manifest_sha256']}


def resolve_actor(app, identity):
    from modules.access import get_principal
    resolver = app.config.get('DELIVERY_PRINCIPAL_RESOLVER') or get_principal
    principal = resolver(identity['id'])
    if principal is None or principal.is_anonymous or principal.tenant_id != identity['tenant_id']:
        raise PermissionError('导出身份已失效')
    return {'id': int(principal.id), 'tenant_id': int(principal.tenant_id), 'admin': principal.is_admin,
            'approver': principal.is_admin or 'operator' in principal.roles}


def generate(app, identity, job_id):
    with app.app_context():
        database = archive(identity)
        try:
            identity = resolve_actor(app, identity)
            if not database.claim(identity, job_id):
                return
            job = database.get(identity, job_id)
            if job['request'].get('collect_first'):
                sources = sources_for(identity)
                from modules.operations.routes import prepare_batch, resources
                spec, members = prepare_batch(sources.principal, {'instance_ids': job['request']['source_ids'],
                    'name': '报告前巡检', 'concurrency': 2})
                service = resources()[1]
                run, _ = service.store.create(sources.principal, 'report-' + job_id, spec, members)
                database.collection_run(identity, job_id, run['id'])
                service.run(run)
                options = {**job['request'], 'cutoff': datetime.now(timezone.utc).isoformat()}
                snapshot = build_snapshot(sources, database, identity, report_catalog(identity), options)
                completed = service.store.get(run['id'])
                snapshot['collection_results'] = [{key: member.get(key) for key in
                    ('instance_id', 'name', 'status', 'error')} for member in completed['payload']['members']]
                snapshot['coverage']['failed_sources'] = sum(member['status'] != 'done' for member in completed['payload']['members'])
                job = database.refresh_snapshot(identity, job_id, options, snapshot)
            automatic = 'source_ids' in job['request']
            rendered = render_job(job, published=automatic)
            identity = resolve_actor(app, identity)
            database.finish(identity, job_id, rendered, auto_publish=automatic)
        except Exception as error:
            from modules.core.data_security import redact
            current_app.logger.exception('Report generation failed for %s', job_id)
            try:
                database.fail(identity, job_id, redact(str(error), mask_identity=True))
            except PermissionError:
                # Permissions may have been revoked. Leave the task inaccessible.
                current_app.logger.warning('Report task owner permission changed for %s', job_id)


def submit(job, identity):
    app = current_app._get_current_object()
    if app.config.get('DELIVERY_SYNC'):
        generate(app, identity, job['id'])
    else:
        with _lock:
            executor = app.extensions.get('delivery_executor')
            if executor is None:
                executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='report-export')
                app.extensions['delivery_executor'] = executor
        executor.submit(generate, app, identity, job['id'])


@bp.route('/reports/center')
def report_center():
    actor()
    if request.args.get('run_id'):
        return redirect('/?' + urlencode({'run_id': request.args['run_id']}) + '#report-center')
    return redirect('/#report-center')


@bp.route('/api/services/deliverables/catalog')
@bp.route('/api/report-center/catalog')
def delivery_catalog():
    actor()
    return jsonify(ok=True, deliverables=choices(), combinations=COMBINATIONS)


@bp.route('/api/services/deliverables/preview', methods=['POST'])
@bp.route('/api/report-center/preview', methods=['POST'])
def delivery_preview():
    identity = actor()
    services = sources_for(identity)
    body = request.get_json(silent=True)
    if request.path.startswith('/api/report-center') and (not isinstance(body, dict) or 'source_ids' not in body):
        raise ValueError('请选择一个或多个数据源')
    options = request_options(body, services.list(identity, 'asset'))
    snapshot = build_snapshot(services, archive(), identity, report_catalog(identity), options)
    return jsonify(ok=True, request=options, coverage=snapshot['coverage'],
                   sheets={k: len(v) for k, v in snapshot['sheets'].items()},
                   source_manifest_sha256=snapshot['source_manifest_sha256'])


@bp.route('/api/services/deliverables/jobs', methods=['GET', 'POST'])
@bp.route('/api/report-center/jobs', methods=['GET', 'POST'])
def delivery_jobs():
    identity = actor()
    database = archive()
    if request.method == 'GET':
        return jsonify(ok=True, jobs=[summary(v) for v in database.list(identity)])
    data = request.get_json(silent=True)
    if request.path.startswith('/api/report-center') and (not isinstance(data, dict) or 'source_ids' not in data):
        raise ValueError('请选择一个或多个数据源')
    if isinstance(data, dict) and (not isinstance(data.get('previous_id', ''), str) or len(str(data.get('reason', ''))) > 400):
        raise ValueError('修订参数无效或原因超过400字符')
    if isinstance(data,dict) and data.get('previous_id') and database.get(identity,data['previous_id'])['request'].get('history_export'):
        raise ValueError('该文件对应固定的原巡检报告，请在数据库巡检历史中重新导出')
    services = sources_for(identity)
    options = request_options(data, services.list(identity, 'asset'))
    snapshot = build_snapshot(services, database, identity, report_catalog(identity), options)
    job = database.create(identity, options, snapshot, previous_id=data.get('previous_id'), reason=str(data.get('reason') or ''))
    submit(job, identity)
    return jsonify(ok=True, job=summary(database.get(identity, job['id']))), 202


@bp.route('/api/services/deliverables/jobs/<job_id>')
@bp.route('/api/report-center/jobs/<job_id>')
def delivery_job(job_id):
    return jsonify(ok=True, job=summary(archive().get(actor(), job_id)))


@bp.route('/api/services/deliverables/jobs/<job_id>/retry', methods=['POST'])
@bp.route('/api/report-center/jobs/<job_id>/retry', methods=['POST'])
def delivery_retry(job_id):
    identity = actor()
    database = archive()
    old = database.get(identity, job_id)
    stale = datetime.fromisoformat(old['updated_at']) < datetime.now(timezone.utc) - timedelta(minutes=20)
    if old.get('inspection_run_id') and old['state'] == 'generating':
        from modules.operations.routes import resources
        from modules.operations.batch import TERMINAL
        if resources()[1].store.get(old['inspection_run_id'])['status'] not in TERMINAL:
            raise ValueError('关联巡检仍在执行，请等待完成')
    if old['state'] != 'failed' and not (old['state'] in {'queued', 'generating'} and stale):
        raise ValueError('仅失败或中断超过20分钟的生成任务可重试')
    if old['state'] != 'failed':
        database.fail(identity, job_id, '生成中断，由用户重试')
    job = database.create(identity, old['request'], old['snapshot'])
    submit(job, identity)
    return jsonify(ok=True, job=summary(database.get(identity, job['id']))), 202


@bp.route('/api/services/deliverables/jobs/<job_id>/publish', methods=['POST'])
@bp.route('/api/report-center/jobs/<job_id>/publish', methods=['POST'])
def delivery_publish(job_id):
    identity = actor()
    database = archive()
    job = database.get(identity, job_id)
    body = request.get_json(silent=True) or {}
    if body.get('version') != job['version']:
        raise ValueError('报告版本已变化，请刷新后复核')
    if job['state'] != 'draft' or not identity['approver']:
        raise PermissionError('发布需要复核权限及已生成草稿')
    rendered = render_job(job, published=True)
    result = database.finish(identity, job_id, rendered, publish=True, expected_version=job['version'])
    return jsonify(ok=True, job=summary(result))


@bp.route('/api/services/deliverables/<job_id>/download')
@bp.route('/api/report-center/<job_id>/download')
def delivery_download(job_id):
    identity = actor()
    database = archive()
    job = database.get(identity, job_id)
    if job['state'] not in {'draft', 'published'}:
        raise ValueError('报告尚未生成')
    artifact_id = request.args.get('artifact_id')
    if artifact_id:
        info, content = database.artifact(identity, job_id, artifact_id)
        filename = info['name']
        mime = 'application/vnd.openxmlformats-officedocument.' + (
            'spreadsheetml.sheet' if info['format'] == 'xlsx' else 'wordprocessingml.document')
    else:
        content = package(job, database, identity)
        filename = f'DBPulse_{job["report_id"]}_v{job["revision"]}.zip'
        mime = 'application/zip'
        with database.services.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            database._job(db, identity, job_id)
            database.services._audit(db, identity, 'delivery.download-package', job_id, digest(content))
    response = send_file(io.BytesIO(content), download_name=filename, mimetype=mime, as_attachment=True)
    response.headers['X-Content-SHA256'] = digest(content)
    response.headers['Cache-Control'] = 'no-store, private'
    return response


@bp.route('/api/services/deliverables/<job_id>/verification')
@bp.route('/api/report-center/<job_id>/verification')
def delivery_verification(job_id):
    job = archive().get(actor(), job_id)
    return jsonify(ok=True, manifest=job.get('manifest'), signature=job.get('signature'),
                   public_key=job.get('public_key'))


@bp.route('/api/services/deliverables/verify', methods=['POST'])
@bp.route('/api/report-center/verify', methods=['POST'])
def delivery_verify():
    identity = actor()
    database = archive()
    job_id = request.form.get('job_id', '')
    job = database.get(identity, job_id)
    uploaded = request.files.get('file')
    if uploaded is None:
        raise ValueError('请选择待校验报告文件')
    content = uploaded.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise ValueError('在线核验文件上限为8MB，大文件请使用离线校验')
    result = database.verify(identity, job_id, content)
    newer = [value for value in database.list(identity) if value['report_id'] == job['report_id']
             and value['revision'] > job['revision'] and value['state'] == 'published']
    if newer:
        latest = max(newer, key=lambda value: value['revision'])
        result['superseded_by'] = {'job_id': latest['id'], 'revision': latest['revision']}
    result['watermark'] = watermark(content)
    if zipfile.is_zipfile(io.BytesIO(content)):
        from .delivery_render import bounded_zip
        with bounded_zip(content) as source:
            is_package = 'manifest.json' in source.namelist()
        if is_package:
            with bounded_zip(content) as source:
                import json
                from .store import canonical
                manifest = json.loads(source.read('manifest.json'))
            trusted = next((generation for generation in job.get('generations', [])
                            if canonical(generation['manifest']) == canonical(manifest)), None)
            if trusted is None:
                raise ValueError('交付包清单与此报告归档版本不匹配')
            package_result = verify_package(content, trusted['manifest']['key_id'])
            if package_result['report_id'] != job['report_id'] or package_result['revision'] != job['revision']:
                raise ValueError('交付包不属于所选报告版本')
            result.update(package_result, matches_original=digest(package(job, database, identity)) == digest(content),
                          message='交付包签名及成员摘要验证通过')
    artifact_id = request.form.get('artifact_id') or result['watermark'].get('artifact_id')
    known_artifacts = {info['artifact_id'] for generation in job.get('generations', [])
                       for info in generation['manifest']['artifacts']}
    if artifact_id and artifact_id not in known_artifacts and not request.form.get('artifact_id'):
        result['watermark_matches'] = False
        result['diff_reason'] = '水印指向其他制品，请手动选择对照原件'
        artifact_id = None
    if artifact_id:
        info, original = database.artifact(identity, job_id, artifact_id, audit=False)
        result['watermark_matches'] = all(result['watermark'].get(k) == str(info[k]) for k in
                                           ['artifact_id', 'report_id', 'revision', 'content_sha256'])
        if not result['matches_original']:
            try:
                result.update(content_diff(original, content, info['format']))
            except Exception:
                result['diff_reason'] = '文件结构无法可靠比对，请下载归档原件核对'
    return jsonify(ok=True, result=result)


@bp.route('/api/services/deliverables/keys', methods=['GET', 'POST'])
@bp.route('/api/report-center/keys', methods=['GET', 'POST'])
def delivery_keys():
    return jsonify(ok=True, keys=archive().keys(actor(), rotate=request.method == 'POST'))


@bp.route('/api/services/deliverables/observations')
@bp.route('/api/report-center/observations')
def delivery_observations():
    identity = actor()
    sources = sources_for(identity)
    ids = [r['id'] for r in sources.list(identity, 'asset')]
    rows = [{k: v for k, v in value.items() if k != 'source_results'} for value in archive().observations(identity, ids)]
    rows.extend({'id': r['payload'].get('run_id'), 'asset_id': r['asset_id'], 'observed_at': r['updated_at']}
                for r in sources.list(identity, 'inspection') if r['item_id'] == 'run-evidence')
    return jsonify(ok=True, observations=rows)


@bp.route('/api/services/deliverables/assets/<asset_id>/collect', methods=['POST'])
@bp.route('/api/report-center/sources/<asset_id>/collect', methods=['POST'])
def collect_register(asset_id):
    identity = actor()
    return jsonify(ok=True, **collect_source(identity, asset_id))


def collect_source(identity, asset_id):
    asset = sources_for(identity).get(identity, asset_id)
    from modules.pro import get_instance_manager
    instance = get_instance_manager().get_instance_decrypted(asset['payload'].get('instance_id'))
    if not instance:
        raise ValueError('数据源已删除')
    from .inspection import collect
    sources = collect(instance, {**asset['payload'], '_delivery_full_inventory': True})
    observation = archive(identity).observe(identity, asset_id, sources)
    return {'observation': {k: v for k, v in observation.items() if k != 'source_results'},
            'statuses': {k: {field: v.get(field) for field in ['status', 'reason', 'row_count', 'completeness']}
                         for k, v in sources.items()}}


@bp.route('/api/report-center/sources')
def report_source_choices():
    identity = actor()
    records = sources_for(identity).list(identity, 'asset')
    records.sort(key=lambda record: str(record['payload'].get('asset_name') or '').casefold())
    clusters = []
    if current_app.config.get('REPORT_INVENTORY') is None:
        from modules.operations.routes import resources
        sources = sources_for(identity)
        for cluster in resources()[2].list(sources.principal):
            members = [m['instance_id'] for m in cluster['spec']['members'] if m.get('enabled', True)]
            if cluster['spec'].get('service_instance_id'):
                members.append(cluster['spec']['service_instance_id'])
            if set(members) <= sources.visible:
                clusters.append({'id': cluster['id'], 'name': cluster['name'], 'source_ids': list(dict.fromkeys(members))})
    return jsonify(ok=True, clusters=clusters, sources=[{'id': r['id'], **{key: r['payload'].get(key) for key in
        ['asset_name', 'db_type', 'database', 'project_id', 'system', 'environment', 'cluster_id', 'tags']}}
        for r in records])


@bp.route('/api/report-center/events', methods=['GET', 'POST'])
def report_events():
    identity = actor()
    sources = sources_for(identity)
    if request.method == 'POST':
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError('事件资料须为对象')
        return jsonify(ok=True, record=sources.save_event(identity, body))
    return jsonify(ok=True, records=sources.list(identity, 'workflow'), fields=FIELDS, labels=FIELD_LABELS)


@bp.route('/api/report-center/history-xlsx', methods=['POST'])
def history_xlsx():
    identity=actor()
    if not (identity['admin'] and identity['tenant_id']==1):
        raise PermissionError('历史报告仅安装租户管理员可访问')
    body=request.get_json(silent=True)
    if not isinstance(body,dict):
        raise ValueError('请选择原巡检报告')
    from modules.core import paths
    from .history_xlsx import report_file, historical_export
    path=report_file(current_app.config.get('HISTORY_REPORTS_DIR') or paths.REPORTS_DIR,body.get('filename'))
    database=archive(identity)
    options,snapshot,key=historical_export(identity,database,path,
        legacy_context_db=current_app.config.get('HISTORY_CONTEXT_DB') or paths.DATA_DIR/'history.db',
        pro_history_db=current_app.config.get('HISTORY_PRO_DB') or paths.PRO_DATA_DIR/'pro_history.db')
    job=database.create(identity,options,snapshot,cache_key=key)
    if job['state']=='failed':
        job=database.retry_history_export(identity,job['id'])
    if job['state']=='queued':
        submit(job,identity)
    return jsonify(ok=True,job=summary(database.get(identity,job['id']))),202
