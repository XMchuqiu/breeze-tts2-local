"""向后兼容垫片：早期的单模型入口。

当前实现已迁移到 app/engines/ 与 app/engine_manager.py，
此模块仅保留别名，避免旧脚本 import 失败。
"""

from __future__ import annotations

from .engine_manager import EngineManager
from .engines.indextts25 import IndexTTS25Engine
from .models import DEFAULT_MODEL_ID, get_spec

TTSEngine = IndexTTS25Engine

__all__ = ["TTSEngine", "IndexTTS25Engine", "EngineManager", "DEFAULT_MODEL_ID", "get_spec"]
