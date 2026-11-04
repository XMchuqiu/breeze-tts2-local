"""Breeze Voice Studio 启动器入口。

职责：
- 高 DPI、任务栏图标分组、单实例（第二次双击会把已有窗口提到前台）；
- 解析命令行参数（支持无人值守自检）；
- 兜底所有未捕获异常，写崩溃日志并弹窗，绝不让用户面对「双击无反应」。

自检用法（不打开界面，便于排障）：
    python launcher/main.py --check
    python launcher/main.py --check --json
    python launcher/main.py --run-for 8 --script tools/_smoke_target.py
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from .core import (
        APP_NAME,
        IS_WINDOWS,
        LAUNCHER_NAME,
        ChildProcess,
        LauncherConfig,
        LauncherLog,
        Probe,
        log_dir,
        probe as run_probe,
        user_data_dir,
    )
except ImportError:  # 直接 `python launcher/main.py`
    from core import (  # type: ignore[no-redef]
        APP_NAME,
        IS_WINDOWS,
        LAUNCHER_NAME,
        ChildProcess,
        LauncherConfig,
        LauncherLog,
        Probe,
        log_dir,
        probe as run_probe,
        user_data_dir,
    )

MUTEX_NAME = "Local\\BreezeVoiceStudioLauncher"
ERROR_ALREADY_EXISTS = 183
EXIT_ENV_ERROR = 2


# --------------------------------------------------------------------------- #
# Windows 外壳细节
# --------------------------------------------------------------------------- #
def enable_dpi_awareness() -> None:
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def set_app_id() -> None:
    """让任务栏把启动器识别为独立应用，图标不串到 python.exe 上。"""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
            "BreezeBlue.VoiceStudio.Launcher.1"
        )
    except Exception:  # noqa: BLE001
        pass


class SingleInstance:
    """命名互斥体；已存在实例时尝试把它的窗口提到前台。"""

    def __init__(self, name: str) -> None:
        self.handle = None
        self.already_running = False
        if not IS_WINDOWS:
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.handle = kernel32.CreateMutexW(None, False, name)
            self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        except Exception:  # noqa: BLE001
            self.handle = None

    def focus_existing(self, title: str) -> bool:
        if not IS_WINDOWS:
            return False
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = user32.FindWindowW(None, title)
            if not hwnd:
                return False
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
        except Exception:  # noqa: BLE001
            return False


# --------------------------------------------------------------------------- #
# 无界面自检
# --------------------------------------------------------------------------- #
def probe_report(probe: Probe) -> dict:
    return {
        "ready": probe.ready,
        "root": str(probe.root) if probe.root else None,
        "python": str(probe.python) if probe.python else None,
        "python_source": probe.python_source,
        "python_trusted": probe.python_trusted,
        "checks": [
            {
                "key": check.key,
                "label": check.label,
                "ok": check.ok,
                "fatal": check.fatal,
                "detail": check.detail,
                "hint": check.hint,
            }
            for check in probe.checks
        ],
        "searched": probe.searched,
    }


def emit(text: str) -> None:
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except (OSError, UnicodeEncodeError):
        try:
            stream.write(text.encode("ascii", "replace").decode("ascii") + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001
            pass


def cmd_check(cfg: LauncherConfig, cli_root: str | None, as_json: bool) -> int:
    probe = run_probe(cfg, cli_root)
    report = probe_report(probe)
    if as_json:
        emit(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        emit(f"项目根目录：{report['root'] or '未找到'}")
        emit(f"解释器：{report['python'] or '未找到'}（{report['python_source']}）")
        for check in probe.checks:
            mark = "OK  " if check.ok else ("FAIL" if check.fatal else "WARN")
            emit(f"[{mark}] {check.label}: {check.detail}")
            if not check.ok and check.hint:
                emit(f"        建议：{check.hint}")
        if not probe.ready:
            emit("--- 诊断：搜索过的目录 ---")
            for item in report["searched"]:
                emit(f"  {item}")
    return 0 if probe.ready else EXIT_ENV_ERROR


def cmd_run_for(cfg: LauncherConfig, cli_root: str | None, seconds: float) -> int:
    probe = run_probe(cfg, cli_root)
    if not probe.ready or probe.python is None or probe.root is None:
        emit("环境检查未通过，无法启动。用 --check 查看详情。")
        return EXIT_ENV_ERROR

    log = LauncherLog(log_dir(probe.root), "launcher-selftest.log")
    child = ChildProcess(on_line=lambda line: emit(f"[child] {line}"))
    script = probe.root / cfg.script
    emit(f"启动：{probe.python} {script}（cwd={probe.root}，{seconds:g}s 后停止）")
    try:
        child.start(probe.python, script, probe.root, cfg.args, cfg.build_env(), keep_on_exit=False)
    except Exception as exc:  # noqa: BLE001
        emit(f"启动失败：{exc.__class__.__name__}: {exc}")
        return EXIT_ENV_ERROR

    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(0.4)
        if not child.alive():
            break
    alive_before = child.alive()
    code = child.stop()
    emit(f"停止前是否存活：{alive_before}，退出码：{code}")
    log.write(f"selftest exit={code}")
    return 0


# --------------------------------------------------------------------------- #
# 崩溃兜底
# --------------------------------------------------------------------------- #
def report_crash(exc: BaseException) -> None:
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        print(detail, file=sys.stderr)
    except Exception:  # noqa: BLE001 - pythonw 下 stderr 为 None
        pass
    target = user_data_dir() / "logs"
    crash: Path | None
    try:
        target.mkdir(parents=True, exist_ok=True)
        crash = target / "launcher-crash.log"
        with crash.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{detail}")
    except OSError:
        crash = None

    message = (
        f"启动器遇到未处理的错误：\n\n{exc.__class__.__name__}: {exc}\n\n"
        + (f"完整堆栈已写入：\n{crash}" if crash else "无法写入崩溃日志。")
    )
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(LAUNCHER_NAME, message)
        root.destroy()
    except Exception:  # noqa: BLE001
        print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="BreezeVoiceLauncher",
        description="Breeze Voice Studio 启动器：带环境自检与进程托管的图形启动入口。",
    )
    parser.add_argument("--root", help="手工指定项目根目录（含 app/desktop.py）")
    parser.add_argument("--script", help="覆盖启动脚本（默认取 launcher.json 的 script，即 app/desktop.py）")
    parser.add_argument("--check", action="store_true", help="只做环境检查并输出结果，不打开界面")
    parser.add_argument("--json", action="store_true", help="与 --check 搭配，输出 JSON")
    parser.add_argument(
        "--run-for", type=float, metavar="SECONDS", help="启动目标程序并在若干秒后停止（无人值守自检）"
    )
    parser.add_argument("--no-autostart", action="store_true", help="打开启动器后不自动拉起目标程序")
    parser.add_argument("--debug", action="store_true", help="打开 pywebview 调试模式（BREEZE_DEBUG=1）")
    parser.add_argument("--allow-multi", action="store_true", help="允许多开（跳过单实例检查）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = LauncherConfig.load()
    if args.script:
        cfg.script = args.script
    if args.no_autostart:
        cfg.autostart = False
    if args.debug:
        cfg.env["BREEZE_DEBUG"] = "1"

    if args.check:
        return cmd_check(cfg, args.root, args.json)
    if args.run_for is not None:
        return cmd_run_for(cfg, args.root, max(1.0, args.run_for))

    enable_dpi_awareness()
    set_app_id()

    instance = SingleInstance(MUTEX_NAME)
    if instance.already_running and not args.allow_multi:
        if not instance.focus_existing(LAUNCHER_NAME):
            print("启动器已在运行，但无法定位其窗口。请从任务栏或系统托盘打开。", file=sys.stderr)
        return 0

    try:
        from .ui import main as ui_main
    except ImportError:
        from ui import main as ui_main  # type: ignore[no-redef]

    try:
        ui_main(cfg, args.root)
    except BaseException as exc:  # noqa: BLE001 - 兜底，保证有可见反馈
        report_crash(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
