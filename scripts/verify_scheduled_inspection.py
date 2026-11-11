# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""工作任务定时整体巡检与巡检查询资源限制回归。"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import sys
import os
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import verify_operations as operations_test
from modules.intelligence import (
    workflow_task_store as tasks,
    workflow_run_store as runs,
)
from modules.intelligence.task_runner import TaskRunner
from modules.inspection import sql_safety as safety


class ScheduledInspectionTest(unittest.TestCase):
    def setUp(self):
        self.f = operations_test.OperationsTest("test_options_and_template_render")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        from modules.intelligence.views import intelligence_bp

        self.f.app.register_blueprint(intelligence_bp)
        for module, filename in [(tasks, "tasks.db"), (runs, "runs.db")]:
            for key, value in [
                ("_DB_PATH", str(self.f.root / filename)),
                ("_MIGRATED", False),
            ]:
                p = patch.object(module, key, value)
                p.start()
                self.addCleanup(p.stop)
        for name, value in [
            ("modules.pro.get_instance_manager", self.f.manager),
            ("modules.access.get_owner", None),
        ]:
            p = (
                patch(name, return_value=value)
                if value is not None
                else patch(name, side_effect=lambda kind, iid: self.f.owners.get(iid))
            )
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(TaskRunner, "_instance", None)
        p.start()
        self.addCleanup(p.stop)
        self.runner = TaskRunner()
        self.runner.app = self.f.app
        self.addCleanup(self.finish)

    def finish(self):
        self.runner._sched_stop.set()
        for item in list(self.runner._active.values()):
            item["thread"].join(10)

    def create(self, ids, **extra):
        data = dict(
            name="定时整体巡检",
            task_type="batch_inspection",
            workflow_id="",
            instance_id="",
            batch_spec=dict(instance_ids=ids, concurrency=1),
            cron="* * * * *",
            schedule_enabled=True,
        )
        data.update(extra)
        return self.f.client.post("/api/intelligence/workflow-tasks", json=data)

    def wait_run(self, tid):
        deadline = time.time() + 10
        while time.time() < deadline:
            rows = runs.list_runs(tid)
            if rows and rows[0]["status"] != "running":
                return rows[0]
            time.sleep(0.05)
        self.fail("工作任务未完成")

    def test_cron_runs_multiple_sources_and_aggregate_report(self):
        ids = [
            self.f.create("one", configuration_id="CI-1")["id"],
            self.f.create("two", host="192.0.2.11", configuration_id="CI-2")["id"],
        ]
        r = self.create(ids)
        self.assertEqual(r.status_code, 200, r.get_json())
        task = r.get_json()["task"]
        self.assertTrue(task["schedule_enabled"])
        self.assertIn("next_run_at", task)
        now = datetime(2026, 11, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.runner._sched_tick(now)
        self.assertEqual(self.f.dispatch_count, 0)
        self.runner._sched_tick(now + timedelta(minutes=1))
        run = self.wait_run(task["id"])
        self.assertEqual(run["status"], "done", run)
        self.assertEqual(run["trigger"], "cron")
        self.assertEqual(self.f.dispatch_count, 2)
        self.assertTrue(Path(run["report_path"]).is_file())
        self.assertEqual(run["outputs"][0]["summary"]["done"], 2)
        self.runner.stop(task["id"])
        self.assertFalse(tasks.get_task(task["id"])["schedule_enabled"])
        self.runner._sched_tick(now + timedelta(minutes=2))
        self.assertEqual(self.f.dispatch_count, 2)

    def test_invalid_cron_and_ownership(self):
        source = self.f.create()["id"]
        self.assertEqual(self.create([source], cron="bad").status_code, 400)
        task = self.create([source]).get_json()["task"]
        from modules.access import Principal

        self.f.actor = Principal(
            id=99, username="other", tenant_id=2, roles=["operator"]
        )
        self.assertEqual(
            self.f.client.get(
                "/api/intelligence/workflow-tasks/" + str(task["id"])
            ).status_code,
            403,
        )
        self.assertEqual(
            self.f.client.get("/api/intelligence/workflow-tasks").get_json()["tasks"],
            [],
        )

    def test_stop_cancels_queued_sources_and_keeps_report(self):
        ids = [
            self.f.create(
                str(i), host="192.0.2." + str(20 + i), configuration_id=str(i)
            )["id"]
            for i in range(3)
        ]
        task = self.create(ids).get_json()["task"]
        self.assertTrue(self.runner.run_once(task["id"], "manual"))
        deadline = time.time() + 5
        while not self.f.dispatch_count and time.time() < deadline:
            time.sleep(0.01)
        self.runner.stop(task["id"])
        run = self.wait_run(task["id"])
        self.assertEqual(run["status"], "cancelled", run)
        self.assertEqual(self.f.dispatch_count, 1)
        self.assertEqual(run["outputs"][0]["summary"]["cancelled"], 2)
        self.assertTrue(Path(run["report_path"]).exists())
        self.assertFalse(tasks.get_task(task["id"])["schedule_enabled"])

    def test_failed_source_does_not_produce_healthy_work_task(self):
        ids = [self.f.create("broken")["id"]]
        task = self.create(ids).get_json()["task"]
        self.runner.run_once(task["id"], "manual")
        run = self.wait_run(task["id"])
        self.assertEqual(run["status"], "error")
        self.assertTrue(Path(run["report_path"]).is_file())

    def test_legacy_tasks_migrate_without_losing_fields(self):
        db = sqlite3.connect(self.f.root / "tasks.db")
        db.execute(
            "CREATE TABLE workflow_tasks (id INTEGER PRIMARY KEY,name TEXT,workflow_id INTEGER,instance_id TEXT,goal TEXT,cron TEXT,status TEXT,last_run_at TEXT,last_error TEXT,last_duration_ms INTEGER,created_at TEXT,updated_at TEXT)"
        )
        db.execute(
            "INSERT INTO workflow_tasks VALUES(1,'保留旧任务',7,'legacy-source','goal','0 2 * * *','stopped',NULL,NULL,NULL,'2026-01-01','2026-01-01')"
        )
        db.commit()
        db.close()
        task = tasks.get_task(1)
        self.assertEqual(task["name"], "保留旧任务")
        self.assertEqual(task["workflow_id"], 7)
        self.assertEqual(task["task_type"], "workflow")
        self.assertTrue(task["schedule_enabled"])

    def test_worker_does_not_bootstrap_scheduler(self):
        with patch.dict(os.environ, DBPULSE_INSPECTION_WORKER="1"):
            self.runner.bootstrap(self.f.app)
        self.assertIsNone(self.runner._sched_thread)
        self.assertFalse(self.runner._bootstrapped)

    def test_claim_prevents_duplicate_execution_and_nonblocking_slots(self):
        tid = self.create([self.f.create()["id"]]).get_json()["task"]["id"]
        self.runner._sem = threading.Semaphore(0)
        start = time.monotonic()
        self.assertFalse(self.runner.run_once(tid, "manual"))
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertTrue(tasks.claim_task(tid))
        self.assertFalse(tasks.claim_task(tid))

    def test_same_endpoint_serializes_and_pages_are_separate(self):
        a = self.f.create(configuration_id="a")["id"]
        b = self.f.create("other", host="192.0.2.11", configuration_id="b")["id"]
        with patch.object(safety, "inspection_endpoint", return_value="same-endpoint"):
            run = self.f.start([a, b])
        self.f.wait(run)
        self.assertEqual(self.f.max_active, 1)
        for url, heading, absent in [
            ("/operations/inventory", "数据库运维台账", "运维台账与整体巡检"),
            (
                "/operations/inventory?tab=batch",
                "多数据源整体巡检",
                "运维台账与整体巡检",
            ),
        ]:
            html = self.f.client.get(url).get_data(as_text=True)
            self.assertIn(heading, html)
            self.assertNotIn(absent, html)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = list(range(1100))

    def execute(self, sql, *args, **kwargs):
        self.connection.sql.append(sql)
        return self

    def fetchmany(self, size):
        result = self.rows[:size]
        self.rows = self.rows[size:]
        return result

    def fetchall(self):
        raise AssertionError("不可无限fetchall")

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.sql = []
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class SQLSafetyTest(unittest.TestCase):
    def test_reject_write_business_scan_and_expensive_catalog_scan(self):
        for sql in [
            "DELETE FROM sys.tables",
            "SELECT * FROM orders",
            "SELECT * FROM sys.dm_db_index_physical_stats(DB_ID(),NULL,NULL,NULL,NULL)",
            "EXEC master.dbo.sp_readerrorlog 0,1",
            "SELECT * FROM sys.tables a CROSS JOIN sys.tables b",
        ]:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                safety.assert_inspection_sql(sql)

    def test_catalog_cte_and_expression_grammar(self):
        for sql in [
            "SELECT EXTRACT(YEAR FROM SYSDATE) FROM DUAL",
            "WITH a AS (SELECT * FROM sys.tables) SELECT * FROM a",
            "SELECT * FROM sys.tables a, sys.indexes b WHERE a.object_id=b.object_id",
        ]:
            self.assertTrue(safety.assert_inspection_sql(sql))
        with self.assertRaises(ValueError):
            safety.assert_inspection_sql("SELECT * FROM sys.tables a, orders b")

    def test_server_side_limits_and_bounded_fetch(self):
        for kind, query, fragment in [
            ("oracle", "SELECT * FROM V$DATABASE", "ROWNUM <= 1001"),
            ("mysql", "SELECT * FROM information_schema.tables", "LIMIT 1001"),
            ("postgresql", "SELECT * FROM pg_class", "LIMIT 1001"),
            ("sqlserver", "SELECT * FROM sys.tables", "MAXDOP 1"),
        ]:
            raw = FakeConnection()
            conn = safety.InspectionConnection(raw, kind)
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            self.assertEqual(len(rows), 1000)
            self.assertEqual(cursor.fetchall(), [])
            self.assertIn(fragment, raw.sql[-1])
            self.assertTrue(conn.warnings)

    def test_deadline_requests_driver_cancel(self):
        raw = FakeConnection()
        event = threading.Event()
        raw.cancel = event.set
        cursor = safety.InspectionConnection(raw, "mysql").cursor()
        cursor.raw.execute = lambda *args: event.wait(1)
        cursor.connection.configured = True
        with patch.object(safety, "QUERY_TIMEOUT_SECONDS", 0.03), self.assertRaises(
            TimeoutError
        ):
            cursor.execute("SELECT 1")
        self.assertTrue(event.is_set())
        self.assertEqual(raw.rollbacks, 1)
        with self.assertRaises(TimeoutError):
            cursor.execute("SELECT 1")

    def test_strict_fetch_cannot_fall_back_to_fetchall(self):
        from modules.core.dbapi_compat import fetch_rows

        class Eager:
            def fetchall(self):
                raise AssertionError("不能调用")

        with self.assertRaises(Exception):
            fetch_rows(Eager(), 10, strict=True)

    def test_cluster_jdbc_collection_cannot_bypass_limits(self):
        from modules.monitor import jdbc_collect_cli

        raw = FakeConnection()

        def cursor():
            value = FakeCursor(raw)
            value.rows = [(i,) for i in range(1100)]
            value.description = [("object_id",)]
            return value

        raw.cursor = cursor
        with patch.object(
            jdbc_collect_cli, "open_jdbc_connection", return_value=(raw, {})
        ):
            result = jdbc_collect_cli._collect(
                dict(
                    db_type="oracle_jdbc",
                    inspection_safe=True,
                    queries=["SELECT * FROM V$DATABASE", "DELETE FROM sys.tables"],
                )
            )
        self.assertEqual(len(result["q0"]["rows"]), 1000)
        self.assertIn("截断", result["q0"]["error"])
        self.assertIn("只读", result["q1"]["error"])
        self.assertFalse(any(sql.startswith("DELETE") for sql in raw.sql))

    def test_endpoint_lock_releases_and_reclaims_stale_pid(self):
        import tempfile
        from modules.core import paths

        with tempfile.TemporaryDirectory() as directory, patch.object(
            paths, "DATA_DIR", Path(directory)
        ):
            info = dict(host="192.0.2.1", port=3306, db_type="mysql")
            with safety.inspection_lease(info, "mysql"):
                with patch.object(
                    safety, "LEASE_WAIT_SECONDS", 0.01
                ), self.assertRaises(TimeoutError):
                    with safety.inspection_lease(info, "mysql"):
                        pass
            with safety.inspection_lease(info, "mysql"):
                pass
            db = sqlite3.connect(Path(directory) / "inspection_leases.db")
            db.execute(
                "INSERT INTO inspection_leases VALUES(?,?,?,?)",
                (safety.inspection_endpoint(info, "mysql"), 99999999, 0, "stale"),
            )
            db.commit()
            db.close()
            with safety.inspection_lease(info, "mysql"):
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
