"""工作室窗口的系统托盘图标（可选能力）。

托盘不是必需品：pystray / Pillow 缺失、或在无桌面的环境里失败时，
``ShellTray.start()`` 只返回 False，调用方保持普通窗口即可，不影响主流程。

菜单只保留两项关键动作，和启动器保持一致：
- 打开主窗口（双击托盘图标同效）
- 完全退出并关闭
"""

from __future__ import annotations

import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON_CANDIDATES = (
    ROOT / "launcher" / "assets" / "app.ico",
    ROOT / "app" / "web" / "favicon.ico",
    ROOT / "assets" / "app.ico",
)

TRAY_ID = "breeze-voice-studio"


def available() -> tuple[bool, str]:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - 缺依赖只是降级，不该抛给调用方
        return False, f"缺少托盘依赖（{exc.__class__.__name__}: {exc}）"
    return True, ""


def load_icon_image(size: int = 64):
    """优先用项目图标，读不到就现画一个，保证托盘一定有图。"""
    from PIL import Image, ImageDraw

    for candidate in ICON_CANDIDATES:
        try:
            if candidate.is_file():
                image = Image.open(candidate)
                image.load()
                return image.convert("RGBA")
        except Exception:  # noqa: BLE001 - 换下一个候选
            continue

    box = size * 4
    image = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, box - 1, box - 1), fill=(47, 129, 247, 255))
    cx = cy = box * 0.5
    for index, radius in enumerate((0.18, 0.30, 0.42)):
        spread = box * radius
        draw.arc(
            (cx - spread, cy - spread, cx + spread, cy + spread),
            start=-55,
            end=55,
            fill=(255, 255, 255, 255 if index < 2 else 170),
            width=max(2, int(box * 0.045)),
        )
    return image


class ShellTray:
    """pystray 图标的薄封装。菜单动作直接调用传入的回调（由调用方保证线程安全）。"""

    def __init__(self, on_show, on_quit, title: str = "Breeze Voice Studio") -> None:
        self.on_show = on_show
        self.on_quit = on_quit
        self.title = title
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
                pystray.MenuItem("打开主窗口", self._show, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("完全退出并关闭", self._quit),
            )
            self.icon = pystray.Icon(TRAY_ID, load_icon_image(), self.title, menu)
        except Exception as exc:  # noqa: BLE001
            self.error = f"创建托盘图标失败：{exc}"
            self.icon = None
            return False

        self._thread = threading.Thread(target=self._run, name="shell-tray", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        assert self.icon is not None
        try:
            self.icon.run()
        except Exception as exc:  # noqa: BLE001
            self.error = f"托盘图标运行失败：{exc}"
            self.icon = None

    def _show(self, *_args) -> None:
        try:
            self.on_show()
        except Exception:  # noqa: BLE001 - 托盘线程不能崩
            pass

    def _quit(self, *_args) -> None:
        try:
            self.on_quit()
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

    def notify(self, message: str, title: str | None = None) -> None:
        try:
            if self.icon is not None and hasattr(self.icon, "notify"):
                self.icon.notify(message, title or self.title)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["ShellTray", "available", "load_icon_image"]
