"""桌面外壳与网页界面之间的运行时桥接。

网页上的「收进托盘 / 完全退出」必须由原生窗口执行，但 FastAPI 不应该反向
import 桌面模块——那会形成循环依赖，也会让「用浏览器打开」的场景跑不起来。
这里用一张极小的回调注册表解耦：

- ``app/desktop.py`` 启动时 ``register(...)`` 登记 ``hide_to_tray`` /
  ``show_window`` / ``full_exit`` / ``quit_targets``；
- ``app/server.py`` 的 ``/api/app/*`` 只调用这里；没有登记时如实回答
  「当前不在桌面窗口中运行」，前端据此禁用按钮而不是报错。

设置项存在项目根的 ``ui-settings.json``，与 ``config.json`` 分开保存：后者由
``EngineManager`` 整体重写（只保留 active_model），混在一起会被覆盖掉。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "ui-settings.json"

#: 界面设置默认值。新增键时同步改这里，前端读到的永远是一份完整配置。
DEFAULTS: dict = {
    #: 最小化/关闭窗口时收进系统托盘；false 表示关闭即退出本窗口
    "minimize_to_tray": True,
    #: 界面主题：light / dark / system（跟随系统）
    "theme": "system",
}

THEMES = ("light", "dark", "system")

_lock = threading.Lock()
_settings: dict | None = None
_host: dict = {}


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
def _clean(key: str, value: object):
    """把外部传入的值收敛到允许范围：类型不对就退回默认值。"""
    default = DEFAULTS[key]
    if key == "theme":
        return value if value in THEMES else default
    if isinstance(default, bool):
        return bool(value)
    return value


def _coerce(raw: object) -> dict:
    data = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key in DEFAULTS:
            if key in raw:
                data[key] = _clean(key, raw[key])
    return data


def load_settings() -> dict:
    global _settings
    with _lock:
        if _settings is not None:
            return dict(_settings)
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _settings = _coerce(raw)
        return dict(_settings)


def update_settings(patch: dict) -> dict:
    """合并写入并落盘；不可写时只更新内存，返回实际生效的值。"""
    global _settings
    current = load_settings()
    if isinstance(patch, dict):
        for key in DEFAULTS:
            if key in patch:
                current[key] = _clean(key, patch[key])
    with _lock:
        _settings = dict(current)
    try:
        SETTINGS_FILE.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    return dict(current)


# --------------------------------------------------------------------------- #
# 宿主回调
# --------------------------------------------------------------------------- #
def register(**callbacks) -> None:
    """由桌面外壳登记原生能力；未登记的功能对前端表现为「不可用」。"""
    _host.update(callbacks)


def unregister() -> None:
    _host.clear()


def attached() -> bool:
    """是否运行在原生窗口里（浏览器直接访问时为 False）。"""
    return bool(_host.get("hide_to_tray"))


def host(name: str):
    return _host.get(name)


def capability_report() -> dict:
    """给前端的一份「当前能做什么」清单。"""
    tray = _host.get("tray_available")
    if callable(tray):
        try:
            tray = tray()
        except Exception:  # noqa: BLE001
            tray = False
    return {
        "shell": attached(),
        "tray": bool(tray),
        "hide_to_tray": bool(_host.get("hide_to_tray")),
        "full_exit": bool(_host.get("full_exit")),
    }
