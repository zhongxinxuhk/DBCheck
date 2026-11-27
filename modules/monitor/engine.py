# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
实时监控引擎 — 多数据源连接、查询执行、后台定时采集、内存缓冲

架构：
- MonitorEngine 单例：管理所有数据源的监控采集
- _connect_and_query(): 通用连接+查询方法，支持7种数据库类型
- _collect_slow_queries() / _collect_connections(): 采集单个数据源
- _background_loop(): 后台定时采集线程
- 环形缓冲区存储历史连接数数据（最近 72 条，每条记录所有数据源）
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import time
import threading
import logging
from collections import deque
from functools import partial
from modules.pro.instance_manager import get_instance_manager
from modules.db_types.connection_profile import (
    POSTGRESQL_DEFAULT_DATABASE,
    POSTGRESQL_TYPES,
    connection_profile,
    monitor_jdbc_driver,
)
from modules.monitor.worker_transport import run_monitor_worker
from modules.core.parallel import RoundExecutor, round_budget
from modules.core.runtime_tuning import get as _tuning_get
from modules.ssh import resolve_db_endpoint
import modules.monitor.queries as mq
from modules.monitor.screen_metrics import screen_sqls

# 监控专属 logger：经 loghub 汇聚到「运行日志」页（category=monitor），且默认不写
# 共享控制台（由 MonitorConsoleFilter 过滤），从根上避免污染巡检 / 控制台输出。
_log = logging.getLogger('dbcheck.monitor')


# ── JDBC 批量采集通道 ─────────────────────────────────────────
# gbase / db2 / clickhouse 无 python 原生驱动（或驱动不可用），统一走
# 「独立非 gevent 子进程 + JDBC」通道：主进程绝不 startJVM（否则 JPype/JVM
# 与 gevent hub 死锁，整个 Web UI 冻结）。每轮每实例只 spawn 一次子进程，
# 一次跑完该实例全部采集 SQL（连接/慢查/性能计数/容量），结果缓存供本轮复用。
JDBC_BATCH_TTL = 12.0        # 批量结果缓存时长（s），略短于采样间隔 15s
SLOW_FALLBACK_RECHECK = 300  # 依赖可能在运行中被安装或修复
JDBC_BATCH_TIMEOUT = 90      # 子进程硬超时（含 JVM 冷启动 4~10s）

# ── 驱动异常 → 友好提示 ──────────────────────────────────────
# 驱动原始异常（如 "<class 'dmPython.Connection'> returned a result with
# an exception set"）对用户无意义，按常见根因归类成可操作的提示；
# 完整异常始终由调用方写入服务端日志，此处只负责面向用户的文案。
_ERROR_PATTERNS = [
    (('returned a result with an exception set',),
     '连接失败，请检查地址、端口、用户名密码及登录配置'),
    (('connection refused', '拒绝连接', '10061', 'cannot connect'),
     '无法连接数据库主机（数据库未启动或端口不通）'),
    (('timed out', 'timeout', '超时'),
     '连接超时（网络不通或防火墙拦截）'),
    (('getaddrinfo failed', 'name or service not known', 'unknown host',
      'nodename nor servname'),
     '主机地址无法解析，请检查地址配置'),
    (('access denied', 'authentication', 'login failed', 'ora-01017',
      'invalid username', 'password authentication', '密码'),
     '用户名或密码错误'),
    (('unknown database', 'database does not exist', 'database "',
      'specified database'),  # psycopg2: database "xxx" does not exist（库名夹在引号内）
     '数据库/服务名不存在，请检查库名配置'),
    (('ssl', 'certificate'),
     'SSL/证书校验失败'),
]


def _friendly_db_error(e):
    """把驱动异常翻译成用户可读的提示；未命中已知模式时给通用提示。"""
    low = str(e).lower()
    if '可信ca' in low:
        return 'GBase 8a 缺少可信 CA，请配置 CA 或显式开启允许明文直连'
    if 'ora-28009' in low:
        return 'Oracle SYS 账号需要启用 SYSDBA 登录'
    if 'ora-12514' in low:
        return 'Oracle 监听器未识别请求的服务名（ORA-12514），请核对数据源的服务名或 JDBC URL'
    if 'ora-12505' in low:
        return 'Oracle 监听器未识别请求的 SID（ORA-12505），请核对数据源的 SID 或 JDBC URL'
    if 'ssh 监控请使用' in low:
        return str(e)
    if any(code in low for code in ('ora-00942', 'ora-01031', 'permission denied',
                                     'view server state permission')):
        return '监控系统视图不可见或采集权限不足，请检查监控账号授权'
    for keys, msg in _ERROR_PATTERNS:
        if any(k in low for k in keys):
            return msg
    return '数据库连接或查询异常，请检查实例配置或查看服务端日志'


