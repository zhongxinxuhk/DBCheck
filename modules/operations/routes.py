# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""数据库运维中台的资产台账与多数据源巡检。"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import threading
import uuid

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    render_template,
    request,
    send_file,
)
from modules.core import paths
from modules.access import (
    filter_visible,
    get_owner,
    set_owner,
    remove_owner,
    visible_to,
)
from modules.db_types.dbtype_registry import load_all_db_types, get_db_meta
from modules.clusters.store import ClusterStore
from modules.driver_registry import DB_FAMILY_ALIASES, db_family
from .inventory import (
    Inventory,
    ASSET_FIELDS,
    SOURCE_FIELDS,
    TABLES,
    SECRETS,
    BOOLEAN_FIELDS,
    Conflict,
)
from .batch import BatchStore, BatchService, TERMINAL, summary
from .exports import inventory_xlsx

bp = Blueprint("operations", __name__)
_lock = threading.Lock()


def actor(write=False):
    principal = getattr(g, "security_principal", None)
    if principal is None or principal.is_anonymous:
        raise PermissionError("请先登录")
    if write and not (principal.is_admin or "operator" in principal.roles):
        raise PermissionError("维护台账和发起批量巡检需要管理员或运维角色")
    return principal


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("请求内容须为JSON对象")
    return value


def resources():
    app = current_app._get_current_object()
    with _lock:
        value = app.extensions.get("operations")
        if value is None:
            from modules.pro import get_instance_manager

            manager = (
                app.config.get("OPERATIONS_INSTANCE_MANAGER") or get_instance_manager()
            )
            inventory = Inventory(manager)
            batches = BatchStore(
                app.config.get("OPERATIONS_DB_PATH") or paths.DATA_DIR / "operations.db"
            )
            service = BatchService(
                app,
                batches,
                app.config.get("OPERATIONS_REPORTS_DIR") or paths.REPORTS_DIR,
                inventory,
            )
            clusters = app.config.get("CLUSTER_STORE") or ClusterStore(
                paths.DATA_DIR / "clusters.db"
            )
            value = (inventory, service, clusters)
            app.extensions["operations"] = value
        return value


def visible_ids(principal):
    inventory, _, _ = resources()
    return [
        row["id"]
        for row in filter_visible(
            principal, inventory.manager.get_all_instances(), "instance"
        )
    ]


def visible_id(principal, iid):
    if not visible_to(principal, get_owner("instance", iid)):
        raise PermissionError("无权访问此台账或数据源")
    return resources()[0].get(iid)


@bp.errorhandler(PermissionError)
def forbidden(error):
    return jsonify(ok=False, error=str(error)), 403


@bp.errorhandler(LookupError)
def missing(error):
    return jsonify(ok=False, error=str(error)), 404


@bp.errorhandler(Conflict)
def conflict(error):
    return jsonify(ok=False, error=str(error)), 409


@bp.errorhandler(ValueError)
def invalid(error):
    return jsonify(ok=False, error=str(error)), 400


@bp.route("/operations/inventory")
def page():
    principal = actor()
    return render_template(
        "operations_inventory.html",
        can_write=principal.is_admin or "operator" in principal.roles,
    )


@bp.route("/api/operations/options")
def options():
    principal = actor()
    _, _, clusters = resources()
    allowed = set(visible_ids(principal))
    cluster_options = []
    for row in clusters.list(principal):
        ids = [
            m["instance_id"]
            for m in row["spec"].get("members", [])
            if m.get("enabled", True)
        ]
        service_id = row["spec"].get("service_instance_id")
        ids = list(dict.fromkeys(ids + ([service_id] if service_id else [])))
        if set(ids) <= allowed:
            cluster_options.append(
                {"id": row["id"], "name": row["name"], "instance_ids": ids}
            )
    return jsonify(
        ok=True,
        fields=ASSET_FIELDS,
        source_fields=SOURCE_FIELDS,
        tables=TABLES,
        boolean_fields=sorted(BOOLEAN_FIELDS),
        secrets=sorted(SECRETS),
        db_types=[
            {
                "key": m.db_type,
                "name": m.label,
                "port": m.port,
                "user": m.user,
                "database": m.default_database,
            }
            for m in load_all_db_types()
        ],
        clusters=cluster_options,
        families={
            key: db_family(key)
            for key in set(DB_FAMILY_ALIASES) | {m.db_type for m in load_all_db_types()}
        },
    )


