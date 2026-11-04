"""Breeze Voice Studio 后端服务。

职责：
- 多模型注册与切换（IndexTTS 2.5 / IndexTTS 1.5 / F5-TTS）
- 音色库的增删改查与参考音频托管
- 参考音频自动转写（ASR）与音色条件提取（克隆）
- 文本合成任务队列（串行执行，避免显存竞争）
- 为桌面外壳提供静态页面
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import audio_library as library
from . import runtime
from .asr import Transcriber
from .engine_manager import EngineManager
from .models import DEFAULT_MODEL_ID, get_spec
from .voice_store import VoiceError, VoiceStore

ROOT = Path(__file__).resolve().parent.parent
VOICES_DIR = ROOT / "voices"
OUTPUTS_DIR = ROOT / "outputs"
WEB_DIR = ROOT / "app" / "web"

OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
VOICES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Breeze Voice Studio", version="1.1.0")

store = VoiceStore(VOICES_DIR)
manager = EngineManager(ROOT / "config.json")
transcriber = Transcriber()

# ---------------- 任务队列 ----------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_queue_lock = threading.Lock()      # 串行化所有 GPU 任务（推理与模型切换共用）


def _new_job(kind: str, title: str) -> str:
    job_id = f"j_{uuid.uuid4().hex[:10]}"
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": kind,
            "title": title,
            "status": "queued",
            "progress": 0.0,
            "stage": "排队中",
            "created_at": time.time(),
            "result": None,
            "error": None,
        }
    return job_id


def _update_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _run_job(job_id: str, fn) -> None:
    def runner():
        with _queue_lock:
            _update_job(job_id, status="running", stage="开始执行", progress=0.02)
            try:
                result = fn()
                _update_job(job_id, status="done", progress=1.0, stage="已完成", result=result)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                _update_job(job_id, status="error", stage="失败", error=str(exc))

    threading.Thread(target=runner, daemon=True).start()


def _progress_hook(job_id: str):
    def hook(value, desc=""):
        try:
            pct = float(value)
        except (TypeError, ValueError):
            pct = 0.0
        _update_job(job_id, progress=max(0.02, min(0.99, pct)), stage=desc or "合成中")

    return hook


def _scaled_progress(job_id: str, offset: float, scale: float, fallback: str):
    """把子阶段进度映射到任务总进度区间内。"""

    def hook(value, desc=""):
        try:
            pct = float(value)
        except (TypeError, ValueError):
            pct = 0.0
        _update_job(
            job_id,
            progress=max(0.02, min(0.99, offset + scale * pct)),
            stage=desc or fallback,
        )

    return hook


# ---------------- 音色与模型的关联状态 ----------------
def _model_unavailable(spec) -> str:
    """统一的「模型不可用」提示：本地看权重，远程看服务地址。"""
    if getattr(spec, "is_remote", False):
        return (
            f"{spec.name} 未配置推理服务地址：请设置 {spec.endpoint_env}。{spec.install_hint}"
        ).strip()
    return f"{spec.name} 的权重未安装，无法使用。{spec.install_hint}".strip()


def _model_state(voice) -> dict:
    return dict((voice.extra or {}).get("model_state") or {})


def _source_models(voice) -> list[dict]:
    """音色的「来源模型」：已提取就绪的模型列表，含名称与版本。

    多模型架构下，同一段参考音频会为每个模型各自提取声纹特征，
    ``extra.model_state`` 里 ``status == "ready"`` 的条目就是该音色实际出自
    的模型。按提取时间升序排列，最早提取者标记为 ``primary``（初始来源）。
    """
    entries: list[dict] = []
    for model_id, st in _model_state(voice).items():
        if st.get("status") != "ready":
            continue
        try:
            spec = get_spec(model_id)
        except KeyError:
            continue
        entries.append(
            {
                "model_id": model_id,
                "name": spec.name,
                "version": spec.version,
                "vendor": spec.vendor,
                "extracted_at": st.get("extracted_at") or 0.0,
            }
        )
    entries.sort(key=lambda e: e["extracted_at"])
    for i, entry in enumerate(entries):
        entry["primary"] = i == 0
    return entries


def _voice_view(voice, active_model_id: str) -> dict:
    data = voice.to_dict()
    state = _model_state(voice)
    ready_map = {mid: st.get("status") == "ready" for mid, st in state.items()}
    data["model_ready"] = ready_map
    data["active_model_ready"] = ready_map.get(active_model_id, False)
    data["active_model_status"] = (
        "ready" if ready_map.get(active_model_id)
        else ("processing" if (voice.extra or {}).get("status") == "processing" else "pending")
    )
    data["source_models"] = _source_models(voice)
    return data


def _mark_model_ready(voice_id: str, model_id: str, extract_seconds: float | None = None) -> None:
    voice = store.get(voice_id)
    state = _model_state(voice)
    entry = dict(state.get(model_id) or {})
    entry.update({"status": "ready", "extracted_at": time.time()})
    if extract_seconds is not None:
        entry["extract_seconds"] = extract_seconds
    state[model_id] = entry
    extra = dict(voice.extra or {})
    extra["model_state"] = state
    extra["status"] = "ready"
    store.update(voice_id, extra=extra)


def _mark_model_pending(voice_id: str, model_id: str) -> None:
    voice = store.get(voice_id)
    state = _model_state(voice)
    state[model_id] = {"status": "processing", "started_at": time.time()}
    extra = dict(voice.extra or {})
    extra["model_state"] = state
    extra["status"] = "processing"
    store.update(voice_id, extra=extra)


def _migrate_voice_state() -> None:
    """历史音色只有全局 status，没有按模型记录；补齐为默认模型的就绪状态。"""
    for voice in store.list():
        extra = dict(voice.extra or {})
        if extra.get("model_state"):
            continue
        if extra.get("status") == "ready":
            extra["model_state"] = {DEFAULT_MODEL_ID: {"status": "ready", "extracted_at": voice.created_at}}
            store.update(voice.id, extra=extra)


# ---------------- 基础接口 ----------------
@app.get("/api/status")
def api_status():
    engine = manager.status()
    return {
        "engine": engine,
        "models": manager.models(),
        "active_model_id": manager.active_id,
        "active_model": manager.active_spec.to_dict(),
        "asr_available": transcriber.available,
        "voice_count": len(store.list()),
    }


@app.get("/api/models")
def api_models():
    return {"models": manager.models(), "active": manager.active_id}


@app.post("/api/models/activate")
def api_activate_model(payload: dict):
    model_id = (payload.get("model_id") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="缺少 model_id")
    try:
        spec = get_spec(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not spec.installed:
        raise HTTPException(status_code=400, detail=_model_unavailable(spec))
    if model_id == manager.active_id and manager.engine().loaded:
        return {"job_id": None, "model_id": model_id, "already": True}

    job_id = _new_job("switch", f"切换模型：{spec.name}")

    def work():
        info = manager.switch(model_id, load=True, progress=_progress_hook(job_id))
        _update_job(job_id, stage=f"{spec.name} 就绪")
        return info

    _run_job(job_id, work)
    return {"job_id": job_id, "model_id": model_id, "model": spec.to_dict()}


@app.get("/api/voices")
def api_list_voices():
    active = manager.active_id
    return {"voices": [_voice_view(v, active) for v in store.list()], "active_model_id": active}


@app.get("/api/voices/{voice_id}/ref")
def api_voice_ref(voice_id: str):
    try:
        path = store.audio_path(voice_id)
    except VoiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="audio/wav", filename=f"{voice_id}.wav")


@app.post("/api/voices")
def api_create_voice(
    file: UploadFile = File(...),
    name: str = Form(""),
    language: str = Form("ZH"),
    emotion_text: str = Form(""),
    model_id: str = Form(""),
):
    suffix = Path(file.filename or "ref.wav").suffix.lower() or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        voice = store.create(
            src_path=tmp_path,
            name=name or Path(file.filename or "未命名音色").stem,
            language=language,
            transcript="",
            source_name=file.filename or "",
        )
    except VoiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    voice = store.update(voice.id, emotion_text=emotion_text, extra={"status": "processing"})
    target_model = model_id or manager.active_id
    try:
        spec = get_spec(target_model)
    except KeyError:
        spec = manager.active_spec
    job_id = _new_job("clone", f"提取音色：{voice.name}（{spec.name}）")

    ref_path = store.audio_path(voice.id)

    def work():
        _update_job(job_id, stage="识别参考音频文字", progress=0.1)
        text, detected = transcriber.transcribe(ref_path)
        lang = (detected or "").upper() or voice.language
        store.update(
            voice.id,
            transcript=text,
            language=lang if lang in set(spec.capabilities.get("languages") or ["ZH"]) else voice.language,
        )

        _update_job(job_id, stage=f"加载 {spec.name}" if not manager.engine(target_model).loaded else "提取音色特征", progress=0.25)
        info = manager.extract_voice(
            ref_path,
            model_id=target_model,
            language=voice.language,
            ref_text=text,
            progress=_progress_hook(job_id),
        )
        _mark_model_ready(voice.id, target_model, info.get("extract_seconds"))
        return {"voice_id": voice.id, "model_id": target_model, "transcript": text, **info}

    _run_job(job_id, work)
    return {"voice": _voice_view(store.get(voice.id), target_model), "job_id": job_id, "model_id": target_model}


@app.post("/api/voices/{voice_id}/extract")
def api_extract_voice(voice_id: str, payload: dict | None = None):
    """为指定模型重新提取音色特征（切换模型后按需调用）。"""
    payload = payload or {}
    model_id = (payload.get("model_id") or manager.active_id).strip()
    try:
        spec = get_spec(model_id)
        voice = store.get(voice_id)
        ref_path = store.audio_path(voice_id)
    except (KeyError, VoiceError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not spec.installed:
        raise HTTPException(status_code=400, detail=_model_unavailable(spec))

    job_id = _new_job("clone", f"提取音色：{voice.name}（{spec.name}）")

    def work():
        if not voice.transcript:
            _update_job(job_id, stage="识别参考音频文字", progress=0.1)
            text, _ = transcriber.transcribe(ref_path)
            store.update(voice_id, transcript=text)
        else:
            text = voice.transcript
        _update_job(job_id, stage=f"加载 {spec.name}" if not manager.engine(model_id).loaded else "提取音色特征", progress=0.3)
        info = manager.extract_voice(
            ref_path,
            model_id=model_id,
            language=voice.language,
            ref_text=text,
            progress=_progress_hook(job_id),
        )
        _mark_model_ready(voice_id, model_id, info.get("extract_seconds"))
        return {"voice_id": voice_id, "model_id": model_id, **info}

    _run_job(job_id, work)
    return {"job_id": job_id, "model_id": model_id}


@app.patch("/api/voices/{voice_id}")
def api_update_voice(voice_id: str, payload: dict):
    try:
        voice = store.update(
            voice_id,
            name=payload.get("name"),
            emotion_text=payload.get("emotion_text"),
            transcript=payload.get("transcript"),
        )
    except VoiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"voice": _voice_view(voice, manager.active_id)}


@app.delete("/api/voices/{voice_id}")
def api_delete_voice(voice_id: str):
    try:
        store.delete(voice_id)
    except VoiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


# ---------------- 合成 ----------------
def _write_sidecar(out_path: Path, meta: dict) -> None:
    try:
        out_path.with_suffix(".json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


@app.post("/api/synthesize")
def api_synthesize(payload: dict):
    voice_id = payload.get("voice_id", "")
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="请先输入要合成的文本")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="单次合成文本请控制在 2000 字以内")

    try:
        voice = store.get(voice_id)
        ref_path = store.audio_path(voice_id)
    except VoiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    requested_model = (payload.get("model_id") or "").strip()
    target_model = requested_model or manager.active_id
    try:
        spec = get_spec(target_model)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not spec.installed:
        raise HTTPException(status_code=400, detail=_model_unavailable(spec))

    emotion_text = (payload.get("emotion_text") or voice.emotion_text or "").strip()
    duration_factor = float(payload.get("duration_factor") or 1.0)
    duration_factor = max(0.5, min(2.0, duration_factor))
    language = payload.get("language") or voice.language or "ZH"

    out_name = f"{voice.id}_{time.strftime('%Y%m%d_%H%M%S')}.wav"
    out_path = OUTPUTS_DIR / out_name
    job_id = _new_job("synth", f"合成：{voice.name} · {spec.name}")

    def work():
        # 1) 若请求的模型与当前激活模型不同，先完成切换（会释放旧模型显存）
        if target_model != manager.active_id:
            _update_job(job_id, stage=f"切换到 {spec.name}", progress=0.04)
            manager.switch(
                target_model,
                load=True,
                progress=_scaled_progress(job_id, 0.04, 0.24, "切换模型"),
            )

        # 2) 确保当前模型已加载
        _update_job(job_id, stage=f"加载 {spec.name}" if not manager.engine(target_model).loaded else "准备推理", progress=0.3)
        manager.engine(target_model).load()

        # 2.5) 需要参考转写的模型（F5）在缺失转写时先补一次 ASR
        ref_text = voice.transcript or ""
        if spec.capabilities.get("ref_text_required") and not ref_text.strip():
            _update_job(job_id, stage="识别参考音频文字（该模型需要转写）", progress=0.33)
            ref_text, _detected = transcriber.transcribe(ref_path)
            store.update(voice_id, transcript=ref_text)

        # 3) 该模型尚未编码过此音色 → 先提取特征
        if not manager.is_voice_cached(ref_path, target_model):
            _update_job(job_id, stage="提取音色特征（该模型首次使用此音色）", progress=0.35)
            info = manager.extract_voice(
                ref_path,
                model_id=target_model,
                language=language,
                ref_text=ref_text,
                progress=_scaled_progress(job_id, 0.35, 0.1, "提取音色特征"),
            )
            _mark_model_ready(voice_id, target_model, info.get("extract_seconds"))

        _update_job(job_id, stage="合成中", progress=0.5)
        stats = manager.synthesize(
            ref_audio=ref_path,
            text=text,
            output_path=out_path,
            model_id=target_model,
            language=language,
            emotion_text=emotion_text,
            duration_factor=duration_factor,
            ref_text=ref_text,
            progress=_progress_hook(job_id),
        )
        _mark_model_ready(voice_id, target_model, None)
        _write_sidecar(
            out_path,
            {
                "voice_id": voice_id,
                "voice_name": voice.name,
                "model_id": target_model,
                "model_name": spec.name,
                "text": text,
                "created_at": time.time(),
            },
        )
        return {
            "audio_url": f"/api/outputs/{out_name}",
            "file_name": out_name,
            "voice_name": voice.name,
            "text": text,
            **stats,
        }

    _run_job(job_id, work)
    return {"job_id": job_id, "file_name": out_name, "model_id": target_model}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.get("/api/outputs/{file_name}")
def api_output(file_name: str):
    try:
        path = library.resolve_clip(OUTPUTS_DIR, file_name)
    except library.ClipError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="audio/wav", filename=path.name)


# ---------------- 合成音频库 ----------------
@app.get("/api/outputs")
def api_list_outputs(limit: int = 60, offset: int = 0, q: str = ""):
    return library.list_clips(OUTPUTS_DIR, limit=limit, offset=offset, query=q)


@app.post("/api/outputs/open")
def api_open_outputs(payload: dict | None = None):
    """在系统文件管理器中打开（或在目录缺失时先创建）存储路径。"""
    payload = payload or {}
    try:
        created = library.ensure_directory(OUTPUTS_DIR)
        library.open_in_file_manager(OUTPUTS_DIR, payload.get("file_name"))
    except library.ClipError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "path": str(OUTPUTS_DIR), "created": created}


@app.patch("/api/outputs/{file_name}")
def api_rename_output(file_name: str, payload: dict):
    new_name = (payload.get("name") or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="请输入新的文件名")
    try:
        item = library.rename_clip(OUTPUTS_DIR, file_name, new_name)
    except library.ClipError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@app.delete("/api/outputs/{file_name}")
def api_delete_output(file_name: str):
    result = library.delete_clips(OUTPUTS_DIR, [file_name])
    if not result["deleted"]:
        reason = (result["failed"] or [{}])[0].get("reason") or "文件不存在"
        raise HTTPException(status_code=404, detail=reason)
    return {"ok": True, **result}


def _batch_names(payload: dict) -> list[str]:
    names = payload.get("file_names")
    if not isinstance(names, list):
        raise HTTPException(status_code=400, detail="缺少要操作的文件列表")
    cleaned = [str(n).strip() for n in names if str(n).strip()]
    if not cleaned:
        raise HTTPException(status_code=400, detail="请先选择要操作的语音")
    if len(cleaned) > 500:
        raise HTTPException(status_code=400, detail="单次最多操作 500 个文件")
    return cleaned


@app.post("/api/outputs/batch-delete")
def api_batch_delete_outputs(payload: dict):
    return {"ok": True, **library.delete_clips(OUTPUTS_DIR, _batch_names(payload))}


@app.post("/api/outputs/batch-rename")
def api_batch_rename_outputs(payload: dict):
    prefix = (payload.get("prefix") or "").strip()
    if not prefix:
        raise HTTPException(status_code=400, detail="请输入统一前缀")
    return {"ok": True, **library.batch_rename_clips(OUTPUTS_DIR, _batch_names(payload), prefix)}


@app.get("/api/history")
def api_history(limit: int = 20):
    data = library.list_clips(OUTPUTS_DIR, limit=limit)
    return {
        "items": [
            {
                "file_name": it["file_name"],
                "audio_url": it["audio_url"],
                "created_at": it["created_at"],
                "size_kb": it["size_kb"],
                "duration_sec": it["duration_sec"],
                "model_id": it["model_id"],
                "model_name": it["model_name"],
                "text": (it["text"] or "")[:60],
            }
            for it in data["items"]
        ]
    }


# ---------------- 引擎控制 ----------------
@app.post("/api/engine/load")
def api_engine_load():
    if manager.engine().loaded:
        return {"ok": True, "already": True}
    job_id = _new_job("engine", f"加载 {manager.active_spec.name}")

    def work():
        info = manager.load_active()
        return info

    _run_job(job_id, work)
    return {"job_id": job_id}


@app.post("/api/engine/unload")
def api_engine_unload():
    manager.unload()
    return {"ok": True}


# ---------------- 桌面外壳控制 ----------------
# 网页里的「收进托盘 / 完全退出」由 desktop.py 通过 runtime 登记的回调执行；
# 用浏览器直接打开时没有宿主，这里如实返回 False，前端据此禁用按钮。
EXIT_SCOPE = (
    "本窗口进程",
    "本地后端服务（uvicorn）",
    "复用模式下由其他入口启动的后端",
    "托盘图标",
    "logs/instance.json 实例记录",
)


@app.get("/api/app/runtime")
def api_app_runtime():
    targets = {}
    describe = runtime.host("quit_targets")
    if callable(describe):
        try:
            targets = describe() or {}
        except Exception:  # noqa: BLE001 - 描述信息拿不到不影响功能
            targets = {}
    return {
        "pid": os.getpid(),
        "settings": runtime.load_settings(),
        "capabilities": runtime.capability_report(),
        "targets": targets,
        "exit_scope": list(EXIT_SCOPE),
    }


@app.put("/api/app/settings")
def api_app_settings(payload: dict | None = None):
    settings = runtime.update_settings(payload or {})
    apply_settings = runtime.host("apply_settings")
    if callable(apply_settings):
        try:
            apply_settings()
        except Exception:  # noqa: BLE001 - 设置已落盘，应用失败只影响本次
            pass
    return {"settings": settings, "capabilities": runtime.capability_report()}


@app.get("/theme-boot.js")
def theme_boot():
    """首屏同步注入主题，避免浅色/深色之间闪一下。

    放在 <head> 最前面、由服务端直接生成：脚本执行时 body 还没渲染，
    data-theme 已经就位，CSS 变量一次解析到位，不存在"先亮后暗"的闪烁。
    不用 localStorage 是因为两套入口（bat / exe）的 WebView2 数据目录可能不同，
    服务端 ui-settings.json 才是唯一可信来源。
    """
    theme = runtime.load_settings().get("theme", "system")
    if theme not in runtime.THEMES:
        theme = "system"
    script = (
        "(function(){"
        f"var p={json.dumps(theme)};"
        "var m=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)');"
        "var r=p==='system'?(m&&m.matches?'dark':'light'):p;"
        "var e=document.documentElement;"
        "e.setAttribute('data-theme',r);"
        "e.setAttribute('data-theme-pref',p);"
        "})();"
    )
    return Response(
        script,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.post("/api/app/hide")
def api_app_hide():
    hide = runtime.host("hide_to_tray")
    if not callable(hide):
        return {"ok": False, "reason": "当前不在桌面窗口中运行，浏览器标签页无法隐藏"}
    try:
        return hide() or {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}


@app.post("/api/app/exit")
def api_app_exit():
    full_exit = runtime.host("full_exit")
    if not callable(full_exit):
        return {"ok": False, "reason": "当前不在桌面窗口中运行，直接关闭浏览器标签页即可"}
    # 延迟 0.5 秒：先把 HTTP 响应发回页面，再隐藏窗口/结束进程，避免前端报网络错误
    threading.Timer(0.5, lambda: full_exit("界面按钮")).start()
    return {"ok": True, "scope": list(EXIT_SCOPE)}


# ---------------- 静态页面 ----------------
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def _preload() -> None:
    if os.environ.get("BREEZE_PRELOAD", "1") == "0":
        return
    try:
        manager.load_active()
        print(f"[engine] 模型预加载完成：{manager.active_spec.name}")
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] 预加载失败：{exc}")
    try:
        transcriber.warmup()
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
def _on_startup() -> None:
    _migrate_voice_state()
    threading.Thread(target=_preload, daemon=True).start()
