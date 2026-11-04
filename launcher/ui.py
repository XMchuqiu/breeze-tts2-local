"""启动器界面（tkinter，无第三方 UI 依赖）。

界面只做三件事，但把状态说清楚：
1. 环境自检——项目根、解释器、核心文件、读写权限，缺什么、怎么补，直接写在行内；
2. 启动/停止/重启目标程序，实时滚动子进程输出，启动参数与工作目录打在日志首行；
3. 退出策略可控——关闭窗口时可选「收进托盘继续跑」或「停止工作室并退出」。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:  # 作为包导入（python -m launcher.main）
    from . import core
    from .tray import TrayController, resource_path
except ImportError:  # 作为脚本运行（python launcher/main.py）或 PyInstaller 打包后
    import core  # type: ignore[no-redef]
    from tray import TrayController, resource_path  # type: ignore[no-redef]

FONT = "Microsoft YaHei UI"

C = {
    "bg": "#0a0e13",
    "panel": "#111821",
    "panel2": "#0d131a",
    "line": "#1f2b38",
    "fg": "#e6edf3",
    "muted": "#8b98a5",
    "accent": "#2f81f7",
    "accent_active": "#1f6feb",
    "ok": "#3fb950",
    "warn": "#d29922",
    "err": "#f85149",
    "btn": "#1b2530",
    "btn_active": "#26333f",
}


class LauncherApp:
    POLL_MS = 120

    def __init__(self, cfg: core.LauncherConfig, cli_root: str | None = None) -> None:
        self.cfg = cfg
        self.cli_root = cli_root
        self.probe: core.Probe | None = None
        self.child = core.ChildProcess(on_line=self._on_child_line, on_exit=self._on_child_exit)
        self.tray = TrayController(self)
        self.log: core.LauncherLog | None = None

        self._queue: queue.Queue = queue.Queue()
        self._closing = False
        self._verified = False
        self._verify_message = ""
        self._ready_url: str | None = None
        self._instance_label: tk.Label | None = None
        self._started_at = 0.0
        self._ready_ticks = 0
        self._auto_started = False
        #: 窗口当前是不是「已收进托盘」（用于判断点击是否等同于恢复）
        self._hidden_to_tray = False
        #: withdraw() 自己也会触发 Unmap，用它避免递归
        self._suppress_unmap = False
        self._quit_summary = ""
        self._tray_hint_shown = False

        self.root = tk.Tk()
        self.root.title(core.LAUNCHER_NAME)
        self.root.configure(bg=C["bg"])
        self._center(self.cfg.window_width, self.cfg.window_height)
        self.root.minsize(700, 500)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self._on_unmap)
        self._apply_icon()

        self._build_ui()
        self._refresh_actions()
        self._start_probe()
        self.root.after(self.POLL_MS, self._poll_queue)
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------------ 基础
    def _apply_icon(self) -> None:
        ico = resource_path("assets/app.ico")
        if not ico.is_file():
            ico = core.launcher_dir() / "assets" / "app.ico"
        try:
            if ico.is_file():
                self.root.iconbitmap(str(ico))
        except tk.TclError:
            pass

    def _dark_titlebar(self, window=None) -> None:
        """让系统标题栏跟随深色界面（Win10 1809+ 支持，失败就保持默认）。"""
        if not core.IS_WINDOWS:
            return
        try:
            import ctypes

            hwnd = (window or self.root).winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            target = parent or hwnd
            value = ctypes.c_int(1)
            for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE：新版本 20，1809 为 19
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    target, attribute, ctypes.byref(value), ctypes.sizeof(value)
                )
                if result == 0:
                    break
        except Exception:  # noqa: BLE001 - 属于外观增强，任何异常都忽略
            pass

    def _center(self, width: int, height: int) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 30)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def dispatch(self, func) -> None:
        """托盘线程 → tkinter 主线程。"""
        try:
            self.root.after(0, func)
        except (RuntimeError, tk.TclError):
            pass

    # ------------------------------------------------------------------ 布局
    def _build_ui(self) -> None:
        pad = 18
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)

        # 头部
        header = tk.Frame(self.root, bg=C["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=pad, pady=(pad, 4))
        header.grid_columnconfigure(0, weight=1)

        title_box = tk.Frame(header, bg=C["bg"])
        title_box.grid(row=0, column=0, sticky="w")
        tk.Label(
            title_box, text="Breeze Voice Studio", bg=C["bg"], fg=C["fg"],
            font=(FONT, 16, "bold"),
        ).pack(anchor="w")
        self.subtitle = tk.Label(
            title_box, text="本地语音克隆工作台 · 启动器", bg=C["bg"], fg=C["muted"],
            font=(FONT, 9),
        )
        self.subtitle.pack(anchor="w", pady=(2, 0))

        badge_box = tk.Frame(header, bg=C["bg"])
        badge_box.grid(row=0, column=1, sticky="e")
        self.badge_dot = tk.Label(badge_box, text="●", bg=C["bg"], fg=C["muted"], font=(FONT, 11))
        self.badge_dot.pack(side="left")
        self.badge = tk.Label(
            badge_box, text="未启动", bg=C["bg"], fg=C["muted"], font=(FONT, 10, "bold")
        )
        self.badge.pack(side="left", padx=(4, 0))

        # 自检面板
        self.check_panel = tk.Frame(self.root, bg=C["panel"], highlightthickness=1,
                                    highlightbackground=C["line"])
        self.check_panel.grid(row=1, column=0, sticky="ew", padx=pad, pady=(10, 8))
        self.check_panel.grid_columnconfigure(0, weight=1)
        self.check_body = tk.Frame(self.check_panel, bg=C["panel"])
        self.check_body.grid(row=0, column=0, sticky="ew", padx=14, pady=12)
        self.check_body.grid_columnconfigure(0, weight=1)

        # 日志
        log_wrap = tk.Frame(self.root, bg=C["panel"], highlightthickness=1,
                            highlightbackground=C["line"])
        log_wrap.grid(row=2, column=0, sticky="nsew", padx=pad, pady=(0, 8))
        log_wrap.grid_columnconfigure(0, weight=1)
        log_wrap.grid_rowconfigure(1, weight=1)

        log_head = tk.Frame(log_wrap, bg=C["panel"])
        log_head.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 0))
        log_head.grid_columnconfigure(0, weight=1)
        tk.Label(log_head, text="运行日志", bg=C["panel"], fg=C["muted"],
                 font=(FONT, 9, "bold")).grid(row=0, column=0, sticky="w")
        self.log_path_label = tk.Label(log_head, text="", bg=C["panel"], fg=C["muted"],
                                       font=(FONT, 8))
        self.log_path_label.grid(row=0, column=1, sticky="e")

        text_wrap = tk.Frame(log_wrap, bg=C["panel2"])
        text_wrap.grid(row=1, column=0, sticky="nsew", padx=10, pady=(6, 10))
        text_wrap.grid_columnconfigure(0, weight=1)
        text_wrap.grid_rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            text_wrap, bg=C["panel2"], fg="#c9d4e0", insertbackground=C["fg"],
            font=("Consolas", 9), wrap="word", relief="flat", bd=0,
            padx=10, pady=8, height=9, state="disabled", highlightthickness=0,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(text_wrap, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.tag_config("err", foreground=C["err"])
        self.log_text.tag_config("warn", foreground=C["warn"])
        self.log_text.tag_config("ok", foreground=C["ok"])
        self.log_text.tag_config("muted", foreground=C["muted"])
        self.log_text.tag_config("accent", foreground=C["accent"])

        # 状态行（左：实时状态；右：托盘设置开关）
        status_row = tk.Frame(self.root, bg=C["bg"])
        status_row.grid(row=3, column=0, sticky="ew", padx=pad, pady=(0, 6))
        status_row.grid_columnconfigure(0, weight=1)

        self.status = tk.Label(status_row, text="准备中…", bg=C["bg"], fg=C["muted"],
                               font=(FONT, 9), anchor="w")
        self.status.grid(row=0, column=0, sticky="ew")

        self.var_tray = tk.BooleanVar(value=self.cfg.minimize_to_tray)
        self.chk_tray = tk.Checkbutton(
            status_row,
            text="最小化后收进系统托盘",
            variable=self.var_tray,
            command=self._on_tray_toggle,
            bg=C["bg"], fg=C["muted"], activebackground=C["bg"],
            activeforeground=C["fg"], selectcolor=C["panel2"],
            font=(FONT, 9), bd=0, highlightthickness=0, cursor="hand2",
            anchor="e",
        )
        self.chk_tray.grid(row=0, column=1, sticky="e")

        # 按钮行
        bar = tk.Frame(self.root, bg=C["bg"])
        bar.grid(row=4, column=0, sticky="ew", padx=pad, pady=(0, pad))
        bar.grid_columnconfigure(1, weight=1)

        left = tk.Frame(bar, bg=C["bg"])
        left.grid(row=0, column=0, sticky="w")
        self.btn_start = self._button(left, "启动工作室", self.start_target, kind="primary")
        self.btn_start.pack(side="left")
        self.btn_stop = self._button(left, "停止", self.stop_target)
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_restart = self._button(left, "重启", self.restart_target)
        self.btn_restart.pack(side="left", padx=(8, 0))

        right = tk.Frame(bar, bg=C["bg"])
        right.grid(row=0, column=2, sticky="e")
        self.btn_dir = self._button(right, "选择项目目录…", self.choose_root)
        self.btn_dir.pack(side="left")
        self.btn_open = self._button(right, "打开目录", self.open_project)
        self.btn_open.pack(side="left", padx=(8, 0))
        self.btn_log = self._button(right, "查看日志", self.open_log)
        self.btn_log.pack(side="left", padx=(8, 0))
        self.btn_tray = self._button(right, "收进托盘", self.minimize_to_tray)
        self.btn_tray.pack(side="left", padx=(8, 0))
        self.btn_quit = self._button(right, "完全退出", self.request_full_exit)
        self.btn_quit.pack(side="left", padx=(8, 0))

        # 所有控件就位后再渲染占位内容（_render_checks 会用到 log_path_label）
        self._render_checks(None, "正在检查运行环境…")
        self.root.after(60, self._dark_titlebar)

    def _button(self, parent, text: str, command, kind: str = "normal") -> tk.Button:
        primary = kind == "primary"
        btn = tk.Button(
            parent, text=text, command=command,
            font=(FONT, 9, "bold" if primary else "normal"),
            bg=C["accent"] if primary else C["btn"],
            fg="#ffffff" if primary else C["fg"],
            activebackground=C["accent_active"] if primary else C["btn_active"],
            activeforeground="#ffffff" if primary else C["fg"],
            disabledforeground="#5a6672",
            relief="flat", bd=0, padx=14, pady=6, cursor="hand2",
            highlightthickness=0,
        )
        btn.normal_bg = C["accent"] if primary else C["btn"]
        btn.normal_fg = "#ffffff" if primary else C["fg"]
        return btn

    # ------------------------------------------------------------------ 自检
    def _render_checks(self, probe: core.Probe | None, placeholder: str = "") -> None:
        for child in self.check_body.winfo_children():
            child.destroy()

        if probe is None:
            tk.Label(
                self.check_body, text=placeholder, bg=C["panel"], fg=C["muted"],
                font=(FONT, 9), anchor="w",
            ).grid(row=0, column=0, sticky="w")
            if getattr(self, "log_path_label", None) is not None:
                self.log_path_label.configure(text="")
            return

        self._instance_label = None
        for index, check in enumerate(probe.checks):
            color = C["ok"] if check.ok else (C["err"] if check.fatal else C["warn"])
            row = tk.Frame(self.check_body, bg=C["panel"])
            row.grid(row=index * 2, column=0, sticky="ew", pady=(0 if index == 0 else 6, 0))
            row.grid_columnconfigure(2, weight=1)

            tk.Label(row, text="●", bg=C["panel"], fg=color, font=(FONT, 9)).grid(
                row=0, column=0, sticky="nw", padx=(0, 8)
            )
            tk.Label(
                row, text=check.label, bg=C["panel"], fg=C["fg"],
                font=(FONT, 9, "bold"), width=12, anchor="w",
            ).grid(row=0, column=1, sticky="nw")
            detail = tk.Label(
                row, text=check.detail, bg=C["panel"], fg=C["muted"] if check.ok else color,
                font=(FONT, 9), anchor="w", justify="left", wraplength=520,
            )
            detail.grid(row=0, column=2, sticky="w")
            if check.key == "instance":
                self._instance_label = detail

            if not check.ok and check.hint:
                tk.Label(
                    self.check_body, text=f"处理建议：{check.hint}", bg=C["panel"],
                    fg=C["warn"], font=(FONT, 8), anchor="w", justify="left", wraplength=600,
                ).grid(row=index * 2 + 1, column=0, sticky="w", padx=(22, 0), pady=(1, 2))

        if probe.root is not None:
            self.subtitle.configure(text=str(probe.root))
            self._ensure_log(probe.root)
        self.log_path_label.configure(text=str(self.log.path) if self.log else "")

    def _ensure_log(self, root: Path) -> None:
        target = core.log_dir(root)
        if self.log is None or self.log.path.parent != target:
            self.log = core.LauncherLog(target)

    def _start_probe(self) -> None:
        self.probe = None
        self._verified = False
        self._verify_message = ""
        self._render_checks(None, "正在检查运行环境…")
        self._set_status("正在检查运行环境…", "warn")
        self._refresh_actions()

        def worker() -> None:
            try:
                result = core.probe(self.cfg, self.cli_root)
            except Exception as exc:  # noqa: BLE001 - 探测阶段不允许崩掉界面
                self._queue.put(("probe_error", str(exc)))
                return
            self._queue.put(("probe", result))

        threading.Thread(target=worker, name="probe", daemon=True).start()

    def _on_probe(self, probe: core.Probe) -> None:
        self.probe = probe
        self._render_checks(probe)

        if not probe.checks:
            return

        if not probe.ready:
            failed = "、".join(check.label for check in probe.failed)
            self._append_log(f"[启动器] 环境检查未通过：{failed}", "err")
            self._set_status(f"环境检查未通过（{len(probe.failed)} 项异常），按上方提示处理后重试", "err")
            self._set_badge("不可启动", "err")
            self._refresh_actions()
            return

        self._append_log("[启动器] 环境检查通过", "ok")
        self._append_log(f"[启动器] 项目根目录：{probe.root}")
        self._append_log(f"[启动器] 解释器：{probe.python}（{probe.python_source}）")
        if probe.running:
            self._append_log(
                f"[启动器] 检测到已有工作室在运行：{probe.running['url']}"
                f"（PID {probe.running.get('pid')}）",
                "warn",
            )
            self._append_log(
                "[启动器] 本次启动只会新开一个窗口，不会重复加载模型；"
                "关闭该窗口不影响已经在跑的后端",
                "muted",
            )
        self._set_status("环境就绪", "ok")
        self._set_badge("就绪", "ok")

        if probe.needs_verify:
            self._set_status(f"正在验证 {probe.python_source} 的依赖…", "warn")
            self._refresh_actions()

            def worker() -> None:
                ok, message = core.verify_python(probe.python)  # type: ignore[arg-type]
                self._queue.put(("verify", ok, message))

            threading.Thread(target=worker, name="verify", daemon=True).start()
            return

        self._verified = True
        self._refresh_actions()
        self._maybe_autostart()

    def _on_verify(self, ok: bool, message: str) -> None:
        self._verified = ok
        self._verify_message = message
        if ok:
            self._append_log(f"[启动器] 解释器依赖验证通过（{message}）", "ok")
            self._set_status("环境就绪", "ok")
            self._maybe_autostart()
        else:
            self._append_log(f"[启动器] 解释器不可用：{message}", "err")
            self._set_status(f"解释器不可用：{message}", "err")
            self._set_badge("不可启动", "err")
        self._refresh_actions()

    def _maybe_autostart(self) -> None:
        if not self.cfg.autostart or self._auto_started:
            return
        self._auto_started = True
        self.root.after(300, self.start_target)

    # ------------------------------------------------------------------ 目标进程
    def child_alive(self) -> bool:
        return self.child.alive()

    def start_target(self) -> None:
        if self._closing or self.child.alive():
            return
        probe = self.probe
        if probe is None or probe.root is None or probe.python is None:
            messagebox.showerror(core.LAUNCHER_NAME, "环境检查未通过，无法启动。请先按界面提示处理。")
            return
        if not probe.ready or not self._verified:
            messagebox.showerror(
                core.LAUNCHER_NAME,
                "环境检查未通过或解释器依赖缺失，无法启动。\n" + (self._verify_message or ""),
            )
            return

        script = probe.root / self.cfg.script
        self._append_log("[启动器] 正在启动工作室…", "accent")
        try:
            self.child.start(
                python=probe.python,
                script=script,
                cwd=probe.root,
                args=self.cfg.args,
                env=self.cfg.build_env(),
                keep_on_exit=self.cfg.keep_running_on_exit,
            )
        except PermissionError as exc:
            self._fail_start(
                "启动被系统拒绝",
                f"{exc}\n\n常见原因：\n"
                "• 杀毒软件拦截了 python.exe / 项目文件\n"
                "• 项目在 Program Files 等受保护目录，且未取得写权限\n"
                "• 文件被标记为「来自网络」（右键属性 → 解除锁定）\n\n"
                "建议：把项目放到非系统盘的用户目录下再试。",
            )
            return
        except FileNotFoundError as exc:
            self._fail_start("文件缺失", f"{exc}\n\n请重新检查项目是否完整，或点击「选择项目目录…」重新指定。")
            return
        except OSError as exc:
            self._fail_start("无法创建进程", f"{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self._fail_start("启动失败", f"{exc.__class__.__name__}: {exc}")
            return

        self._started_at = time.time()
        self._ready_url = None
        self._ready_ticks = 0
        if self.probe is not None and self.probe.running:
            self._append_log("[启动器] 复用模式：仅新开窗口，后端沿用已有实例", "warn")
        self._append_log(f"[启动器] PID {self.child.pid}", "muted")
        self._append_log(f"[启动器] 工作目录：{self.child.workdir}", "muted")
        self._append_log(f"[启动器] 启动命令：{subprocess_hint(self.child.command)}", "muted")
        self._set_badge("启动中", "warn")
        self._set_status("正在加载模型，首次启动通常需要 20–40 秒…", "warn")
        self._refresh_actions()

        if self.cfg.start_minimized:
            self.root.after(1200, self.minimize_to_tray)

    def _fail_start(self, title: str, detail: str) -> None:
        self._append_log(f"[启动器] {title}：{detail.splitlines()[0]}", "err")
        self._set_status(f"{title}，详见弹窗与日志", "err")
        self._set_badge("启动失败", "err")
        self._refresh_actions()
        messagebox.showerror(f"{core.LAUNCHER_NAME} — {title}", detail, parent=self.root)

    def stop_target(self, then=None) -> None:
        if not self.child.alive():
            if then:
                then()
            return
        self._set_status("正在停止工作室…", "warn")
        self._set_badge("停止中", "warn")
        self._refresh_actions()

        def worker() -> None:
            code = self.child.stop()
            self._queue.put(("stopped", code, then))

        threading.Thread(target=worker, name="stop", daemon=True).start()

    def _on_stopped(self, code: int | None, then) -> None:
        self._append_log(f"[启动器] 工作室已停止（退出码 {code}）", "muted")
        self._set_badge("已停止", "muted")
        self._set_status("已停止", "muted")
        self._ready_url = None
        self._update_instance_label("未检测到（已退出）", C["muted"])
        self.tray.refresh()
        self._refresh_actions()
        if then:
            then()

    def restart_target(self) -> None:
        if not self._verified:
            self.start_target()
            return
        self._append_log("[启动器] 正在重启…", "accent")
        self.stop_target(then=lambda: self.root.after(400, self.start_target))

    def _on_child_line(self, line: str) -> None:
        self._queue.put(("log", line))

    def _on_child_exit(self, code: int) -> None:
        self._queue.put(("child_exit", code))

    def _on_child_exit_main(self, code: int) -> None:
        self._append_log(f"[启动器] 工作室进程结束，退出码 {code}", "ok" if code == 0 else "err")
        if code != 0:
            self._set_status(f"工作室异常退出（退出码 {code}），请看上方日志", "err")
            self._set_badge("异常退出", "err")
        else:
            self._set_status("工作室已关闭", "muted")
            self._set_badge("已停止", "muted")
        self._ready_url = None
        self._update_instance_label("未检测到（已退出）", C["muted"])
        self.tray.refresh()
        self._refresh_actions()

        if code != 0 and not self._closing:
            self.root.lift()
            self.root.focus_force()

    # ------------------------------------------------------------------ 状态
    def _set_status(self, text: str, level: str = "muted") -> None:
        color = {"ok": C["ok"], "warn": C["warn"], "err": C["err"]}.get(level, C["muted"])
        self.status.configure(text=text, fg=color)

    def _set_badge(self, text: str, level: str = "muted") -> None:
        color = {"ok": C["ok"], "warn": C["warn"], "err": C["err"]}.get(level, C["muted"])
        self.badge.configure(text=text, fg=color)
        self.badge_dot.configure(fg=color)

    def _update_instance_label(self, text: str, color: str) -> None:
        """让自检里的「运行实例」一行跟上实时状态。"""
        label = getattr(self, "_instance_label", None)
        if label is None:
            return
        try:
            label.configure(text=text, fg=color)
        except tk.TclError:
            pass

    def _append_log(self, line: str, tag: str | None = None) -> None:
        text = self.log_text
        if tag is None:
            lowered = line.lower()
            if any(key in line for key in ("Traceback", "错误", "失败", "Error", "ERROR")) or "traceback" in lowered:
                tag = "err"
            elif any(key in line for key in ("警告", "warn", "WARN")):
                tag = "warn"
        text.configure(state="normal")
        text.insert("end", line + "\n", tag or ())
        text.see("end")
        lines = int(text.index("end-1c").split(".")[0])
        if lines > 1500:
            text.delete("1.0", "400.0")
        text.configure(state="disabled")
        if self.log is not None:
            self.log.write(line)

    def _refresh_actions(self) -> None:
        can_start = (
            not self._closing
            and self._verified
            and self.probe is not None
            and self.probe.ready
            and not self.child.alive()
        )
        running = self.child.alive()
        self._enable(self.btn_start, can_start)
        self._enable(self.btn_stop, running and not self._closing)
        self._enable(self.btn_restart, self._verified and not self._closing)
        self._enable(self.btn_tray, not self._closing)
        self._enable(self.btn_dir, True)
        self._enable(self.btn_open, self.probe is not None and self.probe.root is not None)
        self._enable(self.btn_log, self.log is not None)
        self._enable(self.btn_quit, not self._closing)

    @staticmethod
    def _enable(button: tk.Button, enabled: bool) -> None:
        if enabled:
            button.configure(
                state="normal",
                bg=getattr(button, "normal_bg", C["btn"]),
                fg=getattr(button, "normal_fg", C["fg"]),
                cursor="hand2",
            )
        else:
            button.configure(state="disabled", bg=C["btn"], fg="#5a6672", cursor="arrow")

    def _tick(self) -> None:
        if self._closing:
            return
        running = self.child.alive()
        if running:
            elapsed = int(time.time() - self._started_at)
            stamp = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
            if self._ready_url:
                self._set_status(f"运行中 {stamp} · {self._ready_url}", "ok")
                self._set_badge("运行中", "ok")
            else:
                self._set_status(f"启动中 {stamp} · 正在加载模型…", "warn")
                self._check_ready_async()
        self.root.after(1000, self._tick)

    def _check_ready_async(self) -> None:
        # 模型加载最长可能 2 分钟以上，探测上限要比加载时间宽裕
        if self._ready_ticks > 180:
            return
        self._ready_ticks += 1
        root = self.probe.root if self.probe else None

        def worker() -> None:
            info = core.instance_info(root, timeout=0.8)
            if info:
                self._queue.put(("ready", info))

        threading.Thread(target=worker, name="ready", daemon=True).start()

    def _on_ready(self, info: dict) -> None:
        self._ready_url = info["url"]
        self._append_log(f"[启动器] 界面已就绪：{info['url']}（若仍在加载模型，界面内会显示进度）", "ok")
        self._set_status(f"运行中 · {info['url']}", "ok")
        self._set_badge("运行中", "ok")
        self._update_instance_label(f"运行中：{info['url']}", C["ok"])
        self.tray.refresh()

    # ------------------------------------------------------------------ 队列
    def _poll_queue(self) -> None:
        try:
            while True:
                event = self._queue.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(event[1])
                elif kind == "probe":
                    self._on_probe(event[1])
                elif kind == "probe_error":
                    self._append_log(f"[启动器] 环境检查异常：{event[1]}", "err")
                    self._set_status(f"环境检查异常：{event[1]}", "err")
                    self._set_badge("检查失败", "err")
                elif kind == "verify":
                    self._on_verify(event[1], event[2])
                elif kind == "child_exit":
                    self._on_child_exit_main(event[1])
                elif kind == "stopped":
                    self._on_stopped(event[1], event[2])
                elif kind == "ready":
                    self._on_ready(event[1])
                elif kind == "quit_done":
                    self._on_quit_done(event[1] or {})
                    return
        except queue.Empty:
            pass
        # 退出过程中仍要继续收消息：停止子进程的结果就是在 _closing 之后才回来的，
        # 这里若跟着 _closing 一起停，清理结果永远写不进日志、只能等兜底计时器。
        if not getattr(self, "_destroyed", False):
            try:
                self.root.after(self.POLL_MS, self._poll_queue)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ 动作
    def open_project(self) -> None:
        if self.probe is None or self.probe.root is None:
            messagebox.showinfo(core.LAUNCHER_NAME, "还没有定位到项目目录。请点击「选择项目目录…」。")
            return
        ok, detail = core.open_in_explorer(self.probe.root)
        if not ok:
            messagebox.showerror(core.LAUNCHER_NAME, detail)

    def open_log(self) -> None:
        if self.log is None:
            messagebox.showinfo(core.LAUNCHER_NAME, "日志还没有生成。")
            return
        ok, detail = core.open_in_explorer(self.log.path)
        if not ok:
            messagebox.showerror(core.LAUNCHER_NAME, detail)

    def open_studio(self) -> None:
        if self._ready_url:
            ok, detail = core.open_url(self._ready_url)
            if not ok:
                messagebox.showerror(core.LAUNCHER_NAME, detail)
            return
        url = core.instance_info(self.probe.root if self.probe else None)
        if url:
            core.open_url(url["url"])
        else:
            messagebox.showinfo(core.LAUNCHER_NAME, "工作室还没有启动，先点「启动工作室」。")

    def choose_root(self) -> None:
        initial = str(self.probe.root) if self.probe and self.probe.root else str(core.launcher_dir())
        picked = filedialog.askdirectory(
            title="选择 BreezeTTS 项目目录（包含 app 子文件夹）",
            initialdir=initial,
            parent=self.root,
        )
        if not picked:
            return
        path = Path(picked)
        if not (path / "app" / "desktop.py").is_file():
            messagebox.showerror(
                core.LAUNCHER_NAME,
                f"{path}\n\n这个目录里没有 app/desktop.py。\n请选择包含 app 文件夹的项目根目录。",
                parent=self.root,
            )
            return
        self.cfg.project_root = str(path)
        self._save_config(silent=True)
        self.cli_root = None
        self._append_log(f"[启动器] 已手工指定项目目录：{path}", "accent")
        self._start_probe()

    def _save_config(self, silent: bool = False) -> None:
        try:
            path = self.cfg.save()
            if not silent:
                self._append_log(f"[启动器] 配置已保存：{path}", "muted")
        except OSError as exc:
            self._append_log(f"[启动器] 配置保存失败（不影响本次运行）：{exc}", "warn")
            if not silent:
                messagebox.showwarning(core.LAUNCHER_NAME, f"配置无法保存：{exc}\n本次运行不受影响。")

    def minimize_to_tray(self) -> bool:
        """普通关闭 / 最小化：窗口收进托盘，进程与服务继续运行。"""
        if self._closing:
            return False
        if not self.tray.running and not self.tray.start():
            self._append_log(f"[启动器] 托盘不可用：{self.tray.error}", "warn")
            messagebox.showwarning(
                core.LAUNCHER_NAME,
                f"托盘组件不可用：\n{self.tray.error}\n\n已改为最小化到任务栏。",
                parent=self.root,
            )
            self.root.iconify()
            return False
        self._suppress_unmap = True
        self.root.withdraw()
        self._hidden_to_tray = True
        self.root.after(150, self._clear_unmap_guard)
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify(
                core.APP_NAME,
                "已收进托盘：右键图标可「打开主窗口」或「退出（完全退出）」",
            )
        self._set_status("已收进托盘 · 工作室继续运行（右键托盘图标可完全退出）", "muted")
        return True

    def show_window(self) -> None:
        self._suppress_unmap = True
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._hidden_to_tray = False
        self.root.after(150, self._clear_unmap_guard)
        # 从托盘回来时把状态行从「已收进托盘」改回去
        if self.child.alive():
            self._set_status(f"运行中 · {self._ready_url}" if self._ready_url else "启动中 · 正在加载模型…",
                             "ok" if self._ready_url else "warn")
        else:
            self._set_status("就绪 · 工作室未运行，点「启动工作室」开始", "muted")

    def _clear_unmap_guard(self) -> None:
        self._suppress_unmap = False

    def _on_unmap(self, event) -> None:
        """点最小化按钮：设置开启时转成收进托盘。"""
        if event.widget is not self.root or self._closing or self._suppress_unmap:
            return
        if not self.cfg.minimize_to_tray or self._hidden_to_tray:
            return
        self.root.after(1, self._hide_after_minimize)

    def _hide_after_minimize(self) -> None:
        try:
            if self._closing or self.root.state() != "iconic":
                return
        except tk.TclError:
            return
        self.minimize_to_tray()

    def _on_tray_toggle(self) -> None:
        value = bool(self.var_tray.get())
        self.cfg.minimize_to_tray = value
        self._save_config(silent=True)
        self._append_log(
            "[启动器] 最小化/关闭窗口时："
            + ("收进系统托盘，工作室继续运行" if value else "不停留托盘，直接询问是否退出")
            + "（设置已保存）",
            "accent",
        )

    # ------------------------------------------------------------------ 退出
    def on_close(self) -> None:
        """窗口右上角 X —— 普通关闭。

        开着托盘设置时只是收进托盘，进程、后端与已加载的模型全部保留；
        想彻底结束请用托盘右键「退出」或界面上的「完全退出」。
        """
        if self._closing:
            return
        if self.cfg.minimize_to_tray and self.minimize_to_tray():
            self._append_log(
                "[启动器] 已收进托盘（普通关闭）：工作室继续运行，显存不释放", "muted"
            )
            return
        self.request_full_exit()

    def request_full_exit(self) -> None:
        """「完全退出并关闭」：结束所有相关进程，再关掉启动器。"""
        if self._closing:
            return
        still_running = self.child.alive() or bool(self.probe and self.probe.running)
        if still_running:
            choice = self._ask_exit_choice()
            if choice == "tray":
                self.minimize_to_tray()
                return
            if choice != "quit":
                return
        self.quit_app(full=True)

    def _ask_exit_choice(self) -> str:
        dlg = tk.Toplevel(self.root)
        dlg.title("完全退出并关闭")
        dlg.configure(bg=C["panel"])
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        width, height = 500, 250
        self.root.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - height) // 2
        dlg.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        self.root.after(30, lambda: self._dark_titlebar(dlg))

        result = {"value": "cancel"}

        tk.Label(
            dlg, text="工作室仍在运行", bg=C["panel"], fg=C["fg"],
            font=(FONT, 11, "bold"),
        ).pack(anchor="w", padx=18, pady=(16, 4))
        tk.Label(
            dlg, text="收进托盘只隐藏窗口；完全退出会结束下列进程，下次使用需重新加载模型。",
            bg=C["panel"], fg=C["muted"], font=(FONT, 9), justify="left", wraplength=460,
        ).pack(anchor="w", padx=18)
        tk.Label(
            dlg,
            text="清理范围：启动器 · 工作室窗口 · 本地后端（含其他入口启动的实例）· 模型进程，"
                 "并释放显存与端口 8765。",
            bg=C["panel"], fg=C["warn"], font=(FONT, 8), justify="left", wraplength=460,
        ).pack(anchor="w", padx=18, pady=(6, 0))

        row = tk.Frame(dlg, bg=C["panel"])
        row.pack(fill="x", padx=18, pady=16)

        def choose(value: str) -> None:
            result["value"] = value
            dlg.destroy()

        self._button(row, "收进托盘（继续运行）", lambda: choose("tray")).pack(side="left")
        self._button(row, "完全退出并关闭", lambda: choose("quit"), kind="primary").pack(
            side="left", padx=(8, 0)
        )
        self._button(row, "取消", lambda: choose("cancel")).pack(side="right")

        dlg.protocol(
            "WM_DELETE_WINDOW",
            lambda: choose("cancel"),
        )
        dlg.wait_window()
        return result["value"]

    def quit_from_tray(self) -> None:
        """托盘右键「退出」：直接完全退出（带一次确认）。"""
        if self._closing:
            return
        if self.child.alive() and not messagebox.askyesno(
            core.LAUNCHER_NAME,
            "完全退出会结束工作室、本地后端与模型进程，并释放显存与端口。\n\n确定退出？",
        ):
            return
        self.quit_app(full=True)

    def quit_app(self, full: bool = True) -> None:
        if self._closing:
            return
        self._closing = True
        self._refresh_actions()
        self._set_status("正在完全退出，清理相关进程…" if full else "正在退出…", "warn")
        self._set_badge("退出中", "warn")

        root = self.probe.root if self.probe else None
        child_pid = self.child.pid

        def worker() -> None:
            stopped: list[int] = []
            failed: list[dict] = []
            port_released = True
            if self.child.alive():
                self.child.stop(timeout=4.0)
                if child_pid:
                    stopped.append(child_pid)
            if full:
                # 完全退出不受 keep_running_on_exit 影响：这个选项的语义就是「一个都不留」
                report = core.terminate_app_processes(
                    root, extra_pids=(child_pid,) if child_pid else ()
                )
                stopped.extend(p for p in report["stopped"] if p not in stopped)
                failed.extend(report["failed"])
                port_released = bool(report["port_released"])
            self._queue.put(
                (
                    "quit_done",
                    {
                        "stopped": stopped,
                        "failed": failed,
                        "port_released": port_released,
                        "full": full,
                    },
                )
            )

        threading.Thread(target=worker, name="quit", daemon=True).start()
        # 兜底：无论 worker 是否卡住，15 秒后强制关窗（Job Object 会收掉子进程）
        self.root.after(15000, self._final_destroy)

    def _on_quit_done(self, summary: dict) -> None:
        if not summary.get("full", True):
            self._final_destroy()
            return
        stopped = summary.get("stopped") or []
        failed = summary.get("failed") or []
        if stopped:
            self._append_log(
                "[启动器] 已结束进程：" + "、".join(f"PID {pid}" for pid in stopped), "ok"
            )
        else:
            self._append_log("[启动器] 没有需要结束的进程", "muted")
        for item in failed:
            self._append_log(
                f"[启动器] 未能结束 PID {item.get('pid')}：{item.get('reason')}", "warn"
            )
        if summary.get("port_released") is False:
            self._append_log(
                "[启动器] 端口仍被占用，可能有权限更高的进程；可稍后用任务管理器确认", "warn"
            )
        else:
            self._append_log("[启动器] 端口已释放，无后台残留", "ok")
        self._final_destroy()

    def _final_destroy(self) -> None:
        if getattr(self, "_destroyed", False):
            return
        self._destroyed = True
        try:
            self.tray.stop()
        finally:
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:
        self.root.mainloop()


def subprocess_hint(command: list[str]) -> str:
    if not command:
        return ""
    parts = [f'"{item}"' if " " in item else item for item in command]
    return " ".join(parts)


def main(cfg: core.LauncherConfig, cli_root: str | None = None) -> None:
    app = LauncherApp(cfg, cli_root)
    app.run()
