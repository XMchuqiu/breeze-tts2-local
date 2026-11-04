"""IndexTTS 1.5 适配器（GPT + BigVGAN，轻量、中英双语、无情感/语速控制）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..models import VENDOR, ModelSpec
from .base import TTSEngineBase, install_progress_hook

if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))


class IndexTTS15Engine(TTSEngineBase):
    def _load_model(self) -> None:
        os.chdir(VENDOR)
        from indextts.infer import IndexTTS

        model_dir = Path(self.spec.model_dir)  # type: ignore[arg-type]
        self._model = IndexTTS(
            cfg_path=str(model_dir / "config.yaml"),
            model_dir=str(model_dir),
            use_fp16=True,
            use_cuda_kernel=False,  # 自定义 CUDA 核需现场编译，默认关闭更稳
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
        install_progress_hook(self._model, progress)
        if emotion_text.strip():
            notes.append("情感提示已忽略：IndexTTS 1.5 不支持文本情感控制")
        if abs(duration_factor - 1.0) > 1e-3:
            notes.append(f"语速因子 {duration_factor:.2f}× 已忽略：IndexTTS 1.5 不支持语速控制")
        self._model.infer(
            audio_prompt=str(ref_audio),
            text=text,
            output_path=str(output_path),
            verbose=verbose,
        )
        return {"emotion_requested": bool(emotion_text.strip()), "emotion_applied": False}

    def _cached_prompt(self) -> str | None:
        return getattr(self._model, "cache_audio_prompt", None)

    def status(self) -> dict:
        info = super().status()
        info["dtype"] = "float16"
        return info