def _probe_sql(db_type):
    return {
        'oracle': 'SELECT 1 FROM DUAL',
        'dm': 'SELECT 1 FROM DUAL',
        'db2': 'SELECT 1 FROM SYSIBM.SYSDUMMY1',
        'gbase': 'SELECT FIRST 1 1 FROM systables',
    }.get(db_type, 'SELECT 1')


def build_slow_source_catalog(instances, snapshots):
    """为慢查询页生成完整数据源清单。

    ``snapshots`` 是 MonitorEngine 的 ``{instance_id: slow_snapshot}``。慢查询
    接口只把「有 SQL 行」的快照展开成表格行，因此某个库没有慢 SQL 时不会出现
    在行列表里；数据源下拉框和「监控数据源」卡片必须独立使用本清单，才能把
    已添加但当前无慢 SQL 的库也显示出来。

    ``instances`` 为 ``None`` 表示实例列表读取失败，此时退回快照清单；传入
    空列表则明确表示当前没有启用的数据源。
    """
    snapshots = snapshots or {}
    catalog = []
    seen = set()
    if instances is not None:
        for inst in instances:
            if not isinstance(inst, dict) or not inst.get('enabled', True):
                continue
            if not inst.get('host'):
                continue
            iid = inst.get('id')
            if not iid or iid in seen:
                continue
            snap = snapshots.get(iid) or {}
            if snap.get('config_updated_at', inst.get('updated_at')) != inst.get('updated_at'):
                snap = {}
            label = "%s (%s:%s)" % (
                inst.get('name', iid),
                inst.get('host', '?'),
                inst.get('port', '?'),
            )
            error = snap.get('error')
            catalog.append({
                'instance_id': iid,
                'source': label,
                'label': label,
                'db_type': inst.get('db_type') or snap.get('db_type', ''),
                'error': error,
                'ts': snap.get('ts', 0),
                'slow_count': 0 if error else len(snap.get('data') or []),
                'collected': bool(snap),
            })
            seen.add(iid)
        return catalog

    for iid, snap in (snapshots or {}).items():
        if not isinstance(snap, dict) or iid in seen:
            continue
        label = snap.get('label', iid)
        error = snap.get('error')
        catalog.append({
            'instance_id': iid,
            'source': label,
            'label': label,
            'db_type': snap.get('db_type', ''),
            'error': error,
            'ts': snap.get('ts', 0),
            'slow_count': 0 if error else len(snap.get('data') or []),
            'collected': True,
        })
        seen.add(iid)
    return catalog


def flatten_slow_queries(snapshots, sources):
    """把慢查询快照展开为表格行，并优先使用当前数据源名称。

    ``sources`` 来自 :func:`build_slow_source_catalog`。实例改名后引擎快照里
    仍是旧 label；若表格继续使用旧 label，前端按新 label 筛选会把行全部过滤掉。
    这里用当前清单的 label/db_type 覆盖快照元数据。
    """
    metadata = {
        item.get('instance_id'): item
        for item in (sources or [])
        if isinstance(item, dict) and item.get('instance_id')
    }
    items = []
    for iid, snapshot in (snapshots or {}).items():
        if not isinstance(snapshot, dict) or iid not in metadata:
            continue
        source = metadata[iid]
        if source.get('error') or source.get('collected') is False:
            continue
        label = source.get('source') or source.get('label') or snapshot.get('label', iid)
        for row in snapshot.get('data') or []:
            if not isinstance(row, dict):
                continue
            items.append({
                'instance_id': iid,
                'source': label,
                'label': label,
                'db_type': source.get('db_type') or snapshot.get('db_type', ''),
                'error': snapshot.get('error'),
                'ts': snapshot.get('ts', 0),
                **row,
            })
    return items


