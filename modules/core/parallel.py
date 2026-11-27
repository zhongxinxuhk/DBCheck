# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""后台采集的有界并发、整轮截止时间与部分结果。"""

from collections import deque
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED


class RoundExecutor:
    """Run rounds with a bounded number of active tasks, including older rounds."""

    def __init__(self, name, max_workers=64):
        self._max_workers = max_workers
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name)
        self._active = {}

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)

    def run(self, items, func, workers, round_timeout=None, *, key=None, on_done=None):
        """Return indexed successes, failures, and unfinished items.

        A single coordinator runs successive rounds. Work still executing after
        a deadline keeps its slot. The next round skips that source while the
        previous call is active, and can schedule other sources in free slots.
        """
        items = list(items)
        results, errors = {}, {}
        try:
            workers = max(1, min(int(workers or 1), self._max_workers))
        except (TypeError, ValueError):
            workers = 1
        deadline = (time.monotonic() + max(0.0, float(round_timeout))
                    if round_timeout else None)
        self._active = {future: item_key for future, item_key in self._active.items()
                        if not future.done()}
        older_keys = set(self._active.values())
        pending = deque(
            (idx, item, key(item) if key else idx) for idx, item in enumerate(items)
        )
        current = {}
        while pending or current:
            if deadline is not None and time.monotonic() >= deadline:
                break
            while pending and len(self._active) < workers:
                idx, item, item_key = pending.popleft()
                if item_key in older_keys:
                    continue
                future = self._pool.submit(func, item)
                self._active[future] = item_key
                current[future] = idx
            if not self._active or (not pending and not current):
                break
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            done, _ = wait(self._active, timeout=remaining, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                self._active.pop(future)
                if future not in current:
                    continue
                idx = current.pop(future)
                try:
                    results[idx] = future.result()
                except Exception as error:
                    errors[idx] = error
                if on_done is not None:
                    on_done(idx, results.get(idx), errors.get(idx))
        for future in current:
            future.cancel()
        timed_out = sorted(set(range(len(items))) - results.keys() - errors.keys())
        return results, errors, timed_out


def run_bounded(items, func, workers=8, round_timeout=None, name='bounded'):
    """Run a one-off round. Timed calls return partial results at the deadline."""
    executor = RoundExecutor(name)
    try:
        return executor.run(items, func, workers, round_timeout)
    finally:
        executor.close()


def round_budget(count, workers, item_timeout, minimum=5.0):
    """按实例数与并发度估算整轮预算：卡住的实例最多占满每个 worker 一次。

    ``count`` 个实例、``workers`` 并发、单实例最长 ``item_timeout`` 秒时，
    整轮最坏耗时约为 ``ceil(count / workers) * item_timeout``；再加少量余量，
    避免把正常但偏慢的实例误判为超时。
    """
    try:
        count = max(0, int(count))
        workers = max(1, int(workers))
        item_timeout = float(item_timeout)
    except (TypeError, ValueError):
        return float(minimum)
    waves = (count + workers - 1) // workers if count else 0
    return max(float(minimum), waves * item_timeout + 2.0)
