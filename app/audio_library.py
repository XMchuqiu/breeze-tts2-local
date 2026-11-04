"""合成音频库：扫描、重命名、删除、在文件管理器中定位。

这一层只碰文件系统，不依赖 FastAPI 与引擎，便于单独测试。
所有对外暴露的文件名都按「单一文件名」处理，拒绝任何带路径分隔符的输入。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
import wave
from pathlib import Path

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}

# Windows 文件名非法字符 + 保留名
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}

#: 时长缓存：绝对路径 -> (mtime, size, seconds)
_DURATION_CACHE: dict[str, tuple[float, float, float | None]] = {}


class ClipError(Exception):
    """文件层面的可预期错误（重名、越界、不存在）。"""


def sanitize_stem(name: str, fallback: str = "clip") -> str:
    """把用户输入整理成安全的文件名主干（不含扩展名）。"""
    stem = (name or "").strip()
    stem = _ILLEGAL.sub("_", stem)
    stem = stem.strip(" .")
    if stem.lower().endswith(tuple(AUDIO_EXTS)):
        stem = Path(stem).stem
    if not stem:
        stem = fallback
    if stem.split(".")[0].upper() in _RESERVED:
        stem = f"_{stem}"
    return stem[:80]


def resolve_clip(directory: Path, name: str) -> Path:
    """把外部传入的文件名解析为目录内的真实文件，越界一律拒绝。"""
    raw = (name or "").strip()
    if not raw or Path(raw).name != raw:
        raise ClipError("文件名不合法")
    path = directory / raw
    if not path.is_file():
        raise ClipError(f"文件不存在：{raw}")
    return path


def probe_duration(path: Path) -> float | None:
    """读取 WAV 头得到时长；失败返回 None（不影响列表展示）。"""
    if path.suffix.lower() != ".wav":
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _DURATION_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    seconds: float | None = None
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if rate:
                seconds = round(handle.getnframes() / float(rate), 2)
    except (wave.Error, OSError, EOFError):
        seconds = None
    _DURATION_CACHE[key] = (stat.st_mtime, stat.st_size, seconds)
    return seconds


def _read_sidecar(path: Path) -> dict:
    sidecar = path.with_suffix(".json")
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_sidecar(path: Path, meta: dict) -> None:
    try:
        path.with_suffix(".json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _item(path: Path) -> dict:
    meta = _read_sidecar(path)
    try:
        stat = path.stat()
        size_kb = round(stat.st_size / 1024, 1)
        created_at = stat.st_mtime
    except OSError:
        size_kb, created_at = 0.0, 0.0
    return {
        "file_name": path.name,
        "stem": path.stem,
        "audio_url": f"/api/outputs/{path.name}",
        "created_at": created_at,
        "size_kb": size_kb,
        "duration_sec": probe_duration(path),
        "model_id": meta.get("model_id"),
        "model_name": meta.get("model_name"),
        "voice_name": meta.get("voice_name"),
        "text": meta.get("text") or "",
    }


def list_clips(directory: Path, limit: int = 60, offset: int = 0, query: str = "") -> dict:
    """按修改时间倒序列出合成音频；目录缺失时返回明确标记而非抛错。"""
    directory_exists = directory.is_dir()
    payload = {
        "items": [],
        "total": 0,
        "offset": max(0, offset),
        "limit": max(1, min(500, limit)),
        "dir_exists": directory_exists,
        "dir_path": str(directory),
    }
    if not directory_exists:
        return payload

    files = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

    items = [_item(p) for p in files]
    keyword = (query or "").strip().lower()
    if keyword:
        items = [
            it
            for it in items
            if keyword in it["file_name"].lower()
            or keyword in (it["text"] or "").lower()
            or keyword in (it["voice_name"] or "").lower()
            or keyword in (it["model_name"] or "").lower()
        ]

    payload["total"] = len(items)
    payload["items"] = items[payload["offset"] : payload["offset"] + payload["limit"]]
    return payload


def rename_clip(directory: Path, old_name: str, new_stem: str) -> dict:
    """重命名单条音频（扩展名保持不变），同步迁移 sidecar。"""
    src = resolve_clip(directory, old_name)
    stem = sanitize_stem(new_stem, src.stem)
    dest = directory / f"{stem}{src.suffix}"
    if dest.name == src.name:
        return _item(src)
    if dest.exists():
        raise ClipError(f"已存在同名文件：{dest.name}，请换一个名称")

    try:
        src.rename(dest)
    except OSError as exc:
        raise ClipError(f"重命名失败：{exc.strerror or exc}") from exc

    side_src = src.with_suffix(".json")
    if side_src.is_file():
        meta = _read_sidecar(src)
        meta["original_name"] = meta.get("original_name") or src.name
        _write_sidecar(dest, meta)
        _unlink_quiet(side_src)
    return _item(dest)


def batch_rename_clips(directory: Path, names: list[str], prefix: str) -> dict:
    """批量改名为「前缀-01、前缀-02…」，重名自动顺延序号。"""
    stem_prefix = sanitize_stem(prefix, "clip")
    renamed: list[dict] = []
    failed: list[dict] = []

    for index, name in enumerate(names, start=1):
        try:
            src = resolve_clip(directory, name)
        except ClipError as exc:
            failed.append({"file_name": name, "reason": str(exc)})
            continue
        seq = index
        while True:
            candidate = f"{stem_prefix}-{seq:02d}"
            dest = directory / f"{candidate}{src.suffix}"
            if dest.name == src.name:
                break
            if not dest.exists():
                break
            seq += 1
        try:
            src.rename(dest)
        except OSError as exc:
            failed.append({"file_name": name, "reason": exc.strerror or str(exc)})
            continue
        side_src = src.with_suffix(".json")
        if side_src.is_file():
            meta = _read_sidecar(src)
            meta["original_name"] = meta.get("original_name") or src.name
            _write_sidecar(dest, meta)
            _unlink_quiet(side_src)
        renamed.append({"from": name, "to": dest.name})

    return {"renamed": renamed, "failed": failed}


def _unlink_quiet(path: Path, attempts: int = 3) -> bool:
    """删除文件，遇到共享冲突（列表请求正在读 sidecar）时短暂重试。"""
    for attempt in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt == attempts - 1:
                return False
            time.sleep(0.05)
    return False


def delete_clips(directory: Path, names: list[str]) -> dict:
    """删除音频及同名 sidecar；逐条汇报结果，单条失败不影响其余。"""
    deleted: list[str] = []
    failed: list[dict] = []
    for name in names:
        try:
            path = resolve_clip(directory, name)
        except ClipError as exc:
            failed.append({"file_name": name, "reason": str(exc)})
            continue
        try:
            path.unlink()
        except OSError as exc:
            failed.append({"file_name": name, "reason": exc.strerror or str(exc)})
            continue
        _DURATION_CACHE.pop(str(path), None)
        _unlink_quiet(path.with_suffix(".json"))
        deleted.append(name)
    return {"deleted": deleted, "failed": failed}


def ensure_directory(directory: Path) -> bool:
    """目录不存在则创建；返回是否为本次新建。"""
    if directory.is_dir():
        return False
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ClipError(f"无法创建存储目录：{exc.strerror or exc}") from exc
    return True


def open_in_file_manager(directory: Path, select_name: str | None = None) -> bool:
    """在系统文件管理器中打开目录；给了文件名则尽量选中它。"""
    if not directory.is_dir():
        raise ClipError("存储目录不存在")
    target = None
    if select_name and Path(select_name).name == select_name:
        candidate = directory / select_name
        if candidate.is_file():
            target = candidate

    try:
        if os.name == "nt":
            if target is not None:
                # 参数直接交给 explorer，引号按字面处理，带空格的路径也能选中
                ctypes.windll.shell32.ShellExecuteW(
                    None, "open", "explorer.exe", f'/select,"{target}"', None, 1
                )
            else:
                os.startfile(str(directory))  # noqa: S606 - 本地目录，非用户可控路径
            return True
        opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"  # type: ignore[attr-defined]
        subprocess.Popen([opener, str(directory)])
        return True
    except Exception as exc:  # noqa: BLE001 - 交给调用方转成友好提示
        raise ClipError(f"无法打开文件管理器：{exc}") from exc
