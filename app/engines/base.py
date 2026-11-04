"""引擎适配层的公共接口与工具。"""

from __future__ import annotations

import gc
import threading
import time
from pathlib import Path

from ..models import ModelSpec


def gpu_snapshot() -> dict:
    info: dict = {}
    try:
        import torch

        info["cuda_available"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            info["gpu"] = torch.cuda.get_device_name(0)
            info["vram_total_gb"] = round(total / 1024 ** 3, 1)
            info["vram_free_gb"] = round(free / 1024 ** 3, 1)
            info["vram_used_gb"] = round(torch.cuda.memory_allocated() / 1024 ** 3, 2)
    except Exception:  # noqa: BLE001
        pass
    return info


def release_vram() -> None:
    """显式回收显存：删除引用 → GC → 清空缓存分配器。"""
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:  # noqa: BLE001
        pass


class TTSEngineBase:
    """所有模型适配器的统一接口。

    每个子类只需实现 `_load_model` / `_infer` / `_cached_prompt`，
    加载状态、显存统计、线程安全由本基类统一处理。
    """

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self._model = None
        self._lock = threading.RLock()
        self._loading = False
        self.load_error: str | None = None
        self.load_seconds: float = 0.0

    # ---------- 状态 ----------
    @property
    def model_id(self) -> str:
        return self.spec.id

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def capabilities(self) -> dict:
        return self.spec.capabilities

    def status(self) -> dict:
        info = {
            "model_id": self.spec.id,
            "model_name": self.spec.name,
            "loaded": self.loaded,
            "loading": self._loading,
            "model_dir": str(self.spec.model_dir or ""),
            "weights_present": self.spec.installed,
            "load_seconds": round(self.load_seconds, 1),
            "error": self.load_error,
            "backend": self.spec.backend,
            "capabilities": self.capabilities,
        }
        info.update(gpu_snapshot())
        return info

    # ---------- 生命周期 ----------
    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            if not self.spec.installed:
                missing = [
                    name for name in self.spec.required_files
                    if not (self.spec.model_dir / name).exists()  # type: ignore[operator]
                ]
                raise RuntimeError(
                    f"模型 {self.spec.name} 权重不完整，缺少：{', '.join(missing)}"
                )
            self._loading = True
            started = time.perf_counter()
            try:
                self._load_model()
                self.load_seconds = time.perf_counter() - started
                self.load_error = None
            except Exception as exc:  # noqa: BLE001
                self.load_error = str(exc)
                self._model = None
                raise
            finally:
                self._loading = False

    def unload(self) -> None:
        with self._lock:
            self._model = None
            release_vram()
            self.load_seconds = 0.0

    # ---------- 推理 ----------
    def synthesize(
        self,
        ref_audio: Path,
        text: str,
        output_path: Path,
        language: str = "ZH",
        emotion_text: str = "",
        duration_factor: float = 1.0,
        ref_text: str = "",
        progress=None,
        verbose: bool = False,
    ) -> dict:
        if not text.strip():
            raise ValueError("合成文本为空")
        with self._lock:
            self.load()
            started = time.perf_counter()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            notes: list[str] = []
            stats = self._infer(
                ref_audio=Path(ref_audio).resolve(),
                text=text,
                output_path=output_path,
                language=(language or "ZH").upper(),
                emotion_text=emotion_text,
                duration_factor=float(duration_factor),
                ref_text=ref_text,
                progress=progress,
                notes=notes,
                verbose=verbose,
            ) or {}
            elapsed = time.perf_counter() - started

            import soundfile as sf

            info = sf.info(str(output_path))
            audio_seconds = info.frames / info.samplerate
            result = {
                "model_id": self.spec.id,
                "model_name": self.spec.name,
                "elapsed_seconds": round(elapsed, 2),
                "audio_seconds": round(audio_seconds, 2),
                "sample_rate": info.samplerate,
                "rtf": round(elapsed / max(audio_seconds, 1e-6), 3),
                "parameters_ignored": notes,
            }
            result.update(stats)
            return result

    def extract_voice(
        self,
        ref_audio: Path,
        language: str = "ZH",
        ref_text: str = "",
        progress=None,
    ) -> dict:
        """用一次极短合成触发说话人条件编码，为后续合成缓存音色特征。"""
        started = time.perf_counter()
        tmp_out = self.spec.model_dir.parent / "outputs" / "_warmup.wav" if self.spec.model_dir else None
        tmp_out = Path(tmp_out) if tmp_out else Path("outputs/_warmup.wav")
        tmp_out.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.synthesize(
                ref_audio=ref_audio,
                text="你好。" if language.upper() in {"ZH", "JA"} else "Hello.",
                output_path=tmp_out,
                language=language,
                ref_text=ref_text,
                progress=progress,
            )
        finally:
            try:
                tmp_out.unlink()
            except OSError:
                pass
        return {"extract_seconds": round(time.perf_counter() - started, 2)}

    def has_voice_cached(self, ref_audio: Path) -> bool:
        """当前进程内是否已缓存该参考音频的音色条件。"""
        prompt = self._cached_prompt()
        if not prompt:
            return False
        try:
            return Path(str(prompt)).resolve() == Path(ref_audio).resolve()
        except OSError:
            return False

    # ---------- 子类实现 ----------
    def _load_model(self) -> None:
        raise NotImplementedError

    def _infer(self, **kwargs) -> dict:
        raise NotImplementedError

    def _cached_prompt(self) -> str | None:
        return None


def install_progress_hook(model, callback) -> None:
    """把统一的进度回调挂到上游模型的 gr_progress 上（IndexTTS 系列）。"""
    if model is not None and callback is not None:
        try:
            model.gr_progress = callback
        except Exception:  # noqa: BLE001
            pass
