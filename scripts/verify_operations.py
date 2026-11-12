# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""数据库运维中台的资产台账与多数据源巡检。"""


import copy
import ast
import re
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cryptography.fernet import Fernet
from flask import Flask, g, jsonify, request
from openpyxl import load_workbook
from docx import Document
from modules.access import Principal
from modules.pro.instance_manager import InstanceManager
from modules.operations.routes import bp, resources
from modules.operations.batch import TERMINAL, summary
from modules.operations.inventory import Inventory, Conflict
from modules.clusters.store import ClusterStore


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dbpulse-operations-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cipher = Fernet(Fernet.generate_key())
        for name in (
            "modules.pro.instance_manager._get_fernet",
            "modules.operations.inventory._get_fernet",
        ):
            p = patch(name, return_value=self.cipher)
            p.start()
            self.addCleanup(p.stop)
        self.manager = InstanceManager(str(self.root / "sources"))
        self.actor = Principal(
            id=11, username="operator", tenant_id=1, roles=["operator"]
        )
        self.owners = {}
        self.app = Flask(__name__, template_folder=str(ROOT / "web_templates"))
        self.app.secret_key = "fixture-only"
        self.clusters = ClusterStore(self.root / "clusters.db")
        self.app.config.update(
            OPERATIONS_INSTANCE_MANAGER=self.manager,
            OPERATIONS_DB_PATH=self.root / "operations.db",
            OPERATIONS_REPORTS_DIR=self.root / "reports",
            CLUSTER_STORE=self.clusters,
        )
        self.app.register_blueprint(bp)
        self.tasks = {}
        self.active = 0
        self.max_active = 0
        self.dispatch_count = 0
        self.guard = threading.Lock()

        @self.app.before_request
        def identity():
            g.security_principal = self.actor

        @self.app.route("/api/inspection/templates")
        def templates():
            return jsonify(success=True, data=[])

        @self.app.route("/api/start_inspection", methods=["POST"])
        def api_start_inspection():
            value = request.get_json()
            iid = value["datasource_id"]
            source = self.manager.get_instance_decrypted(iid)
            self.assertIsNotNone(g.security_principal)
            self.assertTrue(value["isolate_process"])
            self.assertEqual(value["db_type"], source["db_type"])
            tid = "task-" + str(self.dispatch_count)
            self.dispatch_count += 1
            self.tasks[tid] = {"status": "running"}
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)

            def finish():
                try:
                    if source["name"] == "broken":
                        self.tasks[tid] = {
                            "status": "error",
                            "error_msg": "fixture connection failure",
                        }
                        return
                    folder = self.root / "reports"
                    folder.mkdir(exist_ok=True)
                    path = folder / (tid + ".docx")
                    doc = Document()
                    doc.add_heading("明细-" + source["name"], 0)
                    doc.add_paragraph("目标地址 " + source["host"])
                    doc.add_picture(str(ROOT / "assets/brand/dbcheck_logo.png"))
                    t = doc.add_table(rows=2, cols=2)
                    t.cell(0, 0).text = "字段（中文说明）"
                    t.cell(1, 0).text = "unique-detail-" + iid
                    t.cell(1, 1).text = source["host"]
                    doc.save(path)
                    self.tasks[tid] = {
                        "status": "done",
                        "report_file": str(path),
                        "result": {
                            "health_score": 90,
                            "risk_count": 1,
                            "issues": [{"description": "fixture-risk-" + iid}],
                        },
                        "log": (
                            ["部分视图未采集"] if source["name"] == "incomplete" else []
                        ),
                    }
                finally:
                    with self.guard:
                        self.active -= 1

            timer = threading.Timer(0.08, finish)
            timer.start()
            return jsonify(ok=True, task_id=tid)

        def owner_set(kind, iid, principal):
            owner = {
                "owner_user_id": principal.id,
                "owner_tenant_id": principal.tenant_id,
                "scope": "private",
            }
            self.owners[iid] = owner
            return owner

        patches = {
            "modules.operations.routes.get_owner": lambda kind, iid: self.owners.get(
                iid
            ),
            "modules.operations.routes.set_owner": owner_set,
            "modules.operations.routes.remove_owner": lambda kind, iid: self.owners.pop(
                iid, None
            ),
            "modules.operations.routes.filter_visible": lambda actor, rows, kind: [
                r for r in rows if self.allowed(actor, r["id"])
            ],
            "modules.access.get_principal": lambda uid: (
                self.actor if uid == self.actor.id else None
            ),
        }
        for name, fn in patches.items():
            p = patch(name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        self.client = self.app.test_client()
        with self.app.app_context():
            self.inventory, self.service, _ = resources()
        self.service.task = self.tasks.get

    def allowed(self, actor, iid):
        owner = self.owners.get(iid)
        return bool(
            owner
            and owner["owner_tenant_id"] == actor.tenant_id
            and (owner["owner_user_id"] == actor.id or actor.is_admin)
        )

    def create(self, name="source-one", host="192.0.2.10", configuration_id="CI-001"):
        response = self.client.post(
            "/api/operations/inventory",
            json={
                "source": {
                    "name": name,
                    "db_type": "mysql",
                    "host": host,
                    "port": 3306,
                    "user": "fixture-user",
                    "password": "=fixture-secret",
                },
                "metadata": {
                    "configuration_id": configuration_id,
                    "project_id": "P-001",
                    "system": "业务系统",
                    "business": "测试业务",
                    "owner": "张三",
                    "cluster_id": "cluster-one",
                    "node_count": 2,
                    "tables": {
                        "database_parameters": [
                            {"name": "max_connections", "value": 300}
                        ],
                        "tablespaces": [
                            {
                                "name": "user-space",
                                "size": "10 GB",
                                "autoextend": "开启",
                            }
                        ],
                        "accounts": [
                            {
                                "username": "app",
                                "purpose": "应用",
                                "privileges": "SELECT",
                                "owner": "张三",
                            }
                        ],
                        "backup_policies": [
                            {
                                "data_type": "全量",
                                "frequency": "每日",
                                "window": "02:00-03:00",
                            }
                        ],
                    },
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["record"]

    def start(self, ids, **kwargs):
        payload = dict(
            instance_ids=ids,
            request_key="request-" + os.urandom(8).hex(),
            concurrency=2,
            **kwargs
        )
        response = self.client.post("/api/operations/batches", json=payload)
        self.assertEqual(response.status_code, 202, response.get_json())
        return response.get_json()["run"]

    def wait(self, run):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = self.service.store.get(run["id"])
            path = self.root / "reports" / ("整体巡检报告_" + run["id"] + ".docx")
            if value["status"] in TERMINAL and (
                path.is_file() or value["payload"].get("report_error")
            ):
                self.assertFalse(
                    value["payload"].get("report_error"),
                    value["payload"].get("report_error"),
                )
                return value
            time.sleep(0.1)
        self.fail("fixture batch did not complete")

    def test_inventory_crud_uses_one_datasource_and_detects_stale_edits(self):
        record = self.create()
        iid = record["id"]
        self.assertEqual(len(self.manager.get_all_instances()), 1)
        self.assertEqual(
            self.manager.get_instance_decrypted(iid)["password"], "=fixture-secret"
        )
        self.assertNotIn("password", record["source"])
        response = self.client.put(
            "/api/operations/inventory/" + iid,
            json={
                "revision": record["revision"],
                "source": {"host": "192.0.2.20"},
                "metadata": {"location": "机房A"},
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        updated = response.get_json()["record"]
        self.assertEqual(self.manager.get_instance(iid)["host"], "192.0.2.20")
        self.assertEqual(
            self.manager.get_instance_decrypted(iid)["password"], "=fixture-secret"
        )
        self.assertEqual(len(self.manager.get_all_instances()), 1)
        stale = self.client.put(
            "/api/operations/inventory/" + iid,
            json={"revision": record["revision"], "metadata": {"owner": "李四"}},
        )
        self.assertEqual(stale.status_code, 409)
        self.manager.update_instance(iid, {"name": "edited-through-datasource"})
        self.assertEqual(
            self.client.get("/api/operations/inventory/" + iid).get_json()["record"][
                "source"
            ]["name"],
            "edited-through-datasource",
        )
        stale = self.client.delete(
            "/api/operations/inventory/" + iid, json={"revision": updated["revision"]}
        )
        self.assertEqual(stale.status_code, 409)
        fresh = self.inventory.get(iid)
        self.assertEqual(fresh["metadata"]["location"], "机房A")
        response = self.client.delete(
            "/api/operations/inventory/" + iid, json={"revision": fresh["revision"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.manager.get_instance(iid))
        self.assertEqual(
            self.inventory.history(self.actor, {iid})[0]["action"], "delete"
        )

    def test_inventory_list_and_detail_expose_authorized_plaintext_passwords(self):
        record = self.create()
        response = self.client.get('/api/operations/inventory')
        source = response.get_json()['records'][0]['source']
        self.assertEqual(source['password'],'=fixture-secret')
        self.assertEqual(source['host'],'192.0.2.10')
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        detail = self.client.get('/api/operations/inventory/'+record['id']).get_json()['record']
        self.assertEqual(detail['source']['password'],'=fixture-secret')
        self.assertEqual(self.manager.get_instance_decrypted(record['id'])['password'],'=fixture-secret')

    def test_export_has_required_sheets_and_unmasked_exact_values(self):
        record = self.create()
        response = self.client.get("/api/operations/inventory/export")
        self.assertEqual(response.status_code, 200)
        self.addCleanup(response.close)
        workbook = load_workbook(io.BytesIO(response.data), data_only=False)
        expected = [
            "全品类数据库资产底账清单",
            "集群参数表",
            "数据库参数表",
            "数据库基本信息表",
            "逻辑存储空间属性表",
            "用户及权限表",
            "备份策略表",
        ]
        self.assertEqual(workbook.sheetnames[:7], expected)
        sheet = workbook.worksheets[0]
        headers = {cell.value: cell.column for cell in sheet[3]}
        self.assertEqual(sheet.cell(4, headers["数据库IP/主机"]).value, "192.0.2.10")
        password = sheet.cell(4, headers["数据库密码"])
        self.assertEqual(password.value, "=fixture-secret")
        self.assertEqual(password.data_type, "s")
        self.assertEqual(sheet.cell(4, headers["运维责任人"]).value, "张三")
        self.assertEqual(sheet.freeze_panes, "C4")
        self.assertTrue(sheet.tables)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertNotIn(
            b"=fixture-secret", Path(self.manager.instances_db).read_bytes()
        )

    def test_duplicate_connection_and_configuration_id_roll_back_atomically(self):
        self.create()
        response = self.client.post(
            "/api/operations/inventory",
            json={
                "source": {
                    "name": "other",
                    "db_type": "mysql",
                    "host": "192.0.2.99",
                    "port": 3306,
                    "user": "app",
                },
                "metadata": {"configuration_id": "CI-001"},
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.manager.get_all_instances()), 1)
        with self.inventory.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM instances").fetchone()[0], 1
            )
        response = self.client.post(
            "/api/operations/inventory",
            json={
                "source": {
                    "name": "duplicate",
                    "db_type": "mysql",
                    "host": "192.0.2.10",
                    "port": 3306,
                    "user": "fixture-user",
                },
                "metadata": {"configuration_id": "CI-002"},
            },
        )
        self.assertEqual(response.status_code, 409)

    def test_access_limits_export_mutation_and_batch_scope(self):
        record = self.create()
        self.actor = Principal(id=21, username="other", tenant_id=2, roles=["operator"])
        self.assertEqual(
            self.client.get("/api/operations/inventory").get_json()["records"], []
        )
        self.assertEqual(
            self.client.get("/api/operations/inventory/" + record["id"]).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/api/operations/inventory/export?id=" + record["id"]
            ).status_code,
            403,
        )
        response = self.client.post(
            "/api/operations/batches",
            json={"request_key": "foreign-test", "instance_ids": [record["id"]]},
        )
        self.assertEqual(response.status_code, 403)
        self.actor = Principal(id=11, username="reader", tenant_id=1, roles=["user"])
        self.assertEqual(
            self.client.post("/api/operations/inventory", json={}).status_code, 403
        )
        self.assertEqual(
            self.client.post("/api/operations/batches", json={}).status_code, 403
        )
        self.actor = Principal()
        self.assertEqual(self.client.get("/api/operations/inventory").status_code, 403)

    def test_batch_runs_multiple_sources_with_bounded_parallelism_and_one_report(self):
        records = [
            self.create("source-" + str(i), "192.0.2." + str(i + 1), "CI-" + str(i))
            for i in range(4)
        ]
        run = self.start([r["id"] for r in records] + [records[0]["id"]])
        final = self.wait(run)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(summary(final)["total"], 4)
        self.assertEqual(self.dispatch_count, 4)
        self.assertEqual(self.max_active, 2)
        response = self.client.get("/api/operations/batches/" + run["id"] + "/report")
        self.assertEqual(response.status_code, 200)
        self.addCleanup(response.close)
        doc = Document(io.BytesIO(response.data))
        text = (
            "\n".join(p.text for p in doc.paragraphs)
            + "\n"
            + "\n".join(c.text for t in doc.tables for row in t.rows for c in row.cells)
        )
        for record in records:
            self.assertIn("unique-detail-" + record["id"], text)
            self.assertIn(record["source"]["host"], text)
        self.assertIn("fixture-risk-", text)
        self.assertNotIn("[IP]", text)
        self.assertEqual(len(doc.inline_shapes), 4)

    def test_partial_failure_and_incomplete_collection_are_explicit(self):
        good = self.create("incomplete")
        broken = self.create("broken", "192.0.2.11", "CI-002")
        final = self.wait(self.start([good["id"], broken["id"]]))
        self.assertEqual(final["status"], "partial")
        self.assertEqual(summary(final)["failed"], 1)
        self.assertEqual(summary(final)["incomplete"], 1)
        response = self.client.get("/api/operations/batches/" + final["id"] + "/report")
        self.addCleanup(response.close)
        doc = Document(io.BytesIO(response.data))
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("整体结论需结合缺项复核", text)
        self.assertIn("fixture connection failure", text)

    def test_cluster_selection_deduplicates_members_and_service_endpoint(self):
        a = self.create()
        b = self.create("source-two", "192.0.2.11", "CI-002")
        cluster = self.clusters.save(
            self.actor,
            {
                "name": "集群甲",
                "kind": "dm_dsc",
                "members": [
                    {"instance_id": a["id"], "enabled": True},
                    {"instance_id": b["id"], "enabled": True},
                ],
                "service_instance_id": a["id"],
            },
        )
        with patch.object(self.service, "launch"):
            run = self.start([a["id"]], cluster_ids=[cluster["id"], cluster["id"]])
        self.assertEqual(run["summary"]["total"], 2)
        self.assertTrue(
            all(m["cluster_name"] == "集群甲" for m in run["payload"]["members"])
        )

    def test_idempotency_leases_and_interrupted_process_state(self):
        record = self.create()
        payload = {"instance_ids": [record["id"]], "request_key": "repeat-request"}
        with patch.object(self.service, "launch") as launch:
            first = self.client.post("/api/operations/batches", json=payload)
            second = self.client.post("/api/operations/batches", json=payload)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.get_json()["run"]["id"], second.get_json()["run"]["id"])
        self.assertEqual(launch.call_count, 1)
        response = self.client.post(
            "/api/operations/batches", json={**payload, "request_key": "other-request"}
        )
        self.assertEqual(response.status_code, 409)
        run_id = first.get_json()["run"]["id"]
        with self.service.store.connection() as db:
            db.execute(
                "UPDATE operations_batches SET pid=? WHERE id=?", (99999999, run_id)
            )
        run = self.service.store.get(run_id)
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["payload"]["members"][0]["status"], "interrupted")
        with patch.object(self.service, "launch"):
            response = self.client.post(
                "/api/operations/batches",
                json={**payload, "request_key": "new-after-restart"},
            )
        self.assertEqual(response.status_code, 202)

    def test_invalid_inputs_and_template_type_mismatch(self):
        record = self.create()
        for value in (0, 9, True, "2"):
            response = self.client.post(
                "/api/operations/batches",
                json={
                    "instance_ids": [record["id"]],
                    "request_key": "invalid-request",
                    "concurrency": value,
                },
            )
            self.assertEqual(response.status_code, 400)
        response = self.client.put(
            "/api/operations/inventory/" + record["id"],
            json={
                "revision": record["revision"],
                "metadata": {"tables": {"accounts": [{"password": "uncontrolled"}]}},
            },
        )
        self.assertEqual(response.status_code, 400)
        with patch(
            "modules.inspection.dal.get_template", return_value={"db_type": "oracle"}
        ):
            response = self.client.post(
                "/api/operations/batches",
                json={
                    "instance_ids": [record["id"]],
                    "request_key": "invalid-template",
                    "templates": {"mysql": 123},
                },
            )
        self.assertEqual(response.status_code, 400)

    def test_existing_datasource_can_be_enriched_without_recreation(self):
        from modules.pro.instance_manager import DatabaseInstance

        self.manager.add_instance(
            DatabaseInstance(
                id="legacy-one",
                name="既有数据源",
                db_type="mysql",
                host="192.0.2.80",
                port=3306,
                user="existing",
            )
        )
        self.owners["legacy-one"] = {
            "owner_user_id": self.actor.id,
            "owner_tenant_id": 1,
            "scope": "private",
        }
        rows = self.client.get("/api/operations/inventory").get_json()["records"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        result = self.client.put(
            "/api/operations/inventory/legacy-one",
            json={
                "revision": row["revision"],
                "metadata": {"configuration_id": "CI-LEGACY", "location": "机房B"},
            },
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["record"]["id"], "legacy-one")
        self.assertEqual(len(self.manager.get_all_instances()), 1)

    def test_isolated_native_worker_preserves_identity_and_skips_jdbc_preflight(self):
        import types
        from modules import jdbc_inspection_cli as cli
        import modules.web

        app = types.ModuleType("modules.web.app")
        app.socketio = types.SimpleNamespace(emit=lambda *args, **kwargs: None)
        app.tasks = {}
        app.run_inspection_task = lambda tid, *args: app.tasks[tid].update(
            status="done", result={"risk_count": 0}
        )
        payload = {
            "task_id": "isolated-native",
            "db_type": "mongodb",
            "db_info": {"ip": "192.0.2.1", "port": 27017},
            "isolate_process": True,
            "task_metadata": {
                "datasource_id": "native-source",
                "owner_user_id": 11,
                "owner_tenant_id": 1,
            },
        }
        with patch.dict(sys.modules, {"modules.web.app": app}), patch.object(
            modules.web, "app", app, create=True
        ), patch("sys.stdin", io.StringIO(json.dumps(payload))), patch.object(
            cli, "_write_stdout"
        ), patch.object(
            cli, "_tcp_preflight"
        ) as preflight:
            with patch.dict(os.environ):
                self.assertEqual(cli.main(), 0)
        preflight.assert_not_called()
        self.assertEqual(app.tasks["isolated-native"]["datasource_id"], "native-source")
        self.assertEqual(app.tasks["isolated-native"]["owner_user_id"], 11)

    def test_two_concurrent_editors_cannot_overwrite_each_other(self):
        from concurrent.futures import ThreadPoolExecutor

        record = self.create()

        def update(owner):
            try:
                self.inventory.save(
                    self.actor,
                    {"revision": record["revision"], "metadata": {"owner": owner}},
                    record["id"],
                )
                return "saved"
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, ["张三", "李四"]))
        self.assertEqual(sorted(outcomes), ["conflict", "saved"])
        self.assertEqual(self.inventory.get(record["id"])["version"], 2)

    def test_security_middleware_allows_same_origin_frame_and_requires_csrf(self):
        from modules.core.web_security import install_security

        self.app.add_url_rule('/monitor-screen', 'fixture_screen', lambda: '<html><head></head><body>monitor</body></html>')
        install_security(self.app)
        with patch(
            "modules.core.web_security.current_principal", return_value=self.actor
        ):
            response = self.client.get("/operations/inventory")
            self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
            self.assertIn("csrf-token", response.get_data(as_text=True))
            self.assertEqual(self.client.get("/monitor-screen").headers["X-Frame-Options"], "SAMEORIGIN")
            self.assertEqual(self.client.get("/api/operations/options").headers["X-Frame-Options"], "DENY")
            self.assertEqual(
                self.client.post("/api/operations/inventory", json={}).status_code, 403
            )
            with self.client.session_transaction() as session:
                token = session["_csrf"]
            response = self.client.post(
                "/api/operations/inventory",
                json={
                    "source": {
                        "name": "CSRF test",
                        "db_type": "mysql",
                        "host": "192.0.2.1",
                        "port": 3306,
                    },
                    "metadata": {},
                },
                headers={"X-CSRF-Token": token},
            )
            self.assertEqual(response.status_code, 201, response.get_json())

    def test_connection_changes_after_queue_are_detected_before_dispatch(self):
        record = self.create()
        with patch.object(self.service, "launch"):
            run = self.start([record["id"]])
        self.manager.update_instance(record["id"], {"host": "192.0.2.99"})
        self.service.run(self.service.store.get(run["id"]))
        final = self.service.store.get(run["id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(self.dispatch_count, 0)
        self.assertIn("任务创建后变化", final["payload"]["members"][0]["error"])

    def test_registry_metadata_feeds_report_core_attributes(self):
        from types import SimpleNamespace
        from modules.inspection.basic_info import load_overrides
        from modules.core import paths

        record = self.create()
        updated = self.inventory.save(
            self.actor,
            {
                "revision": record["revision"],
                "metadata": {
                    "configuration_id": "CI-REPORT",
                    "location": "机房C",
                    "host_model": "测试主机型号",
                    "version": "8.0",
                    "patch_level": "补丁1",
                    "file_size": "12 GB",
                },
            },
            record["id"],
        )
        keyfile = self.root / "key-exists"
        keyfile.write_bytes(b"fixture-key-presence")
        engine = SimpleNamespace(
            context={"_instance_id": record["id"]},
            host="192.0.2.10",
            port=3306,
            db_type="mysql",
        )
        with patch.object(paths, "PRO_DATA_DIR", self.root / "sources"), patch.object(
            paths, "DB_KEY_PATH", keyfile
        ):
            result = load_overrides(engine)
        self.assertEqual(result["configuration_id"], "CI-REPORT")
        self.assertEqual(result["location"], "机房C")
        self.assertEqual(result["db_size"], "12 GB")
        self.assertIn("补丁1", result["version_patch"])

    def test_deleted_assets_remain_in_audit_and_full_export(self):
        record = self.create()
        response = self.client.delete(
            "/api/operations/inventory/" + record["id"],
            json={"revision": record["revision"]},
        )
        self.assertEqual(response.status_code, 200)
        audit = self.client.get("/api/operations/inventory-history").get_json()[
            "records"
        ]
        self.assertEqual(audit[0]["action"], "delete")
        export = self.client.get("/api/operations/inventory/export")
        self.addCleanup(export.close)
        workbook = load_workbook(io.BytesIO(export.data))
        self.assertGreater(workbook["台账变更记录"].max_row, 3)
        self.assertEqual(workbook["全品类数据库资产底账清单"].max_row, 3)

    def test_inventory_refresh_preserves_existing_source_groups(self):
        from modules.pro.instance_manager import InstanceGroup

        group = InstanceGroup(
            "custom-business", "业务分组", "#123456", "2025-01-01T00:00:00"
        )
        self.manager.add_group(group)
        self.create()
        reloaded = InstanceManager(str(self.root / "sources"))
        saved = next(
            g for g in reloaded.get_all_groups() if g.name == "custom-business"
        )
        self.assertEqual(saved.created_at, "2025-01-01T00:00:00")

    def test_inventory_accepts_every_registered_database_type(self):
        from modules.db_types.dbtype_registry import load_all_db_types

        for meta in load_all_db_types():
            with self.subTest(db_type=meta.db_type):
                response = self.client.post(
                    "/api/operations/inventory",
                    json={
                        "source": {
                            "name": meta.label,
                            "db_type": meta.db_type,
                            "host": "192.0.2.50",
                            "port": meta.port,
                            "user": meta.user,
                            "database": meta.default_database,
                        },
                        "metadata": {"configuration_id": "CI-MATRIX-" + meta.db_type},
                    },
                )
                self.assertEqual(response.status_code, 201, response.get_json())
                iid = response.get_json()["record"]["id"]
                self.assertIsNotNone(self.manager.get_instance(iid))

    def test_metadata_partial_updates_preserve_other_register_tables(self):
        record = self.create()
        response = self.client.put(
            "/api/operations/inventory/" + record["id"],
            json={
                "revision": record["revision"],
                "metadata": {"tables": {"accounts": []}},
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        tables = response.get_json()["record"]["metadata"]["tables"]
        self.assertEqual(tables["accounts"], [])
        self.assertEqual(tables["database_parameters"][0]["name"], "max_connections")
        self.assertTrue(tables["backup_policies"])

    def test_sidebar_entries_are_registered_in_rbac_and_localized(self):
        tree = ast.parse((ROOT / "modules/user_management/seed.py").read_text())
        catalogue = next(
            ast.literal_eval(n.value)
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "menus_data" for t in n.targets)
        )
        codes = {row[0] for row in catalogue}
        required = {
            "inventory",
            "batch-inspection",
            "clusters",
            "services",
            "system-info",
        }
        self.assertTrue(required <= codes)
        html = (ROOT / "web_templates/index.html").read_text(encoding="utf-8-sig")
        sidebar_codes = set(re.findall(r'id="nav-([^"]+)"', html))
        # flow 由专业版扩展登记，license 是既有隐藏入口。
        self.assertFalse(sidebar_codes - codes - {"flow", "license"})
        self.assertLess(html.index('id="nav-inventory"'), html.index('id="nav-wizard"'))
        self.assertIn('id="page-batch-inspection"', html)
        self.assertIn("/operations/inventory?tab=batch", html)
        from i18n import _LANGS, get_all_translations

        for lang in _LANGS:
            translations = get_all_translations(lang)
            for code in required:
                self.assertIn("menu." + code, translations)

    def test_operator_menu_initialization_and_sync_include_operations(self):
        tree = ast.parse((ROOT / "modules/user_management/seed.py").read_text())
        required = {"inventory", "batch-inspection", "clusters", "services"}
        checked = 0
        for n in ast.walk(tree):
            if not isinstance(n, ast.Assign):
                continue
            names = {t.id for t in n.targets if isinstance(t, ast.Name)}
            if "operator_menus" in names:
                self.assertTrue(required <= set(ast.literal_eval(n.value)))
                checked += 1
            if "role_menu_map" in names:
                self.assertTrue(required <= set(ast.literal_eval(n.value)["operator"]))
                checked += 1
        self.assertEqual(checked, 2)

    def test_cluster_and_service_pages_mount_in_main_content(self):
        from flask import render_template
        html = (ROOT/'web_templates/index.html').read_text(encoding='utf-8-sig')
        for code in ('clusters','services'):
            self.assertRegex(html,r'onclick="showPage\(\''+code+r'\'\)" id="nav-'+code+r'"')
            self.assertIn('id="page-'+code+'"',html)
            self.assertNotRegex(html,r'href="/'+code+r'" id="nav-'+code+r'"')
        for template,root_id in [('clusters.html','cluster-workspace'),('service_management.html','service-workspace')]:
            with self.app.test_request_context('/'):
                embedded = render_template(template,embedded=True)
                standalone = render_template(template)
            self.assertIn('id="'+root_id+'"',embedded)
            self.assertNotIn('<!doctype',embedded.lower())
            self.assertNotIn('<body',embedded.lower())
            self.assertIn('<!doctype',standalone.lower())
        js = (ROOT/'assets/web/static/js/service_management.js').read_text()
        self.assertIn('root.querySelectorAll',js)
        self.assertNotIn('document.querySelectorAll',js)
        self.assertIn('window.initServiceManagementPage',js)

    def test_options_and_template_render(self):
        self.assertEqual(self.client.get("/api/operations/options").status_code, 200)
        response = self.client.get("/operations/inventory")
        self.assertEqual(response.status_code, 200)
        self.assertIn("保存台账并同步数据源", response.get_data(as_text=True))
        self.actor = Principal(id=11, username="reader", tenant_id=1, roles=["user"])
        readonly = self.client.get("/operations/inventory").get_data(as_text=True)
        self.assertIn("const canWrite = false;", readonly)


if __name__ == "__main__":
    unittest.main(verbosity=2)
