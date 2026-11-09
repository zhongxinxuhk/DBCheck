# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
DBCheck SQLite 历史记录管理器
将每次巡检的关键指标持久化到 SQLite 数据库，
支持同一数据库实例的历史对比和趋势数据生成。

表结构：
  history_instances   - 数据库实例注册表
  snapshots   - 每次巡检的快照（含提取的数值指标 + 完整 context JSON）
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


import os
import json
import sqlite3
import hashlib
from datetime import datetime
from contextlib import contextmanager

# 忽略的挂载点（外接 ISO/Media 光盘等分区，不应计入磁盘使用率）
IGNORE_MOUNTS = {'/mnt/iso', '/media', '/run/media', '/iso', '/cdrom'}


def _to_native(v):
    """把 JDBC/jaydebeapi 返回的 Java 装箱类型（java.lang.String / Integer / Double /
    BigDecimal 等）转成 Python 原生类型，避免 sqlite3 参数绑定报
    'Error binding parameter N: type java.lang.String is not supported'。

    DM8/SQLServer 等走 JDBC 子进程巡检时，collect_data 取回的结果值是 JVM 包装对象，
    会被原样塞进 context 并透传到快照保存；sqlite3 只接受原生类型，必须在此强转。
    """
    if v is None:
        return None
    # Python 原生标量无需处理
    if isinstance(v, (str, int, float, bool, bytes)):
        return v
    # JPype/JVM 包装对象（java.lang.String 等）：统一转 str，sqlite3 才能绑定
    return str(v)


def _db_key(db_type: str, host: str, port) -> str:
    """生成数据库实例唯一键"""
    raw = f"{db_type}:{host}:{port}"
    return hashlib.md5(raw.encode()).hexdigest()[:12] + f"_{host}_{port}"


@contextmanager
def _get_conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


