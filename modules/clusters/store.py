# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Transactional cluster configuration, durable runs and monitoring history."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError):
    """A configuration revision, active run or collection lease changed."""


class ClusterStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS clusters (
                    id TEXT PRIMARY KEY, tenant INTEGER NOT NULL, owner INTEGER NOT NULL,
                    name TEXT NOT NULL, version INTEGER NOT NULL, spec TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
                    lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS cluster_name ON clusters(tenant,name) WHERE deleted=0;
                CREATE TABLE IF NOT EXISTS cluster_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, cluster_id TEXT NOT NULL,
                    ts REAL NOT NULL, config_version INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cluster_sample_time ON cluster_samples(cluster_id,id);
                CREATE TABLE IF NOT EXISTS cluster_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, cluster_id TEXT NOT NULL,
                    ts REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cluster_runtimes (
                    id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cluster_runs (
                    id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL, request_key TEXT NOT NULL,
                    runtime TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(cluster_id,request_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS cluster_active_run ON cluster_runs(cluster_id)
                    WHERE status IN ('dispatching','running','unconfirmed');
            ''')

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def record(row):
        if row is None:
            raise LookupError('集群不存在')
        result = dict(row)
        result['spec'] = json.loads(result['spec'])
        result.pop('lease_token', None)
        result.pop('lease_until', None)
        return result

    def list(self, principal=None):
        with self.connection() as db:
            if principal is None:
                rows = db.execute('SELECT * FROM clusters WHERE deleted=0 ORDER BY updated DESC').fetchall()
            else:
                rows = db.execute('SELECT * FROM clusters WHERE deleted=0 AND tenant=? '
                                  'AND (owner=? OR ?=1) ORDER BY updated DESC',
                                  (principal.tenant_id, principal.id, int(principal.is_admin))).fetchall()
            return [self.record(row) for row in rows]

    def get(self, cluster_id, principal=None, include_deleted=False):
        with self.connection() as db:
            record = self.record(db.execute('SELECT * FROM clusters WHERE id=?', (cluster_id,)).fetchone())
        if record['deleted'] and not include_deleted:
            raise LookupError('集群已删除')
        if principal is not None and (record['tenant'] != principal.tenant_id or
                                     record['owner'] != principal.id and not principal.is_admin):
            raise PermissionError('无权访问此集群')
        return record

    def save(self, principal, spec, cluster_id=None, version=None):
        now = time.time()
        payload = json.dumps(spec, ensure_ascii=False)
        with self.connection(write=True) as db:
            if cluster_id:
                current = self.record(db.execute('SELECT * FROM clusters WHERE id=?', (cluster_id,)).fetchone())
                if current['tenant'] != principal.tenant_id or (current['owner'] != principal.id and not principal.is_admin):
                    raise PermissionError('无权修改此集群')
                if current['deleted'] or version != current['version']:
                    raise Conflict('集群已被修改，请刷新后重试')
                db.execute('UPDATE clusters SET name=?,spec=?,version=version+1,updated=?,lease_token=NULL,lease_until=0 '
                           'WHERE id=?', (spec['name'], payload, now, cluster_id))
            else:
                cluster_id = str(uuid.uuid4())
                db.execute('INSERT INTO clusters(id,tenant,owner,name,version,spec,updated) VALUES(?,?,?,?,1,?,?)',
                           (cluster_id, principal.tenant_id, principal.id, spec['name'], payload, now))
        return self.get(cluster_id, principal)

    def delete(self, cluster_id, principal, version):
        current = self.get(cluster_id, principal)
        with self.connection(write=True) as db:
            if db.execute("SELECT 1 FROM cluster_runs WHERE cluster_id=? AND status IN ('dispatching','running','unconfirmed')",
                          (cluster_id,)).fetchone():
                raise Conflict('集群巡检仍在执行，请完成后删除')
            changed = db.execute('UPDATE clusters SET deleted=1,version=version+1,updated=?,lease_token=NULL '
                                 'WHERE id=? AND version=? AND deleted=0',
                                 (time.time(), current['id'], version)).rowcount
            if not changed:
                raise Conflict('集群已被修改，请刷新后重试')

    def claim_monitor(self, record, runtime=None):
        token = (runtime+':' if runtime else '')+str(uuid.uuid4())
        lease_seconds = max(90, len(record['spec']['members']) * 15)
        with self.connection(write=True) as db:
            changed = db.execute('UPDATE clusters SET lease_token=?,lease_until=? WHERE id=? AND version=? '
                                 'AND deleted=0 AND lease_until<?',
                                 (token, time.time()+lease_seconds, record['id'], record['version'], time.time())).rowcount
        return token if changed else None

    def latest(self, cluster_id):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM cluster_samples WHERE cluster_id=? ORDER BY id DESC LIMIT 1',
                             (cluster_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def history(self, cluster_id, limit=120, events=False):
        table = 'cluster_events' if events else 'cluster_samples'
        with self.connection() as db:
            rows = db.execute('SELECT * FROM '+table+' WHERE cluster_id=? ORDER BY id DESC LIMIT ?',
                              (cluster_id, min(max(int(limit),1),500))).fetchall()
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in reversed(rows)]

    def finish_monitor(self, record, token, observation, events):
        now = time.time()
        with self.connection(write=True) as db:
            current = db.execute('SELECT lease_token,version,deleted FROM clusters WHERE id=?', (record['id'],)).fetchone()
            if not current or current['lease_token'] != token or current['version'] != record['version'] or current['deleted']:
                return False
            db.execute('INSERT INTO cluster_samples(cluster_id,ts,config_version,payload) VALUES(?,?,?,?)',
                       (record['id'], now, record['version'], json.dumps(observation,ensure_ascii=False)))
            for event in events:
                db.execute('INSERT INTO cluster_events(cluster_id,ts,kind,payload) VALUES(?,?,?,?)',
                           (record['id'], now, event['kind'], json.dumps(event,ensure_ascii=False)))
            for table in ('cluster_samples','cluster_events'):
                db.execute('DELETE FROM '+table+' WHERE cluster_id=? AND id NOT IN '
                           '(SELECT id FROM '+table+' WHERE cluster_id=? ORDER BY id DESC LIMIT 500)',
                           (record['id'],record['id']))
            db.execute('UPDATE clusters SET lease_token=NULL,lease_until=0 WHERE id=?', (record['id'],))
        return True

    def release_monitor(self, cluster_id, token):
        with self.connection(write=True) as db:
            db.execute('UPDATE clusters SET lease_until=0,lease_token=NULL WHERE id=? AND lease_token=?', (cluster_id,token))

    def create_run(self, record, key, runtime):
        now = time.time()
        with self.connection(write=True) as db:
            existing = db.execute('SELECT * FROM cluster_runs WHERE cluster_id=? AND request_key=?',
                                  (record['id'],key)).fetchone()
            if existing:
                return self.run_record(existing), False
            current = self.record(db.execute('SELECT * FROM clusters WHERE id=?',(record['id'],)).fetchone())
            if current['deleted'] or current['version'] != record['version']:
                raise Conflict('集群配置已变化，请刷新后启动巡检')
            payload = {'cluster': record, 'members': [dict(member, status='pending', task_id=None)
                                                       for member in record['spec']['members'] if member['enabled']]}
            run_id = str(uuid.uuid4())
            try:
                db.execute('INSERT INTO cluster_runs VALUES(?,?,?,?,?,?,?,?)',
                           (run_id,record['id'],key,runtime,'dispatching',now,now,json.dumps(payload,ensure_ascii=False)))
            except sqlite3.IntegrityError as error:
                raise Conflict('集群已有执行中的巡检任务') from error
        return self.get_run(run_id), True

    @staticmethod
    def run_record(row):
        if row is None:
            raise LookupError('集群巡检任务不存在')
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        return result

    def get_run(self, run_id):
        with self.connection() as db:
            return self.run_record(db.execute('SELECT * FROM cluster_runs WHERE id=?',(run_id,)).fetchone())

    def runs(self, cluster_id):
        with self.connection() as db:
            return [self.run_record(row) for row in db.execute(
                'SELECT * FROM cluster_runs WHERE cluster_id=? ORDER BY created DESC LIMIT 50',(cluster_id,))]

    def update_run(self, run_id, payload, status):
        with self.connection(write=True) as db:
            db.execute('UPDATE cluster_runs SET payload=?,status=?,updated=? WHERE id=?',
                       (json.dumps(payload,ensure_ascii=False),status,time.time(),run_id))

    def register_runtime(self, runtime):
        import psutil
        with self.connection(write=True) as db:
            db.execute('INSERT INTO cluster_runtimes VALUES(?,?,?)',
                       (runtime,os.getpid(),psutil.Process().create_time()))

    @staticmethod
    def runtime_alive(row):
        if row is None:
            return False
        import psutil
        try:
            return abs(psutil.Process(row['pid']).create_time()-row['started']) < 0.01
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True

    def clear_dead_monitor_leases(self):
        with self.connection(write=True) as db:
            rows = db.execute('SELECT id,lease_token FROM clusters WHERE lease_token IS NOT NULL').fetchall()
            for row in rows:
                runtime,separator,_ = row['lease_token'].partition(':')
                if not separator:
                    continue
                owner = db.execute('SELECT * FROM cluster_runtimes WHERE id=?',(runtime,)).fetchone()
                if not self.runtime_alive(owner):
                    db.execute('UPDATE clusters SET lease_token=NULL,lease_until=0 WHERE id=?',(row['id'],))

    def interrupt_previous_runs(self, runtime):
        with self.connection(write=True) as db:
            rows = db.execute("SELECT * FROM cluster_runs WHERE runtime<>? AND status IN ('dispatching','running','unconfirmed') ",
                              (runtime,)).fetchall()
            for row in rows:
                owner = db.execute('SELECT * FROM cluster_runtimes WHERE id=?',(row['runtime'],)).fetchone()
                if self.runtime_alive(owner):
                    continue
                run = self.run_record(row)
                for member in run['payload']['members']:
                    if member['status'] in ('pending','running','unconfirmed'):
                        member.update(status='interrupted', error='服务重启，原任务执行状态需要复核')
                db.execute("UPDATE cluster_runs SET status='interrupted',payload=?,updated=? WHERE id=?",
                           (json.dumps(run['payload'],ensure_ascii=False),time.time(),run['id']))
