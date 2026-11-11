# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""有界并发工具：按实例并发执行采集函数，避免单个慢实例阻塞整轮。

设计目标（超大实例规模下的监控采集）：
- 并发度可配置，默认 8，线程池按轮创建、轮结束即回收；
- 单个实例抛异常不影响其它实例；
- 整轮有截止时间，超时后取消未完成项并返回已完成部分（部分结果优于整轮失败）；
- 不依赖 gevent，纯标准库，可在后台线程与测试中直接复用。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED


def run_bounded(items, func, workers=8, round_timeout=None, name='bounded'):
    """并发执行 ``func(item)``，返回 ``(results, errors, timed_out)``。

    Args:
        items: 可迭代对象，元素会原样传给 ``func``。
        func: 单元素处理函数。
        workers: 并发线程数；<=1 时退化为顺序执行（行为与旧实现一致）。
        round_timeout: 整轮超时（秒）。到点仍未完成的元素记为超时，已完成的照常返回。
        name: 线程名前缀，便于排查。

    Returns:
        results:   ``{index: value}``，index 为元素在 items 中的下标。
        errors:    ``{index: Exception}``，单元素异常。
        timed_out: ``[index, ...]``，超时未完成的元素下标。

    Notes:
        超时后线程池以 ``shutdown(wait=False)`` 回收，卡在数据库调用里的线程会在
        其调用返回后自然结束；不会阻塞调用方，也不会让整轮采集无限等待。
    """
    items = list(items)
    results, errors, timed_out = {}, {}, []
    if not items:
        return results, errors, timed_out

    try:
        workers = max(1, min(int(workers or 1), len(items)))
    except (TypeError, ValueError):
        workers = 1

    if workers == 1:
        for idx, item in enumerate(items):
            try:
                results[idx] = func(item)
            except Exception as exc:          # 单个实例失败不影响整轮
                errors[idx] = exc
        return results, errors, timed_out

    pool = ThreadPoolExecutor(max_workers=workers,
                              thread_name_prefix='%s-%d' % (name, workers))
    futures = {pool.submit(func, item): idx for idx, item in enumerate(items)}
    pending = set(futures)
    deadline = None if not round_timeout else time.monotonic() + max(0.0, float(round_timeout))
    try:
        while pending:
            timeout = None
            if deadline is not None:
                timeout = max(0.0, deadline - time.monotonic())
                if timeout <= 0:
                    break
            done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                break
            for fut in done:
                idx = futures[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:
                    errors[idx] = exc
        if pending:
            timed_out = sorted(futures[fut] for fut in pending)
    finally:
        for fut in pending:
            fut.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    return results, errors, timed_out


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
