# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""run_bounded / round_budget 的行为约束。"""


import threading
import time
import unittest

from modules.core.parallel import run_bounded, round_budget


class RunBoundedTest(unittest.TestCase):
    def test_empty_items(self):
        results, errors, timed_out = run_bounded([], lambda x: x, workers=4)
        self.assertEqual(results, {})
        self.assertEqual(errors, {})
        self.assertEqual(timed_out, [])

    def test_workers_capped_and_default_order(self):
        results, errors, timed_out = run_bounded([1, 2, 3], lambda x: x * 2, workers=32)
        self.assertEqual(results, {0: 2, 1: 4, 2: 6})
        self.assertEqual(errors, {})
        self.assertEqual(timed_out, [])

    def test_concurrency_never_exceeds_workers(self):
        lock = threading.Lock()
        state = {'now': 0, 'peak': 0}

        def work(_):
            with lock:
                state['now'] += 1
                state['peak'] = max(state['peak'], state['now'])
            time.sleep(0.05)
            with lock:
                state['now'] -= 1
            return True

        results, errors, _ = run_bounded(range(12), work, workers=3)
        self.assertEqual(len(results), 12)
        self.assertEqual(errors, {})
        self.assertLessEqual(state['peak'], 3)
        self.assertGreaterEqual(state['peak'], 2, '并发度未生效，仍是串行执行')

    def test_single_worker_is_sequential(self):
        order = []
        run_bounded([1, 2, 3], lambda x: order.append(x), workers=1)
        self.assertEqual(order, [1, 2, 3])

    def test_single_item_failure_does_not_abort_round(self):
        def work(x):
            if x == 2:
                raise RuntimeError('boom')
            return x

        results, errors, timed_out = run_bounded([1, 2, 3], work, workers=3)
        self.assertEqual(results, {0: 1, 2: 3})
        self.assertIn(1, errors)
        self.assertIsInstance(errors[1], RuntimeError)
        self.assertEqual(timed_out, [])

    def test_round_timeout_returns_partial_results(self):
        def work(x):
            if x == 1:
                time.sleep(0.6)
            return x

        started = time.monotonic()
        results, errors, timed_out = run_bounded(
            [0, 1], work, workers=2, round_timeout=0.15)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5, '整轮未按截止时间返回')
        self.assertEqual(results.get(0), 0)
        self.assertEqual(timed_out, [1])
        self.assertEqual(errors, {})


class RoundBudgetTest(unittest.TestCase):
    def test_budget_scales_with_waves(self):
        self.assertEqual(round_budget(1, 8, 8), 10.0)          # 最少 5s，按 1 波算
        self.assertEqual(round_budget(8, 8, 8), 10.0)
        self.assertEqual(round_budget(16, 8, 8), 18.0)         # 2 波 → 8*2+2
        self.assertEqual(round_budget(100, 8, 5), 67.0)        # 13 波 → 5*13+2

    def test_budget_handles_bad_input(self):
        self.assertEqual(round_budget(0, 8, 8), 5.0)
        self.assertEqual(round_budget('x', 'y', 'z'), 5.0)


if __name__ == '__main__':
    unittest.main()
