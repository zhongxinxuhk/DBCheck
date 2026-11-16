# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""DBCheck 独立离线授权控制台。

这是一个**独立工具**：只依赖 Python 标准库（图形界面使用 tkinter），不导入
DBCheck 的 Flask 应用、模块或运行时配置，也不会被 DBCheck 的 PyInstaller
构建和发布包收集。可单独复制本文件到授权签发机运行：

    python license_console.py

生成的 ``DBP2`` 授权码可粘贴到 DBCheck「系统信息 / 设备与激活」页面完成
激活。授权算法与主程序 ``modules/system_info/license.py`` 保持协议兼容；
如需自定义签名密钥，请让本工具和主程序使用同一个环境变量
``DBPULSE_ACTIVATION_SECRET``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import date, datetime, timedelta

# 与主程序授权校验保持一致的协议常量。修改时必须同时更新主程序。
_CODE_PREFIX = "DBP2"
_LEGACY_CODE_PREFIX = "DBP1"
_SIGNATURE_LENGTH = 64
_LEGACY_SIGNATURE_LENGTH = 20
_SECRET = os.environ.get(
    "DBPULSE_ACTIVATION_SECRET",
    "DBPulse-Offline-Activation-2026",
).encode("utf-8")

EDITION_OPTIONS = ("业务专用版", "商业专用版", "专业版", "企业版", "社区版")
DEFAULT_EDITION = "业务专用版"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _signature(payload_b64: str, prefix: str = _CODE_PREFIX, length: int = _SIGNATURE_LENGTH) -> str:
    message = f"{prefix}.{payload_b64}".encode("ascii")
    return hmac.new(_SECRET, message, hashlib.sha256).hexdigest().upper()[:length]


def new_license_id() -> str:
    """生成可读的授权编号，例如 ``LIC-20261116-1A2B3C4D``。"""
    return "LIC-" + datetime.now().strftime("%Y%m%d") + "-" + secrets.token_hex(4).upper()


