# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck
业务要求基线配置全量补全
========================

用途
----
把《数据库运维服务需求拆解清单》里对甲方的两项硬承诺落成可读配置：

1. **参数基线**（清单 M1-07 / 交付物 D-03《数据库参数表》）
   按四类在保产品——GBase 8a MPP / DM8 / Oracle / MSSQL——补齐
   内存·并发·IO·日志·超时·安全·高可用·运维 八个维度的关键参数基线。

2. **指标阈值基线**（清单 §4.6 阈值基线建议）
   20 类指标的 预警(三级) / 告警(二级) / 紧急(一级) 阈值，
   写入 ``server_thresholds``，由规则引擎在求值时注入为可用变量。

设计约束
--------
- **幂等**：``inspection_baseline`` 按 ``(db_type, param_name)`` 判重，
  已存在即跳过，绝不覆盖用户在 Web UI 里改过的阈值与新增项。
  ``server_thresholds`` 用 ``INSERT OR IGNORE``，同理保留用户修改。
- **单条失败隔离**：参数名写错只会让该条 SQL 在真库返回空集，巡检侧
  静默跳过，不影响其它基线与评分。
- **可预演**：``--dry-run`` 只打印将要插入的行，不写库。

用法
----
    python scripts/gen_business_baselines.py --dry-run
    python scripts/gen_business_baselines.py
    python scripts/gen_business_baselines.py --db data/inspection.db --report
"""

import argparse
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(PROJECT_ROOT, 'data', 'inspection.db')

# 允许以 `python scripts/gen_business_baselines.py` 直接运行时也能 import modules.*
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ═══════════════════════════════════════════════════════════════════════
#  数据结构统一由 modules/inspection/business_baseline_seed.py 提供
#
#  此处只做搬运，不重复定义——否则「重置基线」入口与「初始化/增量补齐」
#  入口又会拿到两份不同的清单，历史漂移会重现。
# ═══════════════════════════════════════════════════════════════════════

from modules.inspection.business_baseline_seed import (  # noqa: E402
    PARAM_BASELINES,
    METRIC_THRESHOLDS,
)

# ═══════════════════════════════════════════════════════════════════════
#  三、落库
# ═══════════════════════════════════════════════════════════════════════

def _existing_baseline_keys(cur):
    """返回已存在的 (db_type, param_name) 集合。"""
    cur.execute("SELECT db_type, param_name FROM inspection_baseline")
    return {(r[0], r[1]) for r in cur.fetchall()}


def _existing_threshold_keys(cur):
    """返回已存在的 server_thresholds.key 集合。"""
    cur.execute("SELECT key FROM server_thresholds")
    return {r[0] for r in cur.fetchall()}


def apply(db_path=DEFAULT_DB, dry_run=False):
    """幂等写入参数基线与指标阈值。

    :return: dict 统计
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    stats = {
        'baseline_inserted': 0,
        'baseline_skipped': 0,
        'threshold_inserted': 0,
        'threshold_skipped': 0,
        'by_db_type': {},
        'new_thresholds': [],
    }

    try:
        have_bl = _existing_baseline_keys(cur)
        have_th = _existing_threshold_keys(cur)

        # ── 参数基线 ──
        for db_type, items in PARAM_BASELINES.items():
            inserted = 0
            for (param, sql, op, expected, risk, zh, en) in items:
                key = (db_type, param)
                if key in have_bl:
                    stats['baseline_skipped'] += 1
                    continue
                if not dry_run:
                    cur.execute(
                        """
                        INSERT INTO inspection_baseline (
                            db_type, param_name, query_sql, operator,
                            expected_value, risk_level, description_zh, description_en
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (db_type, param, sql, op, expected, risk, zh, en),
                    )
                have_bl.add(key)
                inserted += 1
                stats['baseline_inserted'] += 1
            stats['by_db_type'][db_type] = inserted

        # ── 指标阈值 ──
        for (key, value, zh, en, section) in METRIC_THRESHOLDS:
            if key in have_th:
                stats['threshold_skipped'] += 1
                continue
            if not dry_run:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO server_thresholds
                        (key, value, value_str, description_zh, description_en)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (key, float(value), section, f'[§4.6 {section}] {zh}',
                     f'[§4.6 {section}] {en}'),
                )
            have_th.add(key)
            stats['threshold_inserted'] += 1
            stats['new_thresholds'].append((key, value, section))

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return stats


def _print_report(stats, dry_run):
    tag = '（预演，未写库）' if dry_run else ''
    print(f"\n{'=' * 68}")
    print(f"  业务基线配置全量补全 {tag}")
    print(f"{'=' * 68}")

    print("\n【一】参数基线 inspection_baseline")
    print(f"  新增 {stats['baseline_inserted']} 条 / 已存在跳过 {stats['baseline_skipped']} 条")
    for db_type, n in stats['by_db_type'].items():
        total = len(PARAM_BASELINES[db_type])
        print(f"    {db_type:12s} 补 {n:3d} 条（本次清单共 {total} 条）")

    print("\n【二】指标阈值 server_thresholds")
    print(f"  新增 {stats['threshold_inserted']} 条 / 已存在跳过 {stats['threshold_skipped']} 条")
    sections = {}
    for key, value, section in stats['new_thresholds']:
        sections.setdefault(section, []).append((key, value))
    for section, rows in sections.items():
        print(f"    §4.6 {section:10s} {len(rows)} 项")
    print(f"{'=' * 68}\n")


def main():
    ap = argparse.ArgumentParser(description='业务要求基线配置全量补全')
    ap.add_argument('--db', default=DEFAULT_DB, help='inspection.db 路径')
    ap.add_argument('--dry-run', action='store_true', help='只打印，不写库')
    ap.add_argument('--report', action='store_true', help='打印分节报告')
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"[FAIL] 目标库不存在: {args.db}")
        return 2

    stats = apply(db_path=args.db, dry_run=args.dry_run)
    if args.report:
        _print_report(stats, args.dry_run)
    else:
        print(f"[OK] baseline +{stats['baseline_inserted']} / "
              f"threshold +{stats['threshold_inserted']} "
              f"(skip {stats['baseline_skipped']}/{stats['threshold_skipped']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
