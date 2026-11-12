# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Public monitor reads retain authenticated context and reject anonymous writes."""

import ast
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from flask import Flask, g, jsonify, render_template
from modules.access import Principal
from modules.core.web_security import install_security

ROOT = Path(__file__).resolve().parents[3]


def fixture():
    app = Flask(__name__, template_folder=str(ROOT / "web_templates"))
    app.secret_key = "monitor-fixture"
    install_security(app)
    tree = ast.parse((ROOT / "modules/web/app.py").read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "monitor_screen_page"
    )
    function.decorator_list = []
    namespace = {
        "render_template": render_template,
        "__version__": "v1.0.1",
        "_t": lambda key: key,
    }
    exec(
        compile(
            ast.Module(body=[function], type_ignores=[]), "monitor_page_fixture", "exec"
        ),
        namespace,
    )
    app.add_url_rule("/monitor-screen", view_func=namespace["monitor_screen_page"])

    @app.route("/api/dashboard/thresholds", methods=["GET", "POST"])
    def thresholds():
        return jsonify(
            ok=True, actor=getattr(getattr(g, "security_principal", None), "id", None)
        )

    @app.route("/api/operations/inventory")
    def inventory():
        return jsonify(ok=True)

    return app.test_client()


def test_anonymous_monitor_is_read_only_and_other_data_requires_login():
    client = fixture()
    with patch("modules.core.web_security.current_principal", return_value=None):
        response = client.get("/monitor-screen")
        assert response.status_code == 200
        soup = BeautifulSoup(response.data, "html.parser")
        assert soup.select("#th-panel input[disabled]")
        assert soup.select_one('[onclick="saveThresholds()"]') is None
        assert client.get("/api/dashboard/thresholds").status_code == 200
        assert client.post("/api/dashboard/thresholds", json={}).status_code == 401
        assert client.get("/api/operations/inventory").status_code == 401
    with patch.dict("os.environ", {"DBPULSE_PUBLIC_MONITOR": "0"}), patch(
        "modules.core.web_security.current_principal", return_value=None
    ):
        assert client.get("/monitor-screen").status_code == 302
        assert client.get("/api/dashboard/thresholds").status_code == 401


def test_authenticated_monitor_retains_principal_and_csrf_requirements():
    client = fixture()
    actor = Principal(id=1, username="fixture-admin", tenant_id=1, roles=["admin"])
    with patch("modules.core.web_security.current_principal", return_value=actor):
        soup = BeautifulSoup(client.get("/monitor-screen").data, "html.parser")
        assert soup.select_one('[onclick="saveThresholds()"]') is not None
        assert not soup.select("#th-panel input[disabled]")
        assert client.get("/api/dashboard/thresholds").get_json()["actor"] == 1
        assert client.post("/api/dashboard/thresholds", json={}).status_code == 403
        with client.session_transaction() as session:
            token = session["_csrf"]
        assert (
            client.post(
                "/api/dashboard/thresholds", json={}, headers={"X-CSRF-Token": token}
            ).status_code
            == 200
        )


def test_health_matrix_uses_current_thresholds_and_keeps_pending_unknown():
    import json
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the isolated matrix rendering regression")
    source = (ROOT / "web_templates/monitor_screen.html").read_text()
    begin = source.index("function hmV(")
    end = source.index("/* ── 数据预热", begin)
    script = """const vm=require('node:vm');
const context={S:{st_ok:'OK',st_pending:'PENDING',st_warn:'WARN',st_crit:'CRIT',no_data:'UNKNOWN'},C:{crit:'red',warn:'yellow',tx2:'gray'},esc:String,statusColor:()=>'',pctColor:()=>'',tbsColor:()=>''};
const element={innerHTML:'',querySelectorAll:()=>[]};
context.document={getElementById:()=>element};
vm.createContext(context);
vm.runInContext(SOURCE,context);
if(context.hmRisks({lag:100},{warn_repl_lag:30,crit_repl_lag:300})[0].c!=='yellow')throw Error('default threshold mismatch');
if(context.hmRisks({lag:20},{warn_repl_lag:10,crit_repl_lag:15})[0].c!=='red')throw Error('custom threshold ignored');
context.renderHealthMatrix([{id:'fixture',name:'fixture',status:'pending'}],{});
if(!element.innerHTML.includes('PENDING')||element.innerHTML.includes('>OK<'))throw Error('pending shown as healthy');
""".replace("SOURCE", json.dumps(source[begin:end]))
    result = subprocess.run(
        [node, "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
