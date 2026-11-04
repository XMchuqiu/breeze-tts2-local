"""音色库：参考音频规范化、元数据持久化、音色资源管理。"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

TARGET_SR = 22050          # IndexTTS-2.5 参考音频工作采样率
MAX_REF_SECONDS = 20.0     # 参考音频上限，超过则截取能量最强的片段
MIN_REF_SECONDS = 1.5      # 低于该时长基本无法稳定提取音色
PEAK_DBFS = -3.0


class VoiceError(Exception):
    """音色处理过程中的可预期错误。"""


@dataclass
class Voice:
    id: str
    name: str
    created_at: float
    duration_sec: float
    sample_rate: int
    transcript: str = ""
    emotion_text: str = ""
    source_file: str = ""
    ref_audio: str = "ref.wav"
    language: str = "ZH"
    peak_dbfs: float = 0.0
    rms_dbfs: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", name.strip())
    return cleaned.strip("-")[:40] or "voice"


class VoiceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = __import__("threading").RLock()

    # ---------- 查询 ----------
    def list(self) -> list[Voice]:
        voices: list[Voice] = []
        for meta_path in sorted(self.root.glob("*/meta.json")):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                voices.append(Voice(**data))
            except Exception:
                continue
        voices.sort(key=lambda v: v.created_at, reverse=True)
        return voices

    def get(self, voice_id: str) -> Voice:
        meta_path = self.root / voice_id / "meta.json"
        if not meta_path.exists():
            raise VoiceError(f"音色 {voice_id} 不存在")
        return Voice(**json.loads(meta_path.read_text(encoding="utf-8")))

    def audio_path(self, voice_id: str) -> Path:
        voice = self.get(voice_id)
        path = self.root / voice_id / voice.ref_audio
        if not path.exists():
            raise VoiceError(f"音色 {voice_id} 的参考音频缺失")
        return path

    # ---------- 写入 ----------
    def create(
        self,
        src_path: Path,
        name: str,
        transcript: str = "",
        language: str = "ZH",
        source_name: str = "",
    ) -> Voice:
        voice_id = f"v_{uuid.uuid4().hex[:10]}"
        work_dir = self.root / voice_id
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            y, sr = librosa.load(str(src_path), sr=TARGET_SR, mono=True)
        except Exception as exc:  # 解码失败
            shutil.rmtree(work_dir, ignore_errors=True)
            raise VoiceError(f"无法解码音频文件：{exc}") from exc

        if y.size == 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise VoiceError("音频为空，请更换样本")

        duration = y.size / sr
        if duration < MIN_REF_SECONDS:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise VoiceError(f"参考音频过短（{duration:.1f}s），建议 3–15 秒")

        y = self._trim_best_window(y, sr)
        y = self._normalize(y)

        ref_path = work_dir / "ref.wav"
        sf.write(str(ref_path), y, sr, subtype="PCM_16")

        peak = float(np.max(np.abs(y))) or 1e-9
        rms = float(np.sqrt(np.mean(y ** 2))) or 1e-9

        voice = Voice(
            id=voice_id,
            name=name.strip() or _slug(Path(src_path).stem),
            created_at=time.time(),
            duration_sec=round(y.size / sr, 2),
            sample_rate=sr,
            transcript=transcript.strip(),
            source_file=source_name or Path(src_path).name,
            language=language,
            peak_dbfs=round(20 * np.log10(peak), 2),
            rms_dbfs=round(20 * np.log10(rms), 2),
        )
        self._write(voice)
        return voice

    def update(self, voice_id: str, **fields) -> Voice:
        with self._lock:
            voice = self.get(voice_id)
            for key, value in fields.items():
                if hasattr(voice, key) and value is not None:
                    setattr(voice, key, value)
            self._write(voice)
            return voice

    def delete(self, voice_id: str) -> None:
        work_dir = self.root / voice_id
        if not work_dir.exists():
            raise VoiceError(f"音色 {voice_id} 不存在")
        shutil.rmtree(work_dir, ignore_errors=True)

    # ---------- 内部 ----------
    def _write(self, voice: Voice) -> None:
        meta_path = self.root / voice.id / "meta.json"
        meta_path.write_text(
            json.dumps(voice.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _trim_best_window(self, y: np.ndarray, sr: int) -> np.ndarray:
        max_len = int(MAX_REF_SECONDS * sr)
        if y.size <= max_len:
            return y
        hop = int(0.5 * sr)
        frame = 1.0 * sr
        best_start, best_energy = 0, -1.0
        for start in range(0, y.size - max_len + 1, hop):
            seg = y[start:start + max_len]
            energy = float(np.mean(seg[: int(frame)] ** 2))
            if energy > best_energy:
                best_energy, best_start = energy, start
        return y[best_start:best_start + max_len]

    def _normalize(self, y: np.ndarray) -> np.ndarray:
        peak = float(np.max(np.abs(y)))
        if peak < 1e-6:
            raise VoiceError("参考音频几乎为静音，请更换样本")
        target = 10 ** (PEAK_DBFS / 20.0)
        y = y * (target / peak)
        return np.clip(y, -1.0, 1.0).astype(np.float32)