class SQLiteHistoryManager:
    """
    将每次巡检的关键指标持久化到 SQLite 数据库，
    支持同一数据库实例的历史对比和趋势数据生成。

    文件位于：<base_dir>/data/history.db
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS history_instances (
        key         TEXT PRIMARY KEY,   -- _db_key() 生成
        db_type     TEXT NOT NULL,
        host        TEXT NOT NULL,
        port        TEXT NOT NULL,
        label       TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_key TEXT NOT NULL,
        ts          TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS'
        report_time TEXT NOT NULL,
        risk_count  INTEGER NOT NULL DEFAULT 0,
        health_status TEXT NOT NULL DEFAULT '未知',
        -- 提取的数值指标（供趋势图直接使用）
        cpu_usage       REAL,
        mem_usage       REAL,
        disk_usage_max  REAL,
        connections      INTEGER,
        max_connections INTEGER,
        max_used_connections INTEGER,
        queries_total   INTEGER,
        cache_hit_ratio REAL,
        sga_total_mb    REAL,
        max_tablespace_pct REAL,
        connection_usage_pct REAL,
        db_version      TEXT,
        -- 完整 context JSON（供详细对比）
        context_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY (instance_key) REFERENCES history_instances(key) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_snapshots_instance_ts
        ON snapshots(instance_key, ts);
    """

    def __init__(self, base_dir: str):
        self.db_path = os.path.join(base_dir, 'data', 'history.db')
        self._ensure_db()

    def _ensure_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with _get_conn(self.db_path) as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    def _now_str(self) -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _ensure_instance(self, conn, instance_key: str, db_type: str, host: str, port, label: str):
        row = conn.execute(
            "SELECT key FROM history_instances WHERE key=?", (instance_key,)
        ).fetchone()
        if not row:
            now = self._now_str()
            conn.execute(
                "INSERT INTO history_instances (key,db_type,host,port,label,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (instance_key, db_type, host, str(port), label, now, now)
            )
        else:
            conn.execute(
                "UPDATE history_instances SET label=?,updated_at=? WHERE key=?",
                (label, self._now_str(), instance_key)
            )

    def save_snapshot(self, db_type: str, host: str, port, label: str, context: dict):
        """
        从 context 提取关键指标，并存入 SQLite。

        :param db_type: 'mysql'、'pg'、'oracle'、'dm'、'sqlserver'、'tidb'
        :param host: 数据库 IP
        :param port: 数据库端口
        :param label: 数据库标签名
        :param context: getData.checkdb() 返回的 context 字典
        """
        key = _db_key(db_type, host, port)
        snap = self._extract_metrics(db_type, context)
        snap['ts'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        snap['report_time'] = snap['ts']
        snap['risk_count'] = len(context.get('auto_analyze', []))
        snap['health_status'] = context.get('health_status', '未知')

        # 保留完整 context 供后续详细分析（序列化时去掉不可 JSON 序列化的部分）
        try:
            context_json = json.dumps(context, ensure_ascii=False, default=str)
        except Exception:
            context_json = '{}'

        with _get_conn(self.db_path) as conn:
            self._ensure_instance(conn, key, db_type, host, port, label)
            conn.execute("""
                INSERT INTO snapshots (
                    instance_key, ts, report_time, risk_count, health_status,
                    cpu_usage, mem_usage, disk_usage_max,
                    connections, max_connections, max_used_connections,
                    queries_total, cache_hit_ratio, sga_total_mb,
                    max_tablespace_pct, connection_usage_pct, db_version,
                    context_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, tuple(_to_native(x) for x in (
                key, snap['ts'], snap['report_time'], snap['risk_count'], snap['health_status'],
                snap.get('cpu_usage'), snap.get('mem_usage'), snap.get('disk_usage_max'),
                snap.get('connections'), snap.get('max_connections'), snap.get('max_used_connections'),
                snap.get('queries_total'), snap.get('cache_hit_ratio'), snap.get('sga_total_mb'),
                snap.get('max_tablespace_pct'), snap.get('connection_usage_pct'), snap.get('version'),
                context_json
            )))
            # 保留最近 30 条
            conn.execute("""
                DELETE FROM snapshots WHERE instance_key=? AND id NOT IN (
                    SELECT id FROM snapshots WHERE instance_key=? ORDER BY ts DESC LIMIT 30
                )
            """, (key, key))
            conn.commit()
        return key

    def _extract_metrics(self, db_type: str, context: dict) -> dict:
        """从 context 提取可量化的核心指标"""
        def _safe_int(lst, field='Value'):
            """列表为空或字段缺失时返回 None：没采到 ≠ 采到了 0。"""
            if not lst or not isinstance(lst[0], dict) or field not in lst[0]:
                return None
            try:
                return int(str(lst[0].get(field, 0)).replace(',', ''))
            except Exception:
                return None

        def _safe_float(value, field='Value'):
            # Tablespace metrics pass a scalar; other metrics pass a row list.
            if isinstance(value,list):
                if not value or not isinstance(value[0],dict):return None
                value=value[0].get(field)
            if value is None or isinstance(value,bool):return None
            try:
                import math
                number=float(str(value).replace(',','').rstrip('%'))
                return number if math.isfinite(number) else None
            except (ValueError,TypeError):
                return None

        m = {}
        # 库种口径收口到 driver_registry.db_family()。
        # 这套系统里「数据库种类」存在多套写法：系统字典表(schema_knowledge.json)写
        # postgresql、实例表/历史表写 pg、驱动注册表又保留 highgo/hgdb 双名。任何一
        # 处自己写死白名单，都会出现「实例里是 pg、判断里只认 postgres」的静默跳过，
        # 表现就是指标全 None、趋势图整块空白。统一走 db_family 后，同族库自动复用
        # 同一套提取逻辑（kingbase / vastbase / openGauss 等不会再各踩一次）。
        try:
            from ..driver_registry import db_family
            fam = db_family(db_type)
        except Exception:
            fam = (db_type or '').strip().lower()
        sys_info = context.get('system_info', {}) or {}
        # 主机侧采集（CPU/内存/磁盘）缺失时如实写 None，不要写 0.0。
        # 0 会被当成「采集到了，值是 0%」，既误导趋势图，也让「只返回有数据的指标」
        # 的过滤失效（0 被当成有效数据）。
        cpu_val=sys_info.get('cpu')
        if isinstance(cpu_val,dict):cpu_val=cpu_val.get('usage_percent')
        m['cpu_usage']=_safe_float(cpu_val)
        memory=sys_info.get('memory') or {}
        m['mem_usage']=_safe_float(memory.get('usage_percent')) if isinstance(memory,dict) else None

        disks = sys_info.get('disk_list', []) or []
        m['disk_usage_max'] = None
        for d in disks:
            mp = d.get('mountpoint', '/')
            if mp in IGNORE_MOUNTS:
                continue
            pct = _safe_float(d.get('usage_percent'))
            if pct is None:continue
            m['disk_usage_max'] = max(m['disk_usage_max'] or 0.0, pct)

        # disk_usage fallback（SSH 采集器格式）
        if not m.get('disk_usage_max'):
            raw = sys_info.get('disk_usage', '')
            if raw:
                for line in raw.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        try:
                            pct = int(parts[4].rstrip('%'))
                            mp = parts[-1]
                            if mp not in IGNORE_MOUNTS:
                                m['disk_usage_max'] = max(m.get('disk_usage_max') or 0.0, pct)
                        except (ValueError, IndexError):
                            continue

        if fam == 'mysql':
            m['connections'] = _safe_int(context.get('threads_connected', []))
            m['max_connections'] = _safe_int(context.get('max_connections', []))
            m['max_used_connections'] = _safe_int(context.get('max_used_connections', []))
            queries_data = context.get('queries', [])
            m['queries_total'] = _safe_int(queries_data)
            if 'cache_hit_ratio_pct' in context:
                m['cache_hit_ratio'] = context.get('cache_hit_ratio_pct')
            m['version'] = (context.get('myversion', [{}]) or [{}])[0].get('version', '') if context.get('myversion') else ''
        elif fam == 'postgresql':
            # 注意：历史实例里 PG 的 db_type 记为 'pg'（见 save_snapshot 文档），
            # 之前这里只认 'postgres'/'postgresql'，导致 PostgreSQL 实例的
            # connections / cache_hit_ratio 等指标从未落库，趋势图自然是空的。
            pg_conn = context.get('pg_connections', []) or []
            if pg_conn and isinstance(pg_conn[0], dict):
                row = pg_conn[0]
                # 采集器给的是 total_connections，旧代码按 used_connections 取，
                # 取不到就退化成 0 —— 明明有 7 个连接却记成 0。
                used = row.get('used_connections', row.get('total_connections'))
                if used is not None:
                    m['connections'] = int(_to_native(used) or 0)
                max_conn = row.get('max_connections')
                if max_conn is not None:
                    m['max_connections'] = int(_to_native(max_conn) or 0)
                usage = row.get('usage_percent', row.get('connection_usage_pct'))
                if usage is not None:
                    m['connection_usage_pct'] = float(_to_native(usage) or 0.0)
            cache_hits = context.get('pg_cache_hit', []) or []
            if cache_hits:
                m['cache_hit_ratio'] = _safe_float(cache_hits, 'cache_hit_ratio')
            else:
                # 没有预计算的命中率时，用 pg_io_stats 的 blks_hit / (blks_hit + blks_read) 估算
                blocks_hit = blocks_read = 0
                for io_row in (context.get('pg_io_stats', []) or []):
                    if isinstance(io_row, dict):
                        blocks_hit += int(_to_native(io_row.get('blks_hit')) or 0)
                        blocks_read += int(_to_native(io_row.get('blks_read')) or 0)
                total_blocks = blocks_hit + blocks_read
                m['cache_hit_ratio'] = round(blocks_hit * 100.0 / total_blocks, 2) if total_blocks else None
            m['version'] = (context.get('pg_version', [{}]) or [{}])[0].get('version', '') if context.get('pg_version') else ''
        elif fam == 'oracle':
            ora_sess = context.get('ora_sessions', [])
            if ora_sess:
                m['connections'] = _safe_int(ora_sess, 'TOTAL_SESSIONS')
            ora_limit = context.get('ora_session_limit', [])
            if ora_limit:
                m['max_connections'] = _safe_int(ora_limit, 'SESSIONS_LIMIT')
            sga_total = context.get('ora_sga_total', [])
            if sga_total:
                m['sga_total_mb'] = _safe_float(sga_total, 'SGA_TOTAL_MB')
            ts_list = context.get('ora_tablespace', [])
            if ts_list and isinstance(ts_list, list):
                m['max_tablespace_pct'] = max((value for row in ts_list if (value := _safe_float(row.get('USED_PCT_WITH_MAXEXT',row.get('USED_PCT')))) is not None),default=None)
            m['version'] = (context.get('ora_version', [{}]) or [{}])[0].get('BANNER', '') if context.get('ora_version') else ''
        elif fam == 'dm':
            dm_sess = context.get('dm_sessions', [])
            if dm_sess and isinstance(dm_sess, list):
                m['connections'] = _safe_int(dm_sess, 'TOTAL_SESSIONS')
            dm_limit = context.get('dm_session_limit', [])
            if dm_limit and isinstance(dm_limit, list):
                m['max_connections'] = _safe_int(dm_limit, 'SESSIONS_LIMIT')
            dm_sga = context.get('dm_sga_total', [])
            if dm_sga and isinstance(dm_sga, list):
                m['sga_total_mb'] = _safe_float(dm_sga, 'SGA_TOTAL_MB')
            dm_ts = context.get('dm_tablespace', [])
            if dm_ts and isinstance(dm_ts, list):
                m['max_tablespace_pct'] = max((value for row in dm_ts if (value := _safe_float(row.get('USED_PCT'))) is not None),default=None)
            dm_ver = context.get('dm_version')
            m['version'] = (dm_ver or [{}])[0].get('BANNER', '') if dm_ver and isinstance(dm_ver, list) else ''
        elif fam == 'sqlserver':
            ss_conn = context.get('connections', [])
            if ss_conn and isinstance(ss_conn[0], dict):
                m['connections'] = ss_conn[0].get('connection_count', 0)
                m['max_connections'] = ss_conn[0].get('max_connections', 0)
                m['connection_usage_pct'] = ss_conn[0].get('connection_usage_pct', 0)
            host_info = context.get('host', {})
            if host_info:
                m['mem_usage'] = host_info.get('memory_percent', 0) or 0.0
            m['version'] = ''
            ver_rows = context.get('version', [])
            if ver_rows and isinstance(ver_rows, list) and len(ver_rows) > 0:
                for row in ver_rows:
                    if isinstance(row, (list, tuple)) and len(row) >= 2:
                        m['version'] = str(row[1])
                        break
        elif db_type == 'tidb':
            m['connections'] = _safe_int(context.get('threads_connected', []))
            m['max_connections'] = _safe_int(context.get('max_connections', []))
            m['max_used_connections'] = _safe_int(context.get('max_used_connections', []))
            queries_data = context.get('queries', [])
            m['queries_total'] = _safe_int(queries_data)
            m['version'] = (context.get('myversion', [{}]) or [{}])[0].get('version', '') if context.get('myversion') else ''

        return m

    def get_trend(self, db_type: str, host: str, port) -> dict:
        """
        获取指定实例的历史趋势数据，供前端图表使用。

        :return: {
            'labels': ['2026-04-10 08:00', ...],
            'metrics': {
                'mem_usage': [65.2, 70.1, ...],
                'connections': [20, 35, ...],
                ...
            },
            'risk_counts': [1, 2, ...],
            'health_statuses': ['良好', ...],
            'label': '数据库标签名',
            'snapshots_count': 10
        }
        """
        key = _db_key(db_type, host, port)
        with _get_conn(self.db_path) as conn:
            # 获取实例信息
            inst_row = conn.execute(
                "SELECT label FROM history_instances WHERE key=?", (key,)
            ).fetchone()
            if not inst_row:
                return {}

            label = inst_row[0] or ''

            # 获取所有快照（按时间升序）
            rows = conn.execute("""
                SELECT ts, cpu_usage, mem_usage, disk_usage_max,
                       connections, max_connections, max_used_connections,
                       queries_total, cache_hit_ratio, sga_total_mb,
                       max_tablespace_pct, connection_usage_pct,
                       risk_count, health_status
                FROM snapshots
                WHERE instance_key=?
                ORDER BY ts ASC
            """, (key,)).fetchall()

            if not rows:
                return {}

            labels = [r[0] for r in rows]
            metric_keys = [
                'cpu_usage', 'mem_usage', 'disk_usage_max',
                'connections', 'max_connections', 'max_used_connections',
                'queries_total', 'cache_hit_ratio', 'sga_total_mb',
                'max_tablespace_pct', 'connection_usage_pct'
            ]
            metrics = {mk: [] for mk in metric_keys}
            risk_counts = []
            health_statuses = []

            for r in rows:
                risk_counts.append(r[12] or 0)
                health_statuses.append(r[13] or '未知')
                # 直接保留 NULL：NULL 表示「这次巡检没有采集到该指标」，和「采到了 0」
                # 是两件事。早先统一 `or 0.0` 会把未采集变成 0，趋势图就会画出
                # 一条假的 0 基线，或者反过来把所有指标都过滤掉。
                metrics['cpu_usage'].append(r[1])
                metrics['mem_usage'].append(r[2])
                metrics['disk_usage_max'].append(r[3])
                metrics['connections'].append(r[4])
                metrics['max_connections'].append(r[5])
                metrics['max_used_connections'].append(r[6])
                metrics['queries_total'].append(r[7])
                metrics['cache_hit_ratio'].append(r[8])
                metrics['sga_total_mb'].append(r[9])
                metrics['max_tablespace_pct'].append(r[10])
                metrics['connection_usage_pct'].append(r[11])

            # 只返回真正采集到数据的指标
            final_metrics = {}
            for mk, vals in metrics.items():
                if any(v is not None for v in vals):
                    final_metrics[mk] = vals
            # risk_count 是每张快照必有的一项，却不属于上方 metrics 列。
            # 前端按 metrics.risk_count 取数，这里补上，保证「风险数量」这张图
            # 永远有数据可画（此前它只存在于顶层 risk_counts，前端永远读不到）。
            final_metrics['risk_count'] = risk_counts

            return {
                'labels': labels,
                'metrics': final_metrics,
                'risk_counts': risk_counts,
                'health_statuses': health_statuses,
                'label': label,
                'snapshots_count': len(rows)
            }

    def get_comparison(self, db_type: str, host: str, port) -> dict:
        """
        获取最近两次巡检的对比数据。

        :return: {
            'prev': {...metrics...},
            'curr': {...metrics...},
            'diff': {'mem_usage': +5.2, ...}
        }
        """
        key = _db_key(db_type, host, port)
        with _get_conn(self.db_path) as conn:
            rows = conn.execute("""
                SELECT ts, cpu_usage, mem_usage, disk_usage_max,
                       connections, max_connections, max_used_connections,
                       queries_total, cache_hit_ratio, sga_total_mb,
                       max_tablespace_pct, connection_usage_pct,
                       risk_count, health_status
                FROM snapshots
                WHERE instance_key=?
                ORDER BY ts DESC
                LIMIT 2
            """, (key,)).fetchall()

            if not rows or len(rows) < 2:
                return {}

            curr, prev = rows[0], rows[1]  # curr 是最新的

            fields = ['ts', 'cpu_usage', 'mem_usage', 'disk_usage_max',
                      'connections', 'max_connections', 'max_used_connections',
                      'queries_total', 'cache_hit_ratio', 'sga_total_mb',
                      'max_tablespace_pct', 'connection_usage_pct',
                      'risk_count', 'health_status']

            curr_dict = {f: curr[i] for i, f in enumerate(fields)}
            prev_dict = {f: prev[i] for i, f in enumerate(fields)}

            diff = {}
            for k in fields:
                if k in ('ts', 'health_status'):
                    continue
                a, b = curr_dict.get(k), prev_dict.get(k)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    diff[k] = round(a - b, 2)

            return {
                'prev': prev_dict,
                'curr': curr_dict,
                'diff': diff,
                'prev_ts': prev_dict['ts'],
                'curr_ts': curr_dict['ts']
            }

    def list_instances(self) -> list:
        """列出所有已记录的数据库实例"""
        with _get_conn(self.db_path) as conn:
            rows = conn.execute("""
                SELECT i.key, i.db_type, i.host, i.port, i.label,
                       i.updated_at,
                       (SELECT COUNT(*) FROM snapshots s WHERE s.instance_key=i.key) AS snapshot_count,
                       (SELECT s.health_status FROM snapshots s WHERE s.instance_key=i.key
                        ORDER BY s.ts DESC LIMIT 1) AS last_health,
                       (SELECT s.risk_count FROM snapshots s WHERE s.instance_key=i.key
                        ORDER BY s.ts DESC LIMIT 1) AS last_risk,
                       (SELECT s.ts FROM snapshots s WHERE s.instance_key=i.key
                        ORDER BY s.ts DESC LIMIT 1) AS last_time
                FROM history_instances i
                ORDER BY i.updated_at DESC
            """).fetchall()

        result = []
        for r in rows:
            result.append({
                'key': r[0],
                'db_type': r[1],
                'host': r[2],
                'port': r[3],
                'label': r[4] or r[0],
                'updated_at': r[5] or '',
                'snapshots_count': r[6] or 0,
                'last_health': r[7] or '未知',
                'last_risk': r[8] or 0,
                'last_time': r[9] or '',
            })
        return result

    def delete_instance(self, key: str):
        """删除指定实例及其所有快照"""
        with _get_conn(self.db_path) as conn:
            conn.execute("DELETE FROM snapshots WHERE instance_key=?", (key,))
            conn.execute("DELETE FROM history_instances WHERE key=?", (key,))
            conn.commit()
