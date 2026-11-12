# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""数据库运维中台的资产台账与多数据源巡检。"""


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
from .policy import normalize_policy
from .groups import (
    Groups,
    FIELDS as GROUP_FIELDS,
    normalize_filters,
    select as select_group,
    facets,
)

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
    inventory, service, clusters = resources()
    allowed = set(visible_ids(principal))
    records = inventory.list(list(allowed))
    definitions = clusters.list(principal)
    cluster_options = []
    for row in definitions:
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
        grouping_fields=GROUP_FIELDS,
        facets=facets(records, definitions),
        groups=Groups(service.store, inventory).list(principal, records, definitions),
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
    group_ids = data.get("group_ids", [])
    if (
        not isinstance(group_ids, list)
        or len(group_ids) > 1000
        or any(not isinstance(item, str) for item in group_ids)
    ):
        raise ValueError("巡检分组须为编号列表")
    filters = normalize_filters(data.get("selection_filters", {}))
    visible = inventory.list(visible_ids(principal))
    cluster_records = clusters.list(principal)
    scopes = []

    def add_scope(name, conditions):
        matched = select_group(visible, conditions, cluster_records)
        matched_ids = [
            record["id"] for record in matched if record["source"].get("enabled")
        ]
        ids.extend(matched_ids)
        description = (
            "；".join(
                GROUP_FIELDS[key] + "=" + "、".join(values)
                for key, values in conditions.items()
            )
            or "全部当前可见且已启用的数据源"
        )
        scopes.append(
            {
                "name": name,
                "filters": conditions,
                "description": description,
                "instance_ids": matched_ids,
            }
        )

    groups = Groups(service.store, inventory)
    for gid in dict.fromkeys(group_ids):
        group = groups.get(principal, gid)
        add_scope(group["name"], group["filters"])
    if filters:
        add_scope("按业务条件选择", filters)
    cluster_names = {}
    cluster_keys = {}
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
            cluster_keys.setdefault(iid, []).append(cluster["id"])
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
                "cluster_keys": cluster_keys.get(iid)
                or (
                    ["metadata:" + str(m["cluster_id"])] if m.get("cluster_id") else []
                ),
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
        **normalize_policy(data),
        "name": str(data.get("name") or "多数据源整体巡检")[:200],
        "inspector": str(
            data.get("inspector") or principal.display_name or principal.username
        )[:100],
        "concurrency": concurrency,
        "timeout_seconds": timeout,
        "templates": templates,
        "instance_ids": sorted(ids),
        "cluster_ids": sorted(set(cluster_ids)),
        "group_ids": sorted(set(group_ids)),
        "selection_filters": filters,
        "scope_snapshot": scopes,
        "notify_on_completion": data.get("notify_on_completion", False),
    }
    if not isinstance(spec["notify_on_completion"], bool):
        raise ValueError("完成通知开关须为布尔值")
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
    import copy
    from .findings import issue_key, issue_fields

    payload = copy.deepcopy(run["payload"])
    for member in payload["members"]:
        for index, issue in enumerate(member.get("result", {}).get("issues", [])):
            member["result"]["issues"][index] = {
                **issue_fields(issue),
                "fingerprint": issue_key(issue),
            }
    return {
        "id": run["id"],
        "status": run["status"],
        "created": run["created"],
        "updated": run["updated"],
        "payload": payload,
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


@bp.route("/api/operations/inventory/import-template")
def inventory_import_template():
    actor()
    return send_file(
        inventory_xlsx([], []),
        as_attachment=True,
        download_name="数据库运维台账导入模板.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/api/operations/inventory/import-preview", methods=["POST"])
def inventory_import_preview():
    from .imports import InventoryImport

    principal = actor(write=True)
    upload = request.files.get("file")
    if upload is None or not upload.filename.lower().endswith(".xlsx"):
        raise ValueError("请上传标准.xlsx台账")
    content = upload.read(10 * 1024 * 1024 + 1)
    preview = InventoryImport(resources()[0]).preview(
        principal, content, visible_ids(principal), validate_source_type
    )
    return jsonify(ok=True, **preview)


@bp.route("/api/operations/inventory/import-apply", methods=["POST"])
def inventory_import_apply():
    from .imports import InventoryImport

    principal = actor(write=True)
    result = InventoryImport(resources()[0]).apply(
        principal,
        body().get("token"),
        set(visible_ids(principal)),
        lambda iid: set_owner("instance", iid, principal),
        lambda iid: remove_owner("instance", iid),
    )
    return jsonify(ok=True, **result)


@bp.route("/api/operations/inventory/<iid>/test-connection", methods=["POST"])
def inventory_test_connection(iid):
    principal = actor(write=True)
    visible_id(principal, iid)
    from modules.web.app import _conn_test_with_timeout
    from modules.core.data_security import redact

    source = resources()[0].manager.get_instance_decrypted(iid)
    result = _conn_test_with_timeout(source["db_type"], source, "pro")
    # Return the standard driver's connection result through the saved-source entry.
    return jsonify(
        ok=True,
        connected=bool(result.get("ok")),
        message=str(
            redact(
                result.get("msg")
                or result.get("message")
                or result.get("error")
                or "连接检查完成"
            )
        ),
    )


@bp.route("/api/operations/batches/<run_id>/cancel", methods=["POST"])
def batch_cancel(run_id):
    actor(write=True)
    run = authorized_run(run_id)
    if run["status"] not in TERMINAL:
        resources()[1].store.request_cancel(run_id)
    return jsonify(ok=True)


@bp.route("/api/operations/batches/<run_id>/retry", methods=["POST"])
def batch_retry(run_id):
    principal = actor(write=True)
    run = authorized_run(run_id)
    if run["status"] not in TERMINAL:
        raise Conflict("请等待原任务停止后重试")
    ids = [
        member["instance_id"]
        for member in run["payload"]["members"]
        if member["status"] in {"error", "interrupted", "cancelled"}
    ]
    if not ids:
        raise ValueError("没有需要重试的数据源")
    spec = {
        key: value
        for key, value in run["payload"].items()
        if key
        in {
            "inspector",
            "concurrency",
            "timeout_seconds",
            "templates",
            "cluster_concurrency",
            "execution_window",
            "query_timeout",
            "row_limit",
            "notify_on_completion",
        }
    }
    spec.update(
        name=run["payload"]["name"] + " · 失败项重试",
        instance_ids=ids,
        cluster_ids=[],
        request_key=body().get("request_key"),
    )
    return jsonify(ok=True, run=public_run(submit_batch(principal, spec))), 202


def finding_store():
    from .findings import Findings

    inventory, service, _ = resources()
    return Findings(service.store, inventory)


@bp.route("/api/operations/findings", methods=["GET", "POST"])
def findings_collection():
    from .findings import issue_key

    principal = actor(write=request.method == "POST")
    if request.method == "GET":
        return jsonify(
            ok=True,
            records=finding_store().list(principal, set(visible_ids(principal))),
        )
    data = body()
    run = authorized_run(data.get("run_id"))
    iid = data.get("instance_id")
    visible_id(principal, iid)
    return jsonify(
        ok=True,
        record=finding_store().create(principal, run, iid, data.get("fingerprint")),
    )


@bp.route("/api/operations/findings/<fid>", methods=["PUT"])
def finding_update(fid):
    principal = actor(write=True)
    return jsonify(
        ok=True,
        record=finding_store().update(
            principal, fid, body(), set(visible_ids(principal))
        ),
    )


@bp.route("/api/operations/findings/<fid>/recheck", methods=["POST"])
def finding_recheck(fid):
    principal = actor(write=True)
    records = {
        record["id"]: record
        for record in finding_store().list(principal, set(visible_ids(principal)))
    }
    if fid not in records:
        raise LookupError("整改记录不存在")
    record = records[fid]
    original = authorized_run(record["source_run"])
    spec = {
        key: value
        for key, value in original["payload"].items()
        if key
        in {
            "inspector",
            "concurrency",
            "timeout_seconds",
            "templates",
            "cluster_concurrency",
            "execution_window",
            "query_timeout",
            "row_limit",
            "notify_on_completion",
        }
    }
    spec.update(
        name="整改复检 · " + record["name"],
        instance_ids=[record["instance_id"]],
        cluster_ids=[],
        request_key=body().get("request_key"),
    )
    run = submit_batch(principal, spec)
    finding_store().update(
        principal,
        fid,
        {
            "version": record["version"],
            "verification_run": run["id"],
            "note": "已发起复检任务",
        },
        set(visible_ids(principal)),
    )
    return jsonify(ok=True, run=public_run(run)), 202


@bp.route("/api/operations/findings/export")
def findings_export():
    import io
    from openpyxl import Workbook
    from .findings import STATES

    principal = actor()
    book = Workbook()
    page = book.active
    page.title = "巡检整改清单"
    page.append(
        [
            "数据库",
            "问题",
            "风险等级",
            "状态",
            "负责人",
            "计划完成日期",
            "处理措施",
            "原巡检任务",
            "复检任务",
        ]
    )
    for item in finding_store().list(principal, set(visible_ids(principal))):
        page.append(
            [
                item["name"],
                item["description"],
                item["level"],
                STATES[item["state"]],
                item["assignee"],
                item["deadline"],
                item["action"],
                item["source_run"],
                item["verification_run"],
            ]
        )
        for cell in page[page.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    page.freeze_panes = "A2"
    page.auto_filter.ref = page.dimensions
    output = io.BytesIO()
    book.save(output)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="巡检整改清单.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/api/operations/groups", methods=["GET", "POST"])
def groups_collection():
    principal = actor(write=request.method == "POST")
    inventory, service, clusters = resources()
    store = Groups(service.store, inventory)
    if request.method == "POST":
        return jsonify(ok=True, record=store.save(principal, body())), 201
    records = inventory.list(visible_ids(principal))
    definitions = clusters.list(principal)
    return jsonify(
        ok=True,
        records=store.list(principal, records, definitions),
        facets=facets(records, definitions),
        fields=GROUP_FIELDS,
    )


@bp.route("/api/operations/groups/<gid>", methods=["PUT", "DELETE"])
def group_record(gid):
    principal = actor(write=True)
    inventory, service, _ = resources()
    store = Groups(service.store, inventory)
    data = body()
    if request.method == "DELETE":
        store.delete(principal, gid, data.get("version"))
        return jsonify(ok=True)
    return jsonify(ok=True, record=store.save(principal, data, gid))


@bp.route("/api/operations/inventory/grouping", methods=["PUT"])
def inventory_grouping():
    principal = actor(write=True)
    inventory = resources()[0]
    data = body()
    ids = data.get("instance_ids")
    revisions = data.get("revisions")
    metadata = data.get("metadata", {})
    source = data.get("source", {})
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > 1000
        or any(not isinstance(iid, str) for iid in ids)
        or not isinstance(revisions, dict)
    ):
        raise ValueError("请选择数据源并提供最新版本")
    if not set(ids) <= set(visible_ids(principal)):
        raise PermissionError("批量分组包含无权维护的数据源")
    if (
        not isinstance(metadata, dict)
        or set(metadata) - {"system", "environment", "owner"}
        or not isinstance(source, dict)
        or set(source) - {"group"}
        or not (metadata or source)
    ):
        raise ValueError("请选择要修改的分组字段")
    if any(
        not isinstance(value, str) or len(value) > 200
        for value in [*metadata.values(), *source.values()]
    ):
        raise ValueError("分组字段须为最多200字符的文本")
    metadata = {key: value.strip() for key, value in metadata.items()}
    source = {key: value.strip() for key, value in source.items()}
    with inventory.connection(write=True) as db:
        for iid in dict.fromkeys(ids):
            inventory._save(
                principal,
                {
                    "revision": revisions.get(iid),
                    "source": source,
                    "metadata": metadata,
                },
                iid=iid,
                db=db,
            )
    inventory._refresh()
    return jsonify(ok=True, updated=len(set(ids)))


@bp.route("/api/operations/groups/preview", methods=["POST"])
def group_preview():
    principal = actor(write=True)
    inventory, _, clusters = resources()
    records = select_group(
        inventory.list(visible_ids(principal)),
        normalize_filters(body().get("filters", {})),
        clusters.list(principal),
    )
    return jsonify(
        ok=True,
        records=[
            {
                "id": record["id"],
                "name": record["source"]["name"],
                "system": record["metadata"].get("system", ""),
                "environment": record["metadata"].get("environment", ""),
                "owner": record["metadata"].get("owner", ""),
            }
            for record in records
            if record["source"].get("enabled")
        ],
    )
