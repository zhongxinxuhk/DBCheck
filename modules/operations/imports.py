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
# 旧版台账模板的标题和表头名称。只做“旧名 -> 现行名”的补全，不覆盖现行列。
MAIN_TITLES = {
    "全品类数据库资产底账清单",
    "资产台账",
    "数据库资产台账",
    "资产底账清单",
    "资产底账",
    "全品类数据库资产底账",
}
SHEET_ALIASES = {
    "数据库基本信息": "数据库基本信息表",
    "数据库基本信息表": "数据库基本信息表",
    "数据源连接": "数据源连接配置",
    "数据源连接表": "数据源连接配置",
    "数据源连接配置表": "数据源连接配置",
    "集群参数": "集群参数表",
    "数据库参数": "数据库参数表",
    "逻辑存储空间": "逻辑存储空间属性表",
    "用户及权限": "用户及权限表",
    "备份策略": "备份策略表",
    "台账变更": "台账变更记录",
    "变更记录": "台账变更记录",
}
IGNORED_SHEETS = {
    "说明",
    "使用说明",
    "模板说明",
    "台账使用说明",
    "填写说明",
    "导入说明",
    "版本信息",
    "README",
    "Sheet",
    "Sheet1",
}
HEADER_ALIASES = {
    "配置项编号": ("配置编号", "CI编号", "资产编号"),
    "资产名称": (
        "数据源名称",
        "资产/数据源名称",
        "资产名",
        "数据库名称",
    ),
    "数据库IP/主机": (
        "数据库地址",
        "数据库IP",
        "IP地址",
        "主机地址",
        "数据库主机",
        "数据库主机地址",
        "主机",
    ),
    "数据库端口": ("端口", "端口号", "数据库端口号"),
    "数据库账号": ("账号", "用户名", "数据库用户", "数据库用户名", "用户"),
    "数据库密码": ("密码", "数据库口令", "口令"),
    "数据源分组": ("分组", "组名"),
    "启用数据源": ("启用", "是否启用", "启用状态"),
    "数据库名称": ("数据库名", "库名"),
    "节点": ("节点名称", "节点名"),
    "变更记录": ("变更引用", "变更记录引用"),
    "责任归属人": ("责任人", "负责人"),
    "运维责任人": ("责任人", "负责人", "备份责任人"),
    "具体权限": ("权限", "权限明细"),
    "权限变更记录": ("变更引用", "权限变更引用"),
    "逻辑存储空间名称": ("表空间名称", "存储空间名称"),
    "存储大小": ("容量", "总大小"),
    "自动扩展模式": ("自动扩展",),
    "备份数据类型": ("备份类型", "数据类型"),
    "备份内容": ("内容",),
    "备份方式": ("方式",),
    "保存周期": ("保留周期", "保存天数"),
    "执行时间窗口": ("备份窗口", "执行窗口"),
}


def _text(value):
    return value.strip().lstrip("\ufeff") if isinstance(value, str) else value


def _canonical_row(values):
    """兼容旧表头：仅在现行表头缺失时从旧列名补值。"""
    row = {_text(key): value for key, value in values.items() if key is not None}
    for canonical, aliases in HEADER_ALIASES.items():
        if row.get(canonical) not in (None, ""):
            continue
        for alias in aliases:
            if row.get(alias) not in (None, ""):
                row[canonical] = row[alias]
                break
    return row


def _header_matches(values, require_name):
    labels = {_text(value) for value in values if isinstance(value, str)}
    has_ci = "配置项编号" in labels or any(
        alias in labels for alias in HEADER_ALIASES["配置项编号"]
    )
    if not has_ci or not require_name:
        return has_ci
    return "资产名称" in labels or any(
        alias in labels for alias in HEADER_ALIASES["资产名称"]
    )


