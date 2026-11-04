"""IndexTTS 2.5 适配器（GPT + S2Mel + BigVGAN，支持情感提示与语速因子）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..models import ROOT, VENDOR, ModelSpec
from .base import TTSEngineBase, install_progress_hook

if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))


class IndexTTS25Engine(TTSEngineBase):
    def __init__(self, spec: ModelSpec, use_bf16: bool = True):
        super().__init__(spec)
        self.use_bf16 = use_bf16
        self.qwen_emo_enabled = False

    def _load_model(self) -> None:
        # 上游代码内部使用 ./checkpoints/hf_cache 相对路径，必须切换到仓库根目录
        os.chdir(VENDOR)
        from indextts.infer_v2_5 import IndexTTS2

        model_dir = Path(self.spec.model_dir)  # type: ignore[arg-type]
        # QwenEmotion 常驻显存约 1.2 GB，8 GB 卡默认关闭，按需通过环境变量开启
        qwen_emo = (
            os.environ.get("BREEZE_QWEN_EMO", "0") == "1"
            and (model_dir / "qwen0.6bemo4-merge").exists()
        )
        self.qwen_emo_enabled = qwen_emo
        self._model = IndexTTS2(
            cfg_path=str(model_dir / "config.yaml"),
            model_dir=str(model_dir),
            use_bf16=self.use_bf16,
            use_cuda_kernel=False,
            use_deepspeed=False,
            use_accel=False,
            use_torch_compile=False,
            use_qwen_emo=qwen_emo,
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
        kwargs = dict(
            spk_audio_prompt=str(ref_audio),
            text=text,
            output_path=str(output_path),
            lang=(language or "ZH").upper(),
            duration_factor=float(duration_factor),
            verbose=verbose,
        )
        emotion_applied = False
        if emotion_text.strip():
            if self.qwen_emo_enabled:
                kwargs.update(use_emo_text=True, emo_text=emotion_text.strip(), emo_alpha=1.0)
                emotion_applied = True
            else:
                notes.append("情感提示已忽略：该模型未启用 QwenEmotion 权重（设置 BREEZE_QWEN_EMO=1 后生效）")
        self._model.infer(**kwargs)
        return {"emotion_requested": bool(emotion_text.strip()), "emotion_applied": emotion_applied}

    def _cached_prompt(self) -> str | None:
        return getattr(self._model, "cache_audio_prompt", None)

    def status(self) -> dict:
        info = super().status()
        info["dtype"] = "bfloat16" if self.use_bf16 else "float32"
        info["emotion_ready"] = self.qwen_emo_enabled
        return info