@bp.route("/api/operations/inventory", methods=["GET", "POST"])
def inventory_collection():
    principal = actor(write=request.method == "POST")
    inventory, _, _ = resources()
    if request.method == "GET":
        records = inventory.list(visible_ids(principal), decrypt=True)
        response = jsonify(ok=True, records=records)
        response.headers["Cache-Control"] = "no-store"
        return response
    data = body()
    validate_source_type(data)
    iid = uuid.uuid4().hex[:12]
    owner = set_owner("instance", iid, principal)
    if owner is None:
        raise PermissionError("数据源归属登记失败，台账尚未创建")
    try:
        record = inventory.save(principal, data, new_id=iid)
    except Exception:
        remove_owner("instance", iid)
        raise
    return jsonify(ok=True, record=record), 201


def validate_source_type(data, existing_type=None):
    if not isinstance(data.get("source", {}), dict):
        raise ValueError("连接信息须为对象")
    db_type = (data.get("source") or {}).get("db_type")
    if isinstance(db_type, str):
        db_type = db_type.strip().lower()
        db_type = {"postgresql": "pg", "dm8": "dm", "oracle_full": "oracle"}.get(
            db_type, db_type
        )
        data["source"]["db_type"] = db_type
    if db_type is not None and (
        not isinstance(db_type, str)
        or (get_db_meta(db_type) is None and db_type != existing_type)
    ):
        raise ValueError("请选择系统已启用的数据库类型")


@bp.route("/api/operations/inventory/<iid>", methods=["GET", "PUT", "DELETE"])
def inventory_record(iid):
    principal = actor(write=request.method != "GET")
    current = visible_id(principal, iid)
    inventory, _, _ = resources()
    if request.method == "GET":
        return jsonify(ok=True, record=inventory.get(iid, decrypt=True))
    data = body()
    if request.method == "DELETE":
        inventory.delete(principal, iid, data.get("revision"))
        remove_owner("instance", iid)
        return jsonify(ok=True)
    validate_source_type(data, current["source"]["db_type"])
    return jsonify(ok=True, record=inventory.save(principal, data, iid))


@bp.route("/api/operations/inventory/<iid>/history")
def inventory_history(iid):
    principal = actor()
    visible_id(principal, iid)
    return jsonify(ok=True, records=resources()[0].history(principal, {iid}))


@bp.route("/api/operations/inventory-history")
def inventory_all_history():
    principal = actor()
    inventory, _, _ = resources()
    events = {
        row["id"]: row
        for row in inventory.history(principal, set(visible_ids(principal)))
    }
    events.update({row["id"]: row for row in inventory.history(principal)})
    return jsonify(
        ok=True,
        records=sorted(events.values(), key=lambda row: row["id"], reverse=True),
    )


@bp.route("/api/operations/inventory/export")
def export_inventory():
    principal = actor()
    inventory, _, _ = resources()
    ids = request.args.getlist("id") or visible_ids(principal)
    if not set(ids) <= set(visible_ids(principal)):
        raise PermissionError("导出范围包含无权访问的数据源")
    records = inventory.list(list(dict.fromkeys(ids)), decrypt=True)
    events = {row["id"]: row for row in inventory.history(principal, set(ids))}
    if not request.args.getlist("id"):
        events.update({row["id"]: row for row in inventory.history(principal)})
    result = inventory_xlsx(
        records, sorted(events.values(), key=lambda row: row["id"], reverse=True)
    )
    response = send_file(
        result,
        as_attachment=True,
        download_name="数据库运维台账.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/operations/batches", methods=["GET", "POST"])
def batch_collection():
    principal = actor(write=request.method == "POST")
    inventory, service, clusters = resources()
    if request.method == "GET":
        return jsonify(
            ok=True, runs=[public_run(r) for r in service.store.list(principal)]
        )
    return jsonify(ok=True, run=public_run(submit_batch(principal, body()))), 202


