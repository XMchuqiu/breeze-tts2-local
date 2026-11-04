"""进程工具：给「完全退出并关闭」用。

只做三件事：确认某个 PID 确实是 python 解释器、强杀进程树、找出与本项目
desktop.py 相关的进程。刻意与 ``launcher/core.py`` 各自独立——启动器是外挂
组件，app 不能反过来依赖它（没有 launcher 目录时 启动.bat 仍要能跑）。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from pathlib import Path

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PYTHON_NAMES = ("python.exe", "pythonw.exe")


def image_path(pid: int) -> str:
    if not IS_WINDOWS or pid <= 0:
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 查询失败一律当作「不是我们的进程」
        return ""
    return ""


def is_python(pid: int) -> bool:
    """确认 PID 仍是 python：防止 PID 被系统复用后误杀无关程序。"""
    return Path(image_path(pid)).name.lower() in PYTHON_NAMES


def kill_tree(pid: int) -> bool:
    if not IS_WINDOWS or pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def parent_map() -> dict[int, int]:
    """pid → 父 pid。取不到返回空字典（调用方按「没有祖先」处理）。"""
    if not IS_WINDOWS:
        return {}
    TH32CS_SNAPPROCESS = 0x00000002
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return {}
            mapping: dict[int, int] = {}
            while True:
                mapping[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
            return mapping
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(snapshot))
    except Exception:  # noqa: BLE001
        return {}


def ancestors(pid: int, depth: int = 32) -> set[int]:
    """沿父链向上收集进程。清理时必须跳过它们，否则会把自己或自己的启动者杀掉。

    uv 建出来的 venv `pythonw.exe` 是一个启动器，真正跑代码的是它的子进程；
    两个进程的命令行里都有 desktop.py。若不排除祖先，taskkill /T 会连自己一起收掉，
    导致「退出前还没来得及清理实例记录」。
    """
    mapping = parent_map()
    found: set[int] = set()
    current = mapping.get(pid, 0)
    while current and current not in found and len(found) < depth:
        found.add(current)
        current = mapping.get(current, 0)
    return found


def find_desktop_processes(root: Path | None = None, timeout: float = 8.0) -> list[dict]:
    """命令行里带着本项目 desktop.py 的 python 进程。

    用 PowerShell CIM 而不是 wmic（新版 Windows 已移除 wmic）。只用于退出路径，
    慢一点可以接受；任何异常都返回空列表，由调用方退回「已知 PID」这条线索。
    """
    if not IS_WINDOWS:
        return []
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" |"
        " Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        run = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = (run.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    needle = str(root).lower().replace("/", "\\") if root else ""
    found: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        command = str(item.get("CommandLine") or "")
        lowered = command.lower().replace("/", "\\")
        if "desktop.py" not in lowered:
            continue
        if needle and needle not in lowered:
            continue
        try:
            pid = int(item.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid != os.getpid():
            found.append({"pid": pid, "command": command})
    return found
