# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""日志生命周期管理：按大小轮转、按份数/天数保留、总预算与磁盘余量保护。

设计目标（长期运行不撑爆磁盘）：
1. **按大小轮转**：每类日志到阈值即移入 ``data/logs/_archive/<family>/``，
   可 gzip 压缩；活跃文件始终从 0 开始，不会无限增长。
2. **按份数 + 天数保留**：归档只留最近 N 份、且不超过 N 天，两者取严。
3. **总预算**：全部日志（活跃 + 归档）超过预算时，从最旧的归档开始删，
   直到降到预算的 80%。
4. **磁盘余量保护**：可用空间低于 ``min_free_mb`` 时进入紧急清理，
   优先删最旧归档，最后才动当天的活跃文件（且只截断、不删除）。
5. **跨进程安全**：轮转用锁文件串行化；跨文件系统（Docker 独立卷）时
   退化为「复制 + 截断」，不丢当天日志。

策略来源：``data/log_policy.json``（可选）> 代码默认值；环境变量可覆盖
总预算与清理间隔。所有路径都限定在 ``data/logs`` 与 ``data/pro_data/monitor.log``，
不会误删用户数据。
"""


import fnmatch
import gzip
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from modules.core import paths

LOG_DIR = Path(paths.LOG_DIR)
MONITOR_LOG = Path(paths.PRO_DATA_DIR) / 'monitor.log'
ARCHIVE_ROOT = LOG_DIR / '_archive'
POLICY_FILE = Path(paths.DATA_DIR) / 'log_policy.json'

DEFAULT_POLICY: dict[str, Any] = {
    'total_budget_mb': 2048,
    'min_free_mb': 512,
    'sweep_interval_seconds': 1800,
    'compress': True,
    'families': {
        # 巡检任务日志：按天一个文件，单文件超 50MB 再切；保留 30 天
        'inspection': {'path': 'inspection-*.log', 'max_mb': 50,
                       'keep': 14, 'keep_days': 30, 'daily': True},
        # 定时调度日志
        'scheduler': {'path': 'scheduler.log', 'max_mb': 10,
                      'keep': 5, 'keep_days': 30},
        # 监控日志（位于 data/pro_data）
        'monitor': {'path': 'monitor.log', 'max_mb': 20,
                    'keep': 3, 'keep_days': 14},
        # AI 护栏审计：合规留痕，保留更久
        'ai_audit': {'path': 'ai_guard_audit.jsonl', 'max_mb': 20,
                     'keep': 10, 'keep_days': 180},
        # 手动/脚本启动的控制台重定向日志
        'console': {'path': 'web_service_restart.log', 'max_mb': 20,
                    'keep': 3, 'keep_days': 14},
        # Windows 服务（NSSM）输出
        'service': {'path': 'service.*.log', 'max_mb': 20,
                    'keep': 3, 'keep_days': 14},
    },
}

LIMITS = {
    'total_budget_mb': (64, 102400),
    'min_free_mb': (0, 102400),
    'sweep_interval_seconds': (60, 86400),
    'max_mb': (1, 1024),
    'keep': (1, 1000),
    'keep_days': (1, 3650),
}

_ENV = {
    'total_budget_mb': 'DBPULSE_LOG_BUDGET_MB',
    'min_free_mb': 'DBPULSE_LOG_MIN_FREE_MB',
    'sweep_interval_seconds': 'DBPULSE_LOG_SWEEP_SECONDS',
}

_lock = threading.Lock()
_policy_cache: dict[str, Any] | None = None
_policy_mtime: float | None = None
_last_sweep: dict[str, Any] = {}
_maintenance_thread: threading.Thread | None = None
_stop_event = threading.Event()

_log = logging.getLogger('dbcheck.system')


def _clamp(key, value):
    low, high = LIMITS.get(key, (None, None))
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _read_file():
    try:
        data = json.loads(POLICY_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print('[log] %s 解析失败，使用默认策略: %s' % (POLICY_FILE, exc), flush=True)
        return {}


def _build_policy() -> dict[str, Any]:
    file_cfg = _read_file()
    result: dict[str, Any] = {
        'compress': bool(file_cfg.get('compress', DEFAULT_POLICY['compress'])),
        'families': {},
    }
    for key in ('total_budget_mb', 'min_free_mb', 'sweep_interval_seconds'):
        value = file_cfg.get(key, DEFAULT_POLICY[key])
        env = os.environ.get(_ENV[key])
        if env not in (None, ''):
            value = env
        clamped = _clamp(key, value)
        result[key] = DEFAULT_POLICY[key] if clamped is None else clamped

    for name, defaults in DEFAULT_POLICY['families'].items():
        merged: dict[str, Any] = dict(defaults)
        incoming = (file_cfg.get('families') or {}).get(name)
        if isinstance(incoming, dict):
            merged.update({k: v for k, v in incoming.items()
                           if k in ('path', 'max_mb', 'keep', 'keep_days', 'daily')})
        for key in ('max_mb', 'keep', 'keep_days'):
            merged[key] = _clamp(key, merged[key]) or defaults[key]
        merged['daily'] = bool(merged.get('daily', False))
        result['families'][name] = merged
    return result


def policy() -> dict[str, Any]:
    """返回当前生效策略（文件 mtime 变化时自动重载）。"""
    global _policy_cache, _policy_mtime
    try:
        mtime = POLICY_FILE.stat().st_mtime
    except OSError:
        mtime = None
    with _lock:
        cache = _policy_cache
        if cache is None or mtime != _policy_mtime:
            cache = _build_policy()
            _policy_cache = cache
            _policy_mtime = mtime
    return cache


def reload_policy() -> dict[str, Any]:
    global _policy_cache, _policy_mtime
    with _lock:
        cache = _build_policy()
        _policy_cache = cache
        _policy_mtime = POLICY_FILE.stat().st_mtime if POLICY_FILE.exists() else None
    return cache


def _family_of(path):
    name = Path(path).name
    for family, cfg in policy()['families'].items():
        pattern = cfg['path']
        if any(ch in pattern for ch in '*?['):
            if fnmatch.fnmatch(name, pattern):
                return family
        elif name == pattern:
            return family
    return 'misc'


def family_policy(family: str) -> dict[str, Any]:
    return policy()['families'].get(family, {
        'max_mb': 10, 'keep': 3, 'keep_days': 14, 'compress': True,
    })


def _archive_dir(family):
    d = ARCHIVE_ROOT / family
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def _rotate_lock():
    """跨进程轮转锁（POSIX fcntl / Windows msvcrt）；拿不到锁也不阻断写入。"""
    lock_path = LOG_DIR / '.rotate.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        handle = open(lock_path, 'a+')
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        yield
    finally:
        if handle is not None:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                handle.close()
            except Exception:
                pass


def _move_or_copy(src, dest):
    """同盘直接改名；跨挂载点（Docker 独立卷）退化为复制 + 截断。"""
    try:
        os.replace(str(src), str(dest))
        return 'move'
    except OSError:
        shutil.copy2(str(src), str(dest))
        with open(src, 'wb'):
            pass
        return 'copy'


def rotate_file(path, family=None, now=None, force=False):
    """按大小轮转单个文件；未达阈值且非 force 时返回 None。"""
    path = Path(path)
    if not path.exists() or path.is_dir():
        return None
    fam = family or _family_of(path)
    cfg = family_policy(fam)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None
    if not force and size < cfg['max_mb'] * 1024 * 1024:
        return None

    with _rotate_lock():
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if not force and size < cfg['max_mb'] * 1024 * 1024:
            return None
        dest_dir = _archive_dir(fam)
        stamp = (now or datetime.now()).strftime('%Y%m%d-%H%M%S')
        dest = dest_dir / ('%s.%s%s' % (path.stem, stamp, path.suffix))
        serial = 1
        while dest.exists():
            dest = dest_dir / ('%s.%s-%d%s' % (path.stem, stamp, serial, path.suffix))
            serial += 1
        try:
            _move_or_copy(path, dest)
        except Exception as exc:
            _log.warning('日志轮转失败 %s: %s', path, exc)
            return None

        if cfg.get('compress', True):
            gz = dest.with_name(dest.name + '.gz')
            try:
                with open(dest, 'rb') as fin, gzip.open(gz, 'wb', compresslevel=6) as fout:
                    shutil.copyfileobj(fin, fout)
                dest.unlink()
                dest = gz
            except Exception as exc:
                _log.warning('日志压缩失败 %s: %s', dest, exc)

        _prune_family(fam, cfg)
        return dest


def _prune_family(family, cfg=None, now=None):
    """只保留最近 keep 份、且不超过 keep_days 天的归档。"""
    cfg = cfg or family_policy(family)
    d = ARCHIVE_ROOT / family
    if not d.is_dir():
        return 0
    now = now or datetime.now()
    files = [f for f in d.iterdir() if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    removed = 0
    keep_days = int(cfg['keep_days'])
    keep = int(cfg['keep'])
    for idx, f in enumerate(files):
        too_many = idx >= keep
        too_old = (now - datetime.fromtimestamp(f.stat().st_mtime)) > timedelta(days=keep_days)
        if too_many or too_old:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _active_files():
    """所有受管活跃文件（含 pro_data 下的 monitor.log）与按天日志。"""
    files = []
    if LOG_DIR.is_dir():
        for f in LOG_DIR.iterdir():
            if f.is_file() and f.suffix in ('.log', '.jsonl'):
                files.append(f)
    if MONITOR_LOG.is_file():
        files.append(MONITOR_LOG)
    return files


def _managed_size():
    total = 0
    for f in _active_files():
        try:
            total += f.stat().st_size
        except OSError:
            pass
    if ARCHIVE_ROOT.is_dir():
        for f in ARCHIVE_ROOT.rglob('*'):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    return total


def _archive_files_oldest_first():
    if not ARCHIVE_ROOT.is_dir():
        return []
    files = [f for f in ARCHIVE_ROOT.rglob('*') if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime)
    return files


def _delete_expired_daily(now, family='inspection'):
    """按天日志：日期早于 keep_days 的整份删除（当天文件不动）。"""
    cfg = family_policy(family)
    if not cfg.get('daily'):
        return 0
    removed = 0
    cutoff = (now - timedelta(days=int(cfg['keep_days']))).date()
    for f in LOG_DIR.glob(cfg['path']):
        if not f.is_file():
            continue
        try:
            stamp = f.stem.split('-')[-1]
            file_date = datetime.strptime(stamp, '%Y%m%d').date()
        except (ValueError, IndexError):
            file_date = datetime.fromtimestamp(f.stat().st_mtime).date()
        if file_date < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def sweep(now=None, free_mb=None):
    """执行一次清理：按份数/天数归档 → 总预算 → 磁盘余量保护。"""
    now = now or datetime.now()
    cfg = policy()
    stats = {'removed': 0, 'freed_bytes': 0, 'rotated': 0,
             'budget_bytes': cfg['total_budget_mb'] * 1024 * 1024,
             'before_bytes': 0, 'after_bytes': 0, 'emergency': False, 'ts': time.time()}

    # 注意：活跃文件不在这里轮转。handler / append_line 在写入侧按大小自转，
    # 清理线程再去动「已被 handler 打开」的文件，会在 Windows 上失败、在 POSIX 上
    # 让 handler 继续写已归档的 inode（日志"消失"）。清理只管归档与过期按天文件。

    stats['before_bytes'] = _managed_size()
    stats['removed'] += _delete_expired_daily(now)
    for family in cfg['families']:
        stats['removed'] += _prune_family(family, cfg['families'][family], now)
    stats['after_bytes'] = _managed_size()
    stats['freed_bytes'] = max(0, stats['before_bytes'] - stats['after_bytes'])

    # 总预算：超过则从最旧归档删到 80%
    target = int(stats['budget_bytes'] * 0.8)
    if _managed_size() > stats['budget_bytes']:
        for f in _archive_files_oldest_first():
            if _managed_size() <= target:
                break
            try:
                size = f.stat().st_size
                f.unlink()
                stats['removed'] += 1
                stats['freed_bytes'] += size
            except OSError:
                pass
        stats['after_bytes'] = _managed_size()

    # 磁盘余量保护
    try:
        free = free_mb if free_mb is not None else shutil.disk_usage(str(LOG_DIR)).free / (1024 * 1024)
    except OSError:
        free = None
    stats['free_mb'] = round(free, 1) if free is not None else None
    if free is not None and free < cfg['min_free_mb']:
        stats['emergency'] = True
        for f in _archive_files_oldest_first():
            if free >= cfg['min_free_mb']:
                break
            try:
                size = f.stat().st_size
                f.unlink()
                stats['removed'] += 1
                stats['freed_bytes'] += size
                free += size / (1024 * 1024)
            except OSError:
                pass
        stats['free_mb'] = round(free, 1)
        if free < cfg['min_free_mb']:
            _log.error('磁盘余量仍然不足（%.0fMB < %dMB），已无归档可删；请扩容或调小保留天数',
                       free, cfg['min_free_mb'])

    global _last_sweep
    stats['after_bytes'] = _managed_size()
    with _lock:
        _last_sweep = stats
    return stats


def append_line(path, line, family=None, encoding='utf-8'):
    """追加一行；写前按阈值轮转。供巡检任务日志、审计日志等无 fd 写入方使用。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fam = family or _family_of(path)
    cfg = family_policy(fam)
    try:
        if path.exists() and path.stat().st_size >= cfg['max_mb'] * 1024 * 1024:
            rotate_file(path, fam)
    except OSError:
        pass
    with open(path, 'a', encoding=encoding, errors='replace') as fh:
        fh.write(line if line.endswith('\n') else line + '\n')


