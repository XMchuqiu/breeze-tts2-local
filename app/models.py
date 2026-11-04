"""模型注册表。

集中描述本应用支持的所有语音合成模型：元信息、能力矩阵、权重位置与安装状态。
新增一个模型只需在此追加一条 ModelSpec，并在 engines/ 下实现对应适配器。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "index-tts"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    version: str
    vendor: str
    description: str
    strengths: tuple[str, ...] = ()
    tradeoffs: tuple[str, ...] = ()
    languages: tuple[str, ...] = ("ZH",)
    vram_gb: float = 0.0
    params: str = ""
    license_name: str = ""
    commercial_use: str = ""
    backend: str = ""
    transport: str = "local"          # local = 本机权重；remote = 远程 HTTP 推理服务
    model_dir: Path | None = None
    required_files: tuple[str, ...] = ()
    endpoint_env: str = ""            # 远程模型的地址来源（环境变量名）
    default_endpoint: str = ""
    install_hint: str = ""
    docs_url: str = ""
    capabilities: dict = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def is_remote(self) -> bool:
        return self.transport == "remote"

    def resolved_endpoint(self) -> str:
        """远程模型的推理服务地址：环境变量优先，其次注册表里的默认值。"""
        if not self.is_remote:
            return ""
        return (os.environ.get(self.endpoint_env, "") or self.default_endpoint or "").strip()

    # 权重是否齐备（决定该模型能否被选中）
    @property
    def installed(self) -> bool:
        if self.is_remote:
            return bool(self.resolved_endpoint())
        if self.model_dir is None or not self.required_files:
            return False
        return all((self.model_dir / name).exists() for name in self.required_files)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "vendor": self.vendor,
            "description": self.description,
            "strengths": list(self.strengths),
            "tradeoffs": list(self.tradeoffs),
            "languages": list(self.languages),
            "vram_gb": self.vram_gb,
            "params": self.params,
            "license": self.license_name,
            "commercial_use": self.commercial_use,
            "backend": self.backend,
            "transport": self.transport,
            "endpoint": self.resolved_endpoint(),
            "installed": self.installed,
            "capabilities": dict(self.capabilities),
            "install_hint": self.install_hint,
            "docs_url": self.docs_url,
            "notes": list(self.notes),
        }


MODEL_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="indextts-2.5",
        name="IndexTTS 2.5",
        version="2.5",
        vendor="bilibili · IndexTeam",
        description="旗舰模型。中英日西阿五语种零样本克隆，支持文本情感提示与语速因子，"
                    "中文自然度与韵律表现最好，首次编码后可缓存音色条件。",
        strengths=("中文自然度最高", "文本情感提示（需 QwenEmotion 权重）", "语速因子 0.5–2.0×", "五语种"),
        tradeoffs=("显存约 5.1 GB", "情感提示默认关闭（额外 1.2 GB 显存）"),
        languages=("ZH", "EN", "JA", "ES", "AR"),
        vram_gb=5.1,
        params="GPT 主干 + S2Mel DiT + BigVGAN",
        license_name="bilibili 自定义协议（可自托管，商用附条件）",
        commercial_use="附条件允许",
        backend="indextts2_5",
        model_dir=VENDOR / "checkpoints",
        required_files=("config.yaml", "gpt.pth", "s2mel.pth", "codec.pth"),
        capabilities={
            "emotion_text": True,
            "duration_factor": True,
            "inline_events": True,
            "ref_text_required": False,
            "languages": ["ZH", "EN", "JA", "ES", "AR"],
        },
        docs_url="https://github.com/index-tts/index-tts",
    ),
    ModelSpec(
        id="indextts-1.5",
        name="IndexTTS 1.5",
        version="1.5",
        vendor="bilibili · IndexTeam",
        description="轻量版本。显存占用约为 2.5 的一半、模型加载更快，仅中英双语；"
                    "不支持情感提示与语速因子，长句韵律略平。",
        strengths=("显存约 2.4 GB", "加载与推理更快", "中英双语稳定"),
        tradeoffs=("无情感提示", "无独立语速控制（可用标点/文本控制停顿）", "不支持日/西/阿"),
        languages=("ZH", "EN"),
        vram_gb=2.4,
        params="GPT 主干 + BigVGAN 声码器",
        license_name="bilibili 自定义协议（可自托管，商用附条件）",
        commercial_use="附条件允许",
        backend="indextts1_5",
        model_dir=ROOT / "checkpoints-1.5",
        required_files=("config.yaml", "gpt.pth", "bigvgan_generator.pth"),
        capabilities={
            "emotion_text": False,
            "duration_factor": False,
            "inline_events": False,
            "ref_text_required": False,
            "languages": ["ZH", "EN"],
        },
        docs_url="https://github.com/index-tts/index-tts",
    ),
    ModelSpec(
        id="f5-tts",
        name="F5-TTS v1 Base",
        version="v1",
        vendor="SWivid · 开源社区",
        description="MIT 协议的非自回归流匹配模型。许可最宽松、可自由商用，长文本稳定；"
                    "需要参考音频的准确转写文本，无情感控制，韵律起伏偏平。",
        strengths=("MIT 许可，商用无附加条款", "长文本稳定", "约 3 GB 显存"),
        tradeoffs=("依赖参考文本（本应用自动用 ASR 补齐）", "无情感/事件控制", "韵律较平"),
        languages=("ZH", "EN"),
        vram_gb=3.0,
        params="0.34B 非自回归 Flow Matching + Vocos",
        license_name="MIT",
        commercial_use="允许",
        backend="f5tts",
        model_dir=ROOT / "models" / "f5tts",
        required_files=("model_1250000.safetensors", "vocab.txt"),
        capabilities={
            "emotion_text": False,
            "duration_factor": True,   # 映射到 speed 参数
            "inline_events": False,
            "ref_text_required": True,
            "languages": ["ZH", "EN"],
        },
        docs_url="https://github.com/SWivid/F5-TTS",
    ),
    ModelSpec(
        id="breeze-tts-2",
        name="Breeze TTS 2",
        version="2",
        vendor="BreezeBlue",
        description="面向实时交互的开源权重模型。音色克隆需要参考音频及其准确文字稿，"
                    "支持自然语言指令控制情绪与语气、句内语音事件（如 [叹气]），首包延迟低。"
                    "权重需 12 GB 以上显存且仅支持 Linux，本机以远程推理服务方式接入。",
        strengths=("指令控制情绪与语气", "句内语音事件", "中英双语", "低首包延迟"),
        tradeoffs=("本机显存不足，需远程 Breeze 服务", "克隆必须提供准确转写", "自托管输出仅限非商用"),
        languages=("ZH", "EN"),
        vram_gb=7.7,          # 服务端 eager 推理占用，仅作参考
        params="3B dense Transformer + Qwen3-TTS 音频分词器",
        license_name="权重：BreezeBlue Research and Non-Commercial License；托管 API 需订阅方可商用",
        commercial_use="仅托管可商用",
        backend="breeze_api",
        transport="remote",
        endpoint_env="BREEZE_TTS_ENDPOINT",
        default_endpoint="http://127.0.0.1:7860",
        install_hint="启动官方推理服务后自动可用："
                     "python -m breeze_infer.api <模型目录> --host 0.0.0.0 --port 7860；"
                     "地址不是本机时用 BREEZE_TTS_ENDPOINT 指定，例如 http://10.0.0.5:7860",
        capabilities={
            "emotion_text": True,       # 映射到官方 instruction（Voice Direction）
            "duration_factor": False,   # 官方接口无语速参数
            "inline_events": True,
            "ref_text_required": True,
            "languages": ["ZH", "EN"],
        },
        notes=("情绪提示通过 instruction 字段下发，会自动把 cfg_scale 提到 4.0 以加强指令遵循。",
               "语音事件中英文分别用 [叹气] / (sigh) 这类括号标记，直接写在文本里。"),
        docs_url="https://github.com/breezeblue-ai/breeze-tts",
    ),
)

SPEC_BY_ID = {spec.id: spec for spec in MODEL_SPECS}
DEFAULT_MODEL_ID = MODEL_SPECS[0].id


def get_spec(model_id: str) -> ModelSpec:
    spec = SPEC_BY_ID.get(model_id)
    if spec is None:
        raise KeyError(f"未知模型：{model_id}")
    return spec


def list_specs() -> list[ModelSpec]:
    return list(MODEL_SPECS)
