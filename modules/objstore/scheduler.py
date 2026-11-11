# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""对象存储备份调度器（进程内守护线程，按「间隔分钟」触发）。

为什么不用 APScheduler：本项目 APScheduler 已被巡检调度占用（cron 语义），
对象存储备份只需要「上次成功时间 + 间隔」这种最朴素的周期语义，独立守护线程
每 30 秒 tick 一次足够，且不会与巡检调度的 job store 互相牵连；进程重启后
下一次 tick 会依据 ``last_run_at`` 自动补跑，无需持久化 job。

线程安全性：``engine.run_backup`` 自带非阻塞锁，重复触发会直接返回「已有任务
在执行」，因此 tick 与手动触发不会并发打包/上传。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from modules.objstore import store
from modules.objstore.crypto import CryptoError
from modules.objstore.s3_client import S3Error

logger = logging.getLogger(__name__)

# 可预期失败：配置/凭据/网络/口令，不值得每次打完整 traceback
EXPECTED_JOB_ERRORS = (ValueError, RuntimeError, CryptoError, S3Error)

TICK_SECONDS = 30
_start_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

_state: Dict[str, Any] = {
    "started": False,
    "running": False,
    "started_at": "",
    "last_tick": "",
    "last_error": "",
    "last_triggered_at": "",
}

# ── 手动/接口触发的一次性作业状态（避免长任务把 HTTP 请求挂死）──────
_job_lock = threading.Lock()
_job: Dict[str, Any] = {
    "id": "",
    "status": "idle",      # idle | running | success | failed
    "started_at": "",
    "finished_at": "",
    "error": "",
    "result": None,
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def trigger_backup(contents=None, trigger: str = "manual") -> Dict[str, Any]:
    """在后台线程执行一次备份并立即返回，前端轮询 /status 拿结果。

    同步执行的问题：报告目录可能到 GB 级，打包 + 上传耗时分钟级，浏览器/反代
    很容易先超时，用户看到「失败」但对象其实上传成功——这比慢更糟。
    """
    cfg = store.load_config(mask=False)
    if not (cfg.get("endpoint") and cfg.get("bucket")
            and cfg.get("access_key") and cfg.get("secret_key")):
        raise ValueError("对象存储未配置完整（Endpoint / Bucket / AccessKey / SecretKey）")
    selected = contents if isinstance(contents, list) and contents else cfg.get("contents")
    if not selected:
        raise ValueError("未选择任何备份内容")

    with _job_lock:
        if _job["status"] == "running":
            return {"started": False,
                    "message": "已有备份任务在执行，请稍后刷新状态",
                    "job": _job_summary()}
        job_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        _job.update({
            "id": job_id,
            "status": "running",
            "started_at": _now(),
            "finished_at": "",
            "error": "",
            "result": None,
        })
    thread = threading.Thread(
        target=_run_job, args=(job_id, contents, trigger),
        name=f"objstore-job-{job_id}", daemon=True,
    )
    thread.start()
    return {"started": True, "job_id": job_id, "job": _job_summary()}


def _run_job(job_id: str, contents, trigger: str) -> None:
    from modules.objstore import engine

    payload: Dict[str, Any]
    try:
        result = engine.run_backup(trigger=trigger, contents=contents)
        archive = result.get("archive") or {}
        payload = {
            "ok": True,
            "object_key": result.get("object_key", ""),
            "size": archive.get("size", 0),
            "size_fmt": archive.get("size_fmt", ""),
            "duration_s": result.get("duration_s", 0),
            "pruned": result.get("pruned", []),
        }
    except EXPECTED_JOB_ERRORS as exc:
        payload = {"ok": False, "error": str(exc)[:400]}
        logger.warning("对象存储备份作业 %s 失败（可预期）：%s", job_id, exc)
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)[:400]}
        logger.exception("对象存储备份作业 %s 失败（未预期错误）", job_id)
    with _job_lock:
        if _job.get("id") == job_id:
            _job.update({
                "status": "success" if payload.get("ok") else "failed",
                "finished_at": _now(),
                "error": payload.get("error", ""),
                "result": payload,
            })


def start() -> Dict[str, Any]:
    """幂等启动后台线程；由 register 阶段调用，异常不应阻断主程序启动。"""
    global _thread
    with _start_lock:
        if _thread and _thread.is_alive():
            return status()
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="objstore-scheduler", daemon=True)
        _thread.start()
        _state["started"] = True
        _state["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return status()


def stop() -> None:
    _stop.set()
    _state["started"] = False


def _loop() -> None:
    # 启动后先等一个 tick，避免与主程序初始化（建库、种子数据）抢 IO
    while not _stop.wait(TICK_SECONDS):
        try:
            tick()
        except Exception as exc:  # pragma: no cover - 守护线程必须吞异常
            _state["last_error"] = str(exc)[:300]
            logger.exception("对象存储定时备份 tick 失败")


def tick() -> Optional[Dict[str, Any]]:
    """检查是否到期；到期则执行一次备份。返回执行结果或 None。"""
    _state["last_tick"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cfg = store.load_config(mask=False)
    if not cfg.get("enabled"):
        return None
    if not cfg.get("bucket") or not cfg.get("access_key") or not cfg.get("secret_key"):
        return None

    from modules.objstore import engine

    if not engine.is_due(cfg):
        return None
    if _state["running"]:
        return None

    _state["running"] = True
    _state["last_triggered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        result = engine.run_backup(trigger="schedule")
        _state["last_error"] = ""
        return result
    except EXPECTED_JOB_ERRORS as exc:
        _state["last_error"] = str(exc)[:300]
        logger.warning("对象存储定时备份失败（可预期）：%s", exc)
        return None
    except Exception as exc:
        _state["last_error"] = str(exc)[:300]
        logger.exception("对象存储定时备份执行失败（未预期错误）")
        return None
    finally:
        _state["running"] = False


def _job_summary() -> Dict[str, Any]:
    with _job_lock:
        return dict(_job)


def status() -> Dict[str, Any]:
    from modules.objstore import engine

    cfg = store.load_config(mask=True)
    return {
        "started": bool(_state["started"]),
        "running": bool(_state["running"]),
        "started_at": _state["started_at"],
        "last_tick": _state["last_tick"],
        "last_triggered_at": _state["last_triggered_at"],
        "last_error": _state["last_error"],
        "enabled": bool(cfg.get("enabled")),
        "interval_minutes": cfg.get("interval_minutes"),
        "next_run_at": engine.next_run_at(cfg),
        "last_run_at": cfg.get("last_run_at", ""),
        "last_status": cfg.get("last_status", ""),
        "job": _job_summary(),
    }
