# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Encrypted report archives sharing service ACLs, transactions and audit chain."""

import base64
import hashlib
import json
import uuid
import zlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import InvalidToken

from .store import canonical, utcnow


def digest(value):
    return hashlib.sha256(value).hexdigest()


class DeliveryStore:
    def __init__(self, services):
        self.services = services
        with services.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS delivery_jobs (
                    id TEXT PRIMARY KEY, tenant INTEGER NOT NULL, owner INTEGER NOT NULL,
                    state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS delivery_scope ON delivery_jobs(tenant,created_at);
                CREATE TABLE IF NOT EXISTS delivery_artifacts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, name TEXT NOT NULL,
                    sha256 TEXT NOT NULL, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS delivery_keys (
                    id TEXT PRIMARY KEY, tenant INTEGER NOT NULL, created_at TEXT NOT NULL,
                    active INTEGER NOT NULL, payload BLOB NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS delivery_active_key ON delivery_keys(tenant) WHERE active=1;
                CREATE TABLE IF NOT EXISTS delivery_observations (
                    id TEXT PRIMARY KEY, tenant INTEGER NOT NULL, asset_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, payload BLOB NOT NULL);
            ''')

    def seal(self, data):
        return self.services.cipher.encrypt(zlib.compress(canonical(data).encode()))

    def decode(self, data):
        try:
            return json.loads(zlib.decompress(self.services.cipher.decrypt(data)))
        except (InvalidToken, zlib.error, ValueError) as error:
            raise ValueError('报告归档完整性校验失败，请恢复归档备份') from error

    def _job(self, db, actor, job_id):
        self.services._verify(db)
        row = db.execute('SELECT * FROM delivery_jobs WHERE id=? AND tenant=?',
                         (job_id, actor['tenant_id'])).fetchone()
        if row is None:
            raise PermissionError('报告不存在或无权访问')
        body = self.decode(row['payload'])
        if body['id'] != row['id'] or body['tenant'] != row['tenant'] or body['owner'] != row['owner']:
            raise ValueError('报告归属完整性校验失败')
        if row['owner'] != actor['id'] and not actor.get('admin') and not actor.get('approver'):
            raise PermissionError('无权访问此报告任务')
        for asset_id in body['request']['asset_ids']:
            self.services.get(actor, asset_id, db)
        body.update(state=row['state'], version=row['version'], updated_at=row['updated_at'])
        return body

    def get(self, actor, job_id):
        with self.services.connection() as db:
            return self._job(db, actor, job_id)

    def list(self, actor):
        results = []
        with self.services.connection() as db:
            self.services._verify(db)
            rows = db.execute('SELECT id FROM delivery_jobs WHERE tenant=? ORDER BY created_at DESC',
                              (actor['tenant_id'],)).fetchall()
            for row in rows:
                try:
                    results.append(self._job(db, actor, row['id']))
                except PermissionError:
                    continue
        return results

    def create(self, actor, request, snapshot, *, previous_id=None, reason=''):
        now = utcnow()
        identity = uuid.uuid4().hex
        with self.services.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.services._verify(db)
            revision = 1
            report_id = identity
            if previous_id:
                previous = self._job(db, actor, previous_id)
                if previous['state'] not in {'published', 'draft'} or not reason.strip():
                    raise ValueError('修订须关联已生成报告并填写原因')
                if previous['request']['asset_ids'] != request['asset_ids']:
                    raise ValueError('修订须保持原报告资产范围')
                if any(previous['request'][key] != request[key] for key in ['deliverables', 'period', 'date']):
                    raise ValueError('修订须保持原报告类型与统计周期')
                if previous['request'].get('workflow_ids', []) != request.get('workflow_ids', []):
                    raise ValueError('修订须保持原专项事件范围')
                report_id = previous['report_id']
                revision = 1 + max((self.decode(r['payload'])['revision'] for r in db.execute(
                    'SELECT payload FROM delivery_jobs WHERE tenant=?', (actor['tenant_id'],))
                    if self.decode(r['payload'])['report_id'] == report_id), default=0)
            body = {'id': identity, 'tenant': actor['tenant_id'], 'owner': actor['id'],
                    'report_id': report_id, 'revision': revision, 'previous_id': previous_id,
                    'reason': reason, 'created_at': now, 'request': request, 'snapshot': snapshot,
                    'artifacts': [], 'error': '', 'publisher': None}
            db.execute('INSERT INTO delivery_jobs(id,tenant,owner,state,created_at,updated_at,payload) '
                       'VALUES(?,?,?,?,?,?,?)',
                       (identity, actor['tenant_id'], actor['id'], 'queued', now, now, self.seal(body)))
            self.services._audit(db, actor, 'delivery.create', identity, digest(canonical(request).encode()))
        return self.get(actor, identity)

    def claim(self, actor, job_id):
        with self.services.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, actor, job_id)
            if job['state'] != 'queued':
                return False
            db.execute("UPDATE delivery_jobs SET state='generating',updated_at=? WHERE id=?", (utcnow(), job_id))
            self.services._audit(db, actor, 'delivery.generating', job_id, '')
        return True

    def key(self, db, tenant):
        row = db.execute('SELECT * FROM delivery_keys WHERE tenant=? AND active=1', (tenant,)).fetchone()
        if row is None:
            private = ec.generate_private_key(ec.SECP256R1())
            encoded = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()).decode()
            public = private.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
            key_id = digest(public)
            db.execute('INSERT INTO delivery_keys VALUES(?,?,?,?,?)',
                       (key_id, tenant, utcnow(), 1, self.seal({'tenant': tenant, 'private': encoded})))
            return key_id, private, public
        secret = self.decode(row['payload'])
        if secret['tenant'] != tenant:
            raise ValueError('签名密钥归属校验失败')
        private = serialization.load_pem_private_key(secret['private'].encode(), password=None)
        public = private.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
        if digest(public) != row['id']:
            raise ValueError('签名密钥指纹不匹配')
        return row['id'], private, public

    def finish(self, actor, job_id, rendered, *, publish=False, expected_version=None):
        """Commit final bytes, signed manifest and lifecycle in one SQLite transaction."""
        with self.services.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, actor, job_id)
            expected_state = 'draft' if publish else 'generating'
            if job['state'] != expected_state or (expected_version is not None and job['version'] != expected_version):
                raise ValueError('报告状态或版本已变化')
            if publish and not actor.get('approver'):
                raise PermissionError('发布报告需要复核权限')
            key_id, private, public = self.key(db, actor['tenant_id'])
            artifacts = []
            for name, content, metadata in rendered:
                artifact_id = metadata['artifact_id']
                info = {**metadata, 'name': name, 'sha256': digest(content), 'size': len(content)}
                archive = {'id': artifact_id, 'job_id': job_id, 'info': info,
                           'content': base64.b64encode(content).decode()}
                db.execute('INSERT INTO delivery_artifacts VALUES(?,?,?,?,?)',
                           (artifact_id, job_id, name, info['sha256'], self.seal(archive)))
                artifacts.append(info)
            state = 'published' if publish else 'draft'
            manifest = {'schema': 'dbpulse-report-manifest-v1', 'job_id': job_id,
                        'report_id': job['report_id'], 'revision': job['revision'], 'state': state,
                        'generated_at': utcnow(), 'cutoff': job['request']['cutoff'],
                        'period_start': job['snapshot']['start'], 'period_end': job['snapshot']['end'],
                        'template_version': 'delivery-v1', 'canonicalization': 'json-sortkeys-utf8-v1',
                        'key_id': key_id, 'algorithm': 'ECDSA-P256-SHA256', 'artifacts': artifacts}
            signed = canonical(manifest).encode()
            signature = base64.b64encode(private.sign(signed, ec.ECDSA(hashes.SHA256()))).decode()
            generation = {'manifest': manifest, 'signature': signature, 'public_key': public.decode()}
            job.setdefault('generations', []).append(generation)
            job.update(artifacts=artifacts, **generation)
            if publish:
                job.update(publisher=actor['id'], published_at=utcnow())
            db.execute('UPDATE delivery_jobs SET state=?,version=version+1,updated_at=?,payload=? WHERE id=?',
                       (state, utcnow(), self.seal(job), job_id))
            self.services._audit(db, actor, 'delivery.' + state, job_id, digest(signed))
        return self.get(actor, job_id)

    def fail(self, actor, job_id, reason):
        with self.services.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, actor, job_id)
            if job['state'] not in {'queued', 'generating'}:
                return
            job['error'] = str(reason)[:400]
            db.execute("UPDATE delivery_jobs SET state='failed',version=version+1,updated_at=?,payload=? WHERE id=?",
                       (utcnow(), self.seal(job), job_id))
            self.services._audit(db, actor, 'delivery.failed', job_id, digest(job['error'].encode()))

    def artifact(self, actor, job_id, artifact_id, *, audit=True):
        with self.services.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._job(db, actor, job_id)
            row = db.execute('SELECT * FROM delivery_artifacts WHERE id=? AND job_id=?',
                             (artifact_id, job_id)).fetchone()
            if row is None:
                raise ValueError('报告文件不存在')
            value = self.decode(row['payload'])
            if value['id'] != artifact_id or value['job_id'] != job_id:
                raise ValueError('报告文件归属校验失败')
            content = base64.b64decode(value['content'])
            if digest(content) != row['sha256'] or value['info']['sha256'] != row['sha256']:
                raise ValueError('报告原件完整性异常，请恢复归档')
            if audit:
                self.services._audit(db, actor, 'delivery.download', job_id, row['sha256'])
            return value['info'], content

    def verify(self, actor, job_id, content):
        job = self.get(actor, job_id)
        actual = digest(content)
        matches = []
        for generation in job.get('generations', []):
            manifest = generation['manifest']
            for info in manifest['artifacts']:
                if info['sha256'] == actual:
                    matches.append((generation, info))
        result = {'sha256': actual, 'matches_original': bool(matches), 'signature_valid': False,
                  'report_id': job['report_id'], 'revision': job['revision'],
                  'state': job['state'], 'key_id': job.get('manifest', {}).get('key_id'),
                  'message': '文件与归档原件不一致'}
        if matches:
            generation, info = matches[0]
            public = serialization.load_pem_public_key(generation['public_key'].encode())
            if digest(generation['public_key'].encode()) != generation['manifest']['key_id']:
                raise ValueError('归档公钥指纹校验失败')
            try:
                public.verify(base64.b64decode(generation['signature']),
                              canonical(generation['manifest']).encode(), ec.ECDSA(hashes.SHA256()))
                result.update(signature_valid=True, artifact_id=info['artifact_id'],
                              signed_state=generation['manifest']['state'], message='与归档原件一致')
            except InvalidSignature:
                result['message'] = '来源签名验证失败'
        with self.services.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._job(db, actor, job_id)
            self.services._audit(db, actor, 'delivery.verify', job_id, actual)
        return result

    def observe(self, actor, asset_id, sources, run_id=None):
        self.services.get(actor, asset_id)
        identity = run_id or uuid.uuid4().hex
        now = utcnow()
        value = {'id': identity, 'asset_id': asset_id, 'tenant': actor['tenant_id'],
                 'observed_at': now, 'source_results': sources}
        with self.services.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.services._verify(db)
            self.services.get(actor, asset_id, db)
            prior = db.execute('SELECT * FROM delivery_observations WHERE id=?', (identity,)).fetchone()
            if prior:
                original = self.decode(prior['payload'])
                if original['tenant'] != actor['tenant_id'] or original['asset_id'] != asset_id:
                    raise PermissionError('采集批次归属不一致')
                if canonical(original['source_results']) != canonical(sources):
                    raise ValueError('同一采集批次已保存不同内容，请使用新批次')
                return original
            db.execute('INSERT INTO delivery_observations VALUES(?,?,?,?,?)',
                       (identity, actor['tenant_id'], asset_id, now, self.seal(value)))
            self.services._audit(db, actor, 'delivery.observation', identity, digest(canonical(value).encode()))
        return value

    def observations(self, actor, asset_ids):
        results = []
        with self.services.connection() as db:
            self.services._verify(db)
            for asset_id in asset_ids:
                self.services.get(actor, asset_id, db)
                for row in db.execute('SELECT * FROM delivery_observations WHERE tenant=? AND asset_id=? ORDER BY observed_at',
                                      (actor['tenant_id'], asset_id)):
                    value = self.decode(row['payload'])
                    if value['tenant'] != actor['tenant_id'] or value['asset_id'] != asset_id or value['id'] != row['id']:
                        raise ValueError('采集观测归属校验失败')
                    results.append(value)
        return results

    def keys(self, actor, rotate=False):
        if rotate and not actor.get('admin'):
            raise PermissionError('签名密钥轮换需要管理员权限')
        with self.services.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.services._verify(db)
            if rotate:
                db.execute('UPDATE delivery_keys SET active=0 WHERE tenant=?', (actor['tenant_id'],))
            self.key(db, actor['tenant_id'])
            if rotate:
                self.services._audit(db, actor, 'delivery.key-rotate', '', '')
            return [{'key_id': r['id'], 'created_at': r['created_at'], 'active': bool(r['active'])}
                    for r in db.execute('SELECT id,created_at,active FROM delivery_keys WHERE tenant=? ORDER BY created_at DESC',
                                        (actor['tenant_id'],))]
