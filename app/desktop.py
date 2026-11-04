"""桌面外壳：内嵌 uvicorn 服务 + pywebview 原生窗口。

启动逻辑：
1. 若已有实例在运行（logs/instance.json 中的端口可响应），直接打开新窗口指向它，
   避免重复加载模型造成显存翻倍；
2. 否则新起一个本地服务，写入实例信息，再创建原生窗口；
3. 窗口关闭时按「普通关闭」处理：开了托盘设置就收进托盘（服务与显存保留），
   否则释放服务与显存后退出本进程；
4. 「完全退出并关闭」是另一条路径：结束本窗口、自带后端、复用模式下的后端、
   托盘图标以及扫描到的其他 desktop.py 进程，清理实例记录后再退出。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

LOG_DIR = ROOT / "logs"
INSTANCE_FILE = LOG_DIR / "instance.json"
WINDOW_TITLE = "Breeze Voice Studio"


def _log(message: str) -> None:
    print(f"[shell] {message}", flush=True)


def _setup_logging() -> None:
    """pythonw 启动时没有控制台，把输出写入日志文件便于排查。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        stream = open(LOG_DIR / "desktop.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = stream
        sys.stderr = stream


def find_free_port(start: int = 8765, span: int = 40) -> int:
    for port in range(start, start + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("没有可用端口")


def wait_until_ready(port: int, timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.6)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def _port_open(port: int, timeout: float = 0.6) -> bool:
    if not port:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _instance_data() -> dict:
    try:
        data = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def running_instance_port() -> int | None:
    """判断是否已有实例在跑。

    用 TCP 连通性而不是 HTTP 请求：新实例正在加载模型时（20–40 秒）后端会被
    GIL 拖慢，HTTP 探测容易超时，从而误判「没有实例」再起一个后端、显存翻倍。
    端口在本进程写入 instance.json 之前就已开始监听，TCP 握手由内核完成。
    """
    try:
        port = int(_instance_data()["port"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        if _port_open(port, timeout=1.5):
            return port
    except OSError:
        return None
    return None


def _write_instance(port: int) -> None:
    try:
        INSTANCE_FILE.write_text(
            json.dumps({"port": port, "pid": os.getpid()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _clear_instance() -> None:
    try:
        if INSTANCE_FILE.exists():
            INSTANCE_FILE.unlink()
    except OSError:
        pass


def _work_area() -> tuple[int, int, int, int]:
    """主屏可用工作区（已排除任务栏）；取不到时退回保守值。"""
    try:
        from ctypes import byref, windll
        from ctypes import wintypes

        rect = wintypes.RECT()
        if windll.user32.SystemParametersInfoW(0x0030, 0, byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001 - 非 Windows 或调用失败都不影响启动
        pass
    return 0, 0, 1280, 800


def _window_geometry() -> dict:
    """把窗口限制在工作区内并居中：否则底部会被任务栏挡住。"""
    left, top, right, bottom = _work_area()
    avail_w, avail_h = max(900, right - left), max(640, bottom - top)
    width = min(1220, avail_w - 60)
    height = min(840, avail_h - 60)
    return {
        "width": width,
        "height": height,
        "x": left + max(0, (avail_w - width) // 2),
        "y": top + max(0, (avail_h - height) // 2),
    }


def _open_window(url: str, webview, js_api=None):
    return webview.create_window(
        WINDOW_TITLE,
        url,
        min_size=(980, 660),
        background_color="#0a0e13",
        text_select=True,
        js_api=js_api,
        **_window_geometry(),
    )


class NativeBridge:
    """暴露给网页的 JS API（pywebview 的 ``js_api``）。

    为什么必须有这一层：复用模式下页面由**后端进程**提供，``/api/app/*`` 会打到
    后端进程登记的钩子上——点「收进托盘」隐藏的会是别人的窗口。走 js_api 才是
    「谁显示这个窗口，就由谁的进程执行」。浏览器直接访问时没有这层，前端自动
    回退到 HTTP 接口。
    """

    def __init__(self) -> None:
        self._shell: DesktopShell | None = None

    def _set_shell(self, shell) -> None:
        """窗口外壳创建完成后再回填（js_api 对象必须先于窗口存在）。"""
        self._shell = shell

    def _wait_shell(self, timeout: float = 5.0):
        deadline = time.time() + timeout
        while self._shell is None and time.time() < deadline:
            time.sleep(0.05)
        return self._shell

    # -- 供 JS 调用 --------------------------------------------------------
    def get_runtime(self) -> dict:
        shell = self._wait_shell()
        from app import runtime

        if shell is None:
            return {
                "settings": runtime.load_settings(),
                "capabilities": {
                    "shell": False,
                    "tray": False,
                    "hide_to_tray": False,
                    "full_exit": False,
                },
                "targets": {},
                "exit_scope": list(_EXIT_SCOPE),
            }
        return {
            "pid": os.getpid(),
            "settings": runtime.load_settings(),
            "capabilities": {
                "shell": True,
                "tray": shell.tray_available(),
                "hide_to_tray": True,
                "full_exit": True,
            },
            "targets": shell.describe_targets(),
            "exit_scope": list(_EXIT_SCOPE),
        }

    def set_minimize_to_tray(self, enabled: bool) -> dict:
        from app import runtime

        settings = runtime.update_settings({"minimize_to_tray": bool(enabled)})
        shell = self._wait_shell()
        if shell is not None:
            shell.apply_settings()
        return {"settings": settings}

    def hide_to_tray(self) -> dict:
        shell = self._wait_shell()
        if shell is None:
            return {"ok": False, "reason": "窗口还没准备好"}
        _log(f"界面请求收进托盘（PID {os.getpid()}）")
        return shell.hide_to_tray()

    def show_window(self) -> dict:
        shell = self._wait_shell()
        return shell.show_window() if shell else {"ok": False, "reason": "窗口还没准备好"}

    def full_exit(self) -> dict:
        shell = self._wait_shell()
        if shell is None:
            return {"ok": False, "reason": "窗口还没准备好"}
        _log(f"界面请求完全退出（PID {os.getpid()}）")
        return shell.full_exit("界面按钮")


_EXIT_SCOPE = (
    "本窗口进程",
    "本地后端服务（uvicorn）",
    "复用模式下由其他入口启动的后端",
    "托盘图标",
    "logs/instance.json 实例记录",
)


class DesktopShell:
    """窗口生命周期的收尾者：托盘、普通关闭、完全退出。

    ``owns_server=True`` 表示本进程自己起了 uvicorn；``False`` 表示复用别的
    进程（启动器/启动.bat 起的那一个）的后端，此时完全退出要把它也结束掉。
    """

    def __init__(
        self,
        window,
        *,
        owns_server: bool,
        server=None,
        server_thread: threading.Thread | None = None,
        backend_port: int | None = None,
        backend_pid: int | None = None,
    ) -> None:
        from app import runtime
        from app import shell_tray
        from app import proc_utils

        self._runtime = runtime
        self._shell_tray = shell_tray
        self._proc = proc_utils

        self.window = window
        self.owns_server = owns_server
        self.server = server
        self.server_thread = server_thread
        self.backend_port = backend_port
        self.backend_pid = backend_pid or (os.getpid() if owns_server else 0)

        self.tray = shell_tray.ShellTray(self._on_tray_show, self._on_tray_quit)
        self._settings = runtime.load_settings()
        self._lock = threading.Lock()
        self._exiting = False
        self._hidden = False
        self._tray_hint_shown = False

    # ------------------------------------------------------------------ 注册
    def attach(self) -> None:
        """接上窗口事件，并把能力登记给 FastAPI（网页里的按钮靠它生效）。"""
        try:
            self.window.events.closing += self._on_closing
            self.window.events.minimized += self._on_minimized
        except Exception as exc:  # noqa: BLE001 - 老版本 pywebview 可能没有该事件
            _log(f"窗口事件注册失败（不影响使用）：{exc}")

        self._runtime.register(
            hide_to_tray=self.hide_to_tray,
            show_window=self.show_window,
            full_exit=self.full_exit,
            apply_settings=self.apply_settings,
            tray_available=self.tray_available,
            quit_targets=self.describe_targets,
        )
        _log(
            "托盘设置："
            + ("最小化/关闭时收进托盘" if self._settings.get("minimize_to_tray") else "关闭窗口即退出")
        )

    # ------------------------------------------------------------------ 托盘
    def tray_available(self) -> bool:
        ok, reason = self._shell_tray.available()
        if not ok:
            _log(f"托盘不可用：{reason}")
        return ok

    def _on_tray_show(self) -> None:
        self.show_window()

    def _on_tray_quit(self) -> None:
        self.full_exit("托盘菜单")

    def hide_to_tray(self) -> dict:
        """收进托盘：窗口隐藏，服务、模型与显存全部保留。"""
        if self._exiting:
            return {"ok": False, "reason": "正在退出"}
        if not self.tray.start():
            _log(f"托盘启动失败：{self.tray.error}")
            return {"ok": False, "reason": self.tray.error or "托盘不可用"}
        try:
            self.window.hide()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"隐藏窗口失败：{exc}"}
        self._hidden = True
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify("已收进托盘：右键图标可「打开主窗口」或「完全退出并关闭」")
        _log("窗口已收进托盘（普通关闭）")
        return {"ok": True}

    def show_window(self) -> dict:
        self._hidden = False
        try:
            self.window.show()
            self.window.restore()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}
        return {"ok": True}

    def apply_settings(self) -> None:
        """网页里改了设置后调用：关掉托盘开关时顺手收起托盘图标。"""
        self._settings = self._runtime.load_settings()
        if not self._settings.get("minimize_to_tray") and not self._hidden:
            self.tray.stop()
        _log(
            "设置已应用："
            + ("收进托盘" if self._settings.get("minimize_to_tray") else "关闭即退出")
        )

    # ------------------------------------------------------------------ 窗口事件
    def _on_closing(self):
        """点窗口 X。返回 False 表示取消关闭。"""
        if self._exiting:
            return True
        if self._settings.get("minimize_to_tray"):
            # 先取消关闭，再异步隐藏：直接在事件里 hide() 会让 winforms 重入
            threading.Timer(0.05, self.hide_to_tray).start()
            return False
        return True

    def _on_minimized(self, *_args) -> None:
        if self._exiting or not self._settings.get("minimize_to_tray"):
            return
        threading.Timer(0.05, self.hide_to_tray).start()

    # ------------------------------------------------------------------ 完全退出
    def describe_targets(self) -> dict:
        return {
            "window_pid": os.getpid(),
            "backend_pid": self.backend_pid,
            "owns_backend": self.owns_server,
            "port": self.backend_port,
        }

    def full_exit(self, reason: str = "") -> dict:
        """完全退出并关闭：结束所有相关进程，不留后台。

        走这条路时窗口先隐藏（避免用户看到"关了一半"），真正的清理在后台线程里做，
        结束后直接 ``os._exit``，确保解释器不会因为残留的非守护线程而挂住。
        """
        with self._lock:
            if self._exiting:
                return {"ok": True, "already": True}
            self._exiting = True

        _log(f"开始完全退出（{reason or '用户请求'}）")
        self.tray.stop()
        try:
            self.window.hide()
        except Exception:  # noqa: BLE001
            pass

        threading.Thread(target=self._exit_worker, name="full-exit", daemon=True).start()
        return {"ok": True, "pid": os.getpid(), "backend_pid": self.backend_pid}

    def _exit_worker(self) -> None:
        killed: list[int] = []
        failed: list[dict] = []
        try:
            # 0) 先擦掉实例记录：即使后面被异常打断，下次启动也不会误判「已有实例」
            if self.owns_server:
                _clear_instance()

            # 1) 复用模式：后端属于别的进程，必须单独结束
            if not self.owns_server and self.backend_pid:
                if self._proc.is_python(self.backend_pid) and self._proc.kill_tree(self.backend_pid):
                    killed.append(self.backend_pid)
                else:
                    failed.append({"pid": self.backend_pid, "reason": "复用后端未能结束"})

            # 2) 自己起的服务：通知 uvicorn 优雅退出
            if self.server is not None:
                self.server.should_exit = True
                if self.server_thread is not None:
                    self.server_thread.join(timeout=4)

            # 3) 兜底扫描：清掉其他入口留下的、与本项目 desktop.py 相关的进程
            #    必须跳过自己和祖先链（venv 的 pythonw 启动器与真实解释器命令行相同）
            protected = self._proc.ancestors(os.getpid()) | {os.getpid()}
            for item in self._proc.find_desktop_processes(ROOT):
                pid = item["pid"]
                if pid in protected or pid in killed:
                    continue
                if not self._proc.is_python(pid):
                    continue
                if self._proc.kill_tree(pid):
                    killed.append(pid)
                else:
                    failed.append({"pid": pid, "reason": "taskkill 失败"})

            # 4) 等端口释放，再清一次实例记录（复用模式的后端可能重写过）
            if self.backend_port:
                deadline = time.time() + 6
                while time.time() < deadline and _port_open(self.backend_port):
                    time.sleep(0.2)

            _clear_instance()
            _log(
                "完全退出完成：结束 %d 个进程%s"
                % (
                    len(killed),
                    "，失败 %d 个" % len(failed) if failed else "",
                )
            )
        except Exception as exc:  # noqa: BLE001 - 清理阶段不允许把退出流程卡死
            _log(f"完全退出时出现异常：{exc}")
        finally:
            time.sleep(0.2)
            os._exit(0)


def main() -> None:
    _setup_logging()

    import webview

    from app import runtime

    existing = running_instance_port()
    bridge = NativeBridge()
    if existing:
        # 已有实例：只开窗口复用后端，避免二次加载模型
        data = _instance_data()
        window = _open_window(f"http://127.0.0.1:{existing}", webview, bridge)
        shell = DesktopShell(
            window,
            owns_server=False,
            backend_port=existing,
            backend_pid=int(data.get("pid") or 0),
        )
        bridge._set_shell(shell)
        shell.attach()
        webview.start(debug=os.environ.get("BREEZE_DEBUG") == "1")
        runtime.unregister()
        return

    import uvicorn

    from app.server import app as fastapi_app

    port = find_free_port()
    config = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not wait_until_ready(port):
        print("本地服务启动超时", file=sys.stderr)
        sys.exit(1)

    _write_instance(port)
    window = _open_window(f"http://127.0.0.1:{port}", webview, bridge)
    shell = DesktopShell(
        window,
        owns_server=True,
        server=server,
        server_thread=thread,
        backend_port=port,
    )
    bridge._set_shell(shell)
    shell.attach()

    try:
        webview.start(debug=os.environ.get("BREEZE_DEBUG") == "1")
    finally:
        runtime.unregister()
        server.should_exit = True
        thread.join(timeout=5)
        _clear_instance()


if __name__ == "__main__":
    main()
