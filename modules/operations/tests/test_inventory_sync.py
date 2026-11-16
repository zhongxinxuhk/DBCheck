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


class InventorySpecializedObservationTests(unittest.TestCase):
    def test_failed_queries_and_initialized_lists_are_not_empty_findings(self):
        from modules.operations.inventory_sync import build_specialized_observation
        specialized = build_specialized_observation({
            'tablespace': [], 'users': [], 'W17_users': [],
            '_query_status': {'W17_users': {'status': 'error', 'reason': 'denied'}},
        })
        self.assertEqual(specialized, {})

    def test_sqlserver_logical_file_names_are_recognized(self):
        from modules.operations.inventory_sync import build_specialized_observation
        specialized = build_specialized_observation({
            'mssql_tablespaces': [{'logical_name': 'master', 'size_mb': 8}],
        })
        row = specialized['tablespaces']['rows'][0]
        self.assertEqual(row, {'name': 'master', 'size': '8 MB'})

    def test_tablespace_and_account_rows_are_normalized_without_secrets(self):
        from modules.operations.inventory_sync import build_specialized_observation

        specialized = build_specialized_observation({
            "W06_tablespace_usage": [
                {"TABLESPACE_NAME": "SYSTEM", "TOTAL_MB": 100,
                 "FREE_MB": 10, "USED_PCT": 90},
                {"TABLESPACE_NAME": "SYSTEM", "TOTAL_MB": 200,
                 "FREE_MB": 20, "USED_PCT": 90},
            ],
            "W17_users_status": [
                {"USERNAME": "APP", "ACCOUNT_STATUS": "OPEN",
                 "EXPIRY_DATE": "2027-01-01", "DEFAULT_TABLESPACE": "USERS",
                 "PROFILE": "DEFAULT", "PASSWORD": "must-not-be-stored"},
            ],
        })
        tablespace = specialized["tablespaces"]
        self.assertEqual(tablespace["status"], "collected")
        self.assertEqual(tablespace["source"], "W06_tablespace_usage")
        self.assertEqual(len(tablespace["rows"]), 1)
        self.assertEqual(tablespace["rows"][0]["name"], "SYSTEM")
        self.assertEqual(tablespace["rows"][0]["size"], "100 MB")
        self.assertEqual(tablespace["rows"][0]["used_pct"], "90%")
        account = specialized["accounts"]["rows"][0]
        self.assertEqual(account["username"], "APP")
        self.assertEqual(account["status"], "OPEN")
        self.assertNotIn("PASSWORD", {key.upper() for key in account})
        self.assertNotIn("must-not-be-stored", str(specialized))

    def test_sqlserver_disabled_login_and_empty_result_semantics(self):
        from modules.operations.inventory_sync import build_specialized_observation

        specialized = build_specialized_observation({
            "W17_logins": [
                {"name": "sa", "is_disabled": 0, "type_desc": "SQL_LOGIN"},
                {"name": "old_login", "is_disabled": 1, "type_desc": "SQL_LOGIN"},
            ],
            "W06_tablespace_usage": [],
        })
        accounts = {row["username"]: row for row in specialized["accounts"]["rows"]}
        self.assertEqual(accounts["sa"]["status"], "启用")
        self.assertEqual(accounts["old_login"]["status"], "禁用")
        self.assertEqual(specialized["tablespaces"]["status"], "not_detected")
        self.assertEqual(specialized["tablespaces"]["rows"], [])

    def test_unrecognized_columns_are_not_reported_as_no_findings(self):
        from modules.operations.inventory_sync import build_specialized_observation

        specialized = build_specialized_observation({
            "W06_tablespace_usage": [{"unexpected_column": "value"}],
            "W17_users_status": [{"unexpected_column": "value"}],
        })
        self.assertEqual(specialized["tablespaces"]["status"], "unrecognized")
        self.assertEqual(specialized["accounts"]["status"], "unrecognized")

    def test_apply_passes_specialized_snapshot_without_auto_applying_it(self):
        from unittest.mock import patch

        captured = {}

        class FakeInventory:
            def record_inspection(self, actor, instance_id, task_id, report_path,
                                  auto_fields, observed_fields, **kwargs):
                captured.update(auto_fields=auto_fields, **kwargs)
                return {"status": "applied"}

        actor = SimpleNamespace(id=7, tenant_id=1, is_admin=True, roles=["admin"])
        specialized = {
            "tablespaces": {"status": "collected", "source": "W06_tablespace_usage",
                            "rows": [{"name": "SYSTEM", "used_pct": "90%"}],
                            "total": 1, "truncated": False},
            "accounts": {"status": "not_detected", "source": "W17_users_status",
                         "rows": [], "total": 0, "truncated": False},
        }
        with patch("modules.access.get_principal", return_value=actor):
            from modules.operations.inventory_sync import apply_inspection_observation
            result = apply_inspection_observation(
                instance_id="abc123", task_id="task-3", tenant_id=1, user_id=7,
                basic_info={}, observed_tables=specialized, inventory=FakeInventory(),
            )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(captured["observed_tables"], specialized)
        self.assertNotIn("tablespaces", captured["auto_fields"])
        self.assertNotIn("SYSTEM", str(captured["auto_fields"]))