def rows(page, require_name=True):
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
            if _header_matches(values, require_name):
                header = values
            elif number > 10:
                raise ValueError(page.title + "缺少标准表头")
            continue
        if number > 10010:
            raise ValueError("单个工作表最多导入10000行")
        if any(value is not None and value != "" for value in values):
            yield number, _canonical_row(dict(zip(header, values)))
    if header is None:
        raise ValueError(page.title + "缺少标准表头")


def _is_blank(page):
    return not any(
        any(value not in (None, "") for value in values)
        for values in page.iter_rows(values_only=True)
    )


def _main_sheet(workbook):
    for page in workbook:
        if _text(page.title) in MAIN_TITLES:
            return page
    # 兼容没有标准标题但保留了标准表头的旧模板。
    for page in workbook:
        try:
            rows(page, require_name=True).__next__()
        except StopIteration:
            return page
        except ValueError:
            continue
        else:
            return page
    return None


def source_value(key, value):
    if key in BOOLEAN_FIELDS:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text not in {"1", "0", "true", "false", "是", "否", "启用", "停用"}:
            raise ValueError("连接开关请填写是或否")
        return text in {"1", "true", "是", "启用"}
    if key in {"port", "ssh_port"}:
        # Excel 重新保存/复制后，整数端口常变成 3306.0、' 3306 ' 或 '3306.0'；
        # 只要数值是 1~65535 的整数就按整数接收，避免导出台账无法回导。
        if isinstance(value, bool):
            raise ValueError("端口必须为1到65535的整数")
        if isinstance(value, int):
            port = value
        elif isinstance(value, float):
            if not value.is_integer():
                raise ValueError("端口必须为1到65535的整数")
            port = int(value)
        else:
            try:
                number = float(str(value).strip())
            except (TypeError, ValueError):
                raise ValueError("端口必须为1到65535的整数") from None
            if not number.is_integer():
                raise ValueError("端口必须为1到65535的整数")
            port = int(number)
        if not 1 <= port <= 65535:
            raise ValueError("端口必须为1到65535的整数")
        return port
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
        main = _main_sheet(workbook)
        if main is None:
            raise ValueError("请使用标准台账导入模板")
        records = {}
        for number, row in rows(main, require_name=True):
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
        seen = {MAIN}
        for page in workbook:
            if page is main:
                continue
            title = SHEET_ALIASES.get(_text(page.title), _text(page.title))
            if title in {MAIN, "台账变更记录"}:
                continue
            if title in seen:
                raise ValueError("台账包含重复工作表：" + page.title)
            seen.add(title)
            if title in IGNORED_SHEETS:
                continue
            if title not in {
                *table_titles,
                "数据库基本信息表",
                "数据源连接配置",
            }:
                if _is_blank(page):
                    continue
                raise ValueError("不支持的工作表：" + page.title)
            if _is_blank(page):
                continue
            for number, row in rows(page, require_name=False):
                ci = str(row.get("配置项编号") or "").strip()
                if ci not in records:
                    raise ValueError(f"{page.title} 第{number}行未匹配到底账配置项")
                record = records[ci]
                if title == "数据源连接配置":
                    record["source"].update(
                        {
                            key: source_value(key, row[label])
                            for key, label in SOURCE_FIELDS.items()
                            if key != "name" and row.get(label) not in (None, "")
                        }
                    )
                else:
                    # 只有基础信息表和集群参数表携带资产级字段；专项表的
                    # “运维责任人/版本”等同名列不能反向覆盖资产主档。
                    if title in {"数据库基本信息表", "集群参数表"}:
                        record["metadata"].update(
                            {
                                key: row[label]
                                for key, label in ASSET_FIELDS.items()
                                if row.get(label) not in (None, "")
                            }
                        )
                    if (
                        title == "数据库基本信息表"
                        and row.get("数据库名称") is not None
                    ):
                        record["source"]["database"] = str(row["数据库名称"])
                    if title in table_titles:
                        key, columns = table_titles[title]
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
