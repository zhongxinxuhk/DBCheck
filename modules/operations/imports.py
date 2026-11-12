# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Excel inventory preview and atomic, replay-safe application."""


import io
import json
import time
import uuid
import zipfile
from datetime import date, datetime

from openpyxl import load_workbook
from .inventory import ASSET_FIELDS, SOURCE_FIELDS, TABLES, BOOLEAN_FIELDS, Conflict

MAIN = "全品类数据库资产底账清单"


def rows(page):
    header = None
    for number, cells in enumerate(page.iter_rows(), 1):
        values = []
        for cell in cells:
            if cell.data_type == "f":
                raise ValueError(f"{page.title} 第{number}行含公式，请粘贴为数值后导入")
            value = cell.value
            if isinstance(value, (date, datetime)):
                value = value.isoformat()
            values.append(value)
        if header is None:
            if "配置项编号" in values and "资产名称" in values:
                header = values
            elif number > 10:
                raise ValueError(page.title + "缺少标准表头")
            continue
        if number > 10010:
            raise ValueError("单个工作表最多导入10000行")
        if any(value is not None and value != "" for value in values):
            yield number, dict(zip(header, values))
    if header is None:
        raise ValueError(page.title + "缺少标准表头")


def source_value(key, value):
    if key in BOOLEAN_FIELDS:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text not in {"1", "0", "true", "false", "是", "否", "启用", "停用"}:
            raise ValueError("连接开关请填写是或否")
        return text in {"1", "true", "是", "启用"}
    if key in {"port", "ssh_port"}:
        if isinstance(value, bool) or not str(value).isdigit():
            raise ValueError("端口必须为整数")
        return int(value)
    if key == "tags":
        if isinstance(value, str) and value.startswith("["):
            return json.loads(value)
        return [part.strip() for part in str(value).split(",") if part.strip()]
    return str(value)


def parse_excel(content):
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("Excel 文件最多10MB")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 200 * 1024 * 1024:
                raise ValueError("Excel 解压内容过大")
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except (zipfile.BadZipFile, KeyError, ValueError, EOFError, OSError) as error:
        raise ValueError("Excel 文件无效或已损坏，请重新下载模板填写") from error
    try:
        if MAIN not in workbook.sheetnames:
            raise ValueError("请使用标准台账导入模板")
        records = {}
        for number, row in rows(workbook[MAIN]):
            ci = str(row.get("配置项编号") or "").strip()
            if not ci or ci in records:
                raise ValueError(f"底账清单第{number}行配置项编号为空或重复")
            source = {
                key: source_value(key, row[label])
                for key, label in SOURCE_FIELDS.items()
                if row.get(label) not in (None, "")
            }
            source["name"] = str(row.get("资产名称") or "").strip()
            metadata = {
                key: row[label]
                for key, label in ASSET_FIELDS.items()
                if row.get(label) not in (None, "")
            }
            metadata["configuration_id"] = ci
            records[ci] = {"source": source, "metadata": metadata}
        if not records:
            raise ValueError("Excel 未填写资产记录")
        table_titles = {
            title: (key, columns) for key, (title, columns) in TABLES.items()
        }
        for page in workbook:
            if page.title in {MAIN, "台账变更记录"}:
                continue
            if page.title not in {*table_titles, "数据库基本信息表", "数据源连接配置"}:
                raise ValueError("不支持的工作表：" + page.title)
            for number, row in rows(page):
                ci = str(row.get("配置项编号") or "").strip()
                if ci not in records:
                    raise ValueError(f"{page.title} 第{number}行未匹配到底账配置项")
                record = records[ci]
                if page.title == "数据源连接配置":
                    record["source"].update(
                        {
                            key: source_value(key, row[label])
                            for key, label in SOURCE_FIELDS.items()
                            if key != "name" and row.get(label) not in (None, "")
                        }
                    )
                else:
                    record["metadata"].update(
                        {
                            key: row[label]
                            for key, label in ASSET_FIELDS.items()
                            if row.get(label) not in (None, "")
                        }
                    )
                    if (
                        page.title == "数据库基本信息表"
                        and row.get("数据库名称") is not None
                    ):
                        record["source"]["database"] = str(row["数据库名称"])
                    if page.title in table_titles:
                        key, columns = table_titles[page.title]
                        item = {
                            key: row[label]
                            for key, label in columns.items()
                            if row.get(label) not in (None, "")
                        }
                        if item:
                            record["metadata"].setdefault("tables", {}).setdefault(
                                key, []
                            ).append(item)
        return list(records.values())
    finally:
        workbook.close()