class InventorySpecializedStoreTests(unittest.TestCase):
    def test_specialized_snapshot_is_stored_and_does_not_overwrite_tables(self):
        import os
        import sqlite3
        import tempfile

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
            actor = SimpleNamespace(id=1, tenant_id=1, is_admin=True, roles=["admin"])
            current = inventory.get("abc")
            inventory.save(
                actor,
                {"revision": current["revision"], "metadata": {
                    "tables": {"tablespaces": [{"name": "SYSTEM", "size": "人工值"}]},
                }},
                iid="abc",
            )
            specialized = {
                "tablespaces": {"status": "collected", "source": "W06_tablespace_usage",
                                "rows": [{"name": "SYSTEM", "size": "100 MB"}],
                                "total": 1, "truncated": False},
                "accounts": {"status": "not_collected", "source": "", "rows": [],
                             "total": 0, "truncated": False},
            }
            result = inventory.record_inspection(
                actor, "abc", "task-special", "/tmp/report.docx", {}, {},
                observed_tables=specialized,
            )
            self.assertEqual(result["status"], "unchanged")
            record = inventory.get("abc")
            self.assertEqual(record["observation"]["specialized"], specialized)
            self.assertEqual(
                record["metadata"]["tables"]["tablespaces"][0]["size"], "人工值"
            )


class InventoryObservationApplyTests(unittest.TestCase):
    def _inventory(self, directory):
        import os
        import sqlite3

        from modules.operations.inventory import Inventory

        class Manager:
            def __init__(self, path):
                self.instances_db = path

            def _bump_rev(self, path):
                return None

            def _load_data(self):
                return None

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
        return Inventory(Manager(path))

    def test_apply_only_adds_missing_rows_and_fills_empty_size(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            inventory = self._inventory(directory)
            actor = SimpleNamespace(id=1, tenant_id=1, is_admin=True,
                                    roles=["admin"], username="admin")
            current = inventory.get("abc")
            inventory.save(
                actor,
                {"revision": current["revision"], "metadata": {"tables": {
                    "tablespaces": [
                        {"name": "SYSTEM", "size": "900 MB"},
                        {"name": "EMPTY"},
                    ],
                    "accounts": [{"username": "APP", "purpose": "手工用途"}],
                }}},
                iid="abc",
            )
            current = inventory.get("abc")
            specialized = {
                "tablespaces": {
                    "status": "collected", "source": "W06_tablespace_usage",
                    "rows": [
                        {"name": "SYSTEM", "size": "1 GB"},
                        {"name": "EMPTY", "size": "5 MB"},
                        {"name": "NEW", "size": "2 MB"},
                    ],
                    "total": 2, "truncated": False,
                },
                "accounts": {
                    "status": "collected", "source": "W17_users_status",
                    "rows": [
                        {"username": "APP", "status": "OPEN"},
                        {"username": "NEWUSER", "status": "OPEN"},
                    ],
                    "total": 2, "truncated": False,
                },
            }
            inventory.record_inspection(
                actor, "abc", "task-apply", "/tmp/report.docx", {}, {},
                observed_tables=specialized,
            )
            result = inventory.apply_observation(
                actor, "abc", {
                    "task_id": "task-apply",
                    "revision": inventory.get("abc")["revision"],
                    "sections": {
                        "tablespaces": ["SYSTEM", "EMPTY", "NEW"],
                        "accounts": ["APP", "NEWUSER"],
                    },
                },
            )
            self.assertEqual(result["status"], "applied")
            tables = result["record"]["metadata"]["tables"]
            self.assertEqual(
                {row["name"] for row in tables["tablespaces"]}, {"SYSTEM", "EMPTY", "NEW"}
            )
            system = next(row for row in tables["tablespaces"] if row["name"] == "SYSTEM")
            self.assertEqual(system["size"], "900 MB")
            new = next(row for row in tables["tablespaces"] if row["name"] == "NEW")
            self.assertEqual(new["size"], "2 MB")
            app = next(row for row in tables["accounts"] if row["username"] == "APP")
            self.assertEqual(app["purpose"], "手工用途")
            self.assertEqual(
                {row["username"] for row in tables["accounts"]}, {"APP", "NEWUSER"}
            )
            self.assertEqual(result["sections"]["tablespaces"]["protected"], 1)
            self.assertEqual(result["sections"]["tablespaces"]["filled"], 1)
            self.assertEqual(result["sections"]["tablespaces"]["added"], 1)
            empty = next(
                row for row in tables["tablespaces"] if row["name"] == "EMPTY"
            )
            self.assertEqual(empty["size"], "5 MB")
            self.assertEqual(result["sections"]["accounts"]["added"], 1)
            actions = [item["action"] for item in inventory.history(actor, {"abc"})]
            self.assertIn("inspection_observation_apply", actions)

    def test_stale_task_id_is_rejected(self):
        import tempfile

        from modules.operations.inventory import Conflict

        with tempfile.TemporaryDirectory() as directory:
            inventory = self._inventory(directory)
            actor = SimpleNamespace(id=1, tenant_id=1, is_admin=True,
                                    roles=["admin"], username="admin")
            specialized = {
                "tablespaces": {
                    "status": "collected", "source": "W06",
                    "rows": [{"name": "NEW", "size": "2 MB"}],
                    "total": 1, "truncated": False,
                },
            }
            inventory.record_inspection(
                actor, "abc", "task-old", "", {}, {},
                observed_tables=specialized,
            )
            inventory.record_inspection(
                actor, "abc", "task-new", "", {}, {},
                observed_tables=specialized,
            )
            with self.assertRaises(Conflict):
                inventory.apply_observation(
                    actor, "abc", {
                        "task_id": "task-old",
                        "revision": inventory.get("abc")["revision"],
                        "sections": {"tablespaces": ["NEW"]},
                    },
                )