def prepare_batch(principal, data):
    """统一校验巡检范围与模板，供手动和定时入口复用。"""
    inventory, service, clusters = resources()
    from modules.inspection.sql_safety import inspection_endpoint

    ids = data.get("instance_ids", [])
    cluster_ids = data.get("cluster_ids", [])
    if (
        not isinstance(ids, list)
        or not isinstance(cluster_ids, list)
        or any(not isinstance(x, str) for x in ids + cluster_ids)
    ):
        raise ValueError("数据源和集群范围须为编号列表")
    ids = list(ids)
    cluster_names = {}
    for cid in dict.fromkeys(cluster_ids):
        cluster = clusters.get(cid, principal)
        members = [
            m["instance_id"]
            for m in cluster["spec"]["members"]
            if m.get("enabled", True)
        ]
        service_id = cluster["spec"].get("service_instance_id")
        if service_id:
            members.append(service_id)
        for iid in dict.fromkeys(members):
            ids.append(iid)
            cluster_names.setdefault(iid, []).append(cluster["name"])
    ids = list(dict.fromkeys(ids))
    if not ids or len(ids) > 1000:
        raise ValueError("每次任务请选择1到1000个数据源")
    if not set(ids) <= set(visible_ids(principal)):
        raise PermissionError("巡检范围包含无权访问的数据源")
    selected_ids = set(ids)
    for cluster in clusters.list(principal):
        cluster_member_ids = {
            m["instance_id"]
            for m in cluster["spec"].get("members", [])
            if m.get("enabled", True)
        }
        if cluster["spec"].get("service_instance_id"):
            cluster_member_ids.add(cluster["spec"]["service_instance_id"])
        for iid in selected_ids & cluster_member_ids:
            names = cluster_names.setdefault(iid, [])
            if cluster["name"] not in names:
                names.append(cluster["name"])
    members = []
    for iid in ids:
        record = inventory.get(iid)
        s, m = record["source"], record["metadata"]
        if not s.get("enabled"):
            raise ValueError("巡检范围包含已停用数据源，请先调整选择")
        members.append(
            {
                "instance_id": iid,
                "endpoint_key": inspection_endpoint(s),
                "name": s["name"],
                "db_type": s["db_type"],
                "host": s["host"],
                "port": s["port"],
                "configuration_id": record["configuration_id"],
                "cluster_name": "、".join(cluster_names.get(iid, []))
                or m.get("cluster_id")
                or "",
                "asset_metadata": {
                    key: value for key, value in m.items() if key != "tables"
                },
                "source_revision": record["revision"],
                "status": "queued",
                "task_id": None,
            }
        )
    concurrency = data.get("concurrency", 1)
    timeout = data.get("timeout_seconds", 1800)
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= 8
    ):
        raise ValueError("并发数须为1到8的整数")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 30 <= timeout <= 86400
    ):
        raise ValueError("单库等待时间须为30到86400秒")
    templates = data.get("templates", {})
    if not isinstance(templates, dict):
        raise ValueError("模板选择须为按数据库类型登记的对象")
    from modules.inspection.dal import get_template
    from modules.driver_registry import db_family

    for kind, tid in templates.items():
        template = get_template(tid)
        if template is None or db_family(template["db_type"]) != db_family(kind):
            raise ValueError("巡检模板与数据库类型不匹配")
    spec = {
        "name": str(data.get("name") or "多数据源整体巡检")[:200],
        "inspector": str(
            data.get("inspector") or principal.display_name or principal.username
        )[:100],
        "concurrency": concurrency,
        "timeout_seconds": timeout,
        "templates": templates,
        "instance_ids": sorted(ids),
        "cluster_ids": sorted(set(cluster_ids)),
    }
    return spec, members


def submit_batch(principal, data):
    key = data.get("request_key")
    if not isinstance(key, str) or not 8 <= len(key) <= 100:
        raise ValueError("请提供8到100字符的请求标识")
    spec, members = prepare_batch(principal, data)
    service = resources()[1]
    service.store.list(principal)
    run, created = service.store.create(principal, key, spec, members)
    if created:
        service.launch(run)
    return run


def authorized_run(run_id):
    principal = actor()
    run = resources()[1].store.get(run_id)
    if run["tenant"] != principal.tenant_id or (
        run["owner"] != principal.id and not principal.is_admin
    ):
        raise PermissionError("无权访问此批量巡检")
    return run


def public_run(run):
    return {
        "id": run["id"],
        "status": run["status"],
        "created": run["created"],
        "updated": run["updated"],
        "payload": run["payload"],
        "summary": summary(run),
    }


@bp.route("/api/operations/batches/<run_id>")
def batch_status(run_id):
    return jsonify(ok=True, run=public_run(authorized_run(run_id)))


@bp.route("/api/operations/batches/<run_id>/report")
def batch_report(run_id):
    run = authorized_run(run_id)
    if run["status"] not in TERMINAL:
        raise Conflict("巡检尚未结束，请等待整体报告生成")
    output = resources()[1].ensure_report(run)
    return send_file(
        output, as_attachment=True, download_name="整体巡检报告_" + run_id + ".docx"
    )
