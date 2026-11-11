# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""只读审计巡检模板与基线SQL，不连接任何客户数据库。"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from modules.inspection.sql_safety import assert_inspection_sql


def audit(database):
    result = {
        "templates": 0,
        "baselines": 0,
        "non_sql": 0,
        "by_type": collections.Counter(),
        "blocked": [],
    }
    with sqlite3.connect(
        Path(database).resolve().as_uri() + "?mode=ro", uri=True
    ) as connection:
        queries = connection.execute(
            "SELECT t.db_type,q.id,q.query_sql FROM inspection_query q JOIN inspection_chapter c ON c.id=q.chapter_id JOIN inspection_template t ON t.id=c.template_id"
        )
        for kind, identifier, sql in queries:
            if kind in {"mongodb", "redis", "redis-cluster"}:
                result["non_sql"] += 1
                continue
            result["templates"] += 1
            result["by_type"][kind] += 1
            try:
                assert_inspection_sql(sql)
            except ValueError as error:
                result["blocked"].append(
                    dict(
                        source="template",
                        db_type=kind,
                        id=identifier,
                        reason=str(error),
                    )
                )
        for kind, name, sql in connection.execute(
            "SELECT db_type,param_name,query_sql FROM inspection_baseline WHERE enabled=1"
        ):
            if not sql or kind in {"mongodb", "redis", "redis-cluster"}:
                continue
            result["baselines"] += 1
            try:
                assert_inspection_sql(sql)
            except ValueError as error:
                result["blocked"].append(
                    dict(
                        source="baseline",
                        db_type=kind,
                        param_name=name,
                        reason=str(error),
                    )
                )
    result["by_type"] = dict(result["by_type"])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data/inspection.db")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.database)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output)