def _parse_date(value: str):
    """兼容日期和日期时间字符串，返回 date；空值返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def issue_activation_code(
    licensed_to_type: str,
    licensed_to: str,
    deployment_org: str = "",
    edition: str = DEFAULT_EDITION,
    expires_at: str = "",
    device_serial: str = "",
    issued_at: str = "",
    license_id: str = "",
    valid_days: int = 0,
    code_version: int = 2,
) -> str:
    """生成离线授权码。

    ``code_version=2`` 生成当前 ``DBP2`` 授权码；``code_version=1`` 仅用于
    兼容测试。``expires_at`` 为空且 ``valid_days`` 大于 0 时，从今天起算；
    两者都为空表示长期有效。
    """
    serial = str(device_serial or "").strip().upper()
    if not serial:
        raise ValueError("device_serial is required")
    to = (licensed_to or "").strip()
    if not to:
        raise ValueError("licensed_to is required")
    if licensed_to_type not in {"personal", "organization"}:
        raise ValueError("licensed_to_type must be personal or organization")
    if code_version not in {1, 2}:
        raise ValueError("code_version must be 1 or 2")

    days = int(valid_days or 0)
    if days < 0:
        raise ValueError("valid_days must not be negative")
    expiry = _parse_date(expires_at)
    if days and expiry is None:
        expiry = date.today() + timedelta(days=days)
    expires_value = expiry.isoformat() if expiry else ""

    issued_value = str(issued_at or datetime.now().isoformat(timespec="seconds")).strip()
    lid = str(license_id or "").strip()
    if code_version == 2 and not lid:
        lid = new_license_id()

    payload = {
        "v": code_version,
        "typ": licensed_to_type,
        "to": to,
        "dep": (deployment_org or "").strip(),
        "ed": (edition or DEFAULT_EDITION).strip(),
        "iat": issued_value,
        "exp": expires_value,
        "dev": serial,
    }
    if code_version == 2:
        payload.update({"lid": lid, "days": days})

    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64encode(raw)
    prefix = _CODE_PREFIX if code_version == 2 else _LEGACY_CODE_PREFIX
    length = _SIGNATURE_LENGTH if code_version == 2 else _LEGACY_SIGNATURE_LENGTH
    return f"{prefix}.{payload_b64}.{_signature(payload_b64, prefix, length)}"


def _update_expiry(days_var, expiry_var) -> None:
    text = days_var.get().strip()
    try:
        days = int(text)
        if days < 1 or days > 36500:
            raise ValueError
        expiry_var.set("预计到期：" + (date.today() + timedelta(days=days)).isoformat())
    except (TypeError, ValueError):
        expiry_var.set("预计到期：请输入 1–36500 天")


def _copy_code(root, code_text) -> None:
    from tkinter import messagebox

    code = code_text.get("1.0", "end").strip()
    if not code:
        messagebox.showinfo("复制授权码", "当前没有可复制的授权码。", parent=root)
        return
    root.clipboard_clear()
    root.clipboard_append(code)
    root.update()
    messagebox.showinfo("复制授权码", "授权码已复制到剪贴板。", parent=root)


def _clear_code(code_text) -> None:
    code_text.delete("1.0", "end")


def _build_gui() -> "object":
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("DBCheck 授权控制台（独立版）")
    root.geometry("980x720")
    root.minsize(820, 640)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Title.TLabel", font=("TkDefaultFont", 17, "bold"))
    style.configure("Hint.TLabel", foreground="#64748b")

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(3, weight=1)

    header = ttk.Frame(outer)
    header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
    ttk.Label(header, text="DBCheck 授权控制台", style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        header,
        text="独立离线签发工具，不连接服务器、不依赖主程序；授权码与设备序列号绑定。",
        style="Hint.TLabel",
    ).pack(anchor="w", pady=(3, 0))

    form = ttk.LabelFrame(outer, text="授权信息", padding=12)
    form.grid(row=1, column=0, sticky="ew")
    form.columnconfigure(1, weight=1)

    serial_var = tk.StringVar()
    type_var = tk.StringVar(value="单位")
    to_var = tk.StringVar()
    org_var = tk.StringVar()
    edition_var = tk.StringVar(value=DEFAULT_EDITION)
    activated_var = tk.StringVar()
    days_var = tk.StringVar(value="365")
    license_id_var = tk.StringVar(value=new_license_id())
    expiry_var = tk.StringVar()

    def add_field(row, label, widget):
        ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        widget.grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)

    serial_entry = ttk.Entry(form, textvariable=serial_var)
    add_field(0, "设备序列号 *", serial_entry)

    ttk.Label(form, text="授权类型 *").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=5)
    ttk.Combobox(
        form, textvariable=type_var, state="readonly", values=("单位", "个人")
    ).grid(row=1, column=1, sticky="w", pady=5)

    add_field(2, "授权对象 *", ttk.Entry(form, textvariable=to_var))
    add_field(3, "部署单位", ttk.Entry(form, textvariable=org_var))

    ttk.Label(form, text="授权版本 *").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=5)
    ttk.Combobox(
        form,
        textvariable=edition_var,
        state="normal",
        values=EDITION_OPTIONS,
    ).grid(row=4, column=1, sticky="ew", pady=5)

    ttk.Label(form, text="激活时间").grid(row=5, column=0, sticky="w", padx=(0, 12), pady=5)
    ttk.Entry(form, textvariable=activated_var, state="readonly").grid(
        row=5, column=1, columnspan=2, sticky="ew", pady=5
    )

    days_row = ttk.Frame(form)
    ttk.Spinbox(days_row, from_=1, to=36500, textvariable=days_var, width=10).pack(side="left")
    ttk.Label(days_row, text="天").pack(side="left", padx=(6, 12))
    ttk.Label(days_row, textvariable=expiry_var, style="Hint.TLabel").pack(side="left")
    days_var.trace_add("write", lambda *_args: _update_expiry(days_var, expiry_var))
    add_field(6, "到期天数 *", days_row)

    id_row = ttk.Frame(form)
    ttk.Entry(id_row, textvariable=license_id_var, state="readonly").pack(
        side="left", fill="x", expand=True
    )
    ttk.Button(
        id_row,
        text="重新生成",
        width=10,
        command=lambda: license_id_var.set(new_license_id()),
    ).pack(side="left", padx=(8, 0))
    add_field(7, "授权编号", id_row)

    actions = ttk.Frame(outer)
    actions.grid(row=2, column=0, sticky="ew", pady=12)
    code_frame = ttk.LabelFrame(outer, text="授权码（完整复制）", padding=10)
    code_frame.grid(row=3, column=0, sticky="nsew")
    code_frame.columnconfigure(0, weight=1)
    code_frame.rowconfigure(0, weight=1)
    code_text = tk.Text(code_frame, height=15, wrap="word", font=("TkFixedFont", 10), undo=True)
    code_text.grid(row=0, column=0, sticky="nsew")
    scrollbar = ttk.Scrollbar(code_frame, orient="vertical", command=code_text.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    code_text.configure(yscrollcommand=scrollbar.set)

    def _set_activation_now():
        activated_var.set(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def _generate():
        try:
            serial = serial_var.get().strip().upper()
            target = to_var.get().strip()
            edition = edition_var.get().strip()
            if not serial:
                raise ValueError("请输入设备序列号")
            if not target:
                raise ValueError("请输入授权对象")
            if not edition:
                raise ValueError("请选择或输入授权版本")
            days = int(days_var.get().strip())
            if days < 1 or days > 36500:
                raise ValueError("到期天数须在 1 到 36500 之间")

            now = datetime.now()
            expiry = date.today() + timedelta(days=days)
            _set_activation_now()
            code = issue_activation_code(
                licensed_to_type="organization" if type_var.get() == "单位" else "personal",
                licensed_to=target,
                deployment_org=org_var.get().strip(),
                edition=edition,
                expires_at=expiry.isoformat(),
                device_serial=serial,
                issued_at=now.isoformat(timespec="seconds"),
                license_id=license_id_var.get().strip(),
                valid_days=days,
            )
            code_text.delete("1.0", "end")
            code_text.insert("1.0", code)
            code_text.see("1.0")
            status_var.set(f"已生成：{license_id_var.get()}，有效期至 {expiry.isoformat()}")
        except Exception as exc:
            status_var.set(f"生成失败：{exc}")
            messagebox.showerror("生成失败", str(exc), parent=root)

    ttk.Button(actions, text="生成授权码", command=_generate).pack(side="left")
    ttk.Button(
        actions,
        text="复制授权码",
        command=lambda: _copy_code(root, code_text),
    ).pack(side="left", padx=(8, 0))
    ttk.Button(
        actions, text="清空授权码", command=lambda: _clear_code(code_text)
    ).pack(side="left", padx=(8, 0))
    status_var = tk.StringVar(value="填写信息后点击“生成授权码”。")
    ttk.Label(actions, textvariable=status_var, style="Hint.TLabel").pack(
        side="left", padx=(14, 0)
    )

    _set_activation_now()
    _update_expiry(days_var, expiry_var)
    serial_entry.focus_set()
    return root


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("未找到 tkinter。请安装带 Tk 支持的 Python（Linux 通常安装 python3-tk）。")
        return 2

    try:
        root = _build_gui()
    except Exception as exc:
        print(f"授权控制台启动失败：{exc}")
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