class MonitorEngine:
    """实时监控引擎 — 全局单例"""

    # ── 默认配置 ──
    DEFAULT_INTERVAL = 10  # 采集间隔（秒）
    MAX_HISTORY = 8640     # 允许 5 秒间隔保留最近 12 小时的内存样本
    QUERY_TIMEOUT = 15     # 单次查询超时（秒）

    def __init__(self):
        self._lock = threading.Lock()
        self._collect_lock = threading.Lock()
        self._running = False
        self._thread = None
        self._interval = self.DEFAULT_INTERVAL
        self._executor = RoundExecutor('monitor')
        # 当前慢查询数据: {instance_id: {'data': [...], 'error': str, 'ts': float, 'db_type': str, 'label': str}}
        self._slow_queries = {}
        # 当前连接数据: {instance_id: {'data': [...], 'error': str, 'ts': float, 'total': int, 'max_conn': int, 'db_type': str, 'label': str}}
        self._connections = {}
        # 连接数历史: deque of {ts, instances: [{id, label, total, max_conn, db_type}]}
        self._conn_history = deque(maxlen=self.MAX_HISTORY)
        # 最后一次采集时间
        self._last_collect_ts = 0
        # JDBC 批量采集缓存: {iid: {'ts': float, 'res': {sql: rows|('__err__',msg)}, 'lock': Lock}}
        self._jdbc_cache = {}
        # 慢查询主 SQL 依赖缺失记忆（如 pg_stat_statements 扩展未安装）:
        # 这些实例每轮直接走 fallback，不再重复刷错误日志
        self._slow_prim_skip = {}

    # ═══════════════════════════════════════════════════════════
    #  启停控制
    # ═══════════════════════════════════════════════════════════

    def start(self, interval=None):
        """启动后台采集线程"""
        if interval:
            self._interval = max(5, min(interval, 300))
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._background_loop, daemon=True, name='MonitorCollector')
            self._thread.start()

    def stop(self):
        """停止后台采集"""
        with self._lock:
            self._running = False
            self._thread = None

    def set_interval(self, interval):
        """修改采集间隔（秒）"""
        self._interval = max(5, min(interval, 300))

    @property
    def is_running(self):
        return self._running

    @property
    def interval(self):
        return self._interval

    # ═══════════════════════════════════════════════════════════
    #  数据读取（线程安全）
    # ═══════════════════════════════════════════════════════════

    def get_slow_queries(self):
        """获取当前慢查询数据"""
        with self._lock:
            return dict(self._slow_queries)

    def get_connections(self):
        """获取当前连接数据"""
        with self._lock:
            return dict(self._connections)

    def get_collection_snapshots(self):
        """Return session and slow-query results published in the same round."""
        with self._lock:
            return dict(self._connections), dict(self._slow_queries)

    def get_conn_history(self):
        """获取连接数历史"""
        with self._lock:
            return list(self._conn_history)

    def get_status(self):
        """获取监控状态"""
        with self._lock:
            return self._status_unlocked()

    def _status_unlocked(self):
        return {
            'running': self._running,
            'interval': self._interval,
            'last_collect_ts': self._last_collect_ts,
            'data_source_count': len(self._slow_queries),
        }

    def get_page_snapshot(self, *, slow_queries=False):
        with self._lock:
            data = self._slow_queries if slow_queries else self._connections
            return dict(data), self._status_unlocked()

    # ═══════════════════════════════════════════════════════════
    #  手动触发采集
    # ═══════════════════════════════════════════════════════════

    def trigger_collect(self):
        """手动触发一次采集（不阻塞）"""
        t = threading.Thread(target=self._do_collect, daemon=True)
        t.start()

    # ═══════════════════════════════════════════════════════════
    #  后台循环
    # ═══════════════════════════════════════════════════════════

    def _background_loop(self):
        while self._running and threading.current_thread() is self._thread:
            _started = time.monotonic()
            try:
                self._do_collect()
            except Exception:
                pass
            # 保持「固定周期」：单轮耗时从间隔里扣除，而不是「跑完再睡满间隔」。
            # 否则实际采样周期 = 单轮耗时 + interval，大规模下会持续漂移。
            _deadline = _started + max(1.0, float(self._interval))
            while (self._running and threading.current_thread() is self._thread
                   and time.monotonic() < _deadline):
                time.sleep(min(0.5, max(0.01, _deadline - time.monotonic())))

    def _do_collect(self):
        """执行一轮采集"""
        if not self._collect_lock.acquire(blocking=False):
            return
        try:
            im = get_instance_manager()
            # 本轮 SQL 与结果标签使用同一份已解密配置。
            all_instances = im.get_all_instances_decrypted()
            instances = [i for i in all_instances if i.get('enabled', True)]
            with self._lock:
                self._slow_prim_skip = {
                    inst['id']: self._slow_prim_skip[inst['id']] for inst in instances
                    if inst['id'] in self._slow_prim_skip
                }
            # 明确标识：后台监控采集是独立后台任务，遍历全部已保存实例，
            # 与用户手动发起的巡检无关，避免日志混流被误判为「巡检串库」。
            _log.info("===== 后台监控采集任务（独立后台线程，遍历 %d 个实例，与手动巡检无关）=====", len(instances))
            if not instances:
                with self._lock:
                    self._slow_queries = {}
                    self._connections = {}
                    self._last_collect_ts = time.time()
                return

            # ── 超大实例规模：按实例并发采集，而非串行遍历 ──────────
            # 历史问题：单线程 for 循环 + 每实例 2~3 次新建连接，任何一个慢库都会
            # head-of-line 阻塞整轮采集，表现为「监控页停在上一轮、其它库不刷新」。
            # 现在改为有界并发 + 整轮截止时间：单个库卡住只损失自己这一轮。
            workers = int(_tuning_get('monitor', 'max_workers'))
            item_timeout = float(_tuning_get('monitor', 'instance_timeout'))
            budget = round_budget(len(instances), workers, item_timeout)
            _t0 = time.monotonic()
            results, errors, timed_out = self._executor.run(
                instances, self._collect_instance, workers=workers,
                round_timeout=budget, key=lambda inst: inst['id'],
            )

            new_slow = {}
            new_conn = {}
            conn_history_items = []

            for idx, inst in enumerate(instances):
                iid = inst['id']
                if not inst.get('host'):
                    _log.info("跳过空 host 实例: %s", iid)
                    continue
                entry = results.get(idx)
                if entry is None:
                    exc = errors.get(idx)
                    reason = _friendly_db_error(exc) if exc else '采集超时'
                    db_type = mq.normalize_db_type(inst.get('db_type', ''))
                    label = f"{inst.get('name', iid)} ({inst.get('host', '?')}:{inst.get('port', '?')})"
                    new_slow[iid] = {
                        'data': [], 'error': reason,
                        'config_updated_at': inst.get('updated_at'),
                        'ts': time.time(), 'db_type': db_type, 'label': label,
                    }
                    new_conn[iid] = {
                        'data': [], 'error': reason,
                        'incomplete': True,
                        'config_updated_at': inst.get('updated_at'),
                        'ts': time.time(), 'total': 0, 'max_conn': 0,
                        'db_type': db_type, 'label': label,
                    }
                    continue

                db_type = entry['db_type']
                label = entry['label']
                entry['slow']['config_updated_at'] = inst.get('updated_at')
                new_slow[iid] = entry['slow']
                cn = entry['conn']
                cn['config_updated_at'] = inst.get('updated_at')
                new_conn[iid] = cn
                try:
                    _max_conn = max(0, int(cn.get('max_conn') or 0))
                except (TypeError, ValueError):
                    _max_conn = 0
                if cn.get('error'):
                    continue
                conn_history_items.append({
                    'id': iid,
                    'label': label,
                    'total': cn.get('total', 0),
                    'active': (cn.get('connections') or {}).get('active'),
                    'max_conn': _max_conn,
                    'db_type': db_type,
                })

            ts = time.time()
            with self._lock:
                self._slow_queries = new_slow
                self._connections = new_conn
                self._last_collect_ts = ts
                self._conn_history.append({
                    'ts': ts,
                    'instances': conn_history_items,
                })

            _log.info(
                "监控采集：会话成功 %d/%d，采集线程异常 %d，未完成 %d，并发 %d，耗时 %.2fs",
                len(conn_history_items), len(instances), len(errors), len(timed_out),
                workers, time.monotonic() - _t0,
            )
        except Exception as e:
            _log.error("采集失败: %s", e)
        finally:
            self._collect_lock.release()

    def _collect_instance(self, inst):
        """采集单个实例（慢查询 + 连接），供并发调度器调用。

        任何单实例异常均在此层兜住并转成结构化错误，不影响同轮其它实例。
        """
        iid = inst['id']
        db_type = mq.normalize_db_type(inst.get('db_type', ''))
        label = f"{inst.get('name', iid)} ({inst.get('host', '?')}:{inst.get('port', '?')})"

        query = partial(self.run_snapshot_query, inst)
        try:
            slow = self._collect_slow(
                iid, db_type, label, query=query, revision=inst.get('updated_at'))
        except Exception as e:
            _log.warning("慢查询采集失败 %s: %s", label, e)
            slow = {
                'data': [], 'error': _friendly_db_error(e),
                'ts': time.time(), 'db_type': db_type, 'label': label,
            }

        try:
            conn = self._collect_conn(iid, db_type, label, query=query)
        except Exception as e:
            _log.warning("连接采集失败 %s: %s", label, e)
            conn = {
                'data': [], 'error': _friendly_db_error(e),
                'ts': time.time(), 'total': 0, 'max_conn': 0,
                'db_type': db_type, 'label': label,
            }

        return {'id': iid, 'db_type': db_type, 'label': label,
                'slow': slow, 'conn': conn}

    # ═══════════════════════════════════════════════════════════
    #  采集实现
    # ═══════════════════════════════════════════════════════════

    def _collect_slow(self, instance_id, db_type, label, *, query=None, revision=None):
        query = query or partial(self._connect_and_query, instance_id)
        sql = mq.SLOW_QUERY_TEMPLATES.get(db_type)
        if not sql:
            return {'data': [], 'error': f'不支持的类型: {db_type}',
                    'ts': time.time(), 'db_type': db_type, 'label': label}

        fallback_sql = mq.SLOW_QUERY_FALLBACK_TEMPLATES.get(db_type)
        # PostgreSQL 13 前后 pg_stat_statements 的统计列名不同；主 SQL 失败时
        # 先按旧列名重试，避免不必要地降级到 pg_stat_activity 而丢掉累计统计。
        legacy_sql = (mq.PG_SLOW_QUERY_LEGACY_SQL
                      if db_type in ('postgresql', 'pg', 'ivorysql', 'hgdb')
                      else None)
        rows = None
        with self._lock:
            cache_key, expires = self._slow_prim_skip.get(instance_id, (None, 0))
        skip_primary = cache_key == (db_type, revision) and time.monotonic() < expires
        if not skip_primary:
            try:
                rows = query(sql)
            except Exception as e:
                low = str(e).lower()
                if legacy_sql and any(marker in low for marker in (
                        'mean_exec_time', 'max_exec_time', 'total_exec_time')):
                    try:
                        rows = query(legacy_sql)
                        _log.info("%s 使用 PostgreSQL 13 以下慢查询 SQL", label)
                    except Exception as legacy_error:
                        _log.warning("慢查询兼容 SQL 失败 %s: %s", label, legacy_error)
                # 依赖对象缺失（pg_stat_statements / performance_schema 未安装）
                # 或列不存在（Oracle 11gR1 无 max_elapsed_time → ORA-00904
                # invalid identifier，#63）→ 记忆化：首行错误摘要打印一次，
                # 后续轮直接 fallback，不再重复刷日志
                dep_missing = ('does not exist' in low or "doesn't exist" in low
                               or 'ora-00904' in low or 'invalid identifier' in low)
                if rows is None and fallback_sql and dep_missing:
                    with self._lock:
                        self._slow_prim_skip[instance_id] = (
                            (db_type, revision), time.monotonic() + SLOW_FALLBACK_RECHECK)
                    _log.info("%s 慢查询主 SQL 依赖缺失（%s），后续轮次直接使用 fallback",
                              label, str(e).splitlines()[0][:100])
                elif rows is None:
                    _log.warning("慢查询 SQL 失败 %s: %s", label, e)
                if rows is None and not fallback_sql:
                    return {'data': [], 'error': _friendly_db_error(e),
                            'ts': time.time(), 'db_type': db_type, 'label': label}
        fallback_used = False
        # A reachable DM8 instance may have SQL execution history disabled.
        if rows is None or (db_type == 'dm' and not rows):
            # fallback：主 SQL 失败降级，或已记忆依赖缺失直接走此路（静默）
            try:
                rows = query(fallback_sql)
                fallback_used = True
                if not skip_primary:
                    _log.info("%s 使用 fallback 慢查询 SQL", label)
            except Exception as fb:
                _log.warning("慢查询 fallback 亦失败 %s: %s", label, fb)
                return {'data': [], 'error': _friendly_db_error(fb),
                        'ts': time.time(), 'db_type': db_type, 'label': label}

        if db_type == 'dm' and fallback_used:
            rows = [{**row, 'sample_kind': 'running'} for row in rows]

        result = {
            'data': rows,
            'error': None,
            'ts': time.time(),
            'db_type': db_type,
            'label': label,
        }
        return result

    def _collect_conn(self, instance_id, db_type, label, *, query=None):
        query = query or partial(self._connect_and_query, instance_id)
        conn_sql = mq.CONNECTION_TEMPLATES.get(db_type)
        if not conn_sql:
            return {'data': [], 'error': f'不支持的类型: {db_type}',
                    'ts': time.time(), 'total': 0, 'max_conn': 0,
                    'connections': {}, 'usage_pct': 0,
                    'db_type': db_type, 'label': label}

        try:
            rows = query(conn_sql)
        except Exception as e:
            _log.warning("连接 SQL 失败 %s: %s", label, e)
            return {'data': [], 'error': _friendly_db_error(e),
                    'ts': time.time(), 'total': 0, 'max_conn': mq.MAX_CONNECTION_DEFAULTS.get(db_type, 100),
                    'connections': {'active': 0, 'idle': 0, 'blocked': 0}, 'usage_pct': 0,
                    'db_type': db_type, 'label': label}

        if db_type == 'dm' and any(row.get('trx_id') for row in rows):
            try:
                waiting = query(mq.DM_BLOCKED_TRANSACTIONS_SQL)
            except Exception as error:
                _log.debug("达梦事务阻塞视图不可用 %s: %s", label, error)
            else:
                blocked_ids = {row['trx_id'] for row in waiting}
                rows = [
                    {**row, 'state': 'blocked'} if row.get('trx_id') in blocked_ids else row
                    for row in rows
                ]

        # 获取最大连接数
        max_sql = mq.MAX_CONN_QUERY_SQL.get(db_type)
        max_conn = mq.MAX_CONNECTION_DEFAULTS.get(db_type, 100)
        if max_sql:
            try:
                max_rows = query(max_sql)
                if max_rows and max_rows[0]:
                    val = list(max_rows[0].values())[0]
                    try:
                        max_conn = int(val)
                    except (ValueError, TypeError):
                        pass
            except Exception:
                pass

        total = len(rows)
        if rows:
            try:
                total = max(total, int(rows[0].get('total_connections') or 0))
            except (ValueError, TypeError):
                pass
        # 统计连接状态分布
        active = 0
        idle = 0
        blocked = 0
        for r in rows:
            state_val = None
            for k, v in r.items():
                if 'state' in k.lower():
                    state_val = str(v).lower() if v else ''
                    break
            if state_val in ('sleep', 'sleeping', 'inactive') or (state_val or '').startswith('idle'):
                idle += 1
            elif state_val in ('blocked', 'locked') or (
                    db_type not in ('sqlserver', 'dm') and state_val in ('waiting', 'wait')):
                blocked += 1
            else:
                active += 1

        result = {
            'data': rows,
            'error': None,
            'ts': time.time(),
            'total': total,
            'max_conn': max_conn,
            'connections': {'active': active, 'idle': idle, 'blocked': blocked},
            'usage_pct': round(total / max_conn * 100, 1) if max_conn > 0 else 0,
            'db_type': db_type,
            'label': label,
        }
        return result

    # ═══════════════════════════════════════════════════════════
    #  JDBC 批量子进程采集（gbase / db2 / clickhouse）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _jdbc_batch_sqls(db_type):
        """该类型一轮采集所需的全部 SQL（模板去重，主/备选全带上）。"""
        sqls, seen = [], set()

        def _add(s):
            if s and s not in seen:
                seen.add(s)
                sqls.append(s)

        for src in (mq.CONNECTION_TEMPLATES, mq.MAX_CONN_QUERY_SQL,
                    mq.SLOW_QUERY_TEMPLATES, mq.SLOW_QUERY_FALLBACK_TEMPLATES):
            _add(src.get(db_type))
        if db_type == 'dm':
            _add(mq.DM_BLOCKED_TRANSACTIONS_SQL)
        if db_type in ('postgresql', 'pg', 'ivorysql', 'hgdb'):
            _add(mq.PG_SLOW_QUERY_LEGACY_SQL)
        _add(_probe_sql(db_type))
        # 大屏指标 SQL：family 条目为 {key: sql}，逐条带上（repl/repl8 同理）
        for s in screen_sqls(db_type).values():
            _add(s)
        return sqls

    def _jdbc_query(self, inst, db_type, sql, timeout=None, template_key=None, *, native=False):
        """JDBC 批量通道查询单条 SQL：结果来自每轮一次的子进程批量采集缓存。

        首个调用方（引擎采集线程或大屏采样线程）触发一次子进程批量执行，
        同轮内其余查询（含另一侧线程的 run_query）直接读缓存，避免每条 SQL
        都冷启动一次 JVM（4~10s）。inst 为已解密实例 dict。
        ``template_key``：采集 SQL 模板键（oracle/pg 等），缺省同 db_type；
        *_jdbc 专属类型的连接器 id 与模板键不同（oracle_jdbc vs oracle）。
        """
        instance_id = inst.get('id')
        signature = (db_type, template_key, native, inst.get('updated_at'))
        with self._lock:
            cache = self._jdbc_cache.setdefault(instance_id, {'lock': threading.Lock()})
        with cache['lock']:
            if (cache.get('signature') == signature
                    and time.monotonic() - cache['ts'] <= JDBC_BATCH_TTL):
                return self._jdbc_pick(cache['res'], sql)
            runner = self._native_run_batch if native else self._jdbc_run_batch
            res = runner(inst, db_type, timeout, template_key=template_key)
            cache.update(ts=time.monotonic(), res=res, signature=signature)
            return self._jdbc_pick(res, sql)

    @staticmethod
    def _jdbc_pick(res, sql):
        """从批量结果中取单条 SQL 的行；错误标记转成异常（沿用友好文案链路）。"""
        entry = res.get(sql)
        if entry is None:
            raise RuntimeError('该类型批量采集未覆盖此 SQL: %s' % sql[:60])
        if isinstance(entry, tuple):
            raise RuntimeError(entry[1])
        return entry

    def _jdbc_run_batch(self, inst, db_type, timeout=None, template_key=None):
        sqls = self._jdbc_batch_sqls(template_key or db_type)
        return self._run_monitor_batch(inst, db_type, sqls, timeout)

    def _native_run_batch(self, inst, db_type, timeout=None, template_key=None):
        sqls = self._jdbc_batch_sqls(template_key or db_type)
        return self._run_monitor_batch(inst, db_type, sqls, timeout, native=True)

    @staticmethod
    def _run_monitor_batch(inst, db_type, sqls, timeout, *, native=False):
        timeout = timeout or JDBC_BATCH_TIMEOUT
        try:
            data = run_monitor_worker({
                'source': inst, 'db_type': db_type, 'queries': sqls, 'timeout': timeout,
                'native_sql': native,
            }, timeout)
        except (TimeoutError, ValueError, RuntimeError, OSError) as error:
            data = {'fatal': str(error)}
        result = {}
        for index, sql in enumerate(sqls):
            entry = data.get(f'q{index}') or {}
            error = data.get('fatal') or entry.get('error')
            result[sql] = ('__err__', error or '采集失败') if error or not entry else entry['rows']
        return result

    # ═══════════════════════════════════════════════════════════
    #  通用连接+查询
    # ═══════════════════════════════════════════════════════════

    def run_query(self, instance_id, sql, timeout=None):
        """公共查询入口：供大屏指标适配层（screen_metrics）等外部复用连接逻辑。"""
        return self._connect_and_query(instance_id, sql, timeout=timeout)

    def probe(self, instance_id, db_type, *, query=None):
        """对指标查询失败的实例验证数据库是否仍能响应只读查询。"""
        query = query or partial(self.run_query, instance_id)
        try:
            if not query(_probe_sql(mq.normalize_db_type(db_type))):
                raise RuntimeError('探活查询未返回结果')
        except Exception as error:
            _log.warning("实例 %s 探活失败: %s", instance_id, error)
            return _friendly_db_error(error)
        return None

    def _connect_and_query(self, instance_id, sql, timeout=None):
        """连接到指定数据源并执行 SQL，返回 list of dicts"""
        im = get_instance_manager()
        inst = im.get_instance_decrypted(instance_id)
        if not inst:
            raise ValueError(f"实例 {instance_id} 不存在")
        return self.run_snapshot_query(inst, sql, timeout=timeout)

    def run_snapshot_query(self, inst, sql, timeout=None):
        """使用调用方持有的服务端连接配置执行本轮 SQL。"""
        if not inst.get('enabled', True):
            raise ValueError(f"实例 {inst.get('id')} 已停用")
        if timeout is None:
            timeout = self.QUERY_TIMEOUT

        inst = connection_profile(inst)
        db_type = mq.normalize_db_type(inst.get('db_type'))
        connector = monitor_jdbc_driver(inst, db_type)
        if connector:
            if connector in POSTGRESQL_TYPES and sql not in self._jdbc_batch_sqls(db_type):
                result = self._run_monitor_batch(inst, connector, [sql], timeout)
                return self._jdbc_pick(result, sql)
            template_key = db_type if connector != db_type or db_type in ('sqlserver', 'dm') else None
            return self._jdbc_query(inst, connector, sql, timeout=timeout,
                                    template_key=template_key)

        if sql in self._jdbc_batch_sqls(db_type):
            return self._jdbc_query(inst, db_type, sql, timeout=timeout, native=True)
        result = self._run_monitor_batch(inst, db_type, [sql], timeout, native=True)
        return self._jdbc_pick(result, sql)

    def _create_connection(self, inst, password, db_type, timeout):
        """根据 db_type 创建数据库连接"""
        host = inst.get('host', '')
        if not host:
            raise ValueError('主机地址不能为空')
        port = int(inst['port'])
        user = inst['user']

        # SSH 隧道：配置了「经 SSH 隧道连接数据库」时把 host/port 换成本地转发
        # 端点。缺此步，数据库仅监听 SSH 主机回环（host 填 127.0.0.1）的场景下
        # 监控大屏会直连巡检机自身回环 → Connection refused → 误判宕机，
        # 而巡检（app.py / api.py 显式 open_db_tunnel）却正常，形成
        # 「巡检通过、大屏宕机」的分裂现象。
        host, port, _tun = resolve_db_endpoint(inst)

        if db_type == 'gbase8a':
            # GBase 8a 走 MySQL 协议原生连接（不是 8s 的 JDBC 通道）。
            # 端点已由 resolve_db_endpoint 处理过 SSH 隧道；只有真的拿到隧道
            # 时才标记 _tunnel_ready（此时明文 pymysql 走已加密的本地转发），
            # 否则保留直连路径，让 connect_gbase8a 继续强制 TLS CA 校验。
            # 监控只取集群级 STATUS/PROCESSLIST，不绑定虚拟集群，故不传
            # database/schema 触发 USE VC / select_db。
            from modules.db_types.compatibility import connect_gbase8a
            options = dict(inst)
            options.update(host=host, port=port, password=password,
                           database='', virtual_cluster='', schema='',
                           ssh_tunnel=False)
            if _tun is not None:
                options['_tunnel_ready'] = True
            options['monitor_stream_results'] = True
            return connect_gbase8a(options)

        if db_type in ('mysql', 'mariadb', 'oceanbase', 'starrocks', 'doris'):
            # MariaDB / OceanBase MySQL 租户与 MySQL 协议/参数高度兼容，复用同款 pymysql 连接逻辑。
            # OceanBase MySQL 租户的 database 即租户名，默认 sys。
            import pymysql
            db_name = inst.get('database')
            if db_type == 'oceanbase':
                db_name = db_name or 'sys'
            elif db_name is None:
                db_name = 'mysql'
            return pymysql.connect(
                host=host, port=port, user=user, password=password,
                database=db_name,
                charset='utf8mb4', connect_timeout=timeout, read_timeout=timeout,
                cursorclass=pymysql.cursors.SSCursor,
            )

        elif db_type in ('postgresql', 'pg', 'hgdb', 'kingbase', 'uxdb', 'vastbase',
                         'highgo', 'halodb', 'opengauss', 'greenplum'):
            # 国产 PG 线协议系：瀚高/人大金仓/优炫/羲和直连 PG；Vastbase（海量 G100）
            # 基于 openGauss 内核（openGauss 源自 PG 9.2 分支），仍保留 PG 兼容层，
            # 故同一 psycopg2 + pg 系统视图路径即可覆盖，无需分叉。
            # ⚠️ 本分支收到的是归一后 db_type（hgdb/kingbase/vastbase 等已归一为 pg），
            # default_db 映射必须用实例的原始 db_type 查，否则键全部落空、
            # 库名空缺时一律回落 postgres（瀚高无 postgres 库 → FATAL 报错）。
            # 兜底回落「用户名同名库」（PG 惯例：默认库与用户同名，与 JDBC
            # 数据源测试不指定库名时同口径）。
            raw_type = (inst.get('db_type') or '').lower()
            default_db = {'hgdb': 'highgo', 'kingbase': 'kingbase',
                          'uxdb': 'uxdb',
                          # Vastbase 基于 openGauss 内核，openGauss/Vastbase 均保留 PG
                          # 兼容层且默认库为 postgres，故与 opengauss/greenplum 同取 postgres。
                          'vastbase': 'postgres', 'highgo': 'highgo',
                          'halodb': 'halo',
                          'opengauss': 'postgres', 'greenplum': 'postgres'}.get(raw_type)
            if raw_type in POSTGRESQL_TYPES:
                default_db = POSTGRESQL_DEFAULT_DATABASE
            import psycopg2
            return psycopg2.connect(
                host=host, port=port, user=user, password=password,
                dbname=inst.get('database') or default_db or user,
                client_encoding='UTF8', connect_timeout=timeout,
            )

        elif db_type == 'oracle':
            import oracledb
            # 必须用 makedsn 构造完整 easy-connect 描述符（DESCRIPTION=...），
            # 不能直接把 service_name 当 dsn 传入：裸服务名会被 oracledb 当作 TNS
            # net service name 去解析 tnsnames.ora，而无 config_dir 时会抛
            # DPY-4027: no configuration directory specified。
            svc = inst.get('service_name', '') or ''
            sid = inst.get('sid', '') or ''
            descriptor = (inst.get('jdbc_url') or '').strip()
            if descriptor.upper().startswith('(DESCRIPTION'):
                from modules.oracle_conn_test import _normalize_oracle_dsn
                if _tun is not None:
                    raise ValueError('SSH 监控请使用主机、端口、库名或服务名配置')
                dsn = _normalize_oracle_dsn(descriptor, host, port)
            elif inst.get('use_sid') and (svc or sid or inst.get('database')):
                dsn = oracledb.makedsn(host=host, port=port, sid=svc or sid or inst['database'])
            elif svc:
                dsn = oracledb.makedsn(host=host, port=port, service_name=svc)
            elif sid:
                dsn = oracledb.makedsn(host=host, port=port, sid=sid)
            else:
                dsn = f"{host}:{port}/orcl"
            mode = oracledb.SYSDBA if inst.get('sysdba') else oracledb.AUTH_MODE_DEFAULT
            conn = oracledb.connect(user=user, password=password, dsn=dsn, mode=mode,
                                    tcp_connect_timeout=timeout)
            conn.call_timeout = int(timeout * 1000)
            return conn

        elif db_type in ('sqlserver', 'sqlserver_jdbc', 'mssql'):
            from modules.db_types.compatibility import connect_sqlserver

            return connect_sqlserver(
                dict(inst, host=host, ip=host, port=port, password=password,
                     ssh_tunnel=False, _tunnel_ready=bool(_tun or inst.get('_tunnel_ready'))),
                timeout=timeout,
            )

        elif db_type == 'dm':
            import dmPython
            dsn = f"{host}:{port}"
            return dmPython.connect(user=user, password=password, server=dsn)

        elif db_type == 'tidb':
            import pymysql
            return pymysql.connect(
                host=host, port=port, user=user, password=password,
                database=inst.get('database') or '',
                charset='utf8mb4', connect_timeout=timeout, read_timeout=timeout,
                cursorclass=pymysql.cursors.SSCursor,
                autocommit=True,
            )

        elif db_type == 'ivorysql':
            import psycopg2
            return psycopg2.connect(
                host=host, port=port, user=user, password=password,
                dbname=inst.get('database') or 'ivorysql',
                client_encoding='UTF8', connect_timeout=timeout,
            )

        elif db_type == 'yashandb':
            try:
                import yasdb
            except ImportError:
                raise RuntimeError("YashanDB 驱动未安装，请先安装 yasdb 驱动后再使用崖山数据库监控。")
            return yasdb.connect(host=host, port=port, user=user, password=password)

        else:
            raise ValueError(f"不支持的数据库类型: {db_type}")


# ═══════════════════════════════════════════════════════════
#  全局单例
# ═══════════════════════════════════════════════════════════

_monitor_engine = None
_monitor_lock = threading.Lock()


def get_monitor_engine():
    """获取 MonitorEngine 全局单例"""
    global _monitor_engine
    if _monitor_engine is None:
        with _monitor_lock:
            if _monitor_engine is None:
                _monitor_engine = MonitorEngine()
    return _monitor_engine
