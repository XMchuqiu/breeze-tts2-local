"""Breeze Voice Studio 启动器 —— 运行时核心。

本模块不依赖任何 GUI 库，可单独导入或命令行调用，
负责四件事：

1. 定位「项目根目录」与「Python 解释器」（打包成 exe 后路径会变，需要专门处理）；
2. 读写启动器配置（exe 同目录 launcher.json，不可写时降级到 %LOCALAPPDATA%）；
3. 以子进程方式拉起 app/desktop.py，并把 stdout/stderr 实时回传给界面；
4. 通过 Windows Job Object 保证「启动器退出 → 目标进程随之退出」，不留后台僵尸。

所有 Windows 专有调用都有非 Windows / 无权限的降级路径。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
APP_NAME = "Breeze Voice Studio"
LAUNCHER_NAME = f"{APP_NAME} 启动器"
TARGET_SCRIPT = "app/desktop.py"

#: 启动前必须存在的文件。缺任何一个都无法正常工作，属于致命项。
REQUIRED_FILES: tuple[tuple[str, str], ...] = (
    ("app/desktop.py", "桌面主程序（启动目标）"),
    ("app/server.py", "本地后端服务"),
    ("app/web/index.html", "前端界面页面"),
    ("app/engines/base.py", "模型适配器基类"),
)

#: 允许缺失但会自动创建的目录。
RUNTIME_DIRS: tuple[str, ...] = ("voices", "outputs", "logs")

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
IS_WINDOWS = os.name == "nt"

DEFAULT_ENV: dict[str, str] = {
    "HF_ENDPOINT": "https://hf-mirror.com",
    "TOKENIZERS_PARALLELISM": "false",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
}

#: 这几个包是目标程序真正依赖的，用 PATH 上的 python 时必须先验证。
REQUIRED_MODULES = ("webview", "uvicorn", "fastapi")


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def launcher_dir() -> Path:
    """启动器自身所在目录。打包后是 exe 所在目录，源码运行时是 launcher/ 目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    """可写的用户数据目录（配置 / 日志的兜底位置）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "BreezeVoiceStudio"
    return Path.home() / ".breeze-voice-studio"


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".wtest_{os.getpid()}"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def config_file() -> Path:
    """配置文件位置：优先 exe 同目录（绿色、可随项目拷贝），不可写则落到用户目录。"""
    local = launcher_dir() / "launcher.json"
    if _is_writable_dir(local.parent):
        if not local.exists() or os.access(local, os.W_OK):
            return local
    return user_data_dir() / "launcher.json"


def log_dir(root: Path | None) -> Path:
    """日志目录：项目内 logs/ 可写就用它，否则用用户目录，避免只读盘报错。"""
    if root is not None:
        candidate = root / "logs"
        if _is_writable_dir(candidate):
            return candidate
    fallback = user_data_dir() / "logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def resolve_path_text(path: Path | None) -> str:
    if not path:
        return "未找到"
    try:
        return str(path)
    except OSError:
        return "路径不可读（可能被删除或无权限）"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class LauncherConfig:
    """启动器配置。字段名与 launcher.json 的键一一对应。"""

    project_root: str = ""
    python_exe: str = ""
    script: str = TARGET_SCRIPT
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ENV))
    #: 启动器退出时是否保留目标进程（true 时不做任何清理，风险是留下后台进程）
    keep_running_on_exit: bool = False
    #: 打开启动器后自动拉起目标程序
    autostart: bool = True
    #: 启动成功后自动收进托盘
    start_minimized: bool = False
    #: 最小化/关闭窗口时收进系统托盘（托盘常驻，服务继续运行）；false 时关闭即询问退出
    minimize_to_tray: bool = True
    window_width: int = 780
    window_height: int = 560

    @classmethod
    def load(cls, path: Path | None = None) -> "LauncherConfig":
        cfg = cls()
        target = path or config_file()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cfg
        if not isinstance(raw, dict):
            return cfg
        for key, value in raw.items():
            if not hasattr(cfg, key):
                continue
            default = getattr(cfg, key)
            if isinstance(default, bool):
                setattr(cfg, key, bool(value))
            elif isinstance(default, int):
                try:
                    setattr(cfg, key, int(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(default, str) and isinstance(value, str):
                setattr(cfg, key, value)
            elif isinstance(default, list) and isinstance(value, list):
                setattr(cfg, key, [str(item) for item in value])
            elif isinstance(default, dict) and isinstance(value, dict):
                merged = dict(DEFAULT_ENV)
                merged.update({str(k): str(v) for k, v in value.items()})
                setattr(cfg, key, merged)
        return cfg

    def save(self, path: Path | None = None) -> Path:
        target = path or config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "project_root": self.project_root,
            "python_exe": self.python_exe,
            "script": self.script,
            "args": self.args,
            "env": self.env,
            "keep_running_on_exit": self.keep_running_on_exit,
            "autostart": self.autostart,
            "start_minimized": self.start_minimized,
            "minimize_to_tray": self.minimize_to_tray,
            "window_width": self.window_width,
            "window_height": self.window_height,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({k: str(v) for k, v in self.env.items()})
        return env


# --------------------------------------------------------------------------- #
# 环境探测
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str = ""
    fatal: bool = True
    hint: str = ""


@dataclass
class Probe:
    root: Path | None
    python: Path | None
    python_trusted: bool
    python_source: str
    checks: list[Check]
    searched: list[str] = field(default_factory=list)
    running: dict | None = None

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.fatal and not c.ok]

    @property
    def ready(self) -> bool:
        return self.root is not None and self.python is not None and not self.failed

    @property
    def needs_verify(self) -> bool:
        """选了非项目自带解释器时，必须先验证依赖是否齐全。"""
        return self.python is not None and not self.python_trusted


def _dedupe(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for item in paths:
        try:
            item = item.resolve()
        except OSError:
            continue
        if item not in out:
            out.append(item)
    return out


def root_candidates(cfg: LauncherConfig, cli_root: str | None = None) -> list[Path]:
    """按优先级列出可能的项目根目录。"""
    raw: list[Path] = []
    if cli_root:
        raw.append(Path(cli_root))
    env_root = os.environ.get("BREEZE_ROOT")
    if env_root:
        raw.append(Path(env_root))
    if cfg.project_root:
        raw.append(Path(cfg.project_root))

    base = launcher_dir()
    raw.extend([base, base.parent, base.parent.parent])
    # 绿色包常见结构：项目/dist/启动器.exe
    raw.extend([base.parent.parent.parent])
    raw.append(Path.cwd())

    candidates = _dedupe(raw)
    # 配置里显式指定的根即使暂时无效也要保留在候选里，便于报错时展示
    return candidates


def _looks_like_project(path: Path) -> bool:
    return (path / "app" / "server.py").is_file() and (path / "app" / "web").is_dir()


def find_project_root(cfg: LauncherConfig, cli_root: str | None = None) -> tuple[Path | None, list[str]]:
    candidates = root_candidates(cfg, cli_root)
    for path in candidates:
        if _looks_like_project(path):
            return path, [str(p) for p in candidates]
    return None, [str(p) for p in candidates]


def _venv_python_candidates(root: Path) -> list[tuple[Path, str, bool]]:
    """项目自带的虚拟环境解释器（可信，无需再验证依赖）。"""
    rels = (
        ("vendor/index-tts/.venv/Scripts/pythonw.exe", "项目虚拟环境（无控制台）"),
        ("vendor/index-tts/.venv/Scripts/python.exe", "项目虚拟环境"),
        (".venv/Scripts/pythonw.exe", "项目虚拟环境 .venv"),
        (".venv/Scripts/python.exe", "项目虚拟环境 .venv"),
        ("venv/Scripts/pythonw.exe", "项目虚拟环境 venv"),
        ("venv/Scripts/python.exe", "项目虚拟环境 venv"),
    )
    found: list[tuple[Path, str, bool]] = []
    for rel, label in rels:
        path = root / rel
        if path.is_file():
            found.append((path, label, True))
    return found


def _is_store_stub(path: Path) -> bool:
    """跳过 Windows 应用商店的 python.exe 存根，双击会弹商店而不是运行。"""
    lowered = str(path).lower()
    return "windowsapps" in lowered or "microsoft\\windowsapps" in lowered


def find_python(root: Path, cfg: LauncherConfig) -> tuple[Path | None, str, bool]:
    """返回 (解释器路径, 来源说明, 是否可信)。只做文件级检查，毫秒级返回。"""
    if cfg.python_exe:
        manual = Path(cfg.python_exe).expanduser()
        if manual.is_file():
            return manual, "手工指定", False

    for path, label, trusted in _venv_python_candidates(root):
        return path, label, trusted

    for name in ("pythonw.exe", "python.exe"):
        found = shutil.which(name)
        if found and not _is_store_stub(Path(found)):
            return Path(found), "系统 PATH", False
    return None, "未找到", False


def verify_python(python: Path, timeout: float = 45.0) -> tuple[bool, str]:
    """真正导入一次目标程序的依赖，确认这个解释器能用。"""
    code = "import {0}; print('ok')".format(", ".join(REQUIRED_MODULES))
    try:
        result = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"依赖检测超时（>{int(timeout)} 秒）"
    except OSError as exc:
        return False, f"无法执行该解释器：{exc}"
    if result.returncode == 0:
        return True, "依赖完整"
    message = (result.stderr or result.stdout or "").strip().splitlines()
    tail = message[-1] if message else f"退出码 {result.returncode}"
    return False, f"缺少依赖：{tail[:160]}"


def probe(cfg: LauncherConfig, cli_root: str | None = None) -> Probe:
    """快速探测（不做 python 导入验证，界面不会卡顿）。"""
    root, searched = find_project_root(cfg, cli_root)
    checks: list[Check] = []

    if root is None:
        checks.append(
            Check(
                "root",
                "项目根目录",
                False,
                "在以下位置都没找到 app/desktop.py",
                True,
                "点下方「选择项目目录…」手动指定 BreezeTTS 所在文件夹",
            )
        )
        return Probe(None, None, False, "未找到", checks, searched)

    checks.append(Check("root", "项目根目录", True, str(root)))

    # 核心文件
    missing = [f"{rel}（{desc}）" for rel, desc in REQUIRED_FILES if not (root / rel).is_file()]
    checks.append(
        Check(
            "files",
            "核心文件",
            not missing,
            "齐全" if not missing else "缺失 " + "、".join(missing),
            True,
            "项目可能没下载完整，或被杀毒软件隔离了文件",
        )
    )

    # 启动脚本
    script = root / cfg.script
    checks.append(
        Check(
            "script",
            "启动脚本",
            script.is_file(),
            str(script) if script.is_file() else f"不存在：{script}",
            True,
            "检查 launcher.json 里的 script 字段",
        )
    )

    # 解释器
    python, source, trusted = find_python(root, cfg)
    checks.append(
        Check(
            "python",
            "Python 解释器",
            python is not None,
            f"{python}（{source}）" if python else "没有找到可用的 python.exe",
            True,
            "先运行一次项目里的 安装.bat / uv sync，或手工指定解释器",
        )
    )

    # 运行时目录（可自动创建）
    unwritable: list[str] = []
    for name in RUNTIME_DIRS:
        if not _is_writable_dir(root / name):
            unwritable.append(name)
    checks.append(
        Check(
            "dirs",
            "读写权限",
            not unwritable,
            "正常" if not unwritable else "不可写：" + "、".join(unwritable),
            False,
            "把项目放到非系统盘、或去掉文件夹的只读属性；日志会改写到用户目录",
        )
    )

    # 已有实例（说明为什么两个入口不会打架：后启动的会复用而不是再加载一遍模型）
    running = instance_info(root, timeout=0.8)
    checks.append(
        Check(
            "instance",
            "运行实例",
            True,
            "未检测到（将新起一个后端）"
            if running is None
            else f"已在 {running['url']} 运行（PID {running.get('pid')}），"
            f"本次启动只会打开新窗口，不会重复加载模型",
            False,
        )
    )

    return Probe(root, python, trusted, source, checks, searched, running)


def running_instance_detail(probe: "Probe") -> str:
    for check in probe.checks:
        if check.key == "instance":
            return check.detail
    return ""


# --------------------------------------------------------------------------- #
# 运行中的实例探测
# --------------------------------------------------------------------------- #
def instance_info(root: Path | None, timeout: float = 1.2) -> dict | None:
    """读取 logs/instance.json 并确认端口在监听，判断是否已有工作室在跑。

    这里用 TCP 连通性而不是 HTTP 请求：模型加载期间后端会被 GIL 拖慢，
    HTTP 很容易超时，但端口在 desktop.py 写入 instance.json 之前就已开始监听，
    TCP 握手由内核完成，不受后端忙闲影响。
    """
    if root is None:
        return None
    record = root / "logs" / "instance.json"
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        port = int(data["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return None
    except OSError:
        return None
    return {"port": port, "pid": data.get("pid"), "url": f"http://127.0.0.1:{port}"}


def instance_record(root: Path | None) -> dict:
    """只读取 logs/instance.json，不判断是否存活。"""
    if root is None:
        return {}
    try:
        data = json.loads((root / "logs" / "instance.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def port_in_use(port: int, timeout: float = 0.6) -> bool:
    if not port:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 进程清理（「完全退出并关闭」用）
# --------------------------------------------------------------------------- #
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_image(pid: int) -> str:
    """返回进程可执行文件完整路径，取不到返回空串。"""
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


def is_python_process(pid: int) -> bool:
    """判断 PID 是否是 python 解释器：防止 PID 复用后误杀无关程序。"""
    return Path(process_image(pid)).name.lower() in ("python.exe", "pythonw.exe")


def kill_process_tree(pid: int) -> bool:
    """强杀整棵进程树（含 uvicorn 子线程无进程的场景也安全）。"""
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


def list_app_processes(root: Path | None = None, timeout: float = 8.0) -> list[dict]:
    """枚举命令行里带着本项目 desktop.py 的 python 进程。

    用 PowerShell CIM 而不是 wmic：后者在新版 Windows 上已移除。
    只用于「完全退出」这一条低频路径，慢一点可以接受；任何失败都返回空列表，
    由调用方退回 instance.json + 子进程这两条已知线索。
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


