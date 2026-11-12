# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Windows status window and notification-area launcher for DBPulse."""


import ctypes
import hashlib
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import urllib.request
import uuid
from pathlib import Path

import psutil

from modules.desktop.local_backup import LocalBackup, RecoveryRequired
from modules.desktop.log_output import LogOutput
from modules.desktop.maintenance import Maintenance, CrashRecovery

ADDRESS = "http://127.0.0.1:5003"
# 大屏为匿名只读页，点击后无需登录即可投屏查看实例状态。
MONITOR_ADDRESS = ADDRESS + "/monitor-screen"
TITLE = "DBPulse 运行状态"


def portable_home(directory):
    configured = os.environ.get("DBPULSE_PORTABLE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    for parent in (directory, *directory.parents):
        if (parent / "dbpulse-portable.json").is_file():
            return parent
    return None


class BackendProcess:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.executable = self.directory / "dbcheck.exe"
        self.metadata = (
            (portable_home(self.directory) or self.directory)
            / "data"
            / "desktop_process.json"
        )
        self.process = None
        self.pid = None
        self.started = None
        self.error = ""
        self.lock = threading.RLock()
        try:
            record = json.loads(self.metadata.read_text(encoding="utf-8"))
            process = psutil.Process(record["pid"])
            if (
                abs(process.create_time() - record["started"]) < 0.01
                and Path(process.exe()).resolve() == self.executable
            ):
                self.pid, self.started = process.pid, record["started"]
        except (OSError, ValueError, KeyError, psutil.Error):
            pass

    def owned_running(self):
        if self.pid is None:
            return False
        try:
            process = psutil.Process(self.pid)
            return (
                abs(process.create_time() - self.started) < 0.01
                and process.is_running()
            )
        except psutil.Error:
            return False

    @staticmethod
    def healthy():
        try:
            with urllib.request.urlopen(
                ADDRESS + "/api/v1/health", timeout=1
            ) as response:
                data = json.load(response)
                return (
                    response.status == 200
                    and isinstance(data, dict)
                    and data.get("ok") is True
                    and data.get("status") == "healthy"
                )
        except Exception:
            return False

    def start(self):
        with self.lock:
            if self.owned_running() or self.healthy():
                return
            if not self.executable.is_file():
                raise FileNotFoundError("未找到 dbcheck.exe，请保持发布目录完整")
            self.metadata.parent.mkdir(parents=True, exist_ok=True)
            logs = self.metadata.parent / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            environment = os.environ.copy()
            environment.update(
                PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1"
            )
            with (logs / "desktop-server.log").open("ab") as output:
                output.write(("\n[DBPulse] 正在启动服务…\n").encode("utf-8"))
                output.flush()
                self.process = subprocess.Popen(
                    [str(self.executable), "web"],
                    cwd=self.directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
            self.pid = self.process.pid
            self.started = psutil.Process(self.pid).create_time()
            self.metadata.write_text(
                json.dumps({"pid": self.pid, "started": self.started}), encoding="utf-8"
            )
            self.error = ""

    def stop(self):
        with self.lock:
            if self.owned_running():
                subprocess.run(
                    ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=20,
                )
                try:
                    psutil.Process(self.pid).wait(timeout=10)
                except psutil.NoSuchProcess:
                    pass
            self.pid = self.started = None
            self.metadata.unlink(missing_ok=True)
            self.error = ""

    @property
    def log_path(self):
        return self.metadata.parent / "logs/desktop-server.log"

    def export_backup(self, destination, progress):
        with self.lock:
            return LocalBackup(self.metadata.parent.parent).export(
                destination, progress
            )

    def import_backup(self, source, progress):
        with self.lock:
            resume = False
            safe_to_restart = True

            def stop_before_apply():
                nonlocal resume
                resume = self.owned_running()
                if not resume and self.healthy():
                    raise RuntimeError(
                        "检测到其他启动方式运行的服务，请先停止该服务后导入备份"
                    )
                if resume:
                    progress("正在停止服务以恢复备份…")
                    self.stop()

            try:
                return LocalBackup(self.metadata.parent.parent).restore(
                    source, progress, stop_before_apply
                )
            except RecoveryRequired:
                safe_to_restart = False
                raise
            finally:
                if resume and safe_to_restart:
                    progress("正在按导入前的状态重新启动服务…")
                    self.start()


class TrayIcon:
    """Win32 notification icon with all Tk actions dispatched through a queue."""

    WM_CALLBACK = 0x8001
    WM_SHOW = 0x8002

    def __init__(self, events, identity):
        self.events = events
        self.identity = identity
        self.hwnd = None
        self.available = False
        self.running = False
        self.busy = False
        self.stoppable = False
        self.stopping = False
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=self._safe_run, name="DBPulseTray", daemon=True
        )
        self.thread.start()

    def _safe_run(self):
        try:
            self._run()
        except Exception as error:
            self.events.put(("error", "无法启用托盘：" + str(error)))
            self.ready.set()

    def _run(self):
        from ctypes import wintypes as w

        user = ctypes.WinDLL("user32", use_last_error=True)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        result_type = ctypes.c_ssize_t
        callback = ctypes.WINFUNCTYPE(result_type, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WindowClass(ctypes.Structure):
            _fields_ = [
                ("style", w.UINT),
                ("procedure", callback),
                ("class_extra", ctypes.c_int),
                ("window_extra", ctypes.c_int),
                ("instance", w.HINSTANCE),
                ("icon", w.HICON),
                ("cursor", w.HANDLE),
                ("background", w.HBRUSH),
                ("menu_name", w.LPCWSTR),
                ("class_name", w.LPCWSTR),
            ]

        class Guid(ctypes.Structure):
            _fields_ = [
                ("a", w.DWORD),
                ("b", w.WORD),
                ("c", w.WORD),
                ("d", ctypes.c_ubyte * 8),
            ]

        class NotifyData(ctypes.Structure):
            _fields_ = [
                ("size", w.DWORD),
                ("hwnd", w.HWND),
                ("id", w.UINT),
                ("flags", w.UINT),
                ("callback", w.UINT),
                ("icon", w.HICON),
                ("tip", w.WCHAR * 128),
                ("state", w.DWORD),
                ("state_mask", w.DWORD),
                ("info", w.WCHAR * 256),
                ("version", w.UINT),
                ("info_title", w.WCHAR * 64),
                ("info_flags", w.DWORD),
                ("guid", Guid),
                ("balloon_icon", w.HICON),
            ]

        kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
        kernel.GetModuleHandleW.restype = w.HMODULE
        user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.DefWindowProcW.restype = result_type
        user.CreateWindowExW.argtypes = [
            w.DWORD,
            w.LPCWSTR,
            w.LPCWSTR,
            w.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            w.HWND,
            w.HMENU,
            w.HINSTANCE,
            w.LPVOID,
        ]
        user.CreateWindowExW.restype = w.HWND
        user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        user.RegisterClassW.restype = w.ATOM
        user.LoadIconW.argtypes = [w.HINSTANCE, w.LPCWSTR]
        user.LoadIconW.restype = w.HICON
        user.CreatePopupMenu.restype = w.HMENU
        user.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
        user.TrackPopupMenu.argtypes = [
            w.HMENU,
            w.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            w.HWND,
            ctypes.POINTER(w.RECT),
        ]
        user.DestroyMenu.argtypes = [w.HMENU]
        user.DestroyWindow.argtypes = [w.HWND]
        user.SetForegroundWindow.argtypes = [w.HWND]
        user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        user.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
        shell.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NotifyData)]
        shell.Shell_NotifyIconW.restype = w.BOOL
        user.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
        user.RegisterWindowMessageW.restype = w.UINT
        user.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
        user.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
        user.DispatchMessageW.restype = result_type
        user.LoadImageW.argtypes = [
            w.HINSTANCE,
            w.LPCWSTR,
            w.UINT,
            ctypes.c_int,
            ctypes.c_int,
            w.UINT,
        ]
        user.LoadImageW.restype = w.HANDLE
        taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")
        resource = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        shared = portable_home(Path(sys.executable).resolve().parent)
        if shared is not None:
            resource = shared
        icon = user.LoadImageW(
            None, str(resource / "assets/brand/favicon.ico"), 1, 16, 16, 0x10
        )
        if not icon:
            icon = user.LoadIconW(None, ctypes.cast(ctypes.c_void_p(32512), w.LPCWSTR))
        instance = kernel.GetModuleHandleW(None)
        actions = {100: "show", 101: "browser", 102: "start", 103: "stop", 104: "quit"}

        def update(add=False):
            data = NotifyData()
            data.size = ctypes.sizeof(data)
            data.hwnd = self.hwnd
            data.id = 1
            data.flags = 1 | 2 | 4
            data.callback = self.WM_CALLBACK
            data.icon = icon
            data.tip = "DBPulse · " + ("运行中" if self.running else "已停止")
            self.available = bool(
                shell.Shell_NotifyIconW(0 if add else 1, ctypes.byref(data))
            )

        def menu():
            popup = user.CreatePopupMenu()
            for identifier, label in [
                (100, "显示运行状态"),
                (101, "打开管理页面"),
                (102, "启动服务"),
                (103, "停止服务"),
                (104, "退出 DBPulse"),
            ]:
                disabled = (
                    (identifier in (102, 103) and self.busy)
                    or (identifier == 102 and self.running)
                    or (identifier == 103 and not self.stoppable)
                )
                user.AppendMenuW(popup, 1 if disabled else 0, identifier, label)
            point = w.POINT()
            user.GetCursorPos(ctypes.byref(point))
            user.SetForegroundWindow(self.hwnd)
            user.TrackPopupMenu(popup, 0, point.x, point.y, 0, self.hwnd, None)
            user.PostMessageW(self.hwnd, 0, 0, 0)
            user.DestroyMenu(popup)

        @callback
        def procedure(hwnd, message, wp, lp):
            if message == self.WM_CALLBACK:
                if lp == 0x203:
                    self.events.put("show")
                elif lp == 0x205:
                    menu()
                return 0
            if message == self.WM_SHOW:
                self.events.put("show")
                return 0
            if message == 0x111 and (wp & 0xFFFF) in actions:
                self.events.put(actions[wp & 0xFFFF])
                return 0
            if message == taskbar_created:
                update(add=True)
                return 0
            if message == 0x113:
                if self.stopping:
                    user.DestroyWindow(hwnd)
                else:
                    update(add=not self.available)
                return 0
            if message == 2:
                data = NotifyData(size=ctypes.sizeof(NotifyData), hwnd=hwnd, id=1)
                shell.Shell_NotifyIconW(2, ctypes.byref(data))
                user.PostQuitMessage(0)
                return 0
            return user.DefWindowProcW(hwnd, message, wp, lp)

        name = "DBPulseTray_" + self.identity
        window_class = WindowClass(
            procedure=procedure, instance=instance, class_name=name
        )
        self.callback = procedure
        if not user.RegisterClassW(ctypes.byref(window_class)):
            self.ready.set()
            return
        self.hwnd = user.CreateWindowExW(
            0, name, name, 0, 0, 0, 0, 0, None, None, instance, None
        )
        update(add=True)
        user.SetTimer(self.hwnd, 1, 1500, None)
        self.ready.set()
        message = w.MSG()
        user.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        user.GetMessageW.restype = ctypes.c_int
        while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user.TranslateMessage(ctypes.byref(message))
            user.DispatchMessageW(ctypes.byref(message))

    def close(self):
        self.stopping = True
        self.thread.join(timeout=3)


