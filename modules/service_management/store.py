# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Encrypted tenant-scoped records, immutable evidence and authenticated audit chain."""



import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from cryptography.fernet import Fernet
from modules.core.data_security import redact


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def default_service_store():
    from modules.core import paths
    from modules.pro.instance_manager import _get_fernet

    _get_fernet()
    return ServiceStore(paths.DATA_DIR / 'service_management' / 'services.db',
                        paths.DB_KEY_PATH.read_bytes())


class ServiceStore:
    def __init__(self, path, key):
        self.path = Path(path)
        self.cipher = Fernet(key)
        self.audit_key = hashlib.sha256(key + b'DBPulse-service-audit-v1').digest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        if os.name != 'nt':
            os.chmod(self.path, 0o600)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS service_record_history '
                       '(id TEXT NOT NULL, version INTEGER NOT NULL, recorded_at TEXT NOT NULL, '
                       'record BLOB NOT NULL, PRIMARY KEY(id,version))')
            db.execute('CREATE TABLE IF NOT EXISTS service_finding_links '
                       '(fingerprint TEXT PRIMARY KEY, workflow_id TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS service_finding_observations '
                       '(run_id TEXT NOT NULL,item_id TEXT NOT NULL,workflow_id TEXT NOT NULL,observed_at TEXT NOT NULL, '
                       'PRIMARY KEY(run_id,item_id))')
            db.executescript('''
                             CREATE TABLE IF NOT EXISTS service_records
                             (
                                 id
                                 TEXT
                                 PRIMARY
                                 KEY,
                                 tenant
                                 INTEGER
                                 NOT
                                 NULL,
                                 owner
                                 INTEGER
                                 NOT
                                 NULL,
                                 kind
                                 TEXT
                                 NOT
                                 NULL,
                                 asset_id
                                 TEXT
                                 NOT
                                 NULL
                                 DEFAULT
                                 '',
                                 item_id
                                 TEXT
                                 NOT
                                 NULL
                                 DEFAULT
                                 '',
                                 version
                                 INTEGER
                                 NOT
                                 NULL,
                                 payload
                                 BLOB
                                 NOT
                                 NULL,
                                 created_at
                                 TEXT
                                 NOT
                                 NULL,
                                 updated_at
                                 TEXT
                                 NOT
                                 NULL
                             );
                             CREATE INDEX IF NOT EXISTS service_scope ON service_records(tenant,kind,asset_id,item_id);
                             CREATE TABLE IF NOT EXISTS service_audit
                             (
                                 seq
                                 INTEGER
                                 PRIMARY
                                 KEY
                                 AUTOINCREMENT,
                                 event
                                 TEXT
                                 NOT
                                 NULL,
                                 previous_hash
                                 TEXT
                                 NOT
                                 NULL,
                                 event_hash
                                 TEXT
                                 NOT
                                 NULL
                             );
                             CREATE TABLE IF NOT EXISTS service_leases
                             (
                                 asset_id
                                 TEXT
                                 PRIMARY
                                 KEY,
                                 next_at
                                 TEXT
                                 NOT
                                 NULL
                             );
                             CREATE TABLE IF NOT EXISTS service_asset_identifiers
                             (
                                 tenant
                                 INTEGER
                                 NOT
                                 NULL,
                                 identifier_hash
                                 TEXT
                                 NOT
                                 NULL,
                                 asset_id
                                 TEXT
                                 NOT
                                 NULL
                                 UNIQUE,
                                 PRIMARY
                                 KEY
                             (
                                 tenant,
                                 identifier_hash
                             ));
                             CREATE TABLE IF NOT EXISTS service_asset_history
                             (
                                 asset_id
                                 TEXT
                                 NOT
                                 NULL,
                                 version
                                 INTEGER
                                 NOT
                                 NULL,
                                 payload
                                 BLOB
                                 NOT
                                 NULL,
                                 recorded_at
                                 TEXT
                                 NOT
                                 NULL,
                                 PRIMARY
                                 KEY
                             (
                                 asset_id,
                                 version
                             ));
                             ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _verify(self, db):
        previous = ''
        for row in db.execute('SELECT * FROM service_audit ORDER BY seq'):
            digest = hmac.new(self.audit_key, (previous + row['event']).encode(), hashlib.sha256).hexdigest()
            if row['previous_hash'] != previous or not hmac.compare_digest(digest, row['event_hash']):
                raise ValueError('审计链校验失败，已阻断读取及变更')
            previous = digest
        return previous

    def _audit(self, db, actor, action, record_id, payload_digest):
        previous = self._verify(db)
        event = canonical({'actor': actor['id'], 'tenant': actor['tenant_id'], 'action': action,
                           'record_id': record_id, 'payload_digest': payload_digest, 'time': utcnow()})
        digest = hmac.new(self.audit_key, (previous + event).encode(), hashlib.sha256).hexdigest()
        db.execute('INSERT INTO service_audit(event,previous_hash,event_hash) VALUES(?,?,?)', (event, previous, digest))

    def _visible(self, actor, row, db=None):
        if row['tenant'] != actor['tenant_id']:
            return False
        if actor.get('admin') or row['owner'] == actor['id']:
            return True
        if row['kind'] in {'asset', 'workflow'}:
            return actor['id'] in self._decode(row)['payload'].get('reviewers', [])
        if db is not None and row['asset_id']:
            parent = db.execute("SELECT * FROM service_records WHERE id=? AND kind='asset'",
                                (row['asset_id'],)).fetchone()
            return parent is not None and self._visible(actor, parent, db)
        return False

    def _decode(self, row):
        envelope = json.loads(self.cipher.decrypt(row['payload']))
        scope = {key: row[key] for key in ('id', 'tenant', 'owner', 'kind', 'asset_id', 'item_id', 'version')}
        if envelope.get('scope') != scope:
            raise ValueError('记录归属或版本完整性校验失败')
        return {**dict(row), 'payload': envelope['body']}

    def _seal(self, payload, scope):
        return self.cipher.encrypt(canonical({'scope': scope, 'body': payload}).encode())

    def _remember(self, db, record_id):
        row = db.execute('SELECT * FROM service_records WHERE id=?', (record_id,)).fetchone()
        value = self._decode(row)
        db.execute('INSERT OR IGNORE INTO service_record_history VALUES(?,?,?,?)',
                   (record_id, row['version'], row['updated_at'], self.cipher.encrypt(canonical(value).encode())))

    def reporting_records(self, actor, cutoff):
        """Read authorized records and their cutoff versions in one database snapshot."""
        with self.connection() as db:
            db.execute('BEGIN')
            self._verify(db)
            result = []
            for row in db.execute('SELECT * FROM service_records WHERE tenant=? ORDER BY updated_at,id',
                                  (actor['tenant_id'],)).fetchall():
                if not self._visible(actor, row, db):
                    continue
                value = self._decode(row)
                if value['updated_at'] <= cutoff:
                    result.append(value)
                    continue
                old = db.execute('SELECT record FROM service_record_history WHERE id=? AND recorded_at<=? '
                                 'ORDER BY version DESC LIMIT 1', (row['id'], cutoff)).fetchone()
                if old:
                    historical = json.loads(self.cipher.decrypt(old['record']))
                    if historical['id'] != row['id'] or historical['tenant'] != actor['tenant_id']:
                        raise ValueError('历史记录归属不一致')
                    result.append(historical)
                elif row['kind'] == 'asset':
                    history = db.execute('SELECT * FROM service_asset_history WHERE asset_id=? AND recorded_at<=? '
                                         'ORDER BY version DESC LIMIT 1', (row['id'], cutoff)).fetchone()
                    if history:
                        envelope = json.loads(self.cipher.decrypt(history['payload']))
                        if envelope['scope']['id'] != row['id'] or envelope['scope']['tenant'] != actor['tenant_id']:
                            raise ValueError('历史资产归属不一致')
                        result.append({**value, 'version': history['version'], 'payload': envelope['body'],
                                       'updated_at': history['recorded_at']})
            return result

    def get(self, actor, record_id, db=None):
        if db is None:
            with self.connection() as conn:
                self._verify(conn)
                return self.get(actor, record_id, conn)
        row = db.execute('SELECT * FROM service_records WHERE id=?', (record_id,)).fetchone()
        if row is None or not self._visible(actor, row, db):
            raise PermissionError('记录不存在或无访问权限')
        return self._decode(row)

    def list(self, actor, kind=None, asset_id=None):
        with self.connection() as db:
            self._verify(db)
            rows = db.execute('SELECT * FROM service_records WHERE tenant=? ORDER BY updated_at DESC,id',
                              (actor['tenant_id'],)).fetchall()
            return [self._decode(row) for row in rows if self._visible(actor, row, db)
                    and (kind is None or row['kind'] == kind)
                    and (asset_id is None or row['asset_id'] == asset_id)]

    def save(self, actor, kind, payload, *, record_id=None, version=None, asset_id='', item_id=''):
        if kind not in {'asset', 'evidence', 'inspection', 'workflow'}:
            raise ValueError('记录类型无效')
        data = canonical(redact(payload))
        if len(data.encode()) > 1_000_000:
            raise ValueError('记录超出大小限制')
        now = utcnow()
        record_id = record_id or uuid.uuid4().hex
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._verify(db)
            if kind == 'asset':
                identifier = str(payload.get('configuration_id', '')).strip()
                if not identifier:
                    raise ValueError('资产必须提供配置项编号')
                digest = hmac.new(self.audit_key, (str(actor['tenant_id']) + '|' + identifier).encode(),
                                  hashlib.sha256).hexdigest()
                duplicate = db.execute(
                    'SELECT asset_id FROM service_asset_identifiers WHERE tenant=? AND identifier_hash=?',
                    (actor['tenant_id'], digest)).fetchone()
                if duplicate and duplicate['asset_id'] != record_id:
                    raise ValueError('当前租户的配置项编号已存在')
                db.execute('DELETE FROM service_asset_identifiers WHERE asset_id=?', (record_id,))
                db.execute('INSERT INTO service_asset_identifiers VALUES(?,?,?)',
                           (actor['tenant_id'], digest, record_id))
            if asset_id:
                asset = self.get(actor, asset_id, db)
                if asset['kind'] != 'asset':
                    raise ValueError('目标记录不是资产')
                body = json.loads(data)
                if kind == 'workflow' and body.get('asset_version', asset['version']) != asset['version']:
                    raise ValueError('资产版本已变化，请重新核对方案')
                body['asset_version'] = asset['version']
                data = canonical(body)
            record_owner = asset['owner'] if asset_id and kind in {'evidence', 'inspection'} else actor['id']
            old = db.execute('SELECT 1 FROM service_records WHERE id=?', (record_id,)).fetchone()
            if old:
                record = self.get(actor, record_id, db)
                self._remember(db, record_id)
                if record['kind'] in {'evidence', 'inspection'}:
                    raise ValueError('证据只允许追加，禁止覆盖')
                if record['kind'] != kind or version != record['version']:
                    raise ValueError('记录版本冲突，请刷新后重试')
                if kind == 'asset' and record['owner'] != actor['id'] and not actor.get('admin'):
                    raise PermissionError('复核授权不包含修改资产权限')
                if kind == 'workflow':
                    if record['payload'].get('state') != 'draft' or record['owner'] != actor['id']:
                        raise ValueError('只有申请人可编辑未提交的草稿')
                    body = json.loads(data)
                    body.update(state='draft', requester=actor['id'], timeline=[])
                    for field in ('approved_by', 'approved_at', 'executor'):
                        body.pop(field, None)
                    data = canonical(body)
                scope = {k: record[k] for k in ('id', 'tenant', 'owner', 'kind', 'asset_id', 'item_id', 'version')}
                if kind == 'asset':
                    old_payload = db.execute('SELECT payload FROM service_records WHERE id=?', (record_id,)).fetchone()[
                        'payload']
                    db.execute('INSERT OR IGNORE INTO service_asset_history VALUES(?,?,?,?)',
                               (record_id, record['version'], old_payload, record['updated_at']))
                scope['version'] += 1
                encrypted = self._seal(json.loads(data), scope)
                count = db.execute(
                    'UPDATE service_records SET payload=?,version=version+1,updated_at=? WHERE id=? AND version=?',
                    (encrypted, now, record_id, version)).rowcount
                if count != 1:
                    raise ValueError('并发修改冲突')
            else:
                if version is not None:
                    raise ValueError('新增记录不接受旧版本号')
                body = json.loads(data)
                if kind == 'workflow':
                    body.update(state='draft', requester=actor['id'], timeline=[])
                    for field in ('approved_by', 'approved_at', 'executor'):
                        body.pop(field, None)
                scope = dict(id=record_id, tenant=actor['tenant_id'], owner=record_owner, kind=kind,
                             asset_id=asset_id, item_id=item_id, version=1)
                encrypted = self._seal(body, scope)
                db.execute('INSERT INTO service_records VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (record_id, actor['tenant_id'], record_owner, kind, asset_id, item_id, 1, encrypted, now,
                            now))
            self._audit(db, actor, kind + '.save', record_id, hashlib.sha256(encrypted).hexdigest())
            self._remember(db, record_id)
            if kind == 'asset':
                db.execute('INSERT INTO service_asset_history VALUES(?,?,?,?)',
                           (record_id, scope['version'], encrypted, now))
        return self.get(actor, record_id)

    def asset_history(self, actor, asset_id):
        with self.connection() as db:
            self._verify(db)
            current = self.get(actor, asset_id, db)
            if current['kind'] != 'asset':
                raise ValueError('目标不是资产')
            result = []
            for row in db.execute('SELECT * FROM service_asset_history WHERE asset_id=? ORDER BY version', (asset_id,)):
                envelope = json.loads(self.cipher.decrypt(row['payload']))
                if envelope['scope']['id'] != asset_id or envelope['scope']['tenant'] != actor['tenant_id'] or \
                        envelope['scope']['version'] != row['version']:
                    raise ValueError('资产历史完整性校验失败')
                result.append(
                    {'version': row['version'], 'recorded_at': row['recorded_at'], 'details': envelope['body']})
            return result

    def record_finding(self, actor, asset_id, item_id, run_id, evidence_id, workflow_item):
        """One active remediation per check, preserving approved workflow payloads."""
        fingerprint = hashlib.sha256(canonical([actor['tenant_id'], asset_id, item_id]).encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._verify(db)
            asset = self.get(actor, asset_id, db)
            duplicate = db.execute('SELECT workflow_id FROM service_finding_observations WHERE run_id=? AND item_id=?',
                                   (run_id, item_id)).fetchone()
            if duplicate:
                return duplicate['workflow_id']
            link = db.execute('SELECT workflow_id FROM service_finding_links WHERE fingerprint=?', (fingerprint,)).fetchone()
            previous = self.get(actor, link['workflow_id'], db) if link else None
            now = utcnow()
            if previous and previous['payload'].get('state') != 'closed':
                workflow_id = previous['id']
            else:
                workflow_id = uuid.uuid4().hex
                body = {'title': '巡检异常整改 ' + item_id, 'state': 'draft', 'requester': actor['id'],
                        'asset_version': asset['version'], 'timeline': [], 'detected_at': now, 'priority': 'NORMAL',
                        'module_id': 'M2', 'provenance': 'inspection_finding', 'fingerprint': fingerprint,
                        'recurrence_of': previous['id'] if previous else None,
                        'details': {'finding_reference': evidence_id, 'alert_reference': run_id}}
                scope = dict(id=workflow_id, tenant=actor['tenant_id'], owner=actor['id'], kind='workflow',
                             asset_id=asset_id, item_id=workflow_item, version=1)
                encrypted = self._seal(body, scope)
                db.execute('INSERT INTO service_records VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (workflow_id, actor['tenant_id'], actor['id'], 'workflow', asset_id, workflow_item, 1, encrypted, now, now))
                self._remember(db, workflow_id)
                db.execute('INSERT OR REPLACE INTO service_finding_links VALUES(?,?)', (fingerprint, workflow_id))
                self._audit(db, actor, 'finding.create', workflow_id, hashlib.sha256(encrypted).hexdigest())
            db.execute('INSERT INTO service_finding_observations VALUES(?,?,?,?)', (run_id, item_id, workflow_id, now))
            self._audit(db, actor, 'finding.observe', workflow_id, hashlib.sha256(canonical([run_id, evidence_id]).encode()).hexdigest())
        return workflow_id

    def scheduled_assets(self):
        with self.connection() as db:
            self._verify(db)
            return [self._decode(row) for row in db.execute("SELECT * FROM service_records WHERE kind='asset'")]

    def claim_schedule(self, actor, asset_id, now, next_at):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._verify(db)
            self.get(actor, asset_id, db)
            row = db.execute('SELECT next_at FROM service_leases WHERE asset_id=?', (asset_id,)).fetchone()
            if row and row['next_at'] > now:
                return False
            db.execute(
                'INSERT INTO service_leases VALUES(?,?) ON CONFLICT(asset_id) DO UPDATE SET next_at=excluded.next_at',
                (asset_id, next_at))
            self._audit(db, actor, 'inspection.schedule', asset_id, next_at)
            return True

    def transition(self, actor, record_id, target, version, evidence):
        evidence = dict(evidence)
        updates = evidence.pop('details_update', {})
        measured = {'execution_reference', 'verification_reference', 'validation_reference', 'before_metrics',
                    'after_metrics', 'resolved_at', 'responded_at', 'plan_at', 'intervened_at', 'restore_started',
                    'restore_finished', 'verified_at', 'business_validation', 'database_validation', 'review_reference',
                    'onsite_reference', 'rollback_result', 'outcome', 'rpo_minutes', 'rto_minutes', 'escalated_at',
                    'verified_asset_values'}
        if not isinstance(updates, dict) or set(updates) - measured:
            raise ValueError('执行后仅允许补充测量及验证证据，禁止改写审批方案')
        forbidden = {'requester', 'approved_by', 'approved_at', 'executor', 'state', 'timeline', 'tenant', 'owner',
                     'details', 'asset_updates', 'asset_version', 'asset_version_before'}
        if forbidden.intersection(evidence):
            raise ValueError('禁止客户端设置审批身份或受控状态')
        # Transition and audit append share one transaction, preventing double approval.
        allowed = {'draft': {'submitted'}, 'submitted': {'approved', 'rejected'},
                   'approved': {'executing', 'rejected'}, 'executing': {'verified', 'failed'},
                   'verified': {'closed'}, 'failed': {'submitted'}, 'rejected': {'submitted'}}
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._verify(db)
            record = self.get(actor, record_id, db)
            if record['kind'] != 'workflow' or record['version'] != version:
                raise ValueError('工作流或版本无效')
            data = record['payload']
            self._remember(db, record_id)
            asset = self.get(actor, record['asset_id'], db)
            if data.get('asset_version') != asset['version']:
                raise ValueError('台账版本已变化，原审批方案须重新核对')
            data.setdefault('details', {}).update(redact(updates))
            state = data.get('state', 'draft')
            if target not in allowed.get(state, set()):
                raise ValueError('禁止跳过审批、执行或验证阶段')
            if target == 'approved':
                if actor['id'] == data['requester'] or not actor.get('approver'):
                    raise PermissionError('审批须由另一位具备审批权限的人员完成')
                if not data.get('risk_assessment') or not data.get('rollback_plan'):
                    raise ValueError('审批前必须填写风险评估及回滚方案')
                data['approved_by'] = actor['id']
                data['approved_at'] = utcnow()
            if target in {'executing', 'verified', 'closed'} and not data.get('approved_by'):
                raise ValueError('缺少审批证据')
            if target in {'verified', 'closed'} and not evidence.get('verification_reference'):
                raise ValueError('缺少实际执行后的验证证据')
            if target == 'closed' and actor['id'] == data.get('executor'):
                raise PermissionError('关闭须由另一位人员复核')
            if target == 'executing':
                data['executor'] = actor['id']
                if not evidence.get('execution_reference'):
                    raise ValueError('必须登记受控执行记录')
            asset_updates = data.get('details', {}).get('asset_updates')
            if target == 'closed' and asset_updates:
                from .service import validate_asset
                allowed_fields = {'approved_parameters', 'parameter_snapshot', 'parameter_version', 'tables',
                                  'version', 'patch_level', 'host', 'port', 'database', 'instance_name', 'charset',
                                  'file_size', 'archive_path', 'archive_mode', 'storage_mode', 'disaster_recovery',
                                  'cluster_id', 'cluster_type', 'node_count', 'node_names', 'architecture',
                                  'cluster_network', 'backup_policy_id', 'backup_policy_version', 'backup_window',
                                  'backup_method', 'backup_frequency', 'backup_retention', 'backup_owner'}
                if not isinstance(asset_updates, dict) or set(asset_updates) - allowed_fields:
                    raise ValueError('批准方案包含不允许自动同步的台账字段')
                if actor['id'] != asset['owner'] and not actor.get('admin'):
                    raise PermissionError('同步正式台账须由资产所有者或管理员复核')
                if data['details'].get('verified_asset_values') != asset_updates:
                    raise ValueError('实际验证值与批准的台账更新方案不一致')
                new_payload = {**asset['payload'], **asset_updates, 'change_reference': record_id}
                validate_asset(new_payload)
                self._remember(db, asset['id'])
                asset_scope = {k: asset[k] for k in ('id', 'tenant', 'owner', 'kind', 'asset_id', 'item_id', 'version')}
                asset_scope['version'] += 1
                sealed_asset = self._seal(new_payload, asset_scope)
                changed_at = utcnow()
                count = db.execute('UPDATE service_records SET payload=?,version=version+1,updated_at=? WHERE id=? AND version=?',
                                   (sealed_asset, changed_at, asset['id'], asset['version'])).rowcount
                if count != 1:
                    raise ValueError('台账并发修改冲突')
                db.execute('INSERT INTO service_asset_history VALUES(?,?,?,?)',
                           (asset['id'], asset_scope['version'], sealed_asset, changed_at))
                self._remember(db, asset['id'])
                self._audit(db, actor, 'asset.verified-change', asset['id'], hashlib.sha256(sealed_asset).hexdigest())
                data['asset_version_before'] = asset['version']
                data['asset_version'] = asset_scope['version']
            data.update(redact(evidence))
            data['state'] = target
            data.setdefault('timeline', []).append({'state': target, 'actor': actor['id'], 'time': utcnow()})
            scope = {k: record[k] for k in ('id', 'tenant', 'owner', 'kind', 'asset_id', 'item_id', 'version')}
            scope['version'] += 1
            encrypted = self._seal(data, scope)
            db.execute('UPDATE service_records SET payload=?,version=version+1,updated_at=? WHERE id=? AND version=?',
                       (encrypted, utcnow(), record_id, version))
            self._audit(db, actor, 'workflow.' + target, record_id, hashlib.sha256(encrypted).hexdigest())
            self._remember(db, record_id)
        return self.get(actor, record_id)

    def record_event(self, actor, record_id, version, event, reference):
        fields = {'responded': 'responded_at', 'plan_ready': 'plan_at', 'intervened': 'intervened_at',
                  'resolved': 'resolved_at', 'escalated': 'escalated_at', 'onsite': 'onsite_at'}
        if event not in fields or not isinstance(reference, str) or not reference.strip():
            raise ValueError('服务事件及证据引用无效')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._verify(db)
            record = self.get(actor, record_id, db)
            if record['kind'] != 'workflow' or record['version'] != version or record['payload'].get(
                    'state') == 'closed':
                raise ValueError('事件目标或版本无效，已闭环记录不可追加修改')
            data = record['payload']
            field = fields[event]
            self._remember(db, record_id)
            if data.get(field):
                raise ValueError('服务事件时间已登记，禁止覆盖')
            observed = utcnow()
            data[field] = observed
            data.setdefault('details', {})[field] = observed
            data.setdefault('timeline', []).append(
                {'event': event, 'actor': actor['id'], 'time': observed, 'reference': redact(reference)})
            scope = {k: record[k] for k in ('id', 'tenant', 'owner', 'kind', 'asset_id', 'item_id', 'version')}
            scope['version'] += 1
            encrypted = self._seal(data, scope)
            db.execute('UPDATE service_records SET payload=?,version=version+1,updated_at=? WHERE id=? AND version=?',
                       (encrypted, observed, record_id, version))
            self._audit(db, actor, 'service.event.' + event, record_id, hashlib.sha256(encrypted).hexdigest())
            self._remember(db, record_id)
        return self.get(actor, record_id)