def terminate_app_processes(root: Path | None, extra_pids: tuple[int, ...] = ()) -> dict:
    """结束与本项目相关的所有 python 进程，并清理实例记录。

    覆盖三类来源，去重后逐个确认「确实是 python 进程」再杀：
    1. 调用方显式给出的 PID（启动器自己拉起的子进程及其进程树）；
    2. logs/instance.json 里登记的 PID（可能是 启动.bat / exe 另一入口起的后端）；
    3. 命令行包含本项目 desktop.py 的进程（兜底，处理记录丢失或存在多个实例）。
    """
    report: dict = {"stopped": [], "failed": [], "port": None, "port_released": True}
    record = instance_record(root)
    port = 0
    try:
        port = int(record.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    report["port"] = port or None

    candidates: set[int] = set()
    for pid in extra_pids:
        try:
            if pid and int(pid) > 0:
                candidates.add(int(pid))
        except (TypeError, ValueError):
            continue
    try:
        recorded = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        recorded = 0
    if recorded > 0:
        candidates.add(recorded)
    for item in list_app_processes(root):
        candidates.add(item["pid"])
    candidates.discard(os.getpid())

    for pid in sorted(candidates):
        image = process_image(pid)
        if not image:
            continue  # 已经自行退出，无需处理
        if Path(image).name.lower() not in ("python.exe", "pythonw.exe"):
            # PID 已被别的程序复用：记失败即可，绝不误杀
            report["failed"].append({"pid": pid, "reason": "不是 python 进程（可能已被系统复用）"})
            continue
        if kill_process_tree(pid):
            report["stopped"].append(pid)
        else:
            report["failed"].append({"pid": pid, "reason": "taskkill 失败（权限或被占用）"})

    if port:
        deadline = time.time() + 6.0
        while time.time() < deadline and port_in_use(port):
            time.sleep(0.2)
        report["port_released"] = not port_in_use(port)

    try:
        (root / "logs" / "instance.json").unlink()
    except (OSError, AttributeError, TypeError):
        pass
    return report



# --------------------------------------------------------------------------- #
# Job Object：父进程退出时连带杀掉子进程树
# --------------------------------------------------------------------------- #
class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class KillOnCloseJob:
    """把子进程放进 Job，句柄一关（本进程退出）系统就替我们收尸。"""

    def __init__(self) -> None:
        self.handle = None
        self.error = ""
        if not IS_WINDOWS:
            self.error = "非 Windows 平台，跳过 Job 托管"
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                self.error = f"CreateJobObject 失败（错误码 {ctypes.get_last_error()}）"
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                self.error = f"SetInformationJobObject 失败（错误码 {ctypes.get_last_error()}）"
                kernel32.CloseHandle(handle)
                return
            self._kernel32 = kernel32
            self.handle = handle
        except Exception as exc:  # noqa: BLE001 - 任何异常都降级为普通托管
            self.error = f"Job 初始化异常：{exc}"

    @property
    def active(self) -> bool:
        return self.handle is not None

    def assign(self, pid: int) -> bool:
        if self.handle is None:
            return False
        try:
            access = PROCESS_SET_QUOTA | PROCESS_TERMINATE
            process = self._kernel32.OpenProcess(access, False, pid)
            if not process:
                self.error = f"OpenProcess 失败（PID {pid}，错误码 {ctypes.get_last_error()}）"
                return False
            try:
                if not self._kernel32.AssignProcessToJobObject(self.handle, process):
                    self.error = f"AssignProcessToJobObject 失败（错误码 {ctypes.get_last_error()}）"
                    return False
            finally:
                self._kernel32.CloseHandle(process)
            return True
        except Exception as exc:  # noqa: BLE001
            self.error = f"加入 Job 异常：{exc}"
            return False

    def close(self) -> None:
        if self.handle is not None:
            try:
                self._kernel32.CloseHandle(self.handle)
            except Exception:  # noqa: BLE001
                pass
            self.handle = None


# --------------------------------------------------------------------------- #
# 子进程托管
# --------------------------------------------------------------------------- #
class ChildProcess:
    """托管 app/desktop.py 子进程，实时转发输出，退出时回调退出码。"""

    def __init__(self, on_line=None, on_exit=None) -> None:
        self.on_line = on_line
        self.on_exit = on_exit
        self._proc: subprocess.Popen | None = None
        self._job: KillOnCloseJob | None = None
        self._lock = threading.Lock()
        self.command: list[str] = []
        self.workdir: str = ""
        self.started_at: float = 0.0

    # -- 状态 ---------------------------------------------------------------
    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc else None

    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # -- 启动 ---------------------------------------------------------------
    def start(
        self,
        python: Path,
        script: Path,
        cwd: Path,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        keep_on_exit: bool = False,
    ) -> None:
        if self.alive():
            raise RuntimeError("目标程序已在运行")
        if not python.is_file():
            raise FileNotFoundError(f"解释器不存在：{python}")
        if not script.is_file():
            raise FileNotFoundError(f"启动脚本不存在：{script}")

        self.command = [str(python), str(script), *(args or [])]
        self.workdir = str(cwd)
        flags = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WINDOWS else 0
        self._proc = subprocess.Popen(
            self.command,
            cwd=str(cwd),
            env=env or os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        self.started_at = time.time()

        if not keep_on_exit:
            self._job = KillOnCloseJob()
            if self._job.active and not self._job.assign(self._proc.pid):
                self._emit(f"[启动器] 警告：{self._job.error}，仍可使用，但异常退出时子进程可能残留")

        threading.Thread(target=self._pump, name="child-stdout", daemon=True).start()
        threading.Thread(target=self._wait, name="child-wait", daemon=True).start()

    def _emit(self, line: str) -> None:
        if self.on_line:
            try:
                self.on_line(line)
            except Exception:  # noqa: BLE001 - 界面回调异常不能拖垮读取线程
                pass

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                self._emit(raw.rstrip("\r\n"))
        except Exception as exc:  # noqa: BLE001
            self._emit(f"[启动器] 读取输出失败：{exc}")
        finally:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass

    def _wait(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.wait()
        if self.on_exit:
            try:
                self.on_exit(code)
            except Exception:  # noqa: BLE001
                pass

    # -- 停止 ---------------------------------------------------------------
    def stop(self, timeout: float = 8.0) -> int | None:
        """先温和 terminate，超时再 taskkill /T /F 整棵进程树。"""
        with self._lock:
            proc = self._proc
            if proc is None:
                return None
            if proc.poll() is not None:
                self._release_job()
                return proc.returncode

            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc.pid)
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
            code = proc.poll()
            self._release_job()
            return code

    def _release_job(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None

    @staticmethod
    def _kill_tree(pid: int) -> None:
        kill_process_tree(pid)


# --------------------------------------------------------------------------- #
# 启动器自身日志
# --------------------------------------------------------------------------- #
class LauncherLog:
    """启动器日志：界面一份、文件一份，写文件失败只降级不抛错。"""

    def __init__(self, directory: Path, filename: str = "launcher.log", max_bytes: int = 2 * 1024 * 1024) -> None:
        self.path = directory / filename
        self._lock = threading.Lock()
        self._max = max_bytes
        self.error = ""
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.error = str(exc)

    def write(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size > self._max:
                    backup = self.path.with_suffix(".log.1")
                    try:
                        backup.unlink()
                    except OSError:
                        pass
                    self.path.replace(backup)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(f"[{stamp}] {line}\n")
            except OSError as exc:
                if not self.error:
                    self.error = str(exc)


def open_in_explorer(path: Path) -> tuple[bool, str]:
    """用资源管理器打开目录，失败时返回原因而不是抛异常。"""
    target = path if path.exists() else path.parent
    if not target.exists():
        return False, f"目录不存在：{target}"
    try:
        if IS_WINDOWS:
            os.startfile(str(target))  # noqa: S606 - 由系统外壳打开目录
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True, str(target)
    except OSError as exc:
        return False, f"打开目录失败：{exc}"


def open_url(url: str) -> tuple[bool, str]:
    try:
        if IS_WINDOWS:
            os.startfile(url)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", url])
        return True, url
    except OSError as exc:
        return False, f"打开链接失败：{exc}"
