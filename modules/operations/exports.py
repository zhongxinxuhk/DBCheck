# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""数据库运维中台的资产台账与多数据源巡检。"""


import io
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .inventory import ASSET_FIELDS, SOURCE_FIELDS, TABLES


def display(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def inventory_xlsx(records, history):
    """从已授权、已解密的台账快照导出原值，文本单元格按文本保存。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.page import PageMargins

    workbook = Workbook()
    workbook.remove(workbook.active)
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")

    def sheet(title, headers, rows):
        page = workbook.create_sheet(title)
        end = get_column_letter(len(headers))
        page.merge_cells("A1:" + end + "1")
        page["A1"] = "DBPulse · " + title
        page["A1"].font = Font(name="微软雅黑", size=16, bold=True, color="17365D")
        page.row_dimensions[1].height = 32
        page.merge_cells("A2:" + end + "2")
        page["A2"] = (
            f"导出时间 {timestamp}  资产数 {len(records)}  明细行数 {len(rows)}  数据按登记原值导出"
        )
        page["A2"].font = Font(name="微软雅黑", size=10, color="596979")
        page.row_dimensions[2].height = 26
        for column, header in enumerate(headers, 1):
            cell = page.cell(3, column, header)
            cell.font = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="234F77")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            page.column_dimensions[get_column_letter(column)].width = (
                24
                if any(
                    t in header
                    for t in ("说明", "路径", "配置", "内容", "权限", "记录", "密码")
                )
                else 19
            )
        page.row_dimensions[3].height = 34
        for index, row in enumerate(rows, 4):
            for column, value in enumerate(row, 1):
                cell = page.cell(index, column, display(value))
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                cell.font = Font(name="微软雅黑", size=10)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            page.row_dimensions[index].height = min(
                100,
                max(
                    30,
                    15
                    * max(
                        2, max((len(str(display(v))) // 25 + 1 for v in row), default=2)
                    ),
                ),
            )
        page.freeze_panes = "C4"
        page.sheet_view.showGridLines = False
        page.auto_filter.ref = f"A3:{end}{max(3,page.max_row)}"
        if rows:
            table = Table(
                displayName="Inventory" + str(len(workbook.worksheets)),
                ref=page.auto_filter.ref,
            )
            table.tableStyleInfo = TableStyleInfo(
                name="TableStyleMedium2", showRowStripes=True
            )
            page.add_table(table)
        page.print_title_rows = "1:3"
        page.print_options.horizontalCentered = True
        page.sheet_properties.pageSetUpPr.fitToPage = True
        page.page_setup.orientation = "landscape"
        page.page_setup.paperSize = page.PAPERSIZE_A3
        page.page_setup.fitToWidth = 1
        page.page_setup.fitToHeight = 0
        page.page_margins = PageMargins(
            left=0.25, right=0.25, top=0.4, bottom=0.4, header=0.15, footer=0.15
        )
        page.oddFooter.center.text = "第 &P 页 / 共 &N 页"
        page.oddFooter.right.text = "DBPulse / DBCheck"

    base = ["配置项编号", "资产名称"]
    asset_keys = [
        "project_id",
        "deployment",
        "hardware",
        "software_version",
        "version",
        "system",
        "business",
        "environment",
        "owner",
        "online_date",
        "lifecycle",
        "cluster_id",
        "inventory_date",
        "baseline_version",
        "change_reference",
        "remarks",
    ]
    source_keys = ["db_type", "host", "port", "user", "password", "group", "enabled"]
    rows = []
    for r in records:
        m, s = r["metadata"], r["source"]
        rows.append(
            [r["configuration_id"], s["name"]]
            + [s.get(k, "") for k in source_keys]
            + [m.get(k, "") for k in asset_keys]
        )
    sheet(
        "全品类数据库资产底账清单",
        base
        + [SOURCE_FIELDS[k] for k in source_keys]
        + [ASSET_FIELDS[k] for k in asset_keys],
        rows,
    )

    cluster_keys = [
        "project_id",
        "cluster_id",
        "cluster_type",
        "node_count",
        "node_names",
        "architecture",
        "cluster_network",
    ]
    rows = []
    for r in records:
        m = r["metadata"]
        for item in m.get("tables", {}).get("cluster_parameters", []) or [{}]:
            rows.append(
                [r["configuration_id"], r["source"]["name"]]
                + [m.get(k, "") for k in cluster_keys]
                + [item.get(k, "") for k in TABLES["cluster_parameters"][1]]
            )
    sheet(
        "集群参数表",
        base
        + [ASSET_FIELDS[k] for k in cluster_keys]
        + list(TABLES["cluster_parameters"][1].values()),
        rows,
    )

    for key in ("database_parameters", "tablespaces", "accounts", "backup_policies"):
        title, columns = TABLES[key]
        rows = [
            [r["configuration_id"], r["source"]["name"]]
            + [item.get(k, "") for k in columns]
            for r in records
            for item in r["metadata"].get("tables", {}).get(key, [])
        ]
        sheet(title, base + list(columns.values()), rows)
    basic_keys = [
        "instance_name",
        "charset",
        "file_size",
        "archive_path",
        "archive_mode",
        "host_model",
        "location",
        "os_version",
        "storage_platform",
        "version",
        "patch_level",
        "storage_mode",
        "backup_policy",
        "disaster_recovery",
        "cluster_attributes",
    ]
    rows = [
        [r["configuration_id"], r["source"]["name"], r["source"].get("database", "")]
        + [r["metadata"].get(k, "") for k in basic_keys]
        for r in records
    ]
    sheet(
        "数据库基本信息表",
        base + ["数据库名称"] + [ASSET_FIELDS[k] for k in basic_keys],
        rows,
    )
    # 将客户要求的七类台账放在前面，连接扩展和审计放在末尾。
    order = [
        "全品类数据库资产底账清单",
        "集群参数表",
        "数据库参数表",
        "数据库基本信息表",
        "逻辑存储空间属性表",
        "用户及权限表",
        "备份策略表",
    ]
    for position, title in enumerate(order):
        workbook.move_sheet(
            workbook[title], offset=position - workbook.index(workbook[title])
        )
    extras = [k for k in SOURCE_FIELDS if k not in source_keys and k != "name"]
    sheet(
        "数据源连接配置",
        base + [SOURCE_FIELDS[k] for k in extras],
        [
            [r["configuration_id"], r["source"]["name"]]
            + [r["source"].get(k, "") for k in extras]
            for r in records
        ],
    )

    def flatten(value, prefix=""):
        result = {}
        if isinstance(value, dict):
            for key, item in value.items():
                result.update(
                    flatten(item, prefix + "." + str(key) if prefix else str(key))
                )
        elif isinstance(value, list):
            for index, item in enumerate(value):
                result.update(flatten(item, prefix + "[" + str(index) + "]"))
        else:
            result[prefix] = value
        return result

    changes = []
    for h in history:
        details = h["details"]
        before = details.get("before") or {}
        after = details.get("after") or {}
        left = flatten({k: before.get(k, {}) for k in ("source", "metadata")})
        right = flatten({k: after.get(k, {}) for k in ("source", "metadata")})
        for key in sorted(left.keys() | right.keys()):
            if left.get(key) != right.get(key):
                changes.append(
                    [
                        h["instance_id"],
                        h["created"],
                        h["actor"],
                        h["action"],
                        key,
                        left.get(key, ""),
                        right.get(key, ""),
                    ]
                )
        for key in details.get("credential_fields_updated", []):
            changes.append(
                [
                    h["instance_id"],
                    h["created"],
                    h["actor"],
                    h["action"],
                    key,
                    "",
                    "已更新凭据",
                ]
            )
    sheet(
        "台账变更记录",
        [
            "数据源编号",
            "变更时间（UTC）",
            "操作人编号",
            "操作",
            "字段路径",
            "变更前取值",
            "变更后取值",
        ],
        changes,
    )
    result = io.BytesIO()
    workbook.save(result)
    result.seek(0)
    return result


STATUS = {
    "cancelled": "已取消",
    "queued": "待执行",
    "running": "执行中",
    "unconfirmed": "超时待核对",
    "done": "已生成报告",
    "error": "失败",
    "interrupted": "中断",
    "completed": "完成",
    "partial": "部分完成",
    "failed": "全部失败",
}


def batch_docx(run, reports_dir):
    from docx import Document
    from docx.shared import Cm, Pt
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docxcompose.composer import Composer
    from .batch import summary

    doc = Document()
    section = doc.sections[0]
    section.left_margin = section.right_margin = Cm(1.8)
    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(10)
    style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "微软雅黑")
    doc.add_heading(run["payload"]["name"], 0)
    doc.add_paragraph("多数据源整体巡检报告")
    dt = lambda ts: (
        datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        if ts
        else "尚未结束"
    )
    doc.add_paragraph(
        "执行时间 "
        + dt(run["created"])
        + " 至 "
        + dt(run["payload"].get("finished_at"))
    )
    doc.add_paragraph("巡检人员 " + run["payload"]["inspector"])
    doc.add_paragraph("任务编号 " + run["id"])
    counts = summary(run)
    doc.add_heading("一、执行范围与覆盖情况", 1)
    doc.add_paragraph(
        f"共纳入 {counts['total']} 个数据源，报告生成 {counts['done']} 个，失败或中断 {counts['failed']} 个，采集存在缺项提示 {counts['incomplete']} 个，已停止未执行 {counts.get('cancelled', 0)} 个。"
    )
    if (
        counts["failed"]
        or counts["active"]
        or counts["queued"]
        or counts["incomplete"]
        or counts.get("cancelled")
    ):
        doc.add_paragraph("本次存在失败、未完成或采集缺项，整体结论需结合缺项复核。")
    doc.add_paragraph("报告中的IP、端口、账号及台账业务信息按实际登记值展示。")

    def table(headers, rows):
        t = doc.add_table(rows=1, cols=len(headers), style="Table Grid")
        for cell, text in zip(t.rows[0].cells, headers):
            cell.text = text
        repeat = OxmlElement("w:tblHeader")
        t.rows[0]._tr.get_or_add_trPr().append(repeat)
        for row in rows:
            for cell, value in zip(t.add_row().cells, row):
                cell.text = str(value if value is not None else "未采集")
        return t

    table(
        ["数据源", "数据库类型", "地址", "所属集群", "执行结果", "风险数"],
        [
            [
                m["name"],
                m["db_type"],
                f"{m['host']}:{m['port']}",
                m.get("cluster_name") or "独立实例",
                STATUS.get(m["status"], m["status"])
                + ("（有采集缺项）" if m.get("warnings") else ""),
                (
                    m.get("result", {}).get("risk_count")
                    if m["status"] == "done"
                    else "未取得有效结果"
                ),
            ]
            for m in run["payload"]["members"]
        ],
    )
    comparison = run["payload"].get("comparison", [])
    if comparison:
        doc.add_heading("与上次巡检对比", 1)
        for item in comparison:
            doc.add_heading(item["name"], 2)
            doc.add_paragraph(
                "新增问题 %s，持续问题 %s，已恢复问题 %s，待核对问题 %s"
                % tuple(
                    len(item.get(key, []))
                    for key in ("new", "persistent", "resolved", "unverified")
                )
            )
            if item.get("reason"):
                doc.add_paragraph(item["reason"])
            for key, label in (
                ("new", "新增"),
                ("persistent", "持续"),
                ("resolved", "已恢复"),
                ("unverified", "待核对"),
            ):
                for issue in item.get(key, []):
                    doc.add_paragraph(label + " · " + issue["description"])
            for key, change in item.get("changes", {}).items():
                label = {
                    "health_score": "健康评分",
                    "risk_count": "风险数",
                    "risk_level": "风险等级",
                }.get(key, key)
                doc.add_paragraph(
                    label + " · " + str(change["before"]) + " → " + str(change["after"])
                )
    doc.add_heading("二、集群与业务分组汇总", 1)
    grouped = defaultdict(list)
    for m in run["payload"]["members"]:
        grouped[m.get("cluster_name") or "独立实例"].append(m)
    table(
        ["集群/分组", "纳入数", "完成数", "失败/中断数"],
        [
            [
                name,
                len(ms),
                sum(m["status"] == "done" for m in ms),
                sum(m["status"] in {"error", "interrupted"} for m in ms),
            ]
            for name, ms in grouped.items()
        ],
    )
    scope = run["payload"].get("scope_snapshot", [])
    if scope:
        doc.add_heading("业务分组与本次纳入范围", 1)
        table(
            ["分组/范围", "筛选条件", "本次纳入数"],
            [
                [item["name"], item["description"], len(item["instance_ids"])]
                for item in scope
            ],
        )
    for key, label in (
        ("system", "业务系统"),
        ("environment", "运行环境"),
        ("owner", "运维责任人"),
    ):
        sections = defaultdict(list)
        for member in run["payload"]["members"]:
            sections[member.get("asset_metadata", {}).get(key) or "未登记"].append(
                member
            )
        table(
            [label, "纳入数", "完成数", "失败/中断数"],
            [
                [
                    name,
                    len(items),
                    sum(item["status"] == "done" for item in items),
                    sum(item["status"] in {"error", "interrupted"} for item in items),
                ]
                for name, items in sections.items()
            ],
        )
    doc.add_heading("三、风险与未完成项", 1)
    for m in run["payload"]["members"]:
        issues = m.get("result", {}).get("issues") or []
        if m.get("error") or m.get("warnings") or issues:
            doc.add_heading(m["name"] + " · " + m["host"], 2)
            if m.get("error"):
                doc.add_paragraph(m["error"])
            for warning in m.get("warnings", []):
                doc.add_paragraph(str(warning))
            if issues:
                table(
                    ["风险等级", "问题描述", "处理建议"],
                    [
                        (
                            [
                                issue.get("level", issue.get("col2", "")),
                                issue.get("description", issue.get("col1", "")),
                                issue.get("suggestion", issue.get("col3", "")),
                            ]
                            if isinstance(issue, dict)
                            else ["", str(issue), ""]
                        )
                        for issue in issues
                    ],
                )
    doc.add_heading("四、各数据源完整巡检明细", 1)
    composer = Composer(doc)
    for index, m in enumerate(run["payload"]["members"], 1):
        doc.add_page_break()
        doc.add_heading(f"4.{index} {m['name']} · {m['host']}:{m['port']}", 1)
        raw = m.get("report_file")
        path = Path(raw).resolve() if raw else None
        if path and path.is_relative_to(Path(reports_dir).resolve()) and path.is_file():
            try:
                child = Document(path)
            except Exception:
                doc.add_paragraph("该成员的明细报告无法读取，须重新核对。")
            else:
                composer.append(child)
        else:
            doc.add_paragraph(m.get("error") or "该数据源尚未取得有效明细报告。")
    return composer
