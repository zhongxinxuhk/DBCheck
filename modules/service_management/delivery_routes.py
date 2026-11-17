# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Report center routes with current asset ACL checks at every entry point."""

import io
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from flask import current_app, jsonify, render_template, request, send_file

from .delivery_catalog import choices, COMBINATIONS
from .delivery_data import request_options, build_snapshot
from .delivery_render import render_job, package, watermark, content_diff, verify_package
from .delivery_store import DeliveryStore, digest
from .routes import bp, actor, store, catalog, visible_instance

_lock = threading.Lock()


def archive():
    return DeliveryStore(store())


def summary(job):
    return {k: v for k, v in job.items() if k not in {'snapshot', 'public_key', 'generations'}} | {
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
        database = archive()
        try:
            identity = resolve_actor(app, identity)
            if not database.claim(identity, job_id):
                return
            job = database.get(identity, job_id)
            rendered = render_job(job)
            identity = resolve_actor(app, identity)
            database.finish(identity, job_id, rendered)
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
    return render_template('report_center.html')


@bp.route('/api/services/deliverables/catalog')
def delivery_catalog():
    actor()
    return jsonify(ok=True, deliverables=choices(), combinations=COMBINATIONS)


@bp.route('/api/services/deliverables/preview', methods=['POST'])
def delivery_preview():
    identity = actor()
    services = store()
    options = request_options(request.get_json(silent=True), services.list(identity, 'asset'))
    snapshot = build_snapshot(services, archive(), identity, catalog(), options)
    return jsonify(ok=True, request=options, coverage=snapshot['coverage'],
                   sheets={k: len(v) for k, v in snapshot['sheets'].items()},
                   source_manifest_sha256=snapshot['source_manifest_sha256'])


@bp.route('/api/services/deliverables/jobs', methods=['GET', 'POST'])
def delivery_jobs():
    identity = actor()
    database = archive()
    if request.method == 'GET':
        assets = {a['id']: a['payload'].get('asset_name') for a in store().list(identity, 'asset')}
        plans = [{'id': r['id'], 'title': r['payload'].get('title'),
                  'period': r['payload'].get('details', {}).get('frequency'),
                  'state': r['payload'].get('state'), 'asset_name': assets.get(r['asset_id'])}
                 for r in store().list(identity, 'workflow')
                 if r['payload'].get('provenance') == 'periodic_service_plan' or
                 r['payload'].get('details', {}).get('major_incident')]
        return jsonify(ok=True, jobs=[summary(v) for v in database.list(identity)], plans=plans)
    data = request.get_json(silent=True)
    if isinstance(data, dict) and (not isinstance(data.get('previous_id', ''), str) or len(str(data.get('reason', ''))) > 400):
        raise ValueError('修订参数无效或原因超过400字符')
    options = request_options(data, store().list(identity, 'asset'))
    snapshot = build_snapshot(store(), database, identity, catalog(), options)
    job = database.create(identity, options, snapshot, previous_id=data.get('previous_id'), reason=str(data.get('reason') or ''))
    submit(job, identity)
    return jsonify(ok=True, job=summary(database.get(identity, job['id']))), 202


@bp.route('/api/services/deliverables/jobs/<job_id>')
def delivery_job(job_id):
    return jsonify(ok=True, job=summary(archive().get(actor(), job_id)))


@bp.route('/api/services/deliverables/jobs/<job_id>/retry', methods=['POST'])
def delivery_retry(job_id):
    identity = actor()
    database = archive()
    old = database.get(identity, job_id)
    stale = datetime.fromisoformat(old['updated_at']) < datetime.now(timezone.utc) - timedelta(minutes=20)
    if old['state'] != 'failed' and not (old['state'] in {'queued', 'generating'} and stale):
        raise ValueError('仅失败或中断超过20分钟的生成任务可重试')
    if old['state'] != 'failed':
        database.fail(identity, job_id, '生成中断，由用户重试')
    job = database.create(identity, old['request'], old['snapshot'])
    submit(job, identity)
    return jsonify(ok=True, job=summary(database.get(identity, job['id']))), 202


@bp.route('/api/services/deliverables/jobs/<job_id>/publish', methods=['POST'])
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
def delivery_verification(job_id):
    job = archive().get(actor(), job_id)
    return jsonify(ok=True, manifest=job.get('manifest'), signature=job.get('signature'),
                   public_key=job.get('public_key'))


@bp.route('/api/services/deliverables/verify', methods=['POST'])
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
def delivery_keys():
    return jsonify(ok=True, keys=archive().keys(actor(), rotate=request.method == 'POST'))


@bp.route('/api/services/deliverables/observations')
def delivery_observations():
    identity = actor()
    ids = [r['id'] for r in store().list(identity, 'asset')]
    rows = [{k: v for k, v in value.items() if k != 'source_results'} for value in archive().observations(identity, ids)]
    rows.extend({'id': r['payload'].get('run_id'), 'asset_id': r['asset_id'], 'observed_at': r['updated_at']}
                for r in store().list(identity, 'inspection') if r['item_id'] == 'run-evidence')
    return jsonify(ok=True, observations=rows)


@bp.route('/api/services/deliverables/assets/<asset_id>/collect', methods=['POST'])
def collect_register(asset_id):
    identity = actor()
    asset = store().get(identity, asset_id)
    instance = visible_instance(identity, asset['payload'].get('instance_id'))
    from .inspection import collect
    sources = collect(instance, {**asset['payload'], '_delivery_full_inventory': True})
    observation = archive().observe(identity, asset_id, sources)
    return jsonify(ok=True, observation={k: v for k, v in observation.items() if k != 'source_results'},
                   statuses={k: {field: v.get(field) for field in ['status', 'reason', 'row_count', 'completeness']}
                             for k, v in sources.items()})
