# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""First-round user workflows with isolated assets and synthetic inspection results."""


import io
import json
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_operations import OperationsTest
from modules.access import Principal
from modules.operations.exports import inventory_xlsx
from modules.operations.imports import InventoryImport
from modules.operations.policy import normalize_policy, window_open
from modules.operations.findings import compare_previous, issue_key
from openpyxl import load_workbook


class RoundOne(OperationsTest):
    def upload(self, content):
        return self.client.post(
            "/api/operations/inventory/import-preview",
            data={"file": (io.BytesIO(content), "inventory.xlsx")},
        )

    def workbook(self, records):
        return inventory_xlsx(records, []).getvalue()

    def test_invalid_excel_returns_actionable_error(self):
        response = self.upload(b"invalid excel")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Excel", response.get_json()["error"])

    def test_cluster_slots_honor_existing_stricter_limit_across_tasks(self):
        first = self.create()
        second = self.create("second", "192.0.2.44", "CI-002")
        with patch.object(self.service, "launch"):
            one = self.start([first["id"]])
            two = self.start([second["id"]])
        left, right = one["payload"]["members"][0], two["payload"]["members"][0]
        self.assertTrue(self.service.store.claim_member(one["id"], left, 1))
        self.assertFalse(self.service.store.claim_member(two["id"], right, 2))
        self.service.store.release_member(one["id"], first["id"])
        self.assertTrue(self.service.store.claim_member(two["id"], right, 2))
        for run in (one, two):
            self.service.store.update(run["id"], run["payload"], "cancelled")

    def test_excel_export_preview_apply_preserves_all_registers(self):
        first = self.create()
        record = self.inventory.get(first["id"], decrypt=True)
        record["source"]["name"] = "updated from excel"
        record["metadata"]["owner"] = "李四"
        response = self.upload(self.workbook([record]))
        self.assertEqual(response.status_code, 200, response.get_json())
        preview = response.get_json()
        self.assertEqual(preview["rows"][0]["action"], "更新")
        self.assertEqual(
            self.inventory.get(first["id"])["source"]["name"], "source-one"
        )
        result = self.client.post(
            "/api/operations/inventory/import-apply", json={"token": preview["token"]}
        )
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.get_json()["updated"], 1)
        after = self.inventory.get(first["id"], decrypt=True)
        self.assertEqual(after["source"]["name"], "updated from excel")
        self.assertEqual(after["source"]["password"], "=fixture-secret")
        self.assertEqual(after["metadata"]["tables"], record["metadata"]["tables"])
        replay = self.client.post(
            "/api/operations/inventory/import-apply", json={"token": preview["token"]}
        )
        self.assertEqual(replay.get_json(), result.get_json())
        self.assertEqual(len(self.manager.get_all_instances()), 1)

    def test_excel_new_asset_owns_and_syncs_datasource(self):
        original = self.create()
        record = self.inventory.get(original["id"], decrypt=True)
        record["configuration_id"] = "NEW-001"
        record["metadata"]["configuration_id"] = "NEW-001"
        record["source"].update(name="new asset", host="192.0.2.33")
        preview = self.upload(self.workbook([record])).get_json()
        self.assertTrue(preview["ok"], preview)
        self.assertEqual(preview["rows"][0]["action"], "新增")
        response = self.client.post(
            "/api/operations/inventory/import-apply", json={"token": preview["token"]}
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(self.manager.get_all_instances()), 2)
        self.assertIn(preview["rows"][0]["id"], self.owners)

    def test_excel_duplicate_connection_is_matched_for_update(self):
        original = self.create()
        record = self.inventory.get(original["id"], decrypt=True)
        record["configuration_id"] = "RENUMBERED"
        record["metadata"]["configuration_id"] = "RENUMBERED"
        preview = self.upload(self.workbook([record])).get_json()
        self.assertTrue(preview["ok"], preview)
        self.assertIn("匹配已有连接", preview["rows"][0]["action"])
        self.assertEqual(preview["rows"][0]["id"], original["id"])

    def test_excel_stale_preview_rolls_back_other_rows(self):
        first = self.create()
        second = self.create("second", "192.0.2.44", "CI-002")
        records = self.inventory.list([first["id"], second["id"]], decrypt=True)
        for record in records:
            record["source"]["name"] += "-excel"
        preview = self.upload(self.workbook(records)).get_json()
        self.inventory.save(
            self.actor,
            {
                "revision": second["revision"],
                "metadata": {"remarks": "edited after preview"},
            },
            second["id"],
        )
        response = self.client.post(
            "/api/operations/inventory/import-apply", json={"token": preview["token"]}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.inventory.get(first["id"])["source"]["name"], "source-one"
        )

    def test_excel_formula_and_foreign_preview_are_rejected(self):
        original = self.create()
        record = self.inventory.get(original["id"], decrypt=True)
        preview = self.upload(self.workbook([record])).get_json()
        self.actor = Principal(id=22, username="other", tenant_id=2, roles=["operator"])
        self.assertEqual(
            self.client.post(
                "/api/operations/inventory/import-apply",
                json={"token": preview["token"]},
            ).status_code,
            400,
        )
        book = load_workbook(io.BytesIO(self.workbook([record])))
        book.worksheets[0]["B4"] = "=1+1"
        output = io.BytesIO()
        book.save(output)
        self.assertEqual(self.upload(output.getvalue()).status_code, 400)

    def test_cluster_limit_and_window_cancellation(self):
        records = [
            self.create("node" + str(i), "192.0.2." + str(i + 1), "CI-" + str(i))
            for i in range(3)
        ]
        run = self.start([record["id"] for record in records], cluster_concurrency=1)
        self.wait(run)
        self.assertEqual(self.max_active, 1)
        with patch("modules.operations.batch.window_open", return_value=False):
            run = self.start(
                [records[0]["id"]],
                execution_window={"start": "02:00", "end": "03:00", "weekdays": [0]},
            )
            time.sleep(0.1)
            response = self.client.post(
                "/api/operations/batches/" + run["id"] + "/cancel", json={}
            )
            self.assertEqual(response.status_code, 200)
            final = self.wait(run)
            self.assertEqual(final["status"], "cancelled")
            self.assertEqual(final["payload"]["members"][0]["status"], "cancelled")

    def test_failed_only_retry_and_limits_propagate(self):
        good = self.create()
        broken = self.create("broken", "192.0.2.12", "CI-002")
        run = self.start(
            [good["id"], broken["id"]],
            query_timeout=3,
            row_limit=50,
            cluster_concurrency=2,
        )
        self.wait(run)
        with patch.object(self.service, "launch") as launch:
            result = self.client.post(
                "/api/operations/batches/" + run["id"] + "/retry",
                json={"request_key": "round1-retry-key"},
            )
            self.assertEqual(result.status_code, 202, result.get_json())
            retry = result.get_json()["run"]
            self.assertEqual(
                [member["instance_id"] for member in retry["payload"]["members"]],
                [broken["id"]],
            )
            self.assertEqual(retry["payload"]["query_timeout"], 3)
            self.assertEqual(retry["payload"]["row_limit"], 50)
            self.service.store.update(retry["id"], retry["payload"], "cancelled")
        self.assertEqual(
            self.client.post(
                "/api/operations/batches/" + run["id"] + "/cancel", json={}
            ).status_code,
            200,
        )

    def test_comparison_keeps_missing_collection_unverified(self):
        original = self.create()
        run = self.start([original["id"]])
        previous = self.wait(run)
        current = self.start([original["id"]])
        final = self.wait(current)
        self.assertEqual(len(final["payload"]["comparison"][0]["persistent"]), 1)
        payload = json.loads(json.dumps(final["payload"]))
        member = payload["members"][0]
        member["result"]["issues"] = []
        member["warnings"] = ["view not collected"]
        compared = compare_previous(self.service.store, final, payload)[0]
        self.assertEqual(len(compared["unverified"]), 1)
        self.assertEqual(compared["resolved"], [])
        member["warnings"] = []
        compared = compare_previous(self.service.store, final, payload)[0]
        self.assertEqual(len(compared["resolved"]), 1)

    def test_remediation_creation_edit_recheck_and_verified_close(self):
        original = self.create()
        run = self.start([original["id"]])
        final = self.wait(run)
        issue = final["payload"]["members"][0]["result"]["issues"][0]
        response = self.client.post(
            "/api/operations/findings",
            json={
                "run_id": run["id"],
                "instance_id": original["id"],
                "fingerprint": issue_key(issue),
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        record = response.get_json()["record"]
        update = self.client.put(
            "/api/operations/findings/" + record["id"],
            json={
                "version": record["version"],
                "state": "review",
                "assignee": "张三",
                "action": "fixture remediation",
                "deadline": "2026-12-01",
            },
        )
        self.assertEqual(update.status_code, 200, update.get_json())
        record = update.get_json()["record"]
        result = self.client.post(
            "/api/operations/findings/" + record["id"] + "/recheck",
            json={"request_key": "round1-recheck-key"},
        )
        self.assertEqual(result.status_code, 202, result.get_json())
        verification = self.wait(result.get_json()["run"])
        record = self.client.get("/api/operations/findings").get_json()["records"][0]
        blocked = self.client.put(
            "/api/operations/findings/" + record["id"],
            json={"version": record["version"], "state": "closed"},
        )
        self.assertEqual(blocked.status_code, 400, blocked.get_json())
        verification["payload"]["members"][0]["result"]["issues"] = []
        self.service.store.update(
            verification["id"], verification["payload"], "completed"
        )
        closed = self.client.put(
            "/api/operations/findings/" + record["id"],
            json={"version": record["version"], "state": "closed"},
        )
        self.assertEqual(closed.status_code, 200, closed.get_json())
        self.assertEqual(closed.get_json()["record"]["state"], "closed")
        response = self.client.get("/api/operations/findings/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "巡检整改清单", load_workbook(io.BytesIO(response.data)).sheetnames
        )


class WindowPolicy(unittest.TestCase):
    def test_overnight_window_uses_start_day_and_excludes_end(self):
        window = {"start": "23:00", "end": "02:00", "weekdays": [0]}
        self.assertTrue(window_open(window, datetime(2026, 11, 10, 1)))
        self.assertFalse(window_open(window, datetime(2026, 11, 10, 2)))
        self.assertFalse(window_open(window, datetime(2026, 11, 11, 1)))

    def test_metric_snapshot_only_uses_collected_values(self):
        from modules.operations.findings import snapshot_metrics

        self.assertEqual(snapshot_metrics({}), {})
        value = snapshot_metrics(
            {
                "basic_info_db": {"db_size": "10 GB"},
                "threads_connected": [{"Value": 12}],
                "slow_query_result": {"summary": {"current_long_running": 0}},
            }
        )
        self.assertEqual(
            value, {"数据库文件大小": "10 GB", "当前连接数": 12, "当前长耗时查询数": 0}
        )

    def test_limits_only_allow_bounded_positive_integers(self):
        for value in (True, 0, 16, "15"):
            with self.assertRaises(ValueError):
                normalize_policy({"query_timeout": value})
        self.assertEqual(
            normalize_policy({"query_timeout": 5, "row_limit": 30})["row_limit"], 30
        )


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for case in (RoundOne, WindowPolicy):
        suite.addTests(case(name) for name in case.__dict__ if name.startswith("test_"))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)
