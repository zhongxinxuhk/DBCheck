# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Exercise a packaged release against temporary installation data."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import importlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REQUIRED_MODULES = (
    "flask",
    "flask_socketio",
    "gevent",
    "docx",
    "docxtpl",
    "docxcompose",
    "docxcompose.composer",
    "babel.numbers",
    "babel.dates",
    "openpyxl",
    "yaml",
    "numpy",
    "pandas",
    "psutil",
    "cryptography",
    "bcrypt",
    "jwt",
    "paramiko",
    "requests",
    "croniter",
    "tzdata",
    "bs4",
    "reportlab",
    "PyPDF2",
    "apscheduler",
    "pymysql",
    "psycopg2",
    "oracledb",
    "pymongo",
    "redis",
    "jpype",
    "jaydebeapi",
    "pytds",
    "modules.operations.routes",
    "modules.clusters.routes",
    "modules.inspection.sql_safety",
    "modules.intelligence.task_runner",
)
REQUIRED_ASSETS = (
    "web_templates/operations_inventory.html",
    "web_templates/service_management.html",
    "web_templates/clusters.html",
    "assets/web/static/js/workspace_usability.js",
    "assets/web/static/js/structured_forms.js",
    "assets/web/static/js/inspection_config.js",
    "assets/web/static/css/workspace_usability.css",
    "db/user_management_schema.sql",
    "LICENSE",
    "NOTICE",
)


def main():
    os.environ["DBCheck_NO_GEVENT_PATCH"] = "1"
    os.environ["DBPULSE_INSPECTION_WORKER"] = "1"
    from modules.core import paths

    resource = paths.PROJECT_ROOT
    for asset in REQUIRED_ASSETS:
        if not (resource / asset).is_file():
            raise RuntimeError("Missing release asset: " + asset)
    with tempfile.TemporaryDirectory(
        prefix="dbpulse-package-check-", ignore_cleanup_errors=True
    ) as directory:
        root = Path(directory)
        # An isolated resource root also isolates legacy relative-path migrations.
        for relative in (
            "web_templates",
            "assets",
            "templates",
            "db",
            "i18n",
            "plugins",
            "modules/config",
            "modules/user_management",
        ):
            source = resource / relative
            if source.is_dir():
                shutil.copytree(
                    source,
                    root / relative,
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", "*.db", "*.log"
                    ),
                )
        for name, value in list(vars(paths).items()):
            if isinstance(value, Path) and value.is_relative_to(resource):
                setattr(paths, name, root / value.relative_to(resource))
        os.chdir(root)
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        for name in REQUIRED_MODULES:
            importlib.import_module(name)
        from modules.core.bootstrap_security import initial_admin_password

        if initial_admin_password() != "admin123":
            raise RuntimeError("Unexpected initial administrator password")
        from modules.web.app import app

        client = app.test_client()
        response = client.post(
            "/api/um/auth/login", json={"username": "admin", "password": "admin123"}
        )
        if response.status_code != 200 or response.get_json().get("code") != 0:
            raise RuntimeError("Fresh-install default admin login failed")
        response = client.get("/")
        if (
            response.status_code != 200
            or b"workspace_usability.js" not in response.data
        ):
            raise RuntimeError("Main workspace template failed")
        with client.session_transaction() as session:
            token = session["_csrf"]
        headers = {"X-CSRF-Token": token}
        response = client.post(
            "/api/operations/inventory",
            headers=headers,
            json={
                "source": {
                    "name": "package-check",
                    "db_type": "oracle",
                    "host": "192.0.2.1",
                    "port": 1521,
                    "user": "fixture",
                    "password": "fixture-secret",
                },
                "metadata": {
                    "configuration_id": "PACKAGE-CHECK",
                    "version": "19c",
                    "owner": "fixture",
                    "system": "fixture",
                    "business": "fixture",
                },
            },
        )
        if response.status_code != 201:
            raise RuntimeError("Inventory creation failed: " + str(response.get_json()))
        identifier = response.get_json()["record"]["id"]
        response = client.get("/api/operations/inventory/export")
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(response.data))
        if len(workbook.sheetnames) < 9:
            raise RuntimeError("Inventory Excel export is incomplete")
        response = client.post(
            "/api/intelligence/workflow-tasks",
            headers=headers,
            json={
                "name": "package-check",
                "task_type": "batch_inspection",
                "batch_spec": {"instance_ids": [identifier], "concurrency": 1},
                "cron": "0 2 * * *",
                "schedule_enabled": False,
            },
        )
        if response.status_code != 200 or not response.get_json().get("ok"):
            raise RuntimeError(
                "Scheduled overall inspection configuration failed: "
                + str(response.get_json())
            )
        from docx import Document
        from docxcompose.composer import Composer

        first, second = Document(), Document()
        first.add_paragraph("summary")
        second.add_paragraph("detail")
        composer = Composer(first)
        composer.append(second)
        composer.save(str(root / "merged.docx"))
        if len(Document(root / "merged.docx").paragraphs) != 2:
            raise RuntimeError("Overall report composition failed")
        from modules.user_management.models.db_manager import DBManager
        from modules.user_management.seed import init_seed_data
        from modules.user_management.utils.password import (
            hash_password,
            verify_password,
        )

        manager = DBManager()
        manager.execute(
            "UPDATE um_user SET password=? WHERE username='admin'",
            (hash_password("package-check-changed"),),
        )
        init_seed_data()
        if not verify_password(
            "package-check-changed",
            manager.query_one("SELECT password FROM um_user WHERE username='admin'")[
                "password"
            ],
        ):
            raise RuntimeError("Existing administrator password was reset by seeding")
        result = {
            "ok": True,
            "platform": sys.platform,
            "frozen": bool(getattr(sys, "frozen", False)),
            "required_modules": len(REQUIRED_MODULES),
            "assets": len(REQUIRED_ASSETS),
            "default_admin_login": True,
            "existing_password_preserved": True,
            "inventory_excel": True,
            "scheduled_overall_inspection": True,
            "report_composition": True,
        }
        from modules.monitor.engine import get_monitor_engine
        from modules.monitor.screen_metrics import get_screen_collector
        from modules.core.log_manager import stop_maintenance
        import logging

        application = sys.modules["modules.web.app"]
        monitor_scheduler = getattr(application, "_monitor_sched", None)
        if monitor_scheduler is not None:
            monitor_scheduler.shutdown(wait=False)
        get_monitor_engine().stop()
        get_screen_collector().stop()
        stop_maintenance()
        logging.shutdown()
        os.chdir(resource)
        print("__DBPULSE_PACKAGE_CHECK__" + json.dumps(result), flush=True)
    return 0