def device_serial_number():
    """Encode stable machine information for display by the desktop launcher."""
    machine_id = ""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                machine_id = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
        except OSError:
            pass
    # Keep the identifier independent of installation path and OS patch versions.
    components = ["DBPulse-device-v1", platform.system(), platform.machine()]
    if machine_id:
        components.append(machine_id)
    else:
        components.extend([platform.node(), str(uuid.getnode())])
    digest = hashlib.sha256("\0".join(components).encode("utf-8")).hexdigest()[:24].upper()
    return "DBP-" + "-".join(digest[index:index + 4] for index in range(0, 24, 4))


# --- Window presentation -----------------------------------------------------
# Restrained, native-feeling styling: neutral greys, one brand navy accent and a
# single link blue. The three status colours are also asserted by
# ``--desktop-self-test``, so they must keep their exact values.
UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"

WINDOW_BG = "#f2f4f7"
SURFACE = "#ffffff"
SURFACE_SOFT = "#f7f8fa"
HAIRLINE = "#dfe3e9"
HAIRLINE_SOFT = "#edf0f4"
INK = "#1c2633"
INK_SOFT = "#3c4959"
MUTED = "#5f6b7a"
FAINT = "#8b95a3"
BRAND = "#1d3f66"
BRAND_HOVER = "#26507d"
LINK = "#1b5aa6"
OK_COLOR = "#137c51"
WARN_COLOR = "#986a00"
DANGER_COLOR = "#b3261e"
LOG_BG = "#fcfcfd"

