# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Business grouping, dynamic schedules, scoped membership and report regressions."""


import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_operations import OperationsTest
from modules.access import Principal
from modules.operations.routes import prepare_batch
from modules.operations.groups import select, facets, normalize_filters, send_completion
from modules.operations.exports import inventory_xlsx
from modules.operations.imports import parse_excel
from docx import Document


class GroupChecks(OperationsTest):
    def asset(
        self,
        name="business-db",
        host="192.0.2.10",
        ci="CI-001",
        system="订单系统",
        environment="生产",
        owner="张三",
    ):
        record = self.create(name, host, ci)
        return self.inventory.save(
            self.actor,
            {
                "revision": record["revision"],
                "metadata": {
                    "system": system,
                    "environment": environment,
                    "owner": owner,
                },
            },
            record["id"],
        )

    def group(self, filters=None, shared=False):
        response = self.client.post(
            "/api/operations/groups",
            json={
                "name": "订单生产库",
                "filters": filters or {"system": ["订单系统"], "environment": ["生产"]},
                "shared": shared,
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["record"]

    def prepare(self, **data):
        with self.app.app_context():
            return prepare_batch(self.actor, data)

    def test_bulk_grouping_updates_shared_datasource_and_preserves_credentials(self):
        one = self.asset()
        two = self.asset("two", "192.0.2.11", "CI-002")
        response = self.client.put(
            "/api/operations/inventory/grouping",
            json={
                "instance_ids": [one["id"], two["id"]],
                "revisions": {one["id"]: one["revision"], two["id"]: two["revision"]},
                "metadata": {"environment": "测试", "owner": "李四"},
                "source": {"group": "订单测试"},
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["updated"], 2)
        for iid in (one["id"], two["id"]):
            current = self.inventory.get(iid, decrypt=True)
            self.assertEqual(current["source"]["password"], "=fixture-secret")
            self.assertEqual(current["metadata"]["environment"], "测试")
            self.assertEqual(current["source"]["group"], "订单测试")
        records = self.inventory.list([one["id"], two["id"]], decrypt=True)
        exported = parse_excel(inventory_xlsx(records, []).getvalue())
        self.assertEqual(exported[0]["metadata"]["environment"], "测试")

    def test_stale_bulk_grouping_rolls_back_every_row(self):
        one = self.asset()
        two = self.asset("two", "192.0.2.11", "CI-002")
        self.inventory.save(
            self.actor,
            {"revision": two["revision"], "metadata": {"owner": "已更新"}},
            two["id"],
        )
        response = self.client.put(
            "/api/operations/inventory/grouping",
            json={
                "instance_ids": [one["id"], two["id"]],
                "revisions": {one["id"]: one["revision"], two["id"]: two["revision"]},
                "metadata": {"environment": "测试"},
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.inventory.get(one["id"])["metadata"]["environment"], "生产"
        )

    def test_disabled_sources_are_excluded_from_group_execution_count(self):
        one = self.asset()
        self.inventory.save(
            self.actor,
            {"revision": one["revision"], "source": {"enabled": False}},
            one["id"],
        )
        group = self.group()
        current = self.client.get("/api/operations/groups").get_json()["records"][0]
        self.assertEqual(current["count"], 0)
        self.assertEqual(current["instance_ids"], [])
        with self.assertRaises(ValueError):
            self.prepare(group_ids=[group["id"]])

    def test_group_crud_preview_and_audit_versions(self):
        one = self.asset()
        self.asset("test-db", "192.0.2.11", "CI-002", environment="测试")
        group = self.group()
        records = self.client.get("/api/operations/groups").get_json()["records"]
        self.assertEqual(records[0]["instance_ids"], [one["id"]])
        preview = self.client.post(
            "/api/operations/groups/preview", json={"filters": group["filters"]}
        ).get_json()
        self.assertEqual([record["id"] for record in preview["records"]], [one["id"]])
        changed = self.client.put(
            "/api/operations/groups/" + group["id"],
            json={
                "name": "订单全部库",
                "filters": {"system": ["订单系统"]},
                "version": group["version"],
                "shared": True,
            },
        )
        self.assertEqual(changed.status_code, 200, changed.get_json())
        self.assertEqual(len(changed.get_json()["record"]["history"]), 2)
        stale = self.client.put(
            "/api/operations/groups/" + group["id"],
            json={"name": "stale", "filters": {}, "version": group["version"]},
        )
        self.assertEqual(stale.status_code, 409)
        current = changed.get_json()["record"]
        deleted = self.client.delete(
            "/api/operations/groups/" + group["id"],
            json={"version": current["version"]},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            self.client.get("/api/operations/groups").get_json()["records"], []
        )
        with self.assertRaises(LookupError):
            self.prepare(group_ids=[group["id"]])

    def test_dynamic_group_expands_on_each_execution_and_deduplicates(self):
        one = self.asset()
        group = self.group()
        spec, members = self.prepare(group_ids=[group["id"]], instance_ids=[one["id"]])
        self.assertEqual(len(members), 1)
        two = self.asset("new-db", "192.0.2.11", "CI-002")
        latest, members = self.prepare(group_ids=[group["id"]])
        self.assertEqual(
            set(member["instance_id"] for member in members), {one["id"], two["id"]}
        )
        self.assertEqual(len(spec["scope_snapshot"][0]["instance_ids"]), 1)
        self.assertEqual(len(latest["scope_snapshot"][0]["instance_ids"]), 2)
        self.assertEqual(latest["group_ids"], [group["id"]])

    def test_shared_group_respects_current_datasource_permissions(self):
        hidden = self.asset()
        group = self.group(shared=True)
        creator = self.actor
        self.actor = Principal(id=21, username="other", tenant_id=1, roles=["operator"])
        visible = self.asset("visible", "192.0.2.11", "CI-002")
        record = self.client.get("/api/operations/groups").get_json()["records"][0]
        self.assertEqual(record["instance_ids"], [visible["id"]])
        self.assertNotIn(hidden["id"], record["instance_ids"])
        self.assertFalse(record["can_edit"])
        denied = self.client.put(
            "/api/operations/groups/" + group["id"],
            json={"name": "forbidden", "filters": {}, "version": record["version"]},
        )
        self.assertEqual(denied.status_code, 403)
        _, members = self.prepare(group_ids=[group["id"]])
        self.assertEqual([member["instance_id"] for member in members], [visible["id"]])
        self.actor = Principal(
            id=31, username="other-tenant", tenant_id=2, roles=["admin"]
        )
        self.assertEqual(
            self.client.get("/api/operations/groups").get_json()["records"], []
        )
        with self.assertRaises(LookupError):
            self.prepare(group_ids=[group["id"]])

    def test_private_group_not_visible_to_other_operator(self):
        self.asset()
        self.group()
        self.actor = Principal(id=22, username="other", tenant_id=1, roles=["operator"])
        self.assertEqual(
            self.client.get("/api/operations/groups").get_json()["records"], []
        )

    def test_conditions_combine_or_within_field_and_across_fields(self):
        one = self.asset()
        two = self.asset("two", "192.0.2.11", "CI-002", environment="测试")
        three = self.asset("three", "192.0.2.12", "CI-003", system="财务系统")
        records = self.inventory.list([one["id"], two["id"], three["id"]])
        selected = select(
            records, {"system": ["订单系统", "财务系统"], "environment": ["生产"]}, []
        )
        self.assertEqual({item["id"] for item in selected}, {one["id"], three["id"]})
        with self.assertRaises(ValueError):
            normalize_filters({"environment": "生产"})
        with self.assertRaises(ValueError):
            normalize_filters({"password": ["secret"]})
        spec, members = self.prepare(
            selection_filters={"system": ["订单系统"], "environment": ["测试"]}
        )
        self.assertEqual([member["instance_id"] for member in members], [two["id"]])

    def test_no_matching_scope_does_not_fall_back_to_all_sources(self):
        self.asset()
        with self.assertRaises(ValueError):
            self.prepare(selection_filters={"environment": ["不存在的环境"]})

    def test_group_scope_and_environment_appear_in_report(self):
        one = self.asset()
        group = self.group()
        run = self.start([], group_ids=[group["id"]])
        final = self.wait(run)
        response = self.client.get("/api/operations/batches/" + run["id"] + "/report")
        self.assertEqual(response.status_code, 200)
        self.addCleanup(response.close)
        document = Document(io.BytesIO(response.data))
        text = (
            "\n".join(paragraph.text for paragraph in document.paragraphs)
            + "\n"
            + "\n".join(
                cell.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
            )
        )
        for value in ("订单生产库", "订单系统", "运行环境", "生产", "张三"):
            self.assertIn(value, text)
        self.assertEqual(
            final["payload"]["scope_snapshot"][0]["instance_ids"], [one["id"]]
        )

    def test_notification_is_opt_in_and_uses_configured_channels(self):
        one = self.asset()
        group = self.group()
        with patch(
            "modules.operations.groups.send_completion",
            return_value={"status": "sent", "channels": []},
        ) as sender:
            run = self.start([], group_ids=[group["id"]])
            self.wait(run)
            sender.assert_not_called()
            run = self.start([], group_ids=[group["id"]], notify_on_completion=True)
            self.wait(run)
            import time

            deadline = time.monotonic() + 2
            while not sender.called and time.monotonic() < deadline:
                time.sleep(0.05)
            sender.assert_called_once()
            self.assertEqual(
                sender.call_args.args[0]["payload"]["scope_snapshot"][0]["name"],
                "订单生产库",
            )
        with patch(
            "modules.notify._load_config",
            return_value={
                "email": {"recipients": ["fixture@example.invalid"]},
                "webhook": {"url": "https://fixture.invalid"},
            },
        ), patch("modules.notify.EmailNotifier") as email, patch(
            "modules.notify.WebhookNotifier"
        ) as webhook:
            email.return_value.send_report.return_value = True
            webhook.return_value.send_alert.return_value = True
            result = send_completion(self.service.store.get(run["id"]), "fixture.docx")
            self.assertEqual(result["status"], "sent")
            self.assertEqual(len(result["channels"]), 2)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        GroupChecks(name) for name in GroupChecks.__dict__ if name.startswith("test_")
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
