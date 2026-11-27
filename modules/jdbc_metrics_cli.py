# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
modules/jdbc_metrics_cli.py — JDBC 与 SSH 隧道实时指标采集子进程

作用：
  在**未被 gevent monkey-patch** 的干净子进程里，对 JDBC 和 SSH 隧道数据源
  执行「连接 + 指标深采」，把结果以 JSON 形式
  打印到 stdout 后退出。

  主进程（web_ui / socketio 所在、gevent 已 patch 的进程）的 metrics_collector
  通过 subprocess 调用本 CLI，从而：
    - 绝不在主进程内 startJVM（否则 JPype/JVM 与 gevent hub 死锁 → 整个界面卡死）；
    - 复用 MetricsCollector 既有的 _connect / _collect_deep 逻辑，行为零回归。

  与 modules/jdbc_inspection_cli.py / jdbc_test_cli.py 同源设计：
  输入经 stdin 传入单个 JSON 对象，结果取 stdout 的「最后一个以 { 开头的行」解析。

调用方（metrics_collector._collect_metrics_subprocess）负责设置
  env['DBCheck_NO_GEVENT_PATCH']='1' 与 cwd=PROJECT_ROOT，本文件自身不触发任何 patch。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import sys
import os
import json
import time


def _ensure_project_root_on_path():
    """开发态直接执行本文件时确保项目根目录在 sys.path 上。"""
    if getattr(sys, 'frozen', False):
        return
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)


def _collect(inst: dict, db_type: str) -> dict:
    """在干净子进程内执行连接 + 深采，返回指标字典（含 available/error）。"""
    # 仅在此处懒导入 collector —— 本子进程不引入 gevent monkey-patch。
    from modules.pro.metrics_collector import JVM_METRICS_DB_TYPES, MetricsCollector
    from modules.ssh import DatabaseTunnelError, resolve_db_endpoint

    collector = MetricsCollector(db_path=':memory:')
    if db_type in JVM_METRICS_DB_TYPES:
        inst = dict(inst,
                    database=inst.get('database') or inst.get('service_name'),
                    service_name=inst.get('service_name') or inst.get('database'))
    conn = None
    try:
        tunneled = bool(inst.get('ssh_tunnel'))
        if tunneled and inst.get('jdbc_url'):
            raise DatabaseTunnelError('通过 SSH 连接时请使用主机、端口和库名或服务名')
        started = time.perf_counter()
        conn = collector._connect(inst)
        if conn is None:
            if not tunneled:
                return {'available': False, 'error': 'connect returned None'}
            host, port, _ = resolve_db_endpoint(inst)
            available, latency = collector._tcp_probe(host, port)
            return {'available': available, 'latency_ms': latency,
                    'latency_source': 'tcp_probe'}
        if tunneled and db_type == 'mongodb':
            conn.admin.command('ping')
        connect_ms = round((time.perf_counter() - started) * 1000, 2) if tunneled else None
        metrics = collector._collect_deep(db_type, conn)
        if not isinstance(metrics, dict):
            metrics = {}
        # 速率差分依赖 store 历史，子进程无历史，仅回当前值（前端图表仍可用 store 既有序列）。
        metrics['available'] = True
        if connect_ms is not None:
            metrics.update(latency_ms=connect_ms, latency_source='tunnel_connect')
        return metrics
    except Exception as e:  # noqa: BLE001
        return {'available': False, 'error': str(e)[:300]}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def main() -> int:
    _ensure_project_root_on_path()
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({'available': False, 'error': 'bad payload: %s' % e}))
        return 2

    from modules.db_types.connection_profile import connection_profile

    try:
        inst = connection_profile(payload.get('db_info') or {}, payload.get('db_type'))
        result = _collect(inst, inst['db_type'])
    except Exception as e:  # noqa: BLE001
        result = {'available': False, 'error': 'unexpected: %s' % str(e)[:300]}

    # 仅这一行是结构化结果；插件自身的 [Oracle JDBC] 调试 print 也进 stdout，
    # 但调用方只解析「最后一个以 { 开头的行」，即本行。
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
