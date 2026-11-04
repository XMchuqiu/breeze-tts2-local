"""引擎管理器：多模型注册、切换、显存回收与统一调用入口。

8 GB 显存只容得下一份模型，因此切换采用「先卸载旧模型 → 回收显存 → 懒加载新模型」
的严格串行策略，并把用户选择持久化到 config.json，重启后保持上次选择。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .engines.base import TTSEngineBase, gpu_snapshot, release_vram
from .engines.breeze import BreezeTTSEngine
from .engines.f5tts import F5TTSEngine
from .engines.indextts15 import IndexTTS15Engine
from .engines.indextts25 import IndexTTS25Engine
from .models import DEFAULT_MODEL_ID, SPEC_BY_ID, ModelSpec, get_spec, list_specs

BACKENDS = {
    "indextts2_5": IndexTTS25Engine,
    "indextts1_5": IndexTTS15Engine,
    "f5tts": F5TTSEngine,
    "breeze_api": BreezeTTSEngine,
}


class EngineManager:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self._lock = threading.RLock()
        self._engines: dict[str, TTSEngineBase] = {}
        self._active_id = self._restore_active()

    # ---------- 持久化 ----------
    def _restore_active(self) -> str:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            model_id = data.get("active_model") or DEFAULT_MODEL_ID
        except Exception:  # noqa: BLE001
            model_id = DEFAULT_MODEL_ID
        spec = SPEC_BY_ID.get(model_id)
        if spec is None or not spec.installed:
            # 回退到第一个可用模型，避免指向缺失权重导致启动失败
            for candidate in list_specs():
                if candidate.installed:
                    return candidate.id
            return DEFAULT_MODEL_ID
        return model_id

    def _persist(self) -> None:
        payload = {"active_model": self._active_id}
        try:
            self.config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ---------- 查询 ----------
    @property
    def active_id(self) -> str:
        return self._active_id

    @property
    def active_spec(self) -> ModelSpec:
        return get_spec(self._active_id)

    def engine(self, model_id: str | None = None) -> TTSEngineBase:
        spec = get_spec(model_id or self._active_id)
        with self._lock:
            engine = self._engines.get(spec.id)
            if engine is None:
                backend = BACKENDS.get(spec.backend)
                if backend is None:
                    raise RuntimeError(f"模型 {spec.name} 缺少推理后端实现：{spec.backend}")
                engine = backend(spec)
                self._engines[spec.id] = engine
            return engine

    def models(self) -> list[dict]:
        items = []
        for spec in list_specs():
            item = spec.to_dict()
            engine = self._engines.get(spec.id)
            item["active"] = spec.id == self._active_id
            item["loaded"] = bool(engine and engine.loaded)
            item["load_seconds"] = round(engine.load_seconds, 1) if engine else 0.0
            item["error"] = engine.load_error if engine else None
            # 远程模型额外上报服务可达性，界面据此区分「已安装」与「未连接」
            item["online"] = None
            item["available"] = item["installed"]
            if spec.is_remote and spec.installed:
                probe = self.engine(spec.id).probe()
                item["online"] = probe["online"]
                item["probe_detail"] = probe["detail"]
                item["available"] = probe["online"]
            items.append(item)
        return items

    def status(self) -> dict:
        engine = self.engine()
        info = engine.status()
        info.update(
            {
                "active_model_id": self._active_id,
                "active_model": self.active_spec.to_dict(),
                "models": self.models(),
                "loaded_models": [mid for mid, eng in self._engines.items() if eng.loaded],
            }
        )
        return info

    def gpu(self) -> dict:
        return gpu_snapshot()

    # ---------- 切换与生命周期 ----------
    def switch(self, model_id: str, load: bool = True, progress=None) -> dict:
        """切换当前模型：卸载所有已加载模型 → 回收显存 → 加载目标模型。"""
        spec = get_spec(model_id)
        if not spec.installed:
            raise RuntimeError(f"{spec.name} 的权重未安装，无法切换。{spec.install_hint}".strip())
        if spec.is_remote:
            probe = self.engine(spec.id).probe(force=True)
            if not probe["online"]:
                raise RuntimeError(
                    f"无法连接 {spec.name} 服务（{spec.resolved_endpoint() or '未配置地址'}）："
                    f"{probe['detail']}。{spec.install_hint}".strip()
                )

        with self._lock:
            previous = self._active_id
            if previous == spec.id and (not load or self.engine(spec.id).loaded):
                return {
                    "changed": False,
                    "from": previous,
                    "to": spec.id,
                    "model": spec.to_dict(),
                    "load_seconds": round(self.engine(spec.id).load_seconds, 1),
                }

            if progress:
                progress(0.1, "卸载当前模型，回收显存")
            self._unload_loaded()
            release_vram()

            self._active_id = spec.id
            self._persist()

            load_seconds = 0.0
            if load:
                if progress:
                    progress(0.35, f"加载 {spec.name} 权重")
                self.engine(spec.id).load()
                load_seconds = round(self.engine(spec.id).load_seconds, 1)
            if progress:
                progress(0.95, "模型就绪")

            return {
                "changed": True,
                "from": previous,
                "to": spec.id,
                "model": spec.to_dict(),
                "load_seconds": load_seconds,
            }

    def _unload_loaded(self) -> None:
        for engine in self._engines.values():
            if engine.loaded:
                engine.unload()

    def unload(self) -> dict:
        with self._lock:
            self._unload_loaded()
            release_vram()
            return {"ok": True, "active_model": self._active_id}

    def load_active(self) -> dict:
        with self._lock:
            engine = self.engine()
            engine.load()
            return {"model_id": engine.model_id, "load_seconds": round(engine.load_seconds, 1)}

    # ---------- 推理 ----------
    def synthesize(self, ref_audio: Path, text: str, output_path: Path, model_id: str | None = None, **kwargs) -> dict:
        engine = self.engine(model_id)
        return engine.synthesize(ref_audio=ref_audio, text=text, output_path=output_path, **kwargs)

    def extract_voice(self, ref_audio: Path, model_id: str | None = None, **kwargs) -> dict:
        engine = self.engine(model_id)
        return engine.extract_voice(ref_audio=ref_audio, **kwargs)

    def is_voice_cached(self, ref_audio: Path, model_id: str | None = None) -> bool:
        return self.engine(model_id).has_voice_cached(ref_audio)

    def capabilities(self, model_id: str | None = None) -> dict:
        return self.engine(model_id).capabilities