class RotatingLogHandler(logging.Handler):
    """按大小轮转的文件 handler（单写者文件、多线程安全）。

    替代 ``logging.FileHandler``：写入量累计到阈值时关闭 → 归档 → 重开，
    活跃文件永远不会无限增长。
    """

    def __init__(self, path, family='misc', encoding='utf-8', fmt=None):
        super().__init__()
        self.path = Path(path)
        self.family = family
        self.encoding = encoding
        self._stream = None
        self._size = 0
        self.setFormatter(logging.Formatter(
            fmt or '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'))

    def _policy(self):
        return family_policy(self.family)

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = open(self.path, 'a', encoding=self.encoding, errors='replace')
        try:
            self._size = self.path.stat().st_size
        except OSError:
            self._size = 0

    def emit(self, record):  # logging 保证同一 handler 内串行
        try:
            if self._stream is None:
                self._open()
            msg = self.format(record) + '\n'
            self._stream.write(msg)
            self._stream.flush()
            self._size += len(msg.encode(self.encoding, errors='replace'))
            if self._size >= self._policy()['max_mb'] * 1024 * 1024:
                self._rotate()
        except Exception:
            self.handleError(record)

    def _rotate(self):
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            rotate_file(self.path, self.family)
        finally:
            self._open()

    def close(self):
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def status() -> dict[str, Any]:
    """给运维/接口用的存储概览：各类活跃文件、归档占用、预算与最近一次清理。"""
    cfg = policy()
    families = {}
    families_dirs = {}
    for family, fcfg in cfg['families'].items():
        files = []
        for pattern in [fcfg['path']]:
            base = MONITOR_LOG.parent if family == 'monitor' else LOG_DIR
            for f in base.glob(pattern) if base.is_dir() else []:
                if f.is_file():
                    files.append({'name': f.name, 'size': f.stat().st_size,
                                  'mtime': f.stat().st_mtime})
        d = ARCHIVE_ROOT / family
        backups = []
        if d.is_dir():
            for f in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.is_file():
                    backups.append({'name': f.name, 'size': f.stat().st_size,
                                    'mtime': f.stat().st_mtime})
        families[family] = {
            'policy': fcfg,
            'active': files,
            'backups': backups,
            'active_bytes': sum(x['size'] for x in files),
            'backup_bytes': sum(x['size'] for x in backups),
        }
        families_dirs[family] = len(backups)
    total = _managed_size()
    try:
        disk = shutil.disk_usage(str(LOG_DIR))
        free_mb = round(disk.free / (1024 * 1024), 1)
        total_mb = round(disk.total / (1024 * 1024), 1)
    except OSError:
        free_mb = total_mb = None
    return {
        'policy': cfg,
        'families': families,
        'total_bytes': total,
        'budget_bytes': cfg['total_budget_mb'] * 1024 * 1024,
        'usage_pct': round(total / (cfg['total_budget_mb'] * 1024 * 1024) * 100, 1),
        'disk_free_mb': free_mb,
        'disk_total_mb': total_mb,
        'archive_root': str(ARCHIVE_ROOT),
        'last_sweep': dict(_last_sweep) if _last_sweep else None,
    }


def _maintenance_loop():
    while not _stop_event.is_set():
        try:
            sweep()
        except Exception as exc:
            _log.warning('日志清理失败: %s', exc)
        interval = int(policy()['sweep_interval_seconds'])
        if _stop_event.wait(interval):
            return


def start_maintenance():
    """启动后台清理线程（幂等），并在后台先跑一次。"""
    global _maintenance_thread
    with _lock:
        if _maintenance_thread is not None and _maintenance_thread.is_alive():
            return _maintenance_thread
        _stop_event.clear()
        thread = threading.Thread(
            target=_maintenance_loop, daemon=True, name='log-maintenance')
        thread.start()
        _maintenance_thread = thread
    return thread


def stop_maintenance():
    _stop_event.set()
