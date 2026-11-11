# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""工作流任务运行器 + 周期调度（「工作流任务」运行时的引擎）。

设计要点：
* ``TaskRunner`` 单例：以独立后台线程执行 Workflow 引擎，支持并发上限
  （默认 3）、优雅停止（cooperative cancellation，复用 Workflow.run 的取消点）、
  运行结果落库、进程重启恢复。
* ``WorkflowScheduler``：基于 croniter 的守护线程，按每个任务的 cron 表达式周期触发；
  进程刚启动时不会立即触发，避免所有 cron 任务在启动瞬间集中跑。
* 取消是「协作式」的：置位取消 Event 后，引擎会跑完当前节点、在下一节点前中止
  后续节点（已落库的动作不可回滚）。UI 应提示「停止 = 不再执行后续节点」。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

from __future__ import annotations

import threading
import os
from zoneinfo import ZoneInfo
import time
import traceback
from datetime import datetime
from typing import Any, Dict, Optional

try:
    from croniter import croniter

    _HAS_CRONITER = True
except Exception:  # croniter 缺失时仅禁用调度，不影响手动启停
    croniter = None  # type: ignore
    _HAS_CRONITER = False

from .workflow import Step, Workflow
from .hub import prepare_instance_inputs
from .workflow_store import get_workflow
from .workflow_task_store import (
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_STOPPED,
    get_task,
    list_tasks,
    update_status,
    claim_task,
    runtime_alive,
    request_stop,
)
from .workflow_run_store import (
    RUN_RUNNING,
    RUN_CANCELLED,
    RUN_DONE,
    RUN_ERROR,
    add_run,
    update_run,
)

DEFAULT_CONCURRENCY = 3
_SCHED_INTERVAL_SEC = 30
_STOP_JOIN_TIMEOUT = 120


