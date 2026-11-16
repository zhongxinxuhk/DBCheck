# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import unittest
from types import SimpleNamespace

from modules.operations.inventory_sync import build_candidates


class InventorySyncCandidateTests(unittest.TestCase):
    def test_core_fields_are_auto_and_other_observed_fields_are_not(self):
        candidates = build_candidates(
            {
                "instance_name": "orcl",
                "charset": "AL32UTF8",
                "db_size": "1.03 GB",
                "archive_path": "/arch",
                "archive_mode": "ARCHIVELOG",
                "storage_mode": "file",
                "version_patch": "11.2.0.2.0 / 20240101",
                "backup_policy": "每日全备",
                "dr_status": "同城灾备",
                "cluster_attrs": "RAC 2 节点",
                "location": "机房 A",
            },
            observed_at="2026-11-17T10:20:30+00:00",
        )
        auto = candidates["auto_fields"]
        self.assertEqual(auto["instance_name"], "orcl")
        self.assertEqual(auto["file_size"], "1.03 GB")
        self.assertEqual(auto["version"], "11.2.0.2.0")
        self.assertEqual(auto["patch_level"], "20240101")
        self.assertEqual(auto["inventory_date"], "2026-11-17")
        self.assertNotIn("location", auto)
        self.assertEqual(
            build_candidates(
                {"version_patch": "11.2.0.2.0；补丁：未读取到"},
                observed_at="2026-11-17T10:20:30+00:00",
            )["auto_fields"]["version"],
            "11.2.0.2.0",
        )
        self.assertNotIn("backup_policy", auto)
        self.assertEqual(candidates["observed_fields"]["backup_policy"], "每日全备")
        self.assertEqual(candidates["observed_fields"]["disaster_recovery"], "同城灾备")
        self.assertEqual(candidates["observed_fields"]["cluster_attributes"], "RAC 2 节点")

    def test_host_fields_require_target_server_flag(self):
        basic = {"os_version": "Kylin V11", "host_model": "PowerEdge R750"}
        no_target = build_candidates(basic, system_info={})
        self.assertNotIn("os_version", no_target["observed_fields"])
        self.assertNotIn("host_model", no_target["observed_fields"])
        target = build_candidates(basic, system_info={"_is_target_server": True})
        self.assertEqual(target["auto_fields"]["os_version"], "Kylin V11")
        self.assertEqual(target["auto_fields"]["host_model"], "PowerEdge R750")

    def test_unknown_values_are_not_written(self):
        candidates = build_candidates(
            {
                "instance_name": "未采集",
                "charset": "",
                "db_size": "未登记",
                "version_patch": "未采集（补丁级别未读取到）",
            },
            observed_at="2026-11-17T10:20:30+00:00",
        )
        self.assertNotIn("instance_name", candidates["auto_fields"])
        self.assertNotIn("file_size", candidates["auto_fields"])
        self.assertNotIn("version", candidates["auto_fields"])
        self.assertEqual(candidates["auto_fields"]["inventory_date"], "2026-11-17")


class InventorySyncApplyTests(unittest.TestCase):
    def test_apply_passes_only_service_whitelist_to_inventory(self):
        from unittest.mock import patch

        captured = {}

        class FakeInventory:
            def record_inspection(self, actor, instance_id, task_id, report_path,
                                  auto_fields, observed_fields, **kwargs):
                captured.update(
                    actor=actor,
                    instance_id=instance_id,
                    task_id=task_id,
                    auto_fields=auto_fields,
                    observed_fields=observed_fields,
                    report_path=report_path,
                    **kwargs,
                )
                return {"status": "applied", "changed": sorted(auto_fields)}

        actor = SimpleNamespace(
            id=7, tenant_id=1, is_admin=False, roles=["operator"]
        )
        with patch("modules.access.get_principal", return_value=actor):
            from modules.operations.inventory_sync import apply_inspection_observation
            result = apply_inspection_observation(
                instance_id="abc123",
                task_id="task-1",
                tenant_id=1,
                user_id=7,
                basic_info={
                    "instance_name": "orcl",
                    "backup_policy": "每日全备",
                },
                system_info={"_is_target_server": True},
                report_path="/tmp/report.docx",
                inventory=FakeInventory(),
            )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(captured["auto_fields"]["instance_name"], "orcl")
        self.assertNotIn("backup_policy", captured["auto_fields"])
        self.assertEqual(captured["observed_fields"]["backup_policy"], "每日全备")

    def test_user_without_write_role_is_skipped(self):
        from unittest.mock import patch

        actor = SimpleNamespace(
            id=8, tenant_id=1, is_admin=False, roles=["viewer"]
        )
        with patch("modules.access.get_principal", return_value=actor):
            from modules.operations.inventory_sync import apply_inspection_observation
            result = apply_inspection_observation(
                instance_id="abc123",
                task_id="task-2",
                tenant_id=1,
                user_id=8,
                basic_info={"instance_name": "orcl"},
            )
        self.assertEqual(result["status"], "skipped")
        self.assertIn("写权限", result["reason"])


