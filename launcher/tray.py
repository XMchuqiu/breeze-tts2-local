"""系统托盘封装。

托盘是可选能力：pystray 或 Pillow 缺失、或在无桌面的环境里失败时，
启动器会退化成「普通窗口 + 最小化到任务栏」，绝不因此启动失败。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

try:  # 作为包导入
    from .core import APP_NAME, LAUNCHER_NAME
except ImportError:  # 作为脚本运行或被 PyInstaller 打包时
    from core import APP_NAME, LAUNCHER_NAME  # type: ignore[no-redef]

ICON_RESOURCE = "assets/app.ico"


def resource_path(relative: str) -> Path:
    """打包后资源在 _MEIPASS 临时目录，源码运行时在 launcher/ 下。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def available() -> tuple[bool, str]:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"缺少托盘依赖（{exc.__class__.__name__}: {exc}）"
    return True, ""


def load_icon_image(size: int = 64):
    """优先读 ico，读不到就现画一个，保证托盘一定有图。"""
    from PIL import Image, ImageDraw

    ico = resource_path(ICON_RESOURCE)
    if ico.is_file():
        try:
            image = Image.open(ico)
            image.load()
            return image.convert("RGBA")
        except Exception:  # noqa: BLE001
            pass

    image = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    box = size * 4
    draw.ellipse((0, 0, box - 1, box - 1), fill=(47, 129, 247, 255))
    cx, cy = box * 0.5, box * 0.5
    for index, radius in enumerate((0.18, 0.30, 0.42)):
        width = int(box * 0.045)
        spread = box * radius
        draw.arc(
            (cx - spread, cy - spread, cx + spread, cy + spread),
            start=-55,
            end=55,
            fill=(255, 255, 255, 255 if index < 2 else 170),
            width=width,
        )
    return image


class TrayController:
    """pystray 图标 + 右键菜单。所有菜单动作都丢回 tkinter 主线程执行。"""

    def __init__(self, app) -> None:
        self.app = app
        self.icon = None
        self.error = ""
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.icon is not None

    def start(self) -> bool:
        if self.icon is not None:
            return True
        ok, reason = available()
        if not ok:
            self.error = reason
            return False
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem(
                    "打开主窗口",
                    lambda *_: self.app.dispatch(self.app.show_window),
                    default=True,
                ),
                pystray.MenuItem(
                    "打开工作室界面",
                    lambda *_: self.app.dispatch(self.app.open_studio),
                    enabled=lambda _item: self.app.child_alive(),
                ),
                pystray.MenuItem("重启工作室", lambda *_: self.app.dispatch(self.app.restart_target)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("查看日志", lambda *_: self.app.dispatch(self.app.open_log)),
                pystray.MenuItem("打开项目目录", lambda *_: self.app.dispatch(self.app.open_project)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出（完全退出）", lambda *_: self.app.dispatch(self.app.quit_from_tray)),
            )
            self.icon = pystray.Icon(
                "breeze-voice-studio-launcher",
                load_icon_image(),
                LAUNCHER_NAME,
                menu,
            )
        except Exception as exc:  # noqa: BLE001
            self.error = f"创建托盘图标失败：{exc}"
            self.icon = None
            return False

        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        assert self.icon is not None
        try:
            self.icon.run()
        except Exception as exc:  # noqa: BLE001
            self.error = f"托盘图标运行失败：{exc}"
            self.icon = None

    def refresh(self) -> None:
        """运行状态变化后刷新菜单可用项。"""
        try:
            if self.icon is not None:
                self.icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        icon, self.icon = self.icon, None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass

    def notify(self, title: str, message: str) -> None:
        try:
            if self.icon is not None and hasattr(self.icon, "notify"):
                self.icon.notify(message, title)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["TrayController", "available", "load_icon_image", "resource_path", "APP_NAME"]