class TaskRunner:
    """单例。后台执行工作流任务并维护状态机。"""

    _instance: Optional["TaskRunner"] = None

    def __new__(cls) -> "TaskRunner":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        self.app = None
        self._sched_last_cron = {}
        self._lock = threading.RLock()
        self._active: Dict[int, Dict[str, Any]] = (
            {}
        )  # task_id -> {thread, cancel, run_id}
        self._sem = threading.Semaphore(DEFAULT_CONCURRENCY)
        self._bootstrapped = False
        self._sched_thread: Optional[threading.Thread] = None
        self._sched_stop = threading.Event()
        self._sched_last_tick: Dict[int, datetime] = {}

    # ── 启动挂接 ───────────────────────────────────────
    def bootstrap(self, app=None) -> None:
        """应用启动时调用一次：恢复残留状态并拉起调度守护线程。幂等。"""
        if app is not None:
            self.app = app
        if os.environ.get("DBPULSE_INSPECTION_WORKER") == "1":
            return
        with self._lock:
            if self._bootstrapped:
                return
            self._bootstrapped = True
        # 重启恢复：把残留 running 的任务置为 stopped（内存 handle 已丢失）
        try:
            for t in list_tasks():
                if t["status"] == STATUS_RUNNING and not runtime_alive(t):
                    update_status(
                        t["id"],
                        STATUS_ERROR,
                        last_error="执行进程已退出，请复核上次任务结果",
                    )
                    from .workflow_run_store import list_runs

                    for run in list_runs(t["id"]):
                        if run["status"] == RUN_RUNNING:
                            update_run(
                                run["id"], RUN_ERROR, error="执行进程已退出，任务中断"
                            )
        except Exception:
            pass
        # 拉起调度守护线程
        if _HAS_CRONITER:
            self._sched_stop.clear()
            self._sched_thread = threading.Thread(
                target=self._scheduler_loop, name="wf-scheduler", daemon=True
            )
            self._sched_thread.start()

    # ── 启动 / 调度入口 ─────────────────────────────────
    def start(self, task_id: int) -> bool:
        """手动启动（trigger='manual'）。已在运行则忽略。返回是否成功发起。"""
        with self._lock:
            if task_id in self._active:
                return False
            task = get_task(task_id)
            if not task or task["status"] == STATUS_RUNNING:
                return False
            return self._spawn_locked(task_id, "manual")

    def run_once(self, task_id: int, trigger: str = "cron") -> bool:
        """供调度器调用：仅当未运行才触发。"""
        with self._lock:
            if task_id in self._active:
                return False
            task = get_task(task_id)
            if not task:
                return False
            return self._spawn_locked(task_id, trigger)

    def _spawn_locked(self, task_id: int, trigger: str) -> bool:
        if not self._sem.acquire(blocking=False):
            return False
        run = None
        claimed = False
        started = False
        try:
            claimed = claim_task(task_id)
            if not claimed:
                return False
            cancel = threading.Event()
            run = add_run(task_id, trigger)
            update_status(task_id, STATUS_RUNNING, last_run_at=run["started_at"])
            thread = threading.Thread(
                target=self._run_thread,
                args=(task_id, run["id"], cancel, trigger),
                name="wf-task-" + str(task_id),
                daemon=True,
            )
            self._active[task_id] = {
                "thread": thread,
                "cancel": cancel,
                "run_id": run["id"],
            }
            thread.start()
            started = True
            return True
        except Exception as error:
            self._active.pop(task_id, None)
            if claimed:
                update_status(task_id, STATUS_ERROR, last_error=str(error))
            if run:
                update_run(run["id"], RUN_ERROR, error=str(error))
            raise
        finally:
            if not started:
                self._sem.release()

    def _run_thread(
        self, task_id: int, run_id: int, cancel: threading.Event, trigger: str
    ) -> None:
        try:
            self._execute(task_id, run_id, cancel)
        except Exception as error:
            update_status(task_id, STATUS_ERROR, last_error=str(error))
            update_run(run_id, RUN_ERROR, error=str(error))
        finally:
            self._sem.release()
            with self._lock:
                self._active.pop(task_id, None)

    def _execute(self, task_id: int, run_id: int, cancel: threading.Event) -> None:
        started = time.time()
        task = get_task(task_id)
        if not task:
            return
        if task.get("task_type") == "batch_inspection":
            return self._execute_batch(task, run_id, cancel, started)
        wf = get_workflow(task["workflow_id"])
        if not wf:
            update_status(task_id, STATUS_ERROR, last_error="工作流不存在")
            update_run(run_id, RUN_ERROR, error="工作流不存在")
            return
        steps = [
            Step(
                id=str(s.get("id")),
                kind=s.get("kind", "specialist"),
                ref=s.get("ref", "") or "",
                args=s.get("args") or {},
                label=s.get("label") or str(s.get("id")),
            )
            for s in wf["steps"]
        ]
        edges = [
            (str(e[0]), str(e[1]))
            for e in wf["edges"]
            if isinstance(e, (list, tuple)) and len(e) == 2
        ]
        engine = Workflow(steps, edges)
        # 注入数据源连接信息 + 历史巡检报告，供各专家（如深度巡检专员）实时分析
        wf_inputs = prepare_instance_inputs(task["instance_id"])
        try:
            result = engine.run(
                goal=task.get("goal") or "",
                instance_id=task["instance_id"],
                inputs=wf_inputs,
                cancel_event=cancel,
            )
            dur = int((time.time() - started) * 1000)
            cancelled = bool(result.get("cancelled", False))
            ctx = result.get("ctx") or {}
            findings_count = len(ctx.get("findings", []) or [])
            plan_count = len(ctx.get("plan", []) or [])
            summary = self._summarize(result)
            if cancelled:
                update_status(task_id, STATUS_STOPPED)
                update_run(
                    run_id,
                    RUN_CANCELLED,
                    duration_ms=dur,
                    findings_count=findings_count,
                    plan_count=plan_count,
                    summary=summary,
                    outputs=result.get("outputs", []),
                )
            else:
                update_status(task_id, STATUS_STOPPED, last_duration_ms=dur)
                update_run(
                    run_id,
                    RUN_DONE,
                    duration_ms=dur,
                    findings_count=findings_count,
                    plan_count=plan_count,
                    summary=summary,
                    outputs=result.get("outputs", []),
                )
        except Exception as e:  # 引擎级异常 → 任务置 error，运行记录置 error
            dur = int((time.time() - started) * 1000)
            update_status(task_id, STATUS_ERROR, last_error=str(e))
            update_run(run_id, RUN_ERROR, duration_ms=dur, error=str(e))

    @staticmethod
    def _summarize(result: Dict[str, Any]) -> str:
        findings = (result.get("ctx") or {}).get("findings", []) or []
        items = []
        for f in findings[:5]:
            if isinstance(f, dict):
                items.append("[%s] %s" % (f.get("severity", ""), f.get("title", "")))
            elif hasattr(f, "severity"):
                items.append(
                    "[%s] %s" % (getattr(f, "severity", ""), getattr(f, "title", ""))
                )
        return "；".join(items) or "无发现"

    def _execute_batch(self, task, run_id, cancel, started):
        from modules.access import get_principal
        from modules.operations.routes import submit_batch, resources
        from modules.operations.batch import TERMINAL, summary

        if self.app is None:
            raise RuntimeError("整体巡检调度尚未绑定应用")
        principal = get_principal(task.get("owner_id"))
        if (
            principal is None
            or principal.tenant_id != task["tenant_id"]
            or not (principal.is_admin or "operator" in principal.roles)
        ):
            raise PermissionError("任务所属运维用户或权限已失效")
        if cancel.is_set():
            update_status(task["id"], STATUS_STOPPED)
            update_run(run_id, RUN_CANCELLED, summary="启动前已停止")
            return
        with self.app.app_context():
            spec = dict(
                task["batch_spec"],
                name=task["name"],
                request_key=f'work-task-{task["id"]}-run-{run_id}',
            )
            run = submit_batch(principal, spec)
            service = resources()[1]
            outputs = [
                {
                    "type": "batch_inspection",
                    "batch_run_id": run["id"],
                    "report_url": "/api/operations/batches/" + run["id"] + "/report",
                }
            ]
            update_run(run_id, RUN_RUNNING, outputs=outputs, summary="整体巡检执行中")
            while run["status"] not in TERMINAL:
                latest_task = get_task(task["id"])
                if cancel.is_set() or (latest_task or {}).get("stop_requested"):
                    service.store.request_cancel(run["id"])
                time.sleep(0.5)
                run = service.store.get(run["id"])
            counts = summary(run)
            report = service.ensure_report(run)
            message = f"纳入{counts['total']}个数据源，生成报告{counts['done']}个，失败{counts['failed']}个，采集缺项{counts['incomplete']}个"
            status = (
                RUN_CANCELLED
                if run["status"] == "cancelled"
                else (
                    RUN_DONE
                    if run["status"] == "completed" and not counts["incomplete"]
                    else RUN_ERROR
                )
            )
            outputs[0].update(status=run["status"], summary=counts)
            duration = int((time.time() - started) * 1000)
            update_status(
                task["id"],
                STATUS_ERROR if status == RUN_ERROR else STATUS_STOPPED,
                last_error=message if status == RUN_ERROR else "",
                last_duration_ms=duration,
            )
            update_run(
                run_id,
                status,
                duration_ms=duration,
                findings_count=sum(
                    (m.get("result") or {}).get("risk_count") or 0
                    for m in run["payload"]["members"]
                ),
                summary=message,
                report_path=str(report),
                outputs=outputs,
                error=message if status == RUN_ERROR else "",
            )

    # ── 停止 ───────────────────────────────────────
    def stop(self, task_id: int) -> bool:
        task = get_task(task_id)
        if not task:
            return False
        request_stop(task_id)
        with self._lock:
            record = self._active.get(task_id)
            if record:
                record["cancel"].set()
        if self.app is not None and task.get("task_type") == "batch_inspection":
            from .workflow_run_store import list_runs
            from modules.operations.routes import resources

            with self.app.app_context():
                for run in list_runs(task_id):
                    if run["status"] != RUN_RUNNING:
                        continue
                    for output in run.get("outputs") or []:
                        if output.get("batch_run_id"):
                            resources()[1].store.request_cancel(output["batch_run_id"])
        return True

    # ── 调度循环 ───────────────────────────────────────
    def _scheduler_loop(self) -> None:
        while not self._sched_stop.is_set():
            try:
                self._sched_tick()
            except Exception:
                pass
            self._sched_stop.wait(_SCHED_INTERVAL_SEC)

    def _sched_tick(self, now=None) -> None:
        if not _HAS_CRONITER:
            return
        now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        try:
            tasks = list_tasks()
        except Exception:
            return
        for t in tasks:
            cron = (t.get("cron") or "").strip()
            tid = t["id"]
            if t.get("stop_requested"):
                with self._lock:
                    if tid in self._active:
                        self._active[tid]["cancel"].set()
            if not cron or not t.get("schedule_enabled", True):
                self._sched_last_tick.pop(tid, None)
                self._sched_last_cron.pop(tid, None)
                continue
            if self._sched_last_cron.get(tid) != cron:
                self._sched_last_cron[tid] = cron
                self._sched_last_tick[tid] = now
                continue
            last = self._sched_last_tick.get(tid)
            if last is None:
                # 首次只记录基准时刻，避免启动瞬间集中触发
                self._sched_last_tick[tid] = now
                continue
            try:
                nxt = croniter(cron, last).get_next(datetime)  # type: ignore[arg-type]
                if nxt <= now:
                    if self.run_once(tid, "cron"):
                        self._sched_last_tick[tid] = now
                else:
                    self._sched_last_tick[tid] = last
            except Exception:
                continue


def get_runner() -> TaskRunner:
    return TaskRunner()
