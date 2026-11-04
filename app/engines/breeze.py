"""Breeze TTS 2 适配器（BreezeBlue）。

权重是 Linux + 12 GB 显存起，本机跑不动，因此该模型以「远程推理服务」方式接入：
对端跑官方 `python -m breeze_infer.api <模型目录>`，本适配器按官方 HTTP 契约调用——

    POST {endpoint}/v1/audio/speech   (multipart/form-data)
      text, ref_audio(文件), ref_text, instruction, cfg_scale, seed
    → 24 kHz 单声道 16-bit signed little-endian PCM（也兼容 audio/wav）

参考音频随每次请求内联上传，因此音色条件天然按「参考音频」对齐，
与其他引擎的缓存语义一致。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import requests
import soundfile as sf

from ..models import ModelSpec
from .base import TTSEngineBase

SAMPLE_RATE = 24000
PROBE_TTL = 15.0          # 服务存活探测缓存（秒），避免每次刷新列表都发请求
PROBE_TIMEOUT = 3
CONNECT_TIMEOUT = 6
READ_TIMEOUT = 600


class BreezeAPIError(RuntimeError):
    """远程服务返回的非 200 响应或不可识别的音频负载。"""


class BreezeTTSEngine(TTSEngineBase):
    def __init__(self, spec: ModelSpec):
        super().__init__(spec)
        self._session = requests.Session()
        # 推理服务通常在本机/内网，走系统代理会被改写成绝对 URI 而失败
        self._session.trust_env = False
        self._probed_at = 0.0
        self._online = False
        self._probe_detail = "尚未探测"
        self._last_ref: str | None = None

    # ---------- 服务地址与存活探测 ----------
    @property
    def endpoint(self) -> str:
        return self.spec.resolved_endpoint().rstrip("/")

    def _probe_urls(self) -> list[str]:
        base = self.endpoint
        return [f"{base}/v1/models", f"{base}/health", f"{base}/"]

    def probe(self, force: bool = False) -> dict:
        """探测服务是否可达。任何 HTTP 响应都算「可达」，连接失败才算离线。"""
        now = time.time()
        if not force and now - self._probed_at < PROBE_TTL:
            return {"online": self._online, "detail": self._probe_detail, "endpoint": self.endpoint}

        if not self.endpoint:
            self._online = False
            self._probe_detail = "未配置服务地址"
            self._probed_at = now
            return {"online": False, "detail": self._probe_detail, "endpoint": ""}

        detail = ""
        online = False
        for url in self._probe_urls():
            try:
                resp = self._session.get(url, timeout=PROBE_TIMEOUT, allow_redirects=True)
            except requests.ConnectionError:
                detail = "连接被拒绝，服务未启动或地址不可达"
                continue
            except requests.RequestException as exc:
                detail = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code < 500:
                online = True
                detail = f"HTTP {resp.status_code} @ {url}"
                break
            detail = f"HTTP {resp.status_code} @ {url}"

        self._online = online
        self._probe_detail = detail
        self._probed_at = now
        return {"online": online, "detail": detail, "endpoint": self.endpoint}

    @property
    def online(self) -> bool:
        return self.probe()["online"]

    # ---------- 生命周期 ----------
    def _load_model(self) -> None:
        info = self.probe(force=True)
        if not info["online"]:
            raise RuntimeError(
                f"无法连接 Breeze TTS 服务（{self.endpoint or '未配置地址'}）：{info['detail']}。"
                "请先在可用的 Linux + CUDA 机器（或云 GPU）上启动官方服务"
                "（python -m breeze_infer.api <模型目录> --host 0.0.0.0 --port 7860），"
                "再用 BREEZE_TTS_ENDPOINT 指向该地址。"
            )
        self._model = {"endpoint": self.endpoint}

    def unload(self) -> None:
        # 远程模型不占本地显存，仅解除绑定
        with self._lock:
            self._model = None
            self._last_ref = None
            self.load_seconds = 0.0

    # ---------- 推理 ----------
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
        if abs(float(duration_factor) - 1.0) > 1e-3:
            notes.append(f"语速因子 {float(duration_factor):.2f}× 已忽略：Breeze 接口不提供语速参数")
        if language and language.upper() not in {"ZH", "EN"}:
            notes.append(f"语种 {language} 已忽略：Breeze TTS 2 仅支持中英，将按文本语言自动朗读")

        instruction = emotion_text.strip()
        # 官方建议：带指令时把 cfg_scale 提到 4 以加强指令遵循，纯克隆用 1
        cfg_scale = 4.0 if instruction else 1.0

        if progress:
            progress(0.35, "提交到 Breeze 服务")

        data = {
            "text": text,
            "ref_text": (ref_text or "").strip(),
            "cfg_scale": f"{cfg_scale:g}",
            "seed": "42",
        }
        if instruction:
            data["instruction"] = instruction

        url = f"{self.endpoint}/v1/audio/speech"
        try:
            with open(ref_audio, "rb") as fh:
                files = {"ref_audio": (Path(ref_audio).name, fh, "audio/wav")}
                resp = self._session.post(
                    url, data=data, files=files,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True,
                )
        except requests.RequestException as exc:
            raise BreezeAPIError(f"请求 Breeze 服务失败：{exc}") from exc

        with resp:
            if resp.status_code != 200:
                raise BreezeAPIError(self._describe_http_error(resp))

            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" in ctype:
                raise BreezeAPIError(self._describe_http_error(resp, success_status=True))

            if progress:
                progress(0.7, "接收音频流")

            sample_rate = self._sample_rate_from_headers(resp, ctype)
            raw = resp.content
            if not raw:
                raise BreezeAPIError("Breeze 服务返回了空音频，请检查参考音频与转写文本是否匹配")

            if "wav" in ctype:
                output_path.write_bytes(raw)
            elif "mpeg" in ctype or "mp3" in ctype:
                raise BreezeAPIError("Breeze 服务返回了 MP3，本应用需要 PCM 或 WAV 输出")
            else:
                # 官方默认输出：24 kHz 单声道 s16le PCM
                pcm = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
                sf.write(str(output_path), pcm, sample_rate, subtype="PCM_16")

        self._last_ref = str(ref_audio)
        if verbose:
            print(f"[breeze] {self.endpoint} -> {output_path} ({sample_rate} Hz)")

        return {
            "transport": "remote",
            "endpoint": self.endpoint,
            "cfg_scale": cfg_scale,
            "instruction_applied": bool(instruction),
            "ref_text_chars": len(data["ref_text"]),
        }

    # ---------- 辅助 ----------
    @staticmethod
    def _sample_rate_from_headers(resp: requests.Response, ctype: str) -> int:
        for key in ("x-sample-rate", "x-audio-sample-rate"):
            value = resp.headers.get(key)
            if value and value.isdigit():
                return int(value)
        if "wav" in ctype:
            try:
                import io

                info = sf.info(io.BytesIO(resp.content))
                return int(info.samplerate)
            except Exception:  # noqa: BLE001
                return SAMPLE_RATE
        return SAMPLE_RATE

    @staticmethod
    def _describe_http_error(resp: requests.Response, success_status: bool = False) -> str:
        body = ""
        try:
            body = resp.text.strip()[:400]
        except Exception:  # noqa: BLE001
            pass
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                body = str(
                    payload.get("detail") or payload.get("error") or payload.get("message") or body
                )
        except Exception:  # noqa: BLE001
            pass
        prefix = "Breeze 服务返回 JSON 而非音频" if success_status else f"Breeze 服务返回 HTTP {resp.status_code}"
        return f"{prefix}：{body or '无响应体'}"

    def _cached_prompt(self) -> str | None:
        return self._last_ref

    def status(self) -> dict:
        info = super().status()
        info["transport"] = "remote"
        info["endpoint"] = self.endpoint
        info["online"] = self.probe()["online"]
        info["probe_detail"] = self._probe_detail
        return info
