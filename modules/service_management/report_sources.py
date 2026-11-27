# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Read reports from the authoritative inventory without registering service assets."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
import hashlib
import uuid

from flask import current_app, has_app_context
from modules.core.data_security import redact
from .catalog import FIELDS, load_catalog
from .delivery_catalog import WORKFLOW_KEYS
from .inspection import SOURCES
from .service import validate_details
from .store import utcnow, canonical


def source_time(value):
    if not value:
        return utcnow()
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    return parsed.astimezone(timezone.utc).isoformat()


def report_catalog(identity):
    loaded = None
    try:
        catalog = load_catalog(current_app.config.get('SERVICE_CATALOG_PATH'))
        if int(catalog.get('tenant_id', 1)) == identity['tenant_id']:
            loaded = catalog
    except ValueError:
        pass
    keys = {key for values in WORKFLOW_KEYS.values() for key in values}
    if loaded is None:
        keys |= set(SOURCES)
    builtins = {'id': 'product', 'module_id': 'reports',
        'items': [{'id': 'builtin-' + key, 'key': key, 'title': key,
                   'kind': 'inspection' if key in SOURCES else 'workflow', 'fields': FIELDS.get(key, [])}
                  for key in sorted(keys)]}
    if loaded is not None:
        return {**loaded, 'requirements': [*loaded['requirements'], builtins]}
    return {'source_sha256': None, 'requirements': [builtins]}


