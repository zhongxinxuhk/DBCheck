# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Windows status window and notification-area launcher for DBPulse."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import ctypes
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psutil

ADDRESS = "http://127.0.0.1:5003"
TITLE = "DBPulse 运行状态"


class BackendProcess:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.executable = self.directory / "dbcheck.exe"
        self.metadata = self.directory / "data" / "desktop_process.json"
        self.process = None
        self.pid = None
        self.started = None
        self.error = ""
        self.lock = threading.Lock()
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
            logs = self.directory / "data" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            with (logs / "desktop-server.log").open("ab") as output:
                self.process = subprocess.Popen(
                    [str(self.executable), "web"],
                    cwd=self.directory,
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
            self.pid = self.started = None
            self.metadata.unlink(missing_ok=True)
            self.error = ""


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
                disabled = (identifier == 102 and self.running) or (
                    identifier == 103 and not self.stoppable
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


def main():
    if os.name != "nt":
        raise RuntimeError("此启动器仅适用于 Windows")
    import tkinter as tk
    from tkinter import ttk, messagebox
    import webbrowser
    from ctypes import wintypes as w

    directory = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2]
    )
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
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("420x245")
    root.resizable(False, False)
    events = queue.Queue()
    tray = TrayIcon(events, identity)
    status = tk.StringVar(value="已停止")
    detail = tk.StringVar(value="正在启动服务…")
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)
    indicator = tk.Label(
        frame,
        textvariable=status,
        fg="#b3261e",
        font=("Microsoft YaHei UI", 19, "bold"),
    )
    indicator.pack(anchor="w")
    ttk.Label(frame, text=ADDRESS).pack(anchor="w", pady=(8, 0))
    ttk.Label(frame, textvariable=detail, wraplength=375).pack(anchor="w", pady=(6, 12))
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x")
    busy = False
    quitting = False
    closing = threading.Event()
    test_mode = "--desktop-self-test" in sys.argv
    test_ok = False

    def action(method):
        nonlocal busy
        if busy:
            return
        busy = True
        detail.set("正在处理，请稍候…")

        def worker():
            try:
                method()
            except Exception as error:
                events.put(("error", str(error)))
            finally:
                events.put("idle")

        threading.Thread(target=worker, daemon=True).start()

    start = ttk.Button(buttons, text="启动", command=lambda: action(backend.start))
    start.pack(side="left")
    stop = ttk.Button(buttons, text="停止", command=lambda: action(backend.stop))
    stop.pack(side="left", padx=8)
    ttk.Button(
        buttons, text="打开管理页面", command=lambda: webbrowser.open(ADDRESS)
    ).pack(side="left")

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
        action(backend.stop)

    def poll():
        nonlocal busy, quitting
        while not events.empty():
            event = events.get_nowait()
            if isinstance(event, tuple):
                backend.error = event[1]
                show()
            elif event == "idle":
                busy = False
            elif event == "show":
                show()
            elif event == "browser":
                webbrowser.open(ADDRESS)
            elif event == "start":
                action(backend.start)
            elif event == "stop":
                action(backend.stop)
            elif event == "quit":
                quit_application()
            elif isinstance(event, bool):
                tray.running = event

        tray.stoppable = backend.owned_running()
        pending = tray.stoppable and not tray.running
        status.set("运行中" if tray.running else "启动中…" if pending else "已停止")
        indicator.configure(
            fg="#137c51" if tray.running else "#986a00" if pending else "#b3261e"
        )
        start.configure(
            state="disabled" if busy or tray.running or pending else "normal"
        )
        stop.configure(
            state="normal" if not busy and backend.owned_running() else "disabled"
        )
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
            time.sleep(1)

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
                    test_ok = (
                        running_ok
                        and stopped_ok
                        and tray.available
                        and hidden_ok
                        and restored_ok
                    )
                    output = {
                        "ok": test_ok,
                        "tray_available": tray.available,
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
        root.after(150, lambda: action(backend.start))
        threading.Thread(target=check_health, daemon=True).start()
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
