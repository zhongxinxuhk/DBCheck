# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""运行时容量参数：巡检并发、监控采集并发与超时。

优先级（后者覆盖前者）：
1. 代码内默认值（``DEFAULTS``）
2. ``data/runtime_tuning.json``（运维可热改，重启进程生效）
3. 环境变量（``DBPULSE_*``，容器/服务单元里最方便）

取值范围统一在这里钳制，避免运维把并发调到把目标库打挂。
"""


import json
import os
import threading

from modules.core import paths

CONFIG_FILE = paths.DATA_DIR / 'runtime_tuning.json'

DEFAULTS = {
    'inspection': {
        # 定时巡检同时执行数。默认保持 1（严格串行，最稳）；大规模日巡检再调 2~4。
        'max_concurrent': 1,
        # 需要隔离 JVM 的类型单独限流，避免 JVM 抢 CPU 拖垮原生驱动巡检。
        'jvm_concurrent': 1,
        # 排队等待执行名额的上限，超时后本次触发跳过（不堆积）。
        'acquire_timeout': 1800,
        # 单次巡检最长执行时间；超时先请求中止，名额保留到线程真正退出。
        'watchdog_timeout': 1800,
        # 中止请求发出后，线程仍未退出的强制回收宽限期。
        'watchdog_grace': 300,
    },
    'monitor': {
        'interval': 10,
        'max_workers': 8,
        'instance_timeout': 8,
        'screen_interval': 15,
        'screen_workers': 8,
    },
}

# (min, max) 钳制区间；None 表示不限
LIMITS = {
    ('inspection', 'max_concurrent'): (1, 32),
    ('inspection', 'jvm_concurrent'): (1, 8),
    ('inspection', 'acquire_timeout'): (60, 7200),
    ('inspection', 'watchdog_timeout'): (60, 86400),
    ('inspection', 'watchdog_grace'): (30, 3600),
    ('monitor', 'interval'): (5, 300),
    ('monitor', 'max_workers'): (1, 64),
    ('monitor', 'instance_timeout'): (2, 60),
    ('monitor', 'screen_interval'): (5, 300),
    ('monitor', 'screen_workers'): (1, 64),
}

_ENV = {
    ('inspection', 'max_concurrent'): 'DBPULSE_INSPECTION_MAX_CONCURRENT',
    ('inspection', 'jvm_concurrent'): 'DBPULSE_INSPECTION_JVM_CONCURRENT',
    ('inspection', 'acquire_timeout'): 'DBPULSE_INSPECTION_ACQUIRE_TIMEOUT',
    ('inspection', 'watchdog_timeout'): 'DBPULSE_INSPECTION_WATCHDOG_TIMEOUT',
    ('inspection', 'watchdog_grace'): 'DBPULSE_INSPECTION_WATCHDOG_GRACE',
    ('monitor', 'interval'): 'DBPULSE_MONITOR_INTERVAL',
    ('monitor', 'max_workers'): 'DBPULSE_MONITOR_WORKERS',
    ('monitor', 'instance_timeout'): 'DBPULSE_MONITOR_INSTANCE_TIMEOUT',
    ('monitor', 'screen_interval'): 'DBPULSE_MONITOR_SCREEN_INTERVAL',
    ('monitor', 'screen_workers'): 'DBPULSE_MONITOR_SCREEN_WORKERS',
}

_lock = threading.Lock()
_cache = None
_cache_mtime = None


def _clamp(key, value):
    default = DEFAULTS[key[0]][key[1]]
    try:
        value = int(value) if isinstance(default, int) and not isinstance(default, bool) else float(value)
    except (TypeError, ValueError):
        return default
    low, high = LIMITS.get(key, (None, None))
    if low is not None and value < low:
        value = low
    if high is not None and value > high:
        value = high
    return value


def _read_file():
    try:
        raw = CONFIG_FILE.read_text(encoding='utf-8')
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:                      # 配置损坏不阻断启动
        print('[tuning][WARN] %s 解析失败，使用默认值: %s' % (CONFIG_FILE, exc), flush=True)
        return {}


def _mtime():
    try:
        return CONFIG_FILE.stat().st_mtime
    except OSError:
        return None


def _build():
    file_cfg = _read_file()
    effective = {}
    for section, defaults in DEFAULTS.items():
        effective[section] = {}
        for key, default in defaults.items():
            value = default
            section_cfg = file_cfg.get(section)
            if isinstance(section_cfg, dict) and key in section_cfg:
                value = section_cfg[key]
            env_name = _ENV.get((section, key))
            if env_name and os.environ.get(env_name) not in (None, ''):
                value = os.environ[env_name]
            effective[section][key] = _clamp((section, key), value)
    return effective


def reload():
    """强制重新读取配置文件（环境变量始终实时生效）。"""
    global _cache, _cache_mtime
    with _lock:
        _cache = _build()
        _cache_mtime = _mtime()
    return _cache


def snapshot():
    """返回当前生效的全部参数（配置文件 mtime 变化时自动重载）。"""
    global _cache, _cache_mtime
    mtime = _mtime()
    with _lock:
        if _cache is None or mtime != _cache_mtime:
            _cache = _build()
            _cache_mtime = mtime
        return {section: dict(values) for section, values in _cache.items()}


def get(section, key):
    """读取单个参数；未知参数抛 KeyError，避免拼写错误静默生效。"""
    if (section, key) not in LIMITS and key not in DEFAULTS.get(section, {}):
        raise KeyError('%s.%s' % (section, key))
    return snapshot()[section][key]


def describe():
    """一行摘要，便于启动日志确认当前生效的容量参数。"""
    cfg = snapshot()
    ins = cfg['inspection']
    mon = cfg['monitor']
    return ('巡检并发=%d(JVM %d)、排队上限=%ds、看门狗=%ds/宽限%ds；'
            '监控并发=%d、间隔=%ds、单实例超时=%ds；大屏并发=%d、间隔=%ds'
            % (ins['max_concurrent'], ins['jvm_concurrent'], ins['acquire_timeout'],
               ins['watchdog_timeout'], ins['watchdog_grace'],
               mon['max_workers'], mon['interval'], mon['instance_timeout'],
               mon['screen_workers'], mon['screen_interval']))