class ReportSources:
    """Adapt existing inventory rows to the shared renderer and encrypted archive."""

    def __init__(self, services, inventory, principal, visible, findings=None, clusters=()):
        self.services, self.inventory, self.principal = services, inventory, principal
        self.visible, self.findings = set(visible), findings
        self.clusters = clusters
        self.cipher = services.cipher
        self.connection = services.connection
        self._verify, self._audit = services._verify, services._audit
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS report_event_versions '
                       '(id TEXT,version INTEGER,tenant INTEGER,source_id TEXT,recorded_at TEXT,payload BLOB,PRIMARY KEY(id,version))')

    def normalized(self, record):
        source, metadata = record['source'], record['metadata']
        payload = {**metadata, 'configuration_id': record['configuration_id'],
                   'asset_name': source.get('name') or record['id'], 'instance_id': record['id'],
                   'db_type': source.get('db_type'), 'host': source.get('host'), 'port': source.get('port'),
                   'database': source.get('database'), 'lifecycle': metadata.get('lifecycle') or 'active',
                   'tags': source.get('tags', []),
                   'provenance': 'operations_inventory'}
        related = [cluster for cluster in self.clusters if record['id'] in
                   [member['instance_id'] for member in cluster['spec']['members']]]
        if related:
            payload['cluster_id'] = [cluster['id'] for cluster in related]
            payload['cluster_type'] = [cluster['spec']['kind'] for cluster in related]
            payload['node_names'] = list(dict.fromkeys(member['instance_id'] for cluster in related for member in cluster['spec']['members']))
            payload['node_count'] = len(payload['node_names'])
            payload['tables'] = {**payload.get('tables', {}), 'cluster_nodes': [
                {'cluster_id': cluster['id'], 'cluster_name': cluster['name'], **member}
                for cluster in related for member in cluster['spec']['members']]}
        return {'id': record['id'], 'kind': 'asset', 'tenant': self.principal.tenant_id,
                'owner': self.principal.id, 'asset_id': '', 'version': record['version'],
                'payload': redact(payload, preserve_network=True),
                'created_at': source_time(source.get('created_at')),
                'updated_at': source_time(source.get('updated_at'))}

    def get(self, actor, identity, db=None):
        if actor['tenant_id'] != self.principal.tenant_id or actor['id'] != self.principal.id:
            raise PermissionError('数据源身份不匹配')
        if identity in self.visible:
            try:
                return self.normalized(self.inventory.get(identity))
            except LookupError as error:
                raise PermissionError('数据源已删除或无访问权限') from error
        legacy = self.services.get(actor, identity, db)
        linked = legacy['payload'].get('instance_id')
        if linked and linked not in self.visible:
            raise PermissionError('历史报告数据源权限已变化')
        return legacy

    def list(self, actor, kind=None, asset_id=None):
        if kind == 'asset':
            return [self.normalized(row) for row in self.inventory.list(sorted(self.visible))]
        records = self.services.list(actor, kind, asset_id)
        mapped = []
        for record in records:
            if record['kind'] == 'asset':
                continue
            try:
                parent = self.get(actor, record['asset_id'])
            except PermissionError:
                continue
            source_id = parent['payload'].get('instance_id')
            if source_id in self.visible:
                mapped.append({**record, 'asset_id': source_id, 'payload': {**record['payload'],
                    'legacy_asset_version': record['payload'].get('asset_version'), 'asset_version': None}})
        if kind in (None, 'workflow'):
            mapped.extend(self.events(actor))
        return mapped

    def asset_history(self, actor, identity):
        self.get(actor, identity)
        if identity not in self.visible:
            return self.services.asset_history(actor, identity)
        results = []
        for index, row in enumerate(reversed(self.inventory.history(self.principal, {identity})), 1):
            after = row['details'].get('after')
            if after:
                record = {'id': identity, 'version': index, 'configuration_id': after.get('metadata', {}).get('configuration_id') or 'CI-' + identity,
                          'source': after['source'], 'metadata': after.get('metadata', {})}
                results.append({'version': index, 'recorded_at': row['created'], 'details': self.normalized(record)['payload']})
        return results

    def observation_ids(self, actor, source_ids):
        result = [(identity, identity) for identity in source_ids]
        for record in self.services.list(actor, 'asset'):
            linked = record['payload'].get('instance_id')
            if linked in source_ids and record['id'] != linked:
                result.append((record['id'], linked))
        return result

    def reporting_records(self, actor, cutoff):
        result = []
        for asset in self.list(actor, 'asset'):
            history = [row for row in self.asset_history(actor, asset['id']) if row['recorded_at'] <= cutoff]
            if history:
                last = history[-1]
                result.append({**asset, 'version': last['version'], 'payload': last['details'], 'updated_at': last['recorded_at']})
            elif asset['updated_at'] <= cutoff:
                result.append(asset)
        legacy_assets = {r['id']: r['payload'].get('instance_id') for r in self.services.list(actor, 'asset')}
        for record in self.services.reporting_records(actor, cutoff):
            linked = legacy_assets.get(record['asset_id'])
            if record['kind'] != 'asset' and linked in self.visible:
                result.append({**record, 'asset_id': linked, 'payload': {**record['payload'],
                    'legacy_asset_version': record['payload'].get('asset_version'), 'asset_version': None}})
        result.extend(self.events(actor, cutoff))
        if self.findings:
            for finding in self.findings.list(self.principal, self.visible):
                observed = datetime.fromtimestamp(finding['updated'], timezone.utc).isoformat()
                if observed > cutoff:
                    continue
                details = {'finding_reference': finding['id'], 'owner': finding.get('assignee'),
                           'deadline': finding.get('deadline'), 'action': finding.get('action'),
                           'verification_reference': finding.get('verification_run'),
                           'root_cause': finding.get('root_cause'), 'risk_level': finding.get('level')}
                state = {'pending': 'draft', 'working': 'executing', 'review': 'verified', 'closed': 'closed'}[finding['state']]
                result.append({'id': finding['id'], 'kind': 'workflow', 'asset_id': finding['instance_id'],
                               'item_id': 'builtin-remediation', 'version': finding['version'],
                               'created_at': observed, 'updated_at': observed,
                               'payload': {'title': finding.get('description'), 'details': details, 'state': state,
                                           'provenance': 'operations_findings', 'timeline': []}})
        return sorted(result, key=lambda value: value['updated_at'])

    def events(self, actor, cutoff=None):
        with self.connection() as db:
            self._verify(db)
            rows = db.execute('SELECT * FROM report_event_versions WHERE tenant=? AND recorded_at<=? ORDER BY version DESC',
                              (actor['tenant_id'], cutoff or utcnow())).fetchall()
            latest = {}
            for row in rows:
                if row['source_id'] not in self.visible or row['id'] in latest:
                    continue
                value = json.loads(self.cipher.decrypt(row['payload']))
                if value['asset_id'] != row['source_id'] or value['tenant'] != actor['tenant_id'] or value['id'] != row['id'] or \
                        value['version'] != row['version'] or value['updated_at'] != row['recorded_at']:
                    raise ValueError('事件资料归属校验失败')
                latest[row['id']] = value
            return sorted(latest.values(), key=lambda value: value['updated_at'])

    def save_event(self, actor, data):
        source_id = data.get('source_id')
        source = self.get(actor, source_id)
        if source_id not in self.visible:
            raise ValueError('请选择已有数据源')
        key = data.get('key')
        if key not in {k for values in WORKFLOW_KEYS.values() for k in values}:
            raise ValueError('事件类型无效')
        details = data.get('details') or {}
        validate_details(details)
        state = data.get('state', 'draft')
        if state not in {'draft', 'executing', 'verified'}:
            raise ValueError('资料状态须为待补充、已执行或已验证，整改闭环请在资产问题记录中办理')
        if state == 'verified' and not details.get('verification_reference'):
            raise ValueError('请填写实际验证证据')
        if state == 'verified' and key == 'asset_review' and any(not details.get(field) for field in FIELDS['asset_review']):
            raise ValueError('台账核对需要盘点日期、范围、差异结论及审计依据')
        identity = data.get('id') or uuid.uuid4().hex
        now = utcnow()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._verify(db)
            prior = db.execute('SELECT * FROM report_event_versions WHERE id=? ORDER BY version DESC LIMIT 1', (identity,)).fetchone()
            version = 1
            created = now
            if prior:
                if prior['tenant'] != actor['tenant_id'] or prior['source_id'] != source_id:
                    raise PermissionError('无权修改事件资料')
                if data.get('version') != prior['version']:
                    raise ValueError('事件资料已更新，请刷新后重试')
                old = json.loads(self.cipher.decrypt(prior['payload']))
                if old['item_id'] != 'builtin-' + key:
                    raise ValueError('事件类型应保持一致，请另建资料')
                if old['owner'] != actor['id'] and not actor.get('admin'):
                    raise PermissionError('修改事件资料需要所有者或管理员权限')
                version = prior['version'] + 1
                created = old['created_at']
            body = {'id': identity, 'kind': 'workflow', 'tenant': actor['tenant_id'], 'owner': actor['id'],
                    'asset_id': source_id, 'item_id': 'builtin-' + key, 'version': version,
                    'created_at': created, 'updated_at': now, 'payload': {'title': str(data.get('title') or key)[:200],
                    'details': redact(details), 'state': state, 'provenance': 'report_event',
                    'asset_version': source['version'],
                    'detected_at': details.get('detected_at'), 'priority': details.get('priority'),
                    'timeline': [{'state': state, 'actor': actor['id'], 'time': now}]}}
            sealed = self.cipher.encrypt(canonical(body).encode())
            db.execute('INSERT INTO report_event_versions VALUES(?,?,?,?,?,?)',
                       (identity, version, actor['tenant_id'], source_id, now, sealed))
            self._audit(db, actor, 'report.event', identity, hashlib.sha256(sealed).hexdigest())
        return body