STATUS_TINTS = {
    OK_COLOR: "#e9f4ee",
    WARN_COLOR: "#fbf2dd",
    DANGER_COLOR: "#fbecec",
}


def prepare_win32():
    """Opt into crisp per-monitor DPI rendering and a stable taskbar identity."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "sec.hn.cn.DBPulse.Desktop"
        )
    except (AttributeError, OSError):
        pass


def _colorref(value):
    """Convert ``#rrggbb`` to a Win32 COLORREF (0x00BBGGRR)."""
    red, green, blue = (int(value[index:index + 2], 16) for index in (1, 3, 5))
    return red | (green << 8) | (blue << 16)


def apply_window_chrome(root):
    """Tint the Windows 11 caption to the app background; ignore older builds."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        dwm = ctypes.windll.dwmapi
        # 35 = DWMWA_CAPTION_COLOR, 34 = DWMWA_BORDER_COLOR (Windows 11 22H2+).
        for attribute, color in ((35, SURFACE), (34, HAIRLINE)):
            reference = ctypes.c_int(_colorref(color))
            dwm.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(reference), ctypes.sizeof(reference)
            )
    except (AttributeError, OSError):
        pass


def ui_px(root):
    """Return a pixel scaler so spacing follows the monitor DPI (96 dpi = 1.0)."""
    try:
        scale = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    except Exception:
        scale = 1.0

    def px(value):
        if value == 0:
            return 0
        return max(1, int(round(value * scale)))

    return px


def build_styles(root):
    """Install the ttk theme: flat surfaces, hairline outlines, no gradients."""
    from tkinter import ttk

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=WINDOW_BG, foreground=INK,
                    font=(UI_FONT, 10))
    style.configure("TFrame", background=WINDOW_BG)
    style.configure("TLabel", background=WINDOW_BG, foreground=INK,
                    font=(UI_FONT, 10))
    style.configure(
        "TButton", font=(UI_FONT, 10), padding=(14, 7), borderwidth=1,
        width=0, background=SURFACE, foreground=INK, bordercolor=HAIRLINE,
        lightcolor=SURFACE, darkcolor=SURFACE, focuscolor=SURFACE,
    )
    style.map(
        "TButton",
        background=[("disabled", SURFACE_SOFT), ("pressed", "#e9edf2"),
                    ("active", "#f5f7f9")],
        foreground=[("disabled", FAINT)],
        bordercolor=[("active", "#c9d1db"), ("disabled", HAIRLINE_SOFT)],
    )
    style.configure(
        "Primary.TButton", font=(UI_FONT, 10), padding=(18, 7), width=0,
        background=BRAND, foreground="#ffffff", bordercolor=BRAND,
        lightcolor=BRAND, darkcolor=BRAND,
    )
    style.map(
        "Primary.TButton",
        background=[("disabled", "#b9c4d1"), ("pressed", "#16324f"),
                    ("active", BRAND_HOVER)],
        bordercolor=[("disabled", "#b9c4d1"), ("pressed", "#16324f"),
                     ("active", BRAND_HOVER)],
        foreground=[("disabled", "#f2f4f7")],
    )
    style.configure(
        "Link.TButton", font=(UI_FONT, 9), padding=(8, 5), relief="flat",
        borderwidth=0, width=0, background=SURFACE, foreground=LINK,
        bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE,
        focuscolor=SURFACE,
    )
    style.map(
        "Link.TButton",
        background=[("active", SURFACE), ("pressed", SURFACE),
                    ("disabled", SURFACE)],
        foreground=[("active", "#154c8c"), ("disabled", FAINT)],
    )
    style.configure(
        "Serial.TButton", font=(MONO_FONT, 9), padding=(0, 2), relief="flat",
        borderwidth=0, width=0, background=SURFACE, foreground=MUTED,
        bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE,
        focuscolor=SURFACE,
    )
    style.map(
        "Serial.TButton",
        background=[("active", SURFACE), ("pressed", SURFACE)],
        foreground=[("active", LINK)],
    )
    style.configure(
        "TEntry", padding=(8, 6), fieldbackground=SURFACE, foreground=INK,
        bordercolor=HAIRLINE, lightcolor=HAIRLINE, darkcolor=HAIRLINE,
        insertcolor=INK,
    )
    style.map("TEntry", bordercolor=[("focus", LINK)],
              lightcolor=[("focus", LINK)], darkcolor=[("focus", LINK)])
    style.configure(
        "TCombobox", padding=(8, 5), fieldbackground=SURFACE, background=SURFACE,
        foreground=INK, bordercolor=HAIRLINE, lightcolor=HAIRLINE,
        darkcolor=HAIRLINE, arrowcolor=MUTED,
    )
    style.map(
        "TCombobox",
        bordercolor=[("focus", LINK)],
        fieldbackground=[("readonly", SURFACE)],
        selectbackground=[("readonly", SURFACE)],
        selectforeground=[("readonly", INK)],
    )
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", "#e8eef7")
    root.option_add("*TCombobox*Listbox.selectForeground", INK)
    style.layout(
        "Slim.Vertical.TScrollbar",
        [
            (
                "Vertical.Scrollbar.trough",
                {
                    "children": [
                        (
                            "Vertical.Scrollbar.thumb",
                            {"expand": "1", "sticky": "nswe"},
                        )
                    ],
                    "sticky": "ns",
                },
            )
        ],
    )
    style.configure(
        "Slim.Vertical.TScrollbar", background="#c3cad4", troughcolor=LOG_BG,
        bordercolor=LOG_BG, lightcolor="#c3cad4", darkcolor="#c3cad4", width=9,
    )
    style.map(
        "Slim.Vertical.TScrollbar",
        background=[("active", "#b3bac6"), ("pressed", "#a4acba")],
    )
    return style


def card(parent, padx=18, pady=16):
    """Return ``(outer, inner)`` frames for a hairline-bordered surface card."""
    import tkinter as tk

    outer = tk.Frame(parent, bg=SURFACE, bd=0, highlightbackground=HAIRLINE,
                     highlightcolor=HAIRLINE, highlightthickness=1)
    inner = tk.Frame(outer, bg=SURFACE)
    inner.pack(fill="both", expand=True, padx=padx, pady=pady)
    return outer, inner


def desktop_layout(root, directory, status, detail):
    """Compose the status window and return the widgets ``main`` drives.

    Returns ``(frame, indicator, buttons, backup_buttons, log_header, log_view)``.
    """
    import tkinter as tk
    import webbrowser
    from tkinter import ttk

    build_styles(root)
    px = ui_px(root)
    root.configure(background=WINDOW_BG)
    root.option_add("*tearOff", False)

    width = min(px(960), max(px(880), root.winfo_screenwidth() - px(140)))
    height = min(px(820), max(px(660), root.winfo_screenheight() - px(160)))
    root.minsize(min(px(860), width), min(px(640), height))
    root.geometry(
        "{0}x{1}+{2}+{3}".format(
            width,
            height,
            max(0, (root.winfo_screenwidth() - width) // 2),
            max(0, (root.winfo_screenheight() - height) // 3),
        )
    )

    frame = tk.Frame(root, bg=WINDOW_BG)
    frame.pack(fill="both", expand=True)

    # Header: brand on the left, live status and device identity on the right.
    header = tk.Frame(frame, bg=SURFACE)
    header.pack(fill="x")
    head = tk.Frame(header, bg=SURFACE)
    head.pack(fill="x", padx=px(22), pady=(px(15), px(14)))

    brand = tk.Frame(head, bg=SURFACE)
    brand.pack(side="left")
    # Frozen standard releases use _MEIPASS. Portable assets live in the shared root.
    locations = [directory, Path(getattr(sys, "_MEIPASS", directory))]
    shared = portable_home(Path(directory))
    if shared:
        locations.insert(0, shared)
    for location in locations:
        logo_path = location / "assets/brand/dbcheck_logo_icon.png"
        if not logo_path.is_file():
            continue
        try:
            image = tk.PhotoImage(master=root, file=str(logo_path))
            root.iconphoto(True, image)
            factor = max(1, (max(image.width(), image.height()) + 31) // 32)
            root.brand_image = image.subsample(factor, factor)
            tk.Label(brand, image=root.brand_image, bg=SURFACE).pack(
                side="left", padx=(px(0), px(12))
            )
            break
        except tk.TclError:
            continue
    names = tk.Frame(brand, bg=SURFACE)
    names.pack(side="left")
    tk.Label(names, text="DBPulse", bg=SURFACE, fg=INK,
             font=(UI_FONT, 14, "bold")).pack(anchor="w")
    tk.Label(names, text="数据库智能巡检系统 · 本地服务控制台", bg=SURFACE,
             fg=MUTED, font=(UI_FONT, 9)).pack(anchor="w", pady=(px(1), px(0)))

    right = tk.Frame(head, bg=SURFACE)
    right.pack(side="right")
    pill = tk.Frame(right, bg=SURFACE_SOFT, highlightbackground=HAIRLINE,
                    highlightthickness=1)
    pill.pack(anchor="e")
    dot = tk.Label(pill, text="\u25cf", bg=SURFACE_SOFT, fg=DANGER_COLOR,
                   font=(UI_FONT, 8))
    dot.pack(side="left", padx=(px(9), px(5)), pady=4)
    indicator = tk.Label(pill, textvariable=status, bg=SURFACE_SOFT,
                         fg=DANGER_COLOR, font=(UI_FONT, 10, "bold"))
    indicator.pack(side="left", padx=(px(0), px(10)), pady=4)

    serial_row = tk.Frame(right, bg=SURFACE)
    serial_row.pack(anchor="e", pady=(px(7), px(0)))
    copy_hint = tk.StringVar(master=root, value="点击复制")
    serial = device_serial_number()

    def copy_serial():
        try:
            root.clipboard_clear()
            root.clipboard_append(serial)
            copy_hint.set("已复制")
        except tk.TclError:
            copy_hint.set("复制失败")
        root.after(2500, lambda: copy_hint.set("点击复制"))

    ttk.Button(
        serial_row, text=serial, command=copy_serial, style="Serial.TButton",
        cursor="hand2", takefocus=True,
    ).pack(side="left")
    tk.Label(serial_row, textvariable=copy_hint, bg=SURFACE, fg=FAINT,
             font=(UI_FONT, 9)).pack(side="left", padx=(px(10), px(0)))

    def paint_status(color):
        tint = STATUS_TINTS.get(color, SURFACE_SOFT)
        pill.configure(bg=tint)
        dot.configure(bg=tint, fg=color)
        indicator.configure(bg=tint, fg=color)

    root.paint_status = paint_status

    tk.Frame(frame, bg=HAIRLINE, height=1).pack(fill="x")
    body = tk.Frame(frame, bg=WINDOW_BG)
    body.pack(fill="both", expand=True, padx=px(22), pady=(px(16), px(18)))

    # Service card: address, live detail and the primary actions.
    service_outer, service = card(body)
    service_outer.pack(fill="x")
    address_row = tk.Frame(service, bg=SURFACE)
    address_row.pack(fill="x")
    tk.Label(address_row, text="服务地址", bg=SURFACE, fg=MUTED,
             font=(UI_FONT, 9)).pack(side="left")
    address = tk.Label(address_row, text=ADDRESS, bg=SURFACE, fg=INK_SOFT,
                       font=(MONO_FONT, 10), cursor="hand2")
    address.pack(side="left", padx=(px(12), px(0)))
    address.bind("<Button-1>", lambda _event: webbrowser.open(ADDRESS))

    detail_label = tk.Label(service, textvariable=detail, bg=SURFACE, fg=MUTED,
                            font=(UI_FONT, 10), anchor="w", justify="left",
                            wraplength=max(240, width - 96))
    detail_label.pack(fill="x", pady=(px(9), px(14)))

    def resize_detail(event):
        wrap = max(240, event.width - 40)
        if str(detail_label.cget("wraplength")) != str(wrap):
            detail_label.configure(wraplength=wrap)

    service.bind("<Configure>", resize_detail)

    buttons = tk.Frame(service, bg=SURFACE)
    buttons.pack(fill="x")

    # Licence card: compact key/value rows instead of a badge-heavy placeholder.
    licence_outer, licence = card(body)
    licence_outer.pack(fill="x", pady=(px(14), px(0)))
    licence_head = tk.Frame(licence, bg=SURFACE)
    licence_head.pack(fill="x")
    tk.Label(licence_head, text="软件授权", bg=SURFACE, fg=INK_SOFT,
             font=(UI_FONT, 10, "bold")).pack(side="left")
    tk.Label(licence_head, text="未激活", bg="#f1f3f6", fg=MUTED,
             font=(UI_FONT, 9), padx=px(8), pady=2).pack(side="left", padx=(px(10), px(0)))
    tk.Label(licence_head, text="到期时间  未设置", bg=SURFACE, fg=FAINT,
             font=(UI_FONT, 9)).pack(side="right")

    customer_row = tk.Frame(licence, bg=SURFACE)
    customer_row.pack(fill="x", pady=(px(12), px(10)))
    tk.Label(customer_row, text="授权客户 / 单位", bg=SURFACE, fg=MUTED,
             font=(UI_FONT, 9)).pack(side="left")
    tk.Label(customer_row, text="待授权", bg=SURFACE, fg=INK,
             font=(UI_FONT, 10, "bold")).pack(side="left", padx=(px(16), px(0)))

    entry_row = tk.Frame(licence, bg=SURFACE)
    entry_row.pack(fill="x")
    tk.Label(entry_row, text="激活码", bg=SURFACE, fg=MUTED,
             font=(UI_FONT, 9)).pack(side="left")
    activation_code = tk.StringVar(master=root)
    ttk.Entry(entry_row, textvariable=activation_code, width=30).pack(
        side="left", padx=(px(16), px(10))
    )
    ttk.Button(entry_row, text="激活", state="disabled", width=6).pack(side="left")
    tk.Label(licence, text="激活功能尚未开放，输入区域已预留。", bg=SURFACE,
             fg=FAINT, font=(UI_FONT, 9)).pack(anchor="w", pady=(px(9), px(0)))

    # Backup card: one dense row of destructive/support actions.
    backup_outer, backup = card(body)
    backup_outer.pack(fill="x", pady=(px(14), px(0)))
    backup_head = tk.Frame(backup, bg=SURFACE)
    backup_head.pack(fill="x")
    tk.Label(backup_head, text="数据备份与恢复", bg=SURFACE, fg=INK_SOFT,
             font=(UI_FONT, 10, "bold")).pack(side="left")
    tk.Label(backup_head, text="含账号、数据源、台账、任务与配置", bg=SURFACE,
             fg=FAINT, font=(UI_FONT, 9)).pack(side="right")
    backup_buttons = tk.Frame(backup, bg=SURFACE)
    backup_buttons.pack(fill="x", pady=(px(12), px(0)))

    # Log card: light, monospaced, framed like a console pane.
    log_outer, log_card = card(body, padx=px(0), pady=0)
    log_outer.pack(fill="both", expand=True, pady=(px(14), px(0)))
    log_header = tk.Frame(log_card, bg=SURFACE)
    log_header.pack(fill="x", padx=px(16), pady=(px(12), px(9)))
    tk.Label(log_header, text="运行日志", bg=SURFACE, fg=INK_SOFT,
             font=(UI_FONT, 10, "bold")).pack(side="left")
    tk.Frame(log_card, bg=HAIRLINE_SOFT, height=1).pack(fill="x")
    log_view = tk.Frame(log_card, bg=LOG_BG)
    log_view.pack(fill="both", expand=True)
    return frame, indicator, buttons, backup_buttons, log_header, log_view


def maintenance_dialog(root, settings, state, on_save):
    """Show the automatic-backup settings dialog.

    ``on_save`` receives the validated dict and may raise; the error is shown
    inline and the dialog stays open.
    """
    import tkinter as tk
    from tkinter import ttk

    px = ui_px(root)
    window = tk.Toplevel(root)
    window.title("自动备份与异常恢复")
    window.transient(root)
    window.configure(bg=SURFACE)
    window.resizable(False, False)
    window.grab_set()
    values = {
        key: (
            tk.BooleanVar(value=value)
            if isinstance(value, bool)
            else tk.StringVar(value=str(value))
        )
        for key, value in settings.items()
    }
    panel = tk.Frame(window, bg=SURFACE)
    panel.pack(fill="both", expand=True, padx=px(22), pady=18)
    tk.Label(panel, text="自动备份", bg=SURFACE, fg=INK_SOFT,
             font=(UI_FONT, 10, "bold")).pack(anchor="w")
    tk.Label(panel, text="按计划导出数据并验证可恢复性。", bg=SURFACE,
             fg=FAINT, font=(UI_FONT, 9)).pack(anchor="w", pady=(px(2), px(10)))
    tk.Checkbutton(
        panel, text="启用自动备份并验证", variable=values["automatic_backup"],
        bg=SURFACE, activebackground=SURFACE, selectcolor=SURFACE,
        fg=INK_SOFT, font=(UI_FONT, 10), bd=0, highlightthickness=0,
        cursor="hand2",
    ).pack(anchor="w")

    form = tk.Frame(panel, bg=SURFACE)
    form.pack(fill="x", pady=(px(12), px(0)))
    form.columnconfigure(1, weight=1)
    fields = [
        ("备份周期", "backup_frequency"),
        ("本地备份时间 HH:mm", "backup_time"),
        ("保留最近备份份数", "retention"),
        ("自动重启次数上限", "restart_limit"),
        ("重启等待秒数", "restart_delay"),
    ]
    for row, (label, key) in enumerate(fields):
        tk.Label(form, text=label, bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 9)).grid(
            row=row, column=0, sticky="w", padx=(px(0), px(14)), pady=px(5)
        )
        if key == "backup_frequency":
            ttk.Combobox(
                form, textvariable=values[key], values=["daily", "weekly"],
                state="readonly", width=12,
            ).grid(row=row, column=1, sticky="w")
        else:
            ttk.Entry(form, textvariable=values[key]).grid(
                row=row, column=1, sticky="ew"
            )
    tk.Label(panel, text="daily 为每天，weekly 为每周一，软件运行时执行。",
             bg=SURFACE, fg=FAINT, font=(UI_FONT, 9)).pack(
        anchor="w", pady=(px(9), px(0))
    )
    tk.Checkbutton(
        panel, text="后台异常退出后自动重启",
        variable=values["restart_enabled"], bg=SURFACE,
        activebackground=SURFACE, selectcolor=SURFACE, fg=INK_SOFT,
        font=(UI_FONT, 10), bd=0, highlightthickness=0, cursor="hand2",
    ).pack(anchor="w", pady=(px(10), px(0)))

    tk.Frame(panel, bg=HAIRLINE_SOFT, height=1).pack(fill="x", pady=14)
    tk.Label(panel, text="最近记录", bg=SURFACE, fg=INK_SOFT,
             font=(UI_FONT, 10, "bold")).pack(anchor="w", pady=(px(0), px(6)))
    records = (
        "成功  " + (state["last_success"] or "尚无记录"),
        "验证  " + (state["last_verified"] or "尚无记录"),
        "异常  " + (state["last_error"] or "无"),
    )
    for record in records:
        tk.Label(panel, text=record, bg=SURFACE, fg=MUTED,
                 font=(UI_FONT, 9), anchor="w", justify="left",
                 wraplength=430).pack(fill="x", pady=1)

    feedback = tk.StringVar()
    tk.Label(panel, textvariable=feedback, bg=SURFACE, fg=DANGER_COLOR,
             font=(UI_FONT, 9), anchor="w", justify="left",
             wraplength=430).pack(fill="x", pady=(px(8), px(0)))

    def save_settings():
        try:
            data = {key: value.get() for key, value in values.items()}
            for key in ("retention", "restart_limit", "restart_delay"):
                data[key] = int(data[key])
            on_save(data)
            window.destroy()
        except Exception as error:  # surfaced inline, dialog stays open
            feedback.set(str(error))

    footer = tk.Frame(panel, bg=SURFACE)
    footer.pack(fill="x", pady=(px(14), px(0)))
    ttk.Button(footer, text="取消", command=window.destroy,
               cursor="hand2").pack(side="right")
    ttk.Button(footer, text="保存设置", command=save_settings,
               style="Primary.TButton", cursor="hand2").pack(
        side="right", padx=(px(0), px(8))
    )
    window.update_idletasks()
    left = root.winfo_rootx() + (root.winfo_width() - window.winfo_width()) // 2
    top = root.winfo_rooty() + max(
        40, (root.winfo_height() - window.winfo_height()) // 3
    )
    window.geometry("+{0}+{1}".format(max(0, left), max(0, top)))
    return window


def main():
    if os.name != "nt":
        raise RuntimeError("此启动器仅适用于 Windows")
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    from datetime import datetime
    import webbrowser
    from ctypes import wintypes as w

    directory = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2]
    )
    shared = portable_home(directory)
    if shared is not None:
        os.environ["DBPULSE_PORTABLE_HOME"] = str(shared)
        java = shared / "runtime/windows-x86_64/jre"
        if (java / "bin/java.exe").is_file():
            os.environ["JAVA_HOME"] = str(java)
    identity = hashlib.sha256(str(directory).lower().encode()).hexdigest()[:24]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [w.LPVOID, w.BOOL, w.LPCWSTR]
    kernel.CreateMutexW.restype = w.HANDLE
    mutex = kernel.CreateMutexW(None, False, "Local\\DBPulseDesktop_" + identity)
    if ctypes.get_last_error() == 183:
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
        user.FindWindowW.restype = w.HWND
        hwnd = user.FindWindowW("DBPulseTray_" + identity, None)
        if hwnd:
            user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
            user.PostMessageW(hwnd, TrayIcon.WM_SHOW, 0, 0)
        return 0
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    backend = BackendProcess(directory)
    maintenance = Maintenance(backend.metadata.parent.parent)
    recovery = CrashRecovery()
    recovery.intentional(backend.owned_running())
    prepare_win32()
    root = tk.Tk()
    root.title(TITLE)
    events = queue.Queue()
    tray = TrayIcon(events, identity)
    status = tk.StringVar(value="已停止")
    detail = tk.StringVar(value="正在启动服务…")
    (
        frame,
        indicator,
        buttons,
        backup_buttons,
        log_header,
        log_view,
    ) = desktop_layout(root, directory, status, detail)
    root.update_idletasks()
    apply_window_chrome(root)
    busy = False
    quitting = False
    closing = threading.Event()
    test_mode = "--desktop-self-test" in sys.argv
    test_ok = False

    log_reader = LogOutput(backend.log_path)

    def action(method, label="正在处理，请稍候…"):
        nonlocal busy
        if busy:
            return
        busy = True
        backend.error = ""
        tray.busy = True
        detail.set(label)
        events.put(("log", "[DBPulse] " + label + "\n"))

        def worker():
            try:
                method()
                events.put(("log", "[DBPulse] 操作完成\n"))
            except Exception as error:
                events.put(("error", str(error)))
            finally:
                events.put("idle")

        threading.Thread(target=worker, daemon=True).start()

    def start_service():
        recovery.intentional(True)
        action(backend.start, "正在启动服务…")

    def stop_service():
        recovery.intentional(False)
        action(backend.stop, "正在停止服务…")

    start = ttk.Button(
        buttons, text="启动服务", command=start_service,
        style="Primary.TButton", cursor="hand2",
    )
    start.pack(side="left")
    stop = ttk.Button(
        buttons, text="停止服务", command=stop_service, cursor="hand2"
    )
    stop.pack(side="left", padx=(8, 0))
    tk.Frame(buttons, bg=HAIRLINE_SOFT, width=1, height=20).pack(
        side="left", padx=14
    )
    ttk.Button(
        buttons, text="打开管理页面", command=lambda: webbrowser.open(ADDRESS),
        style="Link.TButton", cursor="hand2",
    ).pack(side="left")
    ttk.Button(
        buttons, text="监控大屏",
        command=lambda: webbrowser.open(MONITOR_ADDRESS),
        style="Link.TButton", cursor="hand2",
    ).pack(side="left", padx=(10, 0))

    def progress(message):
        events.put(("log", "[备份] " + message + "\n"))

    def export_backup():
        if busy:
            return
        folder = backend.metadata.parent.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        selected = filedialog.asksaveasfilename(
            parent=root,
            title="导出 DBPulse 数据备份",
            initialdir=str(folder),
            initialfile="DBPulse-backup-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
            + ".zip",
            defaultextension=".zip",
            filetypes=[("DBPulse 数据备份", "*.zip")],
        )
        if selected:

            def save():
                backend.export_backup(selected, progress)
                maintenance.verification(selected, progress)
                events.put(("notice", "备份已导出到\n" + selected))

            action(save, "正在导出备份，进度见下方日志…")

    def import_backup():
        if busy:
            return
        selected = filedialog.askopenfilename(
            parent=root,
            title="导入 DBPulse 数据备份",
            filetypes=[("DBPulse 数据备份", "*.zip")],
        )
        if not selected or not messagebox.askyesno(
            "导入备份",
            "将恢复备份中的账号、数据源、台账、任务、配置和报告。\n当前服务会暂时停止，原数据会自动保留副本。\n恢复后请使用备份中的账号密码登录。\n\n确定导入所选备份？",
            parent=root,
        ):
            return

        def restore():
            with log_reader.lock:
                try:
                    result = backend.import_backup(selected, progress)
                    maintenance.load()
                    recovery.intentional(backend.owned_running())
                finally:
                    log_reader.reset()
            events.put(
                ("notice", "备份导入完成。原数据副本位于\n" + result["previous_data"])
            )

        action(restore, "正在导入备份，进度见下方日志…")

    export_button = ttk.Button(
        backup_buttons, text="导出备份", command=export_backup, cursor="hand2"
    )
    export_button.pack(side="left")
    import_button = ttk.Button(
        backup_buttons, text="导入备份", command=import_backup, cursor="hand2"
    )
    import_button.pack(side="left", padx=(8, 0))

    def verify_backup():
        if busy:
            return
        selected = filedialog.askopenfilename(
            parent=root,
            title="验证 DBPulse 备份",
            filetypes=[("DBPulse 数据备份", "*.zip")],
        )
        if selected:

            def verify():
                result = maintenance.verification(selected, progress)
                events.put(
                    (
                        "notice",
                        f"备份验证通过，共{result['files']}个文件。数据库可读，密码密钥匹配。",
                    )
                )

            action(verify, "正在验证备份…")

    def maintenance_settings():
        if busy:
            return

        def commit(data):
            maintenance.save(data)
            recovery.intentional(backend.owned_running())
            events.put(("log", "[维护] 自动备份与异常恢复设置已保存\n"))

        maintenance_dialog(root, maintenance.settings, maintenance.state, commit)

    verify_button = ttk.Button(
        backup_buttons, text="验证备份", command=verify_backup, cursor="hand2"
    )
    verify_button.pack(side="left", padx=(8, 0))
    settings_button = ttk.Button(
        backup_buttons, text="自动备份与异常恢复", command=maintenance_settings,
        cursor="hand2",
    )
    settings_button.pack(side="left", padx=(8, 0))

    def maintenance_tick():
        if not busy and not quitting:
            signal = recovery.observe(
                backend.owned_running(), tray.running, maintenance.settings
            )
            if signal == "restart":
                action(backend.start, f"后台异常恢复，第{recovery.attempts}次重启…")
            elif signal:
                backend.error = signal
                events.put(("log", "[异常恢复] " + signal + "\n"))
            if not busy and maintenance.due():
                action(
                    lambda: maintenance.automatic_backup(progress),
                    "正在执行自动备份与验证…",
                )
        if not closing.is_set():
            root.after(1000, maintenance_tick)

    auto_scroll = tk.BooleanVar(value=True)
    terminal = tk.Text(
        log_view,
        height=9,
        bg=LOG_BG,
        fg=INK,
        insertbackground=INK,
        font=(MONO_FONT, 10),
        wrap="word",
        state="disabled",
        relief="flat",
        borderwidth=0,
        highlightthickness=0,
        padx=16,
        pady=11,
        spacing1=1,
        spacing3=1,
        selectbackground="#dbe6f5",
        selectforeground=INK,
    )
    scrollbar = ttk.Scrollbar(
        log_view, orient="vertical", command=terminal.yview,
        style="Slim.Vertical.TScrollbar",
    )
    terminal.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y", padx=(0, 2), pady=6)
    terminal.pack(side="left", fill="both", expand=True)

    def append_log(text):
        terminal.configure(state="normal")
        terminal.insert("end", text)
        lines = int(terminal.index("end-1c").split(".")[0])
        if lines > 2000:
            terminal.delete("1.0", str(lines - 2000) + ".0")
        if auto_scroll.get():
            terminal.see("end")
        terminal.configure(state="disabled")

    def clear_log():
        terminal.configure(state="normal")
        terminal.delete("1.0", "end")
        terminal.configure(state="disabled")

    ttk.Button(
        log_header, text="清空显示", command=clear_log, style="Link.TButton",
        cursor="hand2",
    ).pack(side="right")
    tk.Checkbutton(
        log_header, text="自动滚动", variable=auto_scroll, bg=SURFACE,
        activebackground=SURFACE, selectcolor=SURFACE, fg=MUTED,
        font=(UI_FONT, 9), bd=0, highlightthickness=0, cursor="hand2",
    ).pack(side="right", padx=(0, 2))
    append_log("[DBPulse] 状态窗口已就绪，正在等待后台输出…\n")

    def follow_output():
        while not closing.is_set():
            try:
                text = log_reader.read()
                if text:
                    events.put(("log", text))
            except OSError:
                pass
            closing.wait(0.25)

    def hide():
        if tray.available:
            root.withdraw()
        else:
            messagebox.showinfo(TITLE, "托盘尚未就绪，请稍后再次最小化。")

    def show():
        root.deiconify()
        root.state("normal")
        root.lift()
        root.focus_force()

    def quit_application():
        nonlocal quitting
        if busy:
            detail.set("请等待当前操作完成后退出")
            return
        quitting = True
        recovery.intentional(False)
        action(backend.stop)

    def poll():
        nonlocal busy, quitting
        for _ in range(100):
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, tuple):
                kind, message = event
                if kind == "log":
                    append_log(message)
                elif kind == "notice":
                    messagebox.showinfo(TITLE, message, parent=root)
                elif kind == "error":
                    backend.error = message
                    append_log("[错误] " + message + "\n")
                    show()
            elif event == "idle":
                busy = False
                tray.busy = False
            elif event == "show":
                show()
            elif event == "browser":
                webbrowser.open(ADDRESS)
            elif event == "start":
                start_service()
            elif event == "stop":
                stop_service()
            elif event == "quit":
                quit_application()
            elif isinstance(event, bool):
                tray.running = event

        tray.stoppable = backend.owned_running()
        pending = tray.stoppable and not tray.running
        status.set("运行中" if tray.running else "启动中…" if pending else "已停止")
        color = (
            OK_COLOR if tray.running else WARN_COLOR if pending else DANGER_COLOR
        )
        indicator.configure(fg=color)
        root.paint_status(color)
        start.configure(
            state="disabled" if busy or tray.running or pending else "normal"
        )
        stop.configure(
            state="normal" if not busy and backend.owned_running() else "disabled"
        )
        for button in (export_button, import_button, verify_button, settings_button):
            button.configure(state="disabled" if busy else "normal")
        if not busy:
            detail.set(
                backend.error
                or (
                    "关闭窗口后继续在右下角托盘运行"
                    if tray.running
                    else "正在启动服务，请稍候…" if pending else "点击启动即可运行服务"
                )
            )
        if quitting and not busy:
            if backend.error:
                detail.set("退出失败：" + backend.error)
                quitting = False
            else:
                tray.close()
                root.destroy()
                return
        root.after(200, poll)

    def check_health():
        while not closing.is_set():
            events.put(backend.healthy())
            closing.wait(1)

    root.protocol("WM_DELETE_WINDOW", hide)
    root.bind(
        "<Unmap>",
        lambda event: (
            hide() if event.widget is root and root.state() == "iconic" else None
        ),
    )
    root.after(100, poll)
    if test_mode:

        def test_window():
            nonlocal test_ok
            events.put(True)
            events.put(("log", "[自检] 日志输出正常\n"))

            def verify():
                nonlocal test_ok
                running_ok = (
                    status.get() == "运行中" and indicator.cget("fg") == "#137c51"
                )
                hidden_ok = False
                if tray.available:
                    hide()
                    hidden_ok = root.state() == "withdrawn"
                    show()
                restored_ok = root.state() == "normal"
                events.put(False)

                def finish_check():
                    nonlocal test_ok
                    stopped_ok = (
                        status.get() == "已停止" and indicator.cget("fg") == "#b3261e"
                    )
                    log_ok = "[自检] 日志输出正常" in terminal.get("1.0", "end")
                    backup_ok = False
                    backup_error = ""
                    maintenance_ok = False
                    try:
                        import sqlite3
                        import tempfile
                        from contextlib import closing

                        with tempfile.TemporaryDirectory() as temporary:
                            fixture = Path(temporary) / "installation"
                            (fixture / "data").mkdir(parents=True)
                            database_path = fixture / "data/check.db"
                            with closing(sqlite3.connect(database_path)) as database:
                                database.execute("CREATE TABLE fixture (value TEXT)")
                                database.execute(
                                    "INSERT INTO fixture VALUES ('original')"
                                )
                                database.commit()
                            backup = LocalBackup(fixture)
                            archive = Path(temporary) / "backup.zip"
                            backup.export(archive)
                            database_path.write_bytes(b"changed")
                            backup.restore(archive)
                            with closing(sqlite3.connect(database_path)) as database:
                                backup_ok = database.execute(
                                    "SELECT value FROM fixture"
                                ).fetchone() == ("original",)
                            manager = Maintenance(fixture)
                            manager.save({"automatic_backup": True, "retention": 1})
                            automatic = manager.automatic_backup()
                            check = manager.verification(automatic)
                            probe = CrashRecovery()
                            probe.intentional(True)
                            settings = {**manager.settings, "restart_enabled": True}
                            first = probe.observe(False, False, settings, now=1)
                            second = probe.observe(False, False, settings, now=11)
                            maintenance_ok = bool(
                                check["verified"]
                                and manager.state["last_success"]
                                and first
                                and second == "restart"
                            )
                    except Exception as error:
                        backup_error = str(error)
                    tray_ok = tray.available and hidden_ok
                    allow_headless = '--allow-headless-tray' in sys.argv
                    test_ok = (
                        backup_ok
                        and maintenance_ok
                        and log_ok
                        and running_ok
                        and stopped_ok
                        and (tray_ok or (allow_headless and not tray.available))
                        and restored_ok
                    )
                    output = {
                        "ok": test_ok,
                        "log_output": log_ok,
                        "backup_round_trip": backup_ok,
                        "automatic_backup_verification_and_recovery": maintenance_ok,
                        "backup_error": backup_error,
                        "tray_available": tray.available,
                        "headless_tray_allowed": allow_headless,
                        "tray_interaction_verified": tray_ok,
                        "running_green": running_ok,
                        "stopped_red": stopped_ok,
                        "close_to_tray": hidden_ok,
                        "restore_window": restored_ok,
                    }
                    if "--result-file" in sys.argv:
                        Path(sys.argv[sys.argv.index("--result-file") + 1]).write_text(
                            json.dumps(output), encoding="utf-8"
                        )
                    tray.close()
                    root.destroy()

                root.after(500, finish_check)

            root.after(500, verify)

        root.after(2000, test_window)
    else:
        root.after(150, start_service)
        root.after(1500, maintenance_tick)
        threading.Thread(target=check_health, daemon=True).start()
        threading.Thread(target=follow_output, daemon=True).start()
    try:
        root.mainloop()
    finally:
        closing.set()
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle(mutex)
    return (0 if test_ok else 1) if test_mode else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, str(error), TITLE, 0x10)
        else:
            raise