class InventoryImport:
    def __init__(self, inventory):
        self.inventory = inventory
        with inventory.connection(write=True) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations_imports (token TEXT PRIMARY KEY, tenant INTEGER, owner INTEGER, expires REAL, payload BLOB, result TEXT)"
            )

    def preview(self, actor, content, visible, validate):
        inventory = self.inventory
        current = inventory.list(visible)
        by_ci = {record["configuration_id"]: record for record in current}
        planned = []
        matched_ids = set()

        def endpoint(source):
            return tuple(
                str(source.get(key) or "").strip()
                for key in (
                    "db_type",
                    "host",
                    "port",
                    "user",
                    "database",
                    "service_name",
                )
            )

        by_endpoint = {endpoint(record["source"]): record for record in current}
        for data in parse_excel(content):
            existing = by_ci.get(data["metadata"]["configuration_id"])
            validate(data, existing["source"]["db_type"] if existing else None)
            matched = False
            if existing is None and all(
                data["source"].get(key) for key in ("db_type", "host", "port")
            ):
                existing = by_endpoint.get(endpoint(data["source"]))
                matched = existing is not None
            if existing and existing["id"] in matched_ids:
                raise ValueError("Excel 多行指向同一个已有数据源，请合并重复记录")
            if existing:
                matched_ids.add(existing["id"])
            if existing:
                data["revision"] = existing["revision"]
            planned.append(
                {
                    "id": existing["id"] if existing else uuid.uuid4().hex[:12],
                    "new": existing is None,
                    "matched": matched,
                    "data": data,
                }
            )
        with inventory.connection(write=True) as db:
            db.execute("SAVEPOINT preview")
            try:
                for item in planned:
                    inventory._save(
                        actor,
                        item["data"],
                        iid=None if item["new"] else item["id"],
                        new_id=item["id"],
                        db=db,
                    )
            finally:
                db.execute("ROLLBACK TO preview")
                db.execute("RELEASE preview")
            token = uuid.uuid4().hex
            db.execute("DELETE FROM operations_imports WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO operations_imports VALUES(?,?,?,?,?,NULL)",
                (
                    token,
                    actor.tenant_id,
                    actor.id,
                    time.time() + 3600,
                    inventory.seal(planned),
                ),
            )
        return {
            "token": token,
            "rows": [
                {
                    "id": item["id"],
                    "action": (
                        "新增"
                        if item["new"]
                        else "更新（匹配已有连接）" if item["matched"] else "更新"
                    ),
                    "name": item["data"]["source"]["name"],
                    "configuration_id": item["data"]["metadata"]["configuration_id"],
                    "host": item["data"]["source"].get("host", "沿用现有连接"),
                }
                for item in planned
            ],
        }

    def apply(self, actor, token, visible, own, unown):
        inventory = self.inventory
        created = []
        try:
            with inventory.connection(write=True) as db:
                row = db.execute(
                    "SELECT * FROM operations_imports WHERE token=? AND tenant=? AND owner=?",
                    (token, actor.tenant_id, actor.id),
                ).fetchone()
                if row is None or row["expires"] < time.time():
                    raise ValueError("导入预览已过期，请重新上传")
                if row["result"]:
                    return json.loads(row["result"])
                planned = inventory.open(row["payload"])
                for item in planned:
                    if not item["new"] and item["id"] not in visible:
                        raise PermissionError("导入范围的数据源访问权限已变化")
                    if item["new"]:
                        created.append(item["id"])
                        if own(item["id"]) is None:
                            raise PermissionError("数据源归属登记失败")
                    inventory._save(
                        actor,
                        item["data"],
                        iid=None if item["new"] else item["id"],
                        new_id=item["id"],
                        db=db,
                    )
                result = {
                    "created": len(created),
                    "updated": len(planned) - len(created),
                    "total": len(planned),
                }
                db.execute(
                    "UPDATE operations_imports SET result=? WHERE token=?",
                    (json.dumps(result), token),
                )
        except Exception:
            for iid in created:
                unown(iid)
            raise
        inventory._refresh()
        return result