class InventoryObservationStoreTests(unittest.TestCase):
    def test_observation_is_idempotent_and_auto_update_is_audited(self):
        import os
        import sqlite3
        import tempfile
        from types import SimpleNamespace

        from modules.operations.inventory import Inventory

        class Manager:
            def __init__(self, path):
                self.instances_db = path

            def _bump_rev(self, path):
                return None

            def _load_data(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "instances.db")
            with sqlite3.connect(path) as db:
                db.execute(
                    'CREATE TABLE instances (id TEXT PRIMARY KEY, name TEXT, db_type TEXT, '
                    'host TEXT, port INTEGER, "user" TEXT, password TEXT, "database" TEXT, '
                    'service_name TEXT, tags TEXT, updated_at TEXT, enabled INTEGER)'
                )
                db.execute(
                    "INSERT INTO instances VALUES('abc','test','oracle','127.0.0.1',"
                    "1521,'system','','XE','','[]','2026-01-01',1)"
                )
            inventory = Inventory(Manager(path))
            actor = SimpleNamespace(id=1, tenant_id=1, is_admin=True,
                                    roles=["admin"], username="admin")
            record = inventory.get("abc")
            record = inventory.save(
                actor,
                {
                    "revision": record["revision"],
                    "source": {},
                    "metadata": {
                        "configuration_id": "CI-1",
                        "version": "old",
                        "backup_policy": "人工策略",
                    },
                },
                iid="abc",
            )
            result = inventory.record_inspection(
                actor,
                "abc",
                "task-1",
                "/tmp/report.docx",
                {"version": "11.2", "instance_name": "orcl"},
                {"backup_policy": "每日全备"},
                basic_info={"version_patch": "11.2"},
            )
            self.assertEqual(result["status"], "applied")
            self.assertEqual(
                set(result["changed"]), {"version", "instance_name"}
            )
            current = inventory.get("abc")
            self.assertEqual(current["metadata"]["version"], "11.2")
            self.assertEqual(current["metadata"]["backup_policy"], "人工策略")
            self.assertEqual(
                current["observation"]["observed_fields"]["backup_policy"],
                "每日全备",
            )

            repeated = inventory.record_inspection(
                actor,
                "abc",
                "task-1",
                "/tmp/report.docx",
                {"version": "11.3"},
                {},
            )
            self.assertEqual(repeated["status"], "already_applied")
            self.assertEqual(inventory.get("abc")["metadata"]["version"], "11.2")

            actions = [item["action"] for item in inventory.history(actor, {"abc"})]
            self.assertIn("inspection_auto_update", actions)


class InventoryFirstInspectionTests(unittest.TestCase):
    def test_first_inspection_creates_ledger_row_for_existing_instance(self):
        import os
        import sqlite3
        import tempfile
        from types import SimpleNamespace

        from modules.operations.inventory import Inventory

        class Manager:
            def __init__(self, path):
                self.instances_db = path

            def _bump_rev(self, path):
                return None

            def _load_data(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "instances.db")
            with sqlite3.connect(path) as db:
                db.execute(
                    'CREATE TABLE instances (id TEXT PRIMARY KEY, name TEXT, db_type TEXT, '
                    'host TEXT, port INTEGER, "user" TEXT, password TEXT, "database" TEXT, '
                    'service_name TEXT, tags TEXT, updated_at TEXT, enabled INTEGER)'
                )
                db.execute(
                    "INSERT INTO instances VALUES('abc','test','oracle','127.0.0.1',"
                    "1521,'system','','XE','','[]','2026-01-01',1)"
                )
            inventory = Inventory(Manager(path))
            actor = SimpleNamespace(id=1, tenant_id=1, is_admin=True,
                                    roles=["admin"], username="admin")
            result = inventory.record_inspection(
                actor,
                "abc",
                "task-new",
                "/tmp/report.docx",
                {"version": "11.2.0.2.0"},
                {},
                basic_info={"version_patch": "11.2.0.2.0"},
            )
            self.assertEqual(result["status"], "applied")
            current = inventory.get("abc")
            self.assertEqual(current["configuration_id"], "CI-abc")
            self.assertEqual(current["metadata"]["version"], "11.2.0.2.0")
