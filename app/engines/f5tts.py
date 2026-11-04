"""F5-TTS 适配器（非自回归 Flow Matching + Vocos，MIT 许可）。

与 IndexTTS 的差异：
- 必须提供参考音频的转写文本，因此依赖音色库中的 ASR 结果；
- 通过 `speed` 参数实现语速控制，与 duration_factor 互为倒数；
- 模型无进程内音色缓存，这里自行记录最近一次参考音频以复用“提取”语义。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..models import ModelSpec
from .base import TTSEngineBase


class F5TTSEngine(TTSEngineBase):
    def __init__(self, spec: ModelSpec):
        super().__init__(spec)
        self._last_prompt: str | None = None

    def _load_model(self) -> None:
        # 声码器（charactr/vocos-mel-24khz）从 HuggingFace 拉取，走国内镜像
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        from f5_tts.api import F5TTS

        model_dir = Path(self.spec.model_dir)  # type: ignore[arg-type]
        self._model = F5TTS(
            model="F5TTS_v1_Base",
            ckpt_file=str(model_dir / "model_1250000.safetensors"),
            vocab_file=str(model_dir / "vocab.txt"),
            device="cuda",
            hf_cache_dir=str(model_dir / "hf_cache"),
        )

    def _infer(
        self,
        ref_audio: Path,
        text: str,
        output_path: Path,
        language: str,
        emotion_text: str,
        duration_factor: float,
        ref_text: str,
        progress,
        notes: list[str],
        verbose: bool = False,
    ) -> dict:
        if not (ref_text or "").strip():
            raise RuntimeError(
                "F5-TTS 需要参考音频的转写文本。请先在音色库中重新提取该音色（会自动运行 ASR），"
                "或确认音色带有转写内容。"
            )
        if emotion_text.strip():
            notes.append("情感提示已忽略：F5-TTS 不支持文本情感控制")

        speed = 1.0 / max(0.5, min(2.0, float(duration_factor)))
        self._model.infer(
            ref_file=str(ref_audio),
            ref_text=ref_text.strip(),
            gen_text=text,
            file_wave=str(output_path),
            speed=speed,
            show_info=(lambda *a, **k: None) if not verbose else print,
        )
        self._soft_limit_peak(output_path)
        self._last_prompt = str(ref_audio)
        return {
            "emotion_requested": bool(emotion_text.strip()),
            "emotion_applied": False,
            "speed": round(speed, 3),
        }

    def _cached_prompt(self) -> str | None:
        return self._last_prompt

    @staticmethod
    def _soft_limit_peak(path: Path, ceiling: float = 0.94) -> None:
        """F5 输出常以满刻度收尾，做一次轻度限峰，避免转 16 bit 时削波。"""
        try:
            import numpy as np
            import soundfile as sf

            y, sr = sf.read(str(path), dtype="float32")
            peak = float(np.max(np.abs(y))) if y.size else 0.0
            if peak > ceiling:
                sf.write(str(path), (y * (ceiling / peak)).astype(np.float32), sr, subtype="PCM_16")
        except Exception:  # noqa: BLE001
            pass