def sources_for(identity):
    from modules.access import get_principal
    from modules.access import filter_visible

    context = has_app_context()
    resolver = (current_app.config.get('DELIVERY_PRINCIPAL_RESOLVER') if context else None) or get_principal
    principal = resolver(identity['id'])
    if principal is None or principal.tenant_id != identity['tenant_id']:
        raise PermissionError('数据源身份已失效')
    if not context:
        from modules.core import paths
        from modules.clusters.store import ClusterStore
        from modules.operations.inventory import Inventory
        from modules.pro import get_instance_manager
        from .store import default_service_store

        manager = get_instance_manager()
        visible = {row['id'] for row in filter_visible(principal, manager.get_all_instances(), 'instance')}
        clusters = [cluster for cluster in ClusterStore(paths.DATA_DIR / 'clusters.db').list(principal)
                    if {member['instance_id'] for member in cluster['spec']['members']} <= visible]
        return ReportSources(default_service_store(), Inventory(manager), principal, visible,
                             clusters=clusters)
    from .routes import store

    supplied = current_app.config.get('REPORT_INVENTORY')
    if supplied is not None:
        allowed = current_app.config['REPORT_VISIBLE_IDS'](principal)
        return ReportSources(store(), supplied, principal, allowed)
    from modules.operations.routes import resources, visible_ids, finding_store
    inventory, _, _ = resources()
    visible = set(visible_ids(principal))
    clusters = [cluster for cluster in resources()[2].list(principal) if
                {member['instance_id'] for member in cluster['spec']['members']} <= visible]
    return ReportSources(store(), inventory, principal, visible, finding_store(), clusters)
