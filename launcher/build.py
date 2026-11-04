"""构建启动器 exe（单文件、无控制台）。

双击 `构建启动器.bat` 最终会调用这里；bat 只负责找到解释器并转发，
所有逻辑放在 Python 中，避免批处理在中文/长命令行下的解析问题。

用法：
    python launcher/build.py                # 完整构建
    python launcher/build.py --skip-deps    # 跳过依赖安装检查
    python launcher/build.py --onedir       # 若 %TEMP% 不可写，改用目录模式
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "launcher"
ASSETS = LAUNCHER / "assets"
APP_NAME = "BreezeVoiceStudio"
ICON = ASSETS / "app.ico"
VERSION_FILE = LAUNCHER / "version_info.txt"
ENTRY = LAUNCHER / "main.py"

EXCLUDES = (
    "torch", "torchaudio", "torchvision", "transformers", "datasets",
    "numpy", "scipy", "pandas", "matplotlib", "fastapi", "uvicorn",
    "pydantic", "webview", "gradio", "wandb", "cv2", "onnxruntime",
)

PIP_MIRRORS = ("https://pypi.tuna.tsinghua.edu.cn/simple", None)


def log(text: str = "") -> None:
    print(text, flush=True)


def step(title: str) -> None:
    log()
    log(f"[{time.strftime('%H:%M:%S')}] {title}")


def run(cmd: list[str], **kwargs) -> int:
    try:
        return subprocess.run(cmd, **kwargs).returncode
    except FileNotFoundError:
        return 127
    except OSError as exc:
        log(f"    执行失败：{exc}")
        return 1


def check_environment() -> bool:
    step("1/6 检查构建环境")
    log(f"    解释器：{sys.executable}")
    log(f"    Python：{sys.version.split()[0]}")

    if not (ROOT / "app" / "desktop.py").is_file():
        log("    [错误] 缺少 app/desktop.py")
        log("    请把 launcher 目录和本脚本放在项目根目录（与 app 同级）")
        return False
    if not ENTRY.is_file():
        log(f"    [错误] 缺少入口脚本：{ENTRY}")
        return False
    if sys.version_info < (3, 9):
        log("    [错误] 需要 Python 3.9 或更高版本")
        return False
    log("    项目结构正常")
    return True


def ensure_dependencies(skip: bool) -> bool:
    step("2/6 检查打包工具")
    probe = run(
        [sys.executable, "-c", "import PyInstaller, pystray, PIL"],
        capture_output=True,
    )
    if probe == 0:
        log("    PyInstaller / pystray / pillow 已就绪")
        return True
    if skip:
        log("    [错误] 打包工具不完整，且已指定 --skip-deps")
        return False

    packages = ["pyinstaller", "pystray", "pillow"]
    for mirror in PIP_MIRRORS:
        cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", *packages]
        if mirror:
            cmd += ["-i", mirror]
            log(f"    正在安装（镜像 {mirror}）…")
        else:
            log("    镜像不可用，改用默认源重试 …")
        if run(cmd) == 0:
            break

    if run([sys.executable, "-c", "import PyInstaller, pystray, PIL"], capture_output=True) != 0:
        log("    [错误] 依赖安装失败")
        log("    可在项目虚拟环境里手动执行：")
        log(f'    "{sys.executable}" -m pip install pyinstaller pystray pillow')
        return False
    log("    安装完成")
    return True


def make_icon() -> None:
    step("3/6 生成图标")
    code = run([sys.executable, str(LAUNCHER / "make_icon.py")])
    if code != 0 or not ICON.is_file():
        log("    [警告] 图标生成失败，将使用默认图标继续构建")
    elif ICON.is_file():
        log(f"    图标：{ICON}（{ICON.stat().st_size // 1024} KB）")


def clean_previous(onedir: bool) -> None:
    step("4/6 清理旧产物")
    for path in (ROOT / "build" / "launcher", ROOT / "dist"):
        if path.exists():
            try:
                shutil.rmtree(path)
                log(f"    已删除 {path}")
            except OSError as exc:
                log(f"    [警告] 无法删除 {path}：{exc}（exe 可能正在运行）")
    target = ROOT / f"{APP_NAME}.exe"
    if target.exists():
        try:
            target.unlink()
        except OSError as exc:
            log(f"    [警告] 无法删除旧 exe：{exc}")


def build(onedir: bool) -> bool:
    step("5/6 打包（首次约 1–3 分钟，请耐心等待）")
    mode = "--onedir" if onedir else "--onefile"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", mode, "--noconsole",
        "--name", APP_NAME,
        "--icon", str(ICON),
        "--add-data", f"{ICON}{os.pathsep}assets",
        "--version-file", str(VERSION_FILE),
        # 入口脚本在 launcher/ 下，分析路径必须显式包含它，
        # 否则 `import core` 会被当成缺失模块，打出来的 exe 一启动就 ImportError。
        "--paths", str(LAUNCHER),
        "--hidden-import", "core",
        "--hidden-import", "ui",
        "--hidden-import", "tray",
        "--hidden-import", "pystray._win32",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "launcher"),
        "--specpath", str(ROOT / "build"),
    ]
    for module in EXCLUDES:
        cmd += ["--exclude-module", module]
    cmd.append(str(ENTRY))

    log(f"    模式：{mode}")
    if run(cmd) != 0:
        log("    [错误] PyInstaller 失败，请查看上方输出")
        return False
    return True


def collect(onedir: bool) -> Path | None:
    step("6/6 复制到项目根目录")
    produced = ROOT / "dist" / (APP_NAME if onedir else f"{APP_NAME}.exe")
    if onedir:
        produced = ROOT / "dist" / APP_NAME
    if not produced.exists():
        log(f"    [错误] 未找到产物：{produced}")
        return None

    try:
        if onedir:
            target = ROOT / APP_NAME
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(produced, target)
        else:
            target = ROOT / f"{APP_NAME}.exe"
            shutil.copy2(produced, target)
    except OSError as exc:
        log(f"    [警告] 复制失败（可能 exe 正在运行）：{exc}")
        log(f"    产物仍在：{produced}")
        return produced

    size_mb = (
        sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        if onedir
        else target.stat().st_size
    ) / (1024 * 1024)
    log(f"    已生成：{target}（{size_mb:.1f} MB）")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Breeze Voice Studio 启动器 exe")
    parser.add_argument("--skip-deps", action="store_true", help="不自动安装打包依赖")
    parser.add_argument("--onedir", action="store_true", help="目录模式（%TEMP% 不可写时使用）")
    args = parser.parse_args(argv)

    log("=" * 54)
    log("  Breeze Voice Studio 启动器 - 构建")
    log("  只读取 app/desktop.py，不修改任何原有文件")
    log("=" * 54)

    if not check_environment():
        return 1
    if not ensure_dependencies(args.skip_deps):
        return 1
    make_icon()
    clean_previous(args.onedir)
    if not build(args.onedir):
        return 1
    target = collect(args.onedir)
    if target is None:
        return 1

    log()
    log("=" * 54)
    log("  构建完成")
    log()
    log("  方式一（新增）：双击 BreezeVoiceStudio.exe")
    log("  方式二（原有）：双击 启动.bat（未做任何修改）")
    log()
    log("  两种方式共用同一个后端与音色库，不会重复加载模型。")
    log("=" * 54)
    return 0


if __name__ == "__main__":
    sys.exit(main())
