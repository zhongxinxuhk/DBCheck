# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""把巡检历史里遗留的 md5(db_type-host-port) instance_id 收敛到实例主键。

背景
----
早期 `modules/inspection/run.py::_record_inspection` 用
`md5(f"{db_type}-{host}-{port}")[:12]` 作为 instance_id 落库，而 instances 表
用的是 uuid[:12]。两套主键不一致导致：

  · `/api/pro/dashboard` 的风险分布统计到对不上实例的「幽灵行」；
  · `delete_instance()` 的 `DELETE ... WHERE instance_id = <uuid>` 永远删不掉
    这些历史行，删库后历史残留。

本脚本按「实例当前的 (db_type, host, port)」重算端点指纹，把命中的历史行
改写到实例主键；同端点多实例时不猜，跳过并打印。

仅依赖标准库（不 import modules.pro），可在任意 python3 下直接执行。

用法
----
    python3 scripts/migrate_inspection_instance_ids.py --dry-run
    python3 scripts/migrate_inspection_instance_ids.py
    python3 scripts/migrate_inspection_instance_ids.py --no-backup

幂等：已收敛的记录不会再被处理。默认先备份 pro_history.db。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, 'data', 'pro_data')


def load_instances(instances_db):
    """读取实例端点（只读，不做任何写入）"""
    conn = sqlite3.connect(f"file:{instances_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, name, db_type, host, port FROM instances"
        ).fetchall()
    finally:
        conn.close()
    return [
        {'id': r[0], 'name': r[1], 'db_type': (r[2] or '').lower(),
         'host': r[3], 'port': r[4]}
        for r in rows
    ]


def fingerprint(db_type, host, port):
    raw = f"{db_type}-{host}-{port}".encode()
    return hashlib.md5(raw).hexdigest()[:12]


def main() -> int:
    ap = argparse.ArgumentParser(description="收敛巡检历史 instance_id 到实例主键")
    ap.add_argument('--data-dir', default=DEFAULT_DATA_DIR, help='pro 数据目录')
    ap.add_argument('--dry-run', action='store_true', help='只报告，不写库')
    ap.add_argument('--no-backup', action='store_true', help='跳过备份')
    args = ap.parse_args()

    instances_db = os.path.join(args.data_dir, 'instances.db')
    history_db = os.path.join(args.data_dir, 'pro_history.db')
    for f in (instances_db, history_db):
        if not os.path.exists(f):
            print(f"[migrate] 找不到 {f}")
            return 2

    instances = load_instances(instances_db)
    print(f"[migrate] 数据目录 {args.data_dir}")
    print(f"[migrate] 已注册实例 {len(instances)} 个：")
    for inst in instances:
        print("  - {id}  {name:<14} {db_type:<8} {host}:{port}".format(**inst))

    known = {i['id'] for i in instances}
    cand, ambiguous = {}, set()
    for inst in instances:
        fp = fingerprint(inst['db_type'], inst['host'], inst['port'])
        if fp in cand and cand[fp] != inst['id']:
            ambiguous.add(fp)
            continue
        cand[fp] = inst['id']
    for fp in ambiguous:
        cand.pop(fp, None)

    conn = sqlite3.connect(history_db)
    try:
        rows = conn.execute(
            "SELECT instance_id, COUNT(*) FROM inspection_history GROUP BY instance_id"
        ).fetchall()
        trend_rows = conn.execute(
            "SELECT instance_id, COUNT(*) FROM instance_trend GROUP BY instance_id"
        ).fetchall()
    finally:
        conn.close()

    print("\n[migrate] inspection_history 主键分布")
    todo = []
    for iid, cnt in rows:
        if iid in known:
            print(f"  = {iid}  {cnt:>4} 行  (已是实例主键)")
        elif iid in cand:
            todo.append((iid, cand[iid], cnt))
            print(f"  → {iid}  {cnt:>4} 行  → {cand[iid]}  (可收敛)")
        elif iid in ambiguous:
            print(f"  ! {iid}  {cnt:>4} 行  (同端点多实例，跳过不猜)")
        else:
            print(f"  ! {iid}  {cnt:>4} 行  (无匹配实例，可能已删除，保持原样)")

    trend_todo = [(iid, cand[iid], cnt) for iid, cnt in trend_rows if iid not in known and iid in cand]

    if not todo and not trend_todo:
        print("\n[migrate] 无需迁移，已经是实例主键口径。")
        return 0

    if args.dry_run:
        print(f"\n[migrate] dry-run：将改写 history {sum(t[2] for t in todo)} 行"
              f" / trend {sum(t[2] for t in trend_todo)} 行；去掉 --dry-run 执行。")
        return 0

    if not args.no_backup:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup = f"{history_db}.bak-{stamp}-pre-idmigrate"
        shutil.copy2(history_db, backup)
        print(f"\n[migrate] 已备份 → {backup}")

    changed_hist = changed_trend = 0
    conn = sqlite3.connect(history_db)
    try:
        cur = conn.cursor()
        for old, new, _cnt in todo:
            cur.execute(
                "UPDATE inspection_history SET instance_id = ? WHERE instance_id = ?",
                (new, old))
            changed_hist += cur.rowcount
        for old, new, _cnt in trend_todo:
            # instance_trend 有 UNIQUE(instance_id, date)：先删掉与目标同日冲突的旧行
            cur.execute("""
                DELETE FROM instance_trend
                WHERE instance_id = ?
                  AND date IN (SELECT date FROM instance_trend WHERE instance_id = ?)
            """, (old, new))
            cur.execute(
                "UPDATE instance_trend SET instance_id = ? WHERE instance_id = ?",
                (new, old))
            changed_trend += cur.rowcount
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[migrate] 失败已回滚: {e}")
        return 1
    finally:
        conn.close()

    print(f"[migrate] 完成：history {changed_hist} 行 / trend {changed_trend} 行")
    for old, new, _cnt in todo:
        print(f"  {old} → {new}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
