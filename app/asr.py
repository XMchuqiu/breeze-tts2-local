"""参考音频自动转写（ASR）。

用途：上传参考音频后自动识别其文字内容，供界面展示、情感提示与
IndexTTS-2.5 的发音校正参考。识别失败不阻塞音色注册流程。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_DEFAULT_MODEL = os.environ.get("BREEZE_ASR_MODEL", "small")


class Transcriber:
    """faster-whisper 包装，进程内单例，按需加载。"""

    def __init__(self, model_size: str = _DEFAULT_MODEL, device: str = "cpu"):
        self.model_size = model_size
        self.device = device
        self._model = None
        self._lock = threading.Lock()
        self._failed = False

    @property
    def available(self) -> bool:
        return not self._failed

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type="int8",
                download_root=str(Path(__file__).resolve().parent.parent / ".asr_models"),
            )
            return self._model

    def transcribe(self, audio_path: Path, language: str | None = None) -> tuple[str, str]:
        """返回 (文本, 识别语种)。任何异常都降级为空文本。"""
        if self._failed:
            return "", ""
        try:
            model = self._ensure_model()
            segments, info = model.transcribe(
                str(audio_path),
                language=(language or None),
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = "".join(seg.text for seg in segments).strip()
            return text, getattr(info, "language", "") or ""
        except Exception as exc:  # noqa: BLE001
            print(f"[asr] 转写失败，已跳过：{exc}")
            return "", ""

    def warmup(self) -> None:
        """后台预加载，避免首次上传等待。"""
        try:
            self._ensure_model()
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            print(f"[asr] 模型加载失败，自动转写功能将不可用：{exc}")
