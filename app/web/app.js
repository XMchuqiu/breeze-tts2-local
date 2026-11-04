const $ = (sel) => document.querySelector(sel);

const state = {
  voices: [],
  selectedId: null,
  pendingFile: null,
  busy: false,
  switching: false,
  models: [],
  activeModelId: null,
  audio: new Audio(),
};

/* ---------------- 基础工具 ---------------- */
async function api(path, options = {}) {
  const res = await fetch(path, options);
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json() : null;
  if (!res.ok) {
    throw new Error((data && (data.detail || data.error)) || `请求失败(${res.status})`);
  }
  return data;
}

function toast(message, type = "") {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = `toast show ${type}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => (el.className = "toast"), 3200);
}

function fmtDuration(sec) {
  if (!sec && sec !== 0) return "—";
  return `${sec.toFixed(1)}s`;
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const LANG_LABEL = { ZH: "中文", EN: "英语", JA: "日语", ES: "西语", AR: "阿语" };

function activeSpec() {
  return state.models.find((m) => m.id === state.activeModelId) || null;
}

/* ---------------- 状态与模型 ---------------- */
async function refreshStatus() {
  try {
    const data = await api("/api/status");
    const e = data.engine || {};
    state.models = data.models || [];
    state.activeModelId = data.active_model_id || (state.models[0] && state.models[0].id) || null;

    const dot = $("#engineDot");
    const text = $("#engineText");
    if (e.loaded) {
      dot.className = "dot ready";
      text.textContent = `就绪 · ${e.load_seconds}s 加载`;
    } else if (e.loading) {
      dot.className = "dot loading";
      text.textContent = "模型加载中…";
    } else if (e.transport === "remote" && e.online === false) {
      dot.className = "dot error";
      text.textContent = "远程服务未连接";
    } else if (e.weights_present === false) {
      dot.className = "dot error";
      text.textContent = "缺少模型权重";
    } else {
      dot.className = "dot";
      text.textContent = "模型待加载";
    }
    if (e.vram_total_gb) {
      $("#vramPill").textContent = `显存 ${e.vram_used_gb ?? "—"} / ${e.vram_total_gb} GB`;
    }
    if (e.error) toast(`引擎异常：${e.error}`, "error");

    renderModels();
    applyModelCapabilities();
  } catch (err) {
    $("#engineDot").className = "dot error";
    $("#engineText").textContent = "服务未连接";
  }
}

function modelStateLabel(m) {
  if (m.transport === "remote" && m.installed && m.online === false) {
    return { cls: "off", text: m.active ? "使用中 · 未连接" : "未连接" };
  }
  if (m.active) return { cls: "on", text: m.loaded ? "使用中 · 已加载" : "使用中" };
  if (!m.installed) return { cls: "off", text: m.transport === "remote" ? "未配置" : "未安装" };
  return { cls: "", text: m.loaded ? "已加载" : "已安装" };
}

function modelAvailable(m) {
  return m.available !== undefined ? !!m.available : !!m.installed;
}

function modelHint(m) {
  if (m.transport === "remote") {
    if (!m.installed) return m.install_hint || "未配置服务地址";
    if (m.online === false) return `无法连接 ${m.endpoint || "服务地址"}，请确认 Breeze 服务已启动`;
    return "点击切换到该模型（远程服务）";
  }
  return m.installed ? "点击切换到该模型" : m.install_hint || "权重未安装";
}

function renderModels() {
  const active = activeSpec();
  $("#modelBtnName").textContent = active ? active.name : "未选择";
  if (!state.models.length) {
    $("#modelList").innerHTML = '<div class="empty-hint">未获取到模型列表</div>';
    return;
  }
  $("#modelList").innerHTML = state.models
    .map((m) => {
      const caps = m.capabilities || {};
      const st = modelStateLabel(m);
      const ok = modelAvailable(m);
      const langs = (m.languages || []).map((l) => LANG_LABEL[l] || l).join(" / ");
      const isRemote = m.transport === "remote";
      const headChip = isRemote
        ? `<span class="chip">远程服务 · 服务端 ~${m.vram_gb ? m.vram_gb.toFixed(1) : "?"} GB</span>`
        : `<span class="chip">显存 ~${m.vram_gb ? m.vram_gb.toFixed(1) : "?"} GB</span>`;
      const chips = [
        headChip,
        `<span class="chip">${langs || "—"}</span>`,
        `<span class="chip ${caps.emotion_text ? "ok" : ""}">情感 ${caps.emotion_text ? "✓" : "—"}</span>`,
        `<span class="chip ${caps.duration_factor ? "ok" : ""}">语速 ${caps.duration_factor ? "✓" : "—"}</span>`,
        `<span class="chip ${m.commercial_use === "允许" ? "ok" : "warn"}">${m.commercial_use || "—"}商用</span>`,
      ].join("");
      const strengths = (m.strengths || []).map((s) => `<span class="chip ok">${escapeHtml(s)}</span>`).join("");
      const endpointNote = isRemote && m.endpoint
        ? `<div class="mc-note">服务地址：${escapeHtml(m.endpoint)}${m.online === false ? "（未连接）" : ""}</div>`
        : "";
      return `
        <div class="model-card ${m.active ? "active" : ""} ${ok ? "" : "disabled"}"
             data-model="${m.id}" data-available="${ok}" title="${escapeHtml(modelHint(m))}">
          <div class="mc-top">
            <span class="mc-name">${escapeHtml(m.name)}</span>
            <span class="mc-ver">${escapeHtml(m.vendor || "")}</span>
            <span class="mc-state ${st.cls}">${st.text}</span>
          </div>
          <div class="mc-desc">${escapeHtml(m.description)}</div>
          <div class="mc-chips">${chips}${strengths}</div>
          ${endpointNote}
          ${m.license ? `<div class="mc-note">许可：${escapeHtml(m.license)}</div>` : ""}
          ${ok ? "" : `<div class="mc-note">${escapeHtml(modelHint(m))}</div>`}
        </div>`;
    })
    .join("");
}

function applyModelCapabilities() {
  const spec = activeSpec();
  // 模型列表尚未返回时不做任何判定，避免误禁用输入控件
  if (!spec) return;
  const caps = (spec && spec.capabilities) || {};
  const emotionOk = !!caps.emotion_text;
  const speedOk = !!caps.duration_factor;

  $("#emotionText").disabled = !emotionOk;
  $("#emotionField").classList.toggle("unsupported", !emotionOk);
  $("#emotionCap").textContent = emotionOk ? "" : "该模型不支持";

  $("#speed").disabled = !speedOk;
  $("#speedField").classList.toggle("unsupported", !speedOk);
  $("#speedCap").textContent = speedOk ? "" : "该模型不支持";

  const notes = [];
  if (!emotionOk) notes.push("情感提示");
  if (!speedOk) notes.push("语速");
  const note = $("#capNote");
  if (notes.length) {
    note.hidden = false;
    note.textContent =
      `${spec.name} 不支持${notes.map((n) => `「${n}」`).join("、")}调节：参数仍会保留，切换到支持的模型后自动生效。` +
      (caps.ref_text_required ? " 该模型需要参考音频转写，已由音色库的自动识别结果补齐。" : "");
  } else if (caps.ref_text_required) {
    note.hidden = false;
    note.textContent = "该模型需要参考音频转写，已由音色库的自动识别结果补齐。";
  } else {
    note.hidden = true;
    note.textContent = "";
  }

  // 远程模型：情绪提示走官方 instruction 字段，语义与本地情感向量不同，需显式说明
  if (spec && spec.transport === "remote" && caps.emotion_text) {
    note.hidden = false;
    note.textContent =
      (note.textContent ? note.textContent + " " : "") +
      "情绪与语气提示会作为 instruction 下发给 Breeze 服务，并自动把 cfg_scale 提到 4.0。";
  }

  const sv = state.voices.find((x) => x.id === state.selectedId);
  $("#svEngine").textContent = spec ? `引擎：${spec.name}` : "";
  if (sv && spec) {
    $("#svMeta").textContent =
      `${fmtDuration(sv.duration_sec)} · ${sv.sample_rate} Hz · ${sv.language}` +
      (sv.active_model_ready ? "" : " · 该模型待提取");
  }
}

async function activateModel(modelId) {
  if (state.switching || state.busy) {
    toast("有任务正在执行，请稍后再切换", "error");
    return;
  }
  const spec = state.models.find((m) => m.id === modelId);
  if (!spec) return;
  if (!spec.installed) {
    toast(
      `${spec.name} ${spec.transport === "remote" ? "未配置服务地址" : "权重未安装"}，无法切换`,
      "error"
    );
    return;
  }
  if (spec.transport === "remote" && spec.online === false) {
    toast(`无法连接 ${spec.name} 服务：${spec.endpoint || "未配置地址"}`, "error");
    return;
  }
  if (modelId === state.activeModelId && spec.loaded) {
    closeModelMenu();
    return;
  }

  state.switching = true;
  closeModelMenu();
  $("#modelBtnName").textContent = `切换中 · ${spec.name}`;
  showProgress(0.03, `切换到 ${spec.name}…`);

  try {
    const data = await api("/api/models/activate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: modelId }),
    });
    if (data.job_id) {
      await pollJob(data.job_id, (job) => showProgress(job.progress, job.stage));
    }
    state.activeModelId = modelId;
    await refreshStatus();
    await refreshVoices();
    hideProgress();
    toast(`已切换到 ${spec.name}，当前文本与参数已保留`, "ok");
  } catch (err) {
    toast(err.message, "error");
    hideProgress();
    await refreshStatus();
  } finally {
    state.switching = false;
    updateSynthEnabled();
  }
}

/* ---------------- 音色库 ---------------- */
async function refreshVoices() {
  const data = await api("/api/voices");
  state.voices = data.voices || [];
  if (data.active_model_id) state.activeModelId = data.active_model_id;
  if (state.selectedId && !state.voices.some((v) => v.id === state.selectedId)) {
    state.selectedId = null;
  }
  if (!state.selectedId && state.voices.length) {
    state.selectedId = state.voices[0].id;
  }
  renderVoices();
  renderSelected();
}

function renderVoices() {
  const list = $("#voiceList");
  $("#voiceCount").textContent = state.voices.length;
  if (!state.voices.length) {
    list.innerHTML = '<div class="empty-hint">尚未添加音色</div>';
    return;
  }
  list.innerHTML = state.voices
    .map((v) => {
      const processing = v.active_model_status === "processing";
      const ready = v.active_model_ready;
      let badge;
      if (processing) {
        badge = '<span class="badge working">提取中</span>';
      } else if (ready) {
        badge = '<span class="badge ready">就绪</span>';
      } else {
        badge = '<span class="badge pending" title="首次用当前模型合成时会自动提取音色特征">待提取</span>';
      }
      const srcChips = (v.source_models || [])
        .map(
          (m) =>
            `<span class="src-chip${m.primary ? " primary" : ""}" title="${escapeHtml(m.vendor)} · v${escapeHtml(m.version)}${m.primary ? "（初始来源）" : ""}">${escapeHtml(m.name)}</span>`
        )
        .join("");
      return `
        <div class="voice-card ${v.id === state.selectedId ? "active" : ""}" data-id="${v.id}">
          <div class="vc-top">
            <div class="vc-avatar">${(v.name || "V").slice(0, 1).toUpperCase()}</div>
            <div class="vc-name" title="${escapeHtml(v.name)}">${escapeHtml(v.name)}</div>
            <button class="vc-del" data-del="${v.id}" title="删除">×</button>
          </div>
          <div class="vc-meta">
            ${badge}
            <span>${fmtDuration(v.duration_sec)}</span>
            <span>${v.language || "ZH"}</span>
            <button class="vc-play" data-play="${v.id}">试听</button>
          </div>
          ${srcChips ? `<div class="vc-source">${srcChips}</div>` : ""}
          ${v.transcript ? `<div class="vc-transcript">「${escapeHtml(v.transcript)}」</div>` : ""}
        </div>`;
    })
    .join("");
}

function renderSelected() {
  const v = state.voices.find((x) => x.id === state.selectedId);
  if (!v) {
    $("#svName").textContent = "未选择";
    $("#svMeta").textContent = "";
    $("#svSource").textContent = "";
    $("#synthBtn").disabled = true;
    return;
  }
  $("#svName").textContent = v.name;
  $("#svMeta").textContent = `${fmtDuration(v.duration_sec)} · ${v.sample_rate} Hz · ${v.language}`;
  const sources = v.source_models || [];
  if (sources.length) {
    const names = sources.map((m) => m.name).join(" · ");
    $("#svSource").textContent = `来源：${names}`;
    $("#svSource").title = sources
      .map((m) => `${m.name} v${m.version}（${m.vendor}）${m.primary ? " · 初始来源" : ""}`)
      .join("\n");
  } else {
    $("#svSource").textContent = "";
    $("#svSource").title = "";
  }
  $("#emotionText").value = v.emotion_text || "";
  applyModelCapabilities();
  updateSynthEnabled();
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function updateSynthEnabled() {
  const hasVoice = !!state.voices.find((x) => x.id === state.selectedId);
  const hasText = $("#textInput").value.trim().length > 0;
  $("#synthBtn").disabled = !hasVoice || !hasText || state.busy || state.switching;
}

/* ---------------- 上传与提取 ---------------- */
function bindUpload() {
  const dz = $("#dropzone");
  const input = $("#fileInput");

  dz.addEventListener("click", () => input.click());
  dz.addEventListener("dragover", (e) => {
    e.preventDefault();
    dz.classList.add("drag");
  });
  dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("drag");
    const file = e.dataTransfer.files[0];
    if (file) setPendingFile(file);
  });
  input.addEventListener("change", () => {
    if (input.files[0]) setPendingFile(input.files[0]);
  });

  $("#uploadBtn").addEventListener("click", doUpload);
}

function setPendingFile(file) {
  if (!file.type.startsWith("audio") && !/\.(wav|mp3|m4a|flac|ogg|aac)$/i.test(file.name)) {
    toast("请选择音频文件", "error");
    return;
  }
  state.pendingFile = file;
  const dz = $("#dropzone");
  dz.querySelector(".dz-title").textContent = file.name;
  dz.querySelector(".dz-sub").textContent = `${(file.size / 1024 / 1024).toFixed(2)} MB · 已就绪，点击右侧按钮提取音色`;
  if (!$("#voiceName").value) {
    $("#voiceName").value = file.name.replace(/\.[^.]+$/, "");
  }
  $("#uploadBtn").disabled = false;
}

async function doUpload() {
  if (!state.pendingFile || state.busy) return;
  const form = new FormData();
  form.append("file", state.pendingFile);
  form.append("name", $("#voiceName").value.trim());
  form.append("language", $("#voiceLang").value);
  form.append("model_id", state.activeModelId || "");

  state.busy = true;
  $("#uploadBtn").disabled = true;
  $("#uploadBtn").textContent = "提取中…";
  showProgress(0.02, "上传参考音频…");

  try {
    const data = await api("/api/voices", { method: "POST", body: form });
    const voice = data.voice;
    state.selectedId = voice.id;
    await refreshVoices();

    await pollJob(data.job_id, (job) => {
      showProgress(job.progress, job.stage);
    });

    await refreshVoices();
    const latest = state.voices.find((x) => x.id === voice.id);
    const spec = activeSpec();
    toast(
      latest && latest.transcript
        ? `音色已用 ${spec ? spec.name : "当前模型"} 提取，自动识别：${latest.transcript.slice(0, 20)}…`
        : "音色提取完成",
      "ok"
    );
    resetDropzone();
  } catch (err) {
    toast(err.message, "error");
    showProgress(0, "");
    hideProgress();
    await refreshVoices();
  } finally {
    state.busy = false;
    $("#uploadBtn").textContent = "提取音色";
    $("#uploadBtn").disabled = !state.pendingFile;
    updateSynthEnabled();
  }
}

function resetDropzone() {
  state.pendingFile = null;
  const dz = $("#dropzone");
  dz.querySelector(".dz-title").textContent = "拖入或点击上传参考音频";
  dz.querySelector(".dz-sub").textContent = "3–15 秒单人清晰语音，wav / mp3 / m4a";
  $("#fileInput").value = "";
  $("#voiceName").value = "";
  $("#uploadBtn").disabled = true;
}

/* ---------------- 任务轮询 ---------------- */
function pollJob(jobId, onTick) {
  return new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      try {
        const job = await api(`/api/jobs/${jobId}`);
        if (onTick) onTick(job);
        if (job.status === "done") {
          clearInterval(timer);
          resolve(job.result);
        } else if (job.status === "error") {
          clearInterval(timer);
          reject(new Error(job.error || "任务失败"));
        }
      } catch (err) {
        clearInterval(timer);
        reject(err);
      }
    }, 600);
  });
}

/* ---------------- 合成 ---------------- */
async function doSynthesize() {
  const text = $("#textInput").value.trim();
  if (!text || !state.selectedId || state.busy || state.switching) return;

  state.busy = true;
  updateSynthEnabled();
  $("#synthBtn").textContent = "合成中…";
  $("#result").hidden = true;
  showProgress(0.02, "提交任务…");

  try {
    const { job_id } = await api("/api/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        voice_id: state.selectedId,
        text,
        model_id: state.activeModelId,
        emotion_text: $("#emotionText").value.trim(),
        duration_factor: parseFloat($("#speed").value),
      }),
    });

    const result = await pollJob(job_id, (job) => showProgress(job.progress, job.stage));

    $("#result").hidden = false;
    $("#player").src = result.audio_url;
    $("#player").play().catch(() => {});
    $("#downloadBtn").href = result.audio_url;
    $("#downloadBtn").setAttribute("download", result.file_name);
    $("#revealBtn").dataset.file = result.file_name;
    $("#resultMeta").textContent =
      `${result.model_name || ""} · 耗时 ${result.elapsed_seconds}s · 音频 ${result.audio_seconds}s · RTF ${result.rtf}`;
    const ignored = result.parameters_ignored || [];
    $("#resultText").textContent = ignored.length
      ? ignored.join("；")
      : result.text.slice(0, 60);
    hideProgress();
    refreshLibrary();
    refreshStatus();
    refreshVoices();
    toast(`合成完成 · ${result.model_name || ""}`, "ok");
  } catch (err) {
    toast(err.message, "error");
    hideProgress();
  } finally {
    state.busy = false;
    $("#synthBtn").textContent = "开始合成";
    updateSynthEnabled();
  }
}

/* ---------------- 进度 ---------------- */
function showProgress(progress, stage) {
  const wrap = $("#progressWrap");
  wrap.hidden = false;
  $("#progressFill").style.width = `${Math.round((progress || 0) * 100)}%`;
  $("#progressText").textContent = stage || "处理中…";
}

function hideProgress() {
  $("#progressWrap").hidden = true;
  $("#progressFill").style.width = "0%";
}

/* ---------------- 合成语音库 ---------------- */
const lib = {
  items: [],
  selected: new Set(),
  playing: null,
  queue: [],
  audio: new Audio(),
  dirPath: "",
  dirExists: true,
  total: 0,
};

let modalState = null;

function fmtClock(sec) {
  if (sec == null || Number.isNaN(sec)) return "时长未知";
  const total = Math.max(0, Math.round(sec));
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}:${String(m % 60).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtFullTime(ts) {
  const d = new Date((ts || 0) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function refreshLibrary() {
  try {
    const data = await api("/api/outputs?limit=200");
    lib.items = data.items || [];
    lib.total = data.total || 0;
    lib.dirPath = data.dir_path || "";
    lib.dirExists = data.dir_exists !== false;
    const alive = new Set(lib.items.map((it) => it.file_name));
    [...lib.selected].forEach((n) => {
      if (!alive.has(n)) lib.selected.delete(n);
    });
    if (lib.playing && !alive.has(lib.playing)) stopPlayback();
    renderLibrary();
  } catch (err) {
    $("#libraryList").innerHTML =
      `<div class="empty-hint">读取合成记录失败：${escapeHtml(err.message)}</div>`;
  }
}

function renderLibrary() {
  const list = $("#libraryList");
  $("#libCount").textContent = lib.total;
  $("#libDir").textContent = lib.dirPath;
  $("#libDir").title = lib.dirPath || "";

  if (!lib.dirExists) {
    list.innerHTML = `
      <div class="lib-empty">
        <strong>存储目录不存在</strong>
        <span>目录可能已被移动或删除：<br><code>${escapeHtml(lib.dirPath)}</code><br>
        点击右上角「打开目录」会自动重新创建。</span>
      </div>`;
    updateSelectionUI();
    return;
  }
  if (!lib.items.length) {
    list.innerHTML = `
      <div class="lib-empty">
        <strong>还没有合成记录</strong>
        <span>输入文本并点击「开始合成」，生成的语音会保存到：<br><code>${escapeHtml(lib.dirPath)}</code></span>
      </div>`;
    updateSelectionUI();
    return;
  }

  list.innerHTML = lib.items.map(clipRowHtml).join("");
  updateSelectionUI();
}

function clipRowHtml(it) {
  const selected = lib.selected.has(it.file_name);
  const playing = lib.playing === it.file_name;
  const meta = [
    `<span>${fmtClock(it.duration_sec)}</span>`,
    `<span>${fmtFullTime(it.created_at)}</span>`,
    it.model_name ? `<span>${escapeHtml(it.model_name)}</span>` : "",
    `<span>${it.size_kb} KB</span>`,
  ]
    .filter(Boolean)
    .join('<span class="sep">·</span>');
  const tip = [it.voice_name, it.text].filter(Boolean).join("：");
  return `
    <div class="clip-row ${selected ? "selected" : ""} ${playing ? "playing" : ""}"
         data-name="${escapeHtml(it.file_name)}">
      <input type="checkbox" class="clip-check" ${selected ? "checked" : ""}
             aria-label="选择 ${escapeHtml(it.stem)}" />
      <button class="clip-play" data-act="play" title="播放">${playing ? "❚❚" : "▶"}</button>
      <div class="clip-main">
        <div class="clip-name" title="${escapeHtml(tip || it.file_name)}">${escapeHtml(it.file_name)}</div>
        <div class="clip-meta">${meta}</div>
      </div>
      <div class="clip-actions">
        <button class="clip-act" data-act="rename" title="重命名">✎</button>
        <button class="clip-act del" data-act="delete" title="删除">×</button>
      </div>
    </div>`;
}

function updateSelectionUI() {
  const count = lib.selected.size;
  $("#libSel").textContent = `已选 ${count} 项`;
  const all = $("#selectAll");
  const total = lib.items.length;
  all.disabled = total === 0;
  all.checked = total > 0 && count === total;
  all.indeterminate = count > 0 && count < total;
  document.querySelectorAll("[data-batch]").forEach((btn) => {
    btn.disabled = count === 0;
  });
}

function selectedNames() {
  return lib.items.map((it) => it.file_name).filter((n) => lib.selected.has(n));
}

/* ---------- 播放 ---------- */
function stopPlayback() {
  lib.audio.pause();
  lib.queue = [];
  setPlaying(null);
}

function setPlaying(name) {
  lib.playing = name;
  document.querySelectorAll(".clip-row").forEach((row) => {
    const on = row.dataset.name === name;
    row.classList.toggle("playing", on);
    const btn = row.querySelector(".clip-play");
    if (btn) btn.textContent = on ? "❚❚" : "▶";
  });
}

function startPlay(name) {
  const item = lib.items.find((x) => x.file_name === name);
  if (!item) {
    lib.queue = [];
    return;
  }
  lib.audio.pause();
  const audio = new Audio(item.audio_url);
  lib.audio = audio;
  audio.onended = () => {
    setPlaying(null);
    const next = lib.queue.shift();
    if (next) startPlay(next);
  };
  audio.onerror = () => {
    setPlaying(null);
    toast("播放失败：文件可能已被移动或删除", "error");
  };
  setPlaying(name);
  audio.play().catch(() => {
    setPlaying(null);
    toast("播放失败，请检查音频文件是否完好", "error");
  });
}

function playClip(name) {
  if (lib.playing === name) {
    stopPlayback();
    return;
  }
  lib.queue = [];
  startPlay(name);
}

function playQueue(names) {
  if (!names.length) return;
  const [first, ...rest] = names;
  lib.queue = rest;
  startPlay(first);
  toast(rest.length ? `连续播放 ${names.length} 条语音` : "开始播放", "ok");
}

/* ---------- 弹窗 ---------- */
function submitModal() {
  if (!modalState) return;
  const field = modalState.field;
  if (field) {
    const value = field.value.trim();
    if (!value) {
      field.focus();
      field.classList.add("invalid");
      return;
    }
    closeModal(value);
  } else {
    closeModal(true);
  }
}

function closeModal(result) {
  const mask = $("#modalMask");
  mask.hidden = true;
  const st = modalState;
  modalState = null;
  if (st) st.resolve(result);
}

function openModal({ title, message = "", confirmText = "确定", danger = false, input = null }) {
  return new Promise((resolve) => {
    const mask = $("#modalMask");
    $("#modalTitle").textContent = title;
    $("#modalBody").innerHTML = message;
    mask.querySelector(".modal").classList.toggle("danger", !!danger);
    $("#modalOk").textContent = confirmText;

    let field = null;
    if (input !== null) {
      field = document.createElement("input");
      field.type = "text";
      field.value = input;
      field.maxLength = 80;
      field.spellcheck = false;
      $("#modalBody").appendChild(field);
      field.addEventListener("input", () => field.classList.remove("invalid"));
    }

    mask.hidden = false;
    modalState = { resolve, field };
    if (field) {
      field.focus();
      field.select();
    } else {
      $("#modalOk").focus();
    }
  });
}

function bindModal() {
  $("#modalOk").addEventListener("click", submitModal);
  $("#modalCancel").addEventListener("click", () => closeModal(null));
  $("#modalMask").addEventListener("click", (e) => {
    if (e.target === $("#modalMask")) closeModal(null);
  });
  document.addEventListener("keydown", (e) => {
    if ($("#modalMask").hidden) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeModal(null);
    } else if (e.key === "Enter") {
      e.preventDefault();
      submitModal();
    }
  });
}

/* ---------- 重命名 / 删除 / 打开目录 ---------- */
async function renameClip(name) {
  const item = lib.items.find((x) => x.file_name === name);
  if (!item) return;
  const value = await openModal({
    title: "重命名语音",
    message:
      `<p>扩展名 <b>.wav</b> 会自动保留，文件名中不能出现 <code>\\ / : * ? " &lt; &gt; |</code>。</p>`,
    confirmText: "保存",
    input: item.stem,
  });
  if (value === null || value.trim() === item.stem) return;

  try {
    const data = await api(`/api/outputs/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: value.trim() }),
    });
    if (lib.playing === name) stopPlayback();
    lib.selected.delete(name);
    await refreshLibrary();
    toast(`已重命名为 ${data.item.file_name}`, "ok");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function deleteClips(names) {
  if (!names.length) return;
  const preview = names.slice(0, 5).join("\n");
  const ok = await openModal({
    title: names.length > 1 ? `删除 ${names.length} 条语音？` : "删除这条语音？",
    message:
      `<p>文件将从磁盘移除，<b>无法恢复</b>。</p>` +
      `<div class="modal-list">${escapeHtml(preview)}${
        names.length > 5 ? `\n… 等 ${names.length} 条` : ""
      }</div>`,
    confirmText: "删除",
    danger: true,
  });
  if (!ok) return;

  try {
    const data = await api("/api/outputs/batch-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_names: names }),
    });
    const deleted = data.deleted || [];
    if (deleted.includes(lib.playing)) stopPlayback();
    deleted.forEach((n) => lib.selected.delete(n));
    await refreshLibrary();
    if (deleted.length) {
      toast(`已删除 ${deleted.length} 条语音`, "ok");
    }
    const failed = data.failed || [];
    if (failed.length) {
      toast(`有 ${failed.length} 条未删除：${failed[0].reason}`, "error");
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

async function batchRenameClips(names) {
  if (!names.length) return;
  const value = await openModal({
    title: `批量重命名 ${names.length} 条语音`,
    message:
      `<p>按列表顺序重命名为「前缀-01、前缀-02…」，扩展名保持不变；遇到重名会自动顺延序号。</p>`,
    confirmText: "重命名",
    input: "语音",
  });
  if (value === null) return;

  try {
    const data = await api("/api/outputs/batch-rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_names: names, prefix: value.trim() }),
    });
    if (lib.playing && names.includes(lib.playing)) stopPlayback();
    const renamed = data.renamed || [];
    lib.selected.clear();
    await refreshLibrary();
    toast(`已重命名 ${renamed.length} 条语音`, renamed.length ? "ok" : "error");
    const failed = data.failed || [];
    if (failed.length) toast(`有 ${failed.length} 条失败：${failed[0].reason}`, "error");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function openOutputsDir(fileName) {
  try {
    const data = await api("/api/outputs/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fileName ? { file_name: fileName } : {}),
    });
    toast(
      data.created
        ? "存储目录不存在，已自动创建并打开"
        : fileName
        ? "已在文件管理器中定位到该文件"
        : "已在文件管理器中打开存储目录",
      "ok"
    );
    if (data.created) refreshLibrary();
  } catch (err) {
    toast(err.message, "error");
  }
}

function bindLibrary() {
  $("#openDirBtn").addEventListener("click", () => openOutputsDir());
  $("#refreshLibBtn").addEventListener("click", async () => {
    await refreshLibrary();
    toast("列表已刷新");
  });
  $("#revealBtn").addEventListener("click", () => {
    const name = $("#revealBtn").dataset.file;
    if (name) openOutputsDir(name);
    else openOutputsDir();
  });

  $("#selectAll").addEventListener("change", (e) => {
    if (e.target.checked) lib.items.forEach((it) => lib.selected.add(it.file_name));
    else lib.selected.clear();
    document.querySelectorAll(".clip-row").forEach((row) => {
      row.classList.toggle("selected", lib.selected.has(row.dataset.name));
      const box = row.querySelector(".clip-check");
      if (box) box.checked = lib.selected.has(row.dataset.name);
    });
    updateSelectionUI();
  });

  $("#libraryList").addEventListener("click", (e) => {
    const row = e.target.closest(".clip-row");
    if (!row) return;
    const name = row.dataset.name;

    if (e.target.classList.contains("clip-check")) {
      if (e.target.checked) lib.selected.add(name);
      else lib.selected.delete(name);
      row.classList.toggle("selected", e.target.checked);
      updateSelectionUI();
      return;
    }
    const act = e.target.dataset.act;
    if (act === "play") return playClip(name);
    if (act === "rename") return renameClip(name);
    if (act === "delete") return deleteClips([name]);
    // 点空白处也能播放
    playClip(name);
  });

  document.querySelectorAll("[data-batch]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const names = selectedNames();
      if (!names.length) return;
      const kind = btn.dataset.batch;
      if (kind === "play") playQueue(names);
      else if (kind === "rename") batchRenameClips(names);
      else if (kind === "delete") deleteClips(names);
    });
  });
}

/* ---------------- 模型菜单 ---------------- */
function openModelMenu() {
  $("#modelMenu").hidden = false;
  $("#modelBtn").classList.add("open");
  $("#modelBtn").setAttribute("aria-expanded", "true");
  // 卡片多于一屏时，保证当前使用的模型完整可见
  const active = $("#modelList .model-card.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function closeModelMenu() {
  $("#modelMenu").hidden = true;
  $("#modelBtn").classList.remove("open");
  $("#modelBtn").setAttribute("aria-expanded", "false");
}

function bindModelPicker() {
  $("#modelBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    if ($("#modelMenu").hidden) openModelMenu();
    else closeModelMenu();
  });

  $("#modelMenu").addEventListener("click", (e) => {
    const card = e.target.closest(".model-card");
    if (!card) return;
    activateModel(card.dataset.model);
  });

  document.addEventListener("click", (e) => {
    if (!$("#modelPicker").contains(e.target)) closeModelMenu();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModelMenu();
  });
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  $("#textInput").addEventListener("input", () => {
    $("#charCount").textContent = `${$("#textInput").value.length} / 2000`;
    updateSynthEnabled();
  });

  $("#clearText").addEventListener("click", () => {
    $("#textInput").value = "";
    $("#charCount").textContent = "0 / 2000";
    updateSynthEnabled();
  });

  $("#speed").addEventListener("input", () => {
    $("#speedVal").textContent = `${parseFloat($("#speed").value).toFixed(2)}×`;
  });

  $("#synthBtn").addEventListener("click", doSynthesize);

  $("#unloadBtn").addEventListener("click", async () => {
    try {
      await api("/api/engine/unload", { method: "POST" });
      toast("已释放显存");
      refreshStatus();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  $("#voiceList").addEventListener("click", async (e) => {
    const delId = e.target.dataset.del;
    const playId = e.target.dataset.play;
    if (delId) {
      e.stopPropagation();
      const v = state.voices.find((x) => x.id === delId);
      if (!confirm(`确定删除音色「${v ? v.name : delId}」？`)) return;
      try {
        await api(`/api/voices/${delId}`, { method: "DELETE" });
        if (state.selectedId === delId) state.selectedId = null;
        await refreshVoices();
        toast("已删除");
      } catch (err) {
        toast(err.message, "error");
      }
      return;
    }
    if (playId) {
      e.stopPropagation();
      playAudio(`/api/voices/${playId}/ref`);
      return;
    }
    const card = e.target.closest(".voice-card");
    if (card) {
      state.selectedId = card.dataset.id;
      renderVoices();
      renderSelected();
    }
  });
}

function playAudio(url) {
  state.audio.pause();
  state.audio = new Audio(url);
  state.audio.play().catch(() => toast("播放失败", "error"));
}

/* ---------------- 主题 ---------------- */
// 首屏由 /theme-boot.js 同步写 data-theme，这里只负责后续切换与联动
const THEME_MQ = window.matchMedia("(prefers-color-scheme: dark)");
const THEME_VALUES = ["light", "dark", "system"];
const THEME_LABEL = { light: "浅色主题", dark: "深色主题", system: "跟随系统" };
let themePref = document.documentElement.getAttribute("data-theme-pref") || "system";

function resolveTheme(pref) {
  if (pref === "system") return THEME_MQ.matches ? "dark" : "light";
  return pref === "light" ? "light" : "dark";
}

function renderThemeSeg() {
  document.querySelectorAll("#themeSeg .seg-btn").forEach((btn) => {
    const on = btn.dataset.theme === themePref;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-checked", on ? "true" : "false");
  });
}

function applyTheme(pref, { animate = true } = {}) {
  themePref = THEME_VALUES.includes(pref) ? pref : "system";
  const root = document.documentElement;
  const resolved = resolveTheme(themePref);
  const changed = root.getAttribute("data-theme") !== resolved;

  if (changed && animate) {
    // 只在这 260ms 内挂过渡类：常驻会把悬停位移、进度条动画一起改掉
    root.classList.add("theme-switching");
    clearTimeout(applyTheme._timer);
    applyTheme._timer = setTimeout(() => root.classList.remove("theme-switching"), 260);
  }

  root.setAttribute("data-theme", resolved);
  root.setAttribute("data-theme-pref", themePref);
  renderThemeSeg();
}

async function chooseTheme(pref) {
  if (!THEME_VALUES.includes(pref)) return;
  if (pref === themePref) return;
  const previous = themePref;
  applyTheme(pref);
  try {
    const data = await api("/api/app/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ theme: pref }),
    });
    appRuntime.settings = { ...appRuntime.settings, ...(data.settings || {}) };
    toast(`已切换为${THEME_LABEL[themePref]}`, "ok");
  } catch (err) {
    applyTheme(previous, { animate: false });
    toast(`主题保存失败：${err.message}`, "error");
  }
}

function bindTheme() {
  const seg = $("#themeSeg");
  if (seg) {
    seg.addEventListener("click", (e) => {
      const btn = e.target.closest(".seg-btn");
      if (btn && btn.dataset.theme) chooseTheme(btn.dataset.theme);
    });
  }
  // 系统外观变化：只有「跟随系统」时才联动
  const onSystemChange = () => {
    if (themePref === "system") applyTheme("system");
  };
  if (THEME_MQ.addEventListener) THEME_MQ.addEventListener("change", onSystemChange);
  else if (THEME_MQ.addListener) THEME_MQ.addListener(onSystemChange);
  renderThemeSeg();
}

/* ---------------- 窗口与退出 ---------------- */
const appRuntime = {
  settings: { minimize_to_tray: true },
  capabilities: { shell: false, tray: false, hide_to_tray: false, full_exit: false },
  scope: [],
};

function nativeApi() {
  return (window.pywebview && window.pywebview.api) || null;
}

// 优先走窗口自带的 JS 桥：复用模式下页面是后端发的，HTTP 会操作到别人的窗口
async function nativeCall(method, ...args) {
  const api = nativeApi();
  if (!api || typeof api[method] !== "function") return null;
  try {
    return await api[method](...args);
  } catch (err) {
    // 桥不通就回退 HTTP，不能因为桥的问题把面板卡死
    console.warn("[native]", method, err);
    return null;
  }
}

async function refreshAppRuntime() {
  try {
    let data = await nativeCall("get_runtime");
    if (!data) data = await api("/api/app/runtime");
    appRuntime.settings = data.settings || appRuntime.settings;
    appRuntime.capabilities = data.capabilities || appRuntime.capabilities;
    appRuntime.scope = data.exit_scope || [];
    // 服务端偏好优先（首屏脚本已按它渲染，这里只处理极端情况下的不一致）
    const serverPref = (data.settings && data.settings.theme) || "system";
    if (serverPref !== themePref) applyTheme(serverPref, { animate: false });
    renderAppRuntime();
  } catch (err) {
    /* 拿不到宿主信息只影响这个面板，不影响合成功能 */
  }
}

function renderAppRuntime() {
  const cap = appRuntime.capabilities;
  const badge = $("#shellBadge");
  if (!cap.shell) {
    badge.textContent = "浏览器模式";
    badge.className = "pop-badge warn";
  } else if (!cap.tray) {
    badge.textContent = "托盘不可用";
    badge.className = "pop-badge warn";
  } else {
    badge.textContent = "桌面窗口";
    badge.className = "pop-badge ok";
  }

  const trayToggle = $("#trayToggle");
  trayToggle.checked = !!appRuntime.settings.minimize_to_tray;
  trayToggle.disabled = !cap.shell || !cap.tray;
  $("#hideTrayBtn").disabled = !cap.shell || !cap.tray;
  $("#fullExitBtn").disabled = !cap.full_exit;
  $("#hideTrayBtn").title = cap.shell ? "隐藏窗口到系统托盘" : "浏览器标签页无法隐藏";
}

function openAppMenu() {
  $("#appPop").hidden = false;
  $("#appMenuBtn").setAttribute("aria-expanded", "true");
  refreshAppRuntime();
}

function closeAppMenu() {
  $("#appPop").hidden = true;
  $("#appMenuBtn").setAttribute("aria-expanded", "false");
}

function bindAppMenu() {
  $("#appMenuBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    if ($("#appPop").hidden) openAppMenu();
    else closeAppMenu();
  });

  $("#appPop").addEventListener("click", (e) => e.stopPropagation());
  // pywebview 的 JS 桥要等页面加载完才挂上，就绪后补一次能力/设置
  window.addEventListener("pywebviewready", () => refreshAppRuntime());
  document.addEventListener("click", () => {
    if (!$("#appPop").hidden) closeAppMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#appPop").hidden) closeAppMenu();
  });

  $("#trayToggle").addEventListener("change", async (e) => {
    const value = !!e.target.checked;
    try {
      let data = await nativeCall("set_minimize_to_tray", value);
      if (!data) {
        data = await api("/api/app/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ minimize_to_tray: value }),
        });
      }
      appRuntime.settings = data.settings || appRuntime.settings;
      appRuntime.capabilities = data.capabilities || appRuntime.capabilities;
      renderAppRuntime();
      toast(value ? "已开启：最小化后收进系统托盘" : "已关闭：关闭窗口即退出本窗口", "ok");
    } catch (err) {
      e.target.checked = !value;
      toast(err.message, "error");
    }
  });

  $("#hideTrayBtn").addEventListener("click", async () => {
    closeAppMenu();
    try {
      let res = await nativeCall("hide_to_tray");
      if (!res) res = await api("/api/app/hide", { method: "POST" });
      if (res && res.ok === false) toast(res.reason || "无法收进托盘", "error");
    } catch (err) {
      toast(err.message, "error");
    }
  });

  $("#fullExitBtn").addEventListener("click", async () => {
    closeAppMenu();
    const scope = (appRuntime.scope && appRuntime.scope.length
      ? appRuntime.scope
      : ["本窗口进程", "本地后端服务"]
    )
      .map((item) => `<li>${escapeHtml(item)}</li>`)
      .join("");
    const confirmed = await openModal({
      title: "完全退出并关闭",
      message:
        `<p>将结束以下进程与记录，确认后不会留下后台进程：</p>` +
        `<ul class="modal-list">${scope}</ul>` +
        `<p class="modal-hint">已加载的模型会从显存释放，下次启动需要重新加载（约 20–40 秒）。</p>`,
      confirmText: "完全退出",
      danger: true,
    });
    if (!confirmed) return;
    try {
      let res = await nativeCall("full_exit");
      if (!res) res = await api("/api/app/exit", { method: "POST" });
      if (res && res.ok === false) {
        toast(res.reason || "无法退出", "error");
        return;
      }
      toast("正在完全退出，请稍候…", "ok");
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

/* ---------------- 启动 ---------------- */
async function boot() {
  bindUpload();
  bindEvents();
  bindModelPicker();
  bindModal();
  bindLibrary();
  bindAppMenu();
  bindTheme();
  refreshAppRuntime();
  // 列表不依赖模型状态，先并行拉取，避免被远程模型探测拖住
  refreshLibrary();
  try {
    await refreshVoices();
  } catch (err) {
    toast(`无法加载音色库：${err.message}`, "error");
  }
  await refreshStatus();
  if (location.hash === "#models") openModelMenu();
  setInterval(refreshStatus, 5000);
  // 其他窗口/程序改动过目录时保持列表同步
  setInterval(() => {
    if (!document.hidden && !modalState) refreshLibrary();
  }, 20000);
}

document.addEventListener("DOMContentLoaded", boot);
