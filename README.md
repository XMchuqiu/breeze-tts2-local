# Breeze Voice Studio

本地部署的中文零样本音色克隆与语音合成桌面应用。上传一段参考音频即可提取音色，从音色库中选择目标音色，输入文本生成语音，全程离线运行，音频不离开本机。内置四套可切换的合成模型（三套本机权重 + 一套 BreezeBlue 远程服务），可在质量、速度与许可之间自由取舍。

---

## 1. 交付概览

| 项目 | 内容 |
|---|---|
| 推理引擎 | 可切换：IndexTTS 2.5 / IndexTTS 1.5 / F5-TTS v1 Base / Breeze TTS 2（末位为 BreezeBlue 远程服务；注册表驱动，切换即生效） |
| 运行形态 | FastAPI 本地服务 + pywebview 原生桌面窗口 |
| 界面入口 | 双击 `启动.bat`，无浏览器标签页、无命令行窗口 |
| 核心能力 | 参考音频自动转写 → 音色提取与缓存 → 音色库管理 → 多模型切换 → 文本合成 |
| 实测环境 | Windows 11 / RTX 4060 Laptop 8 GB / 32 GB RAM / E 盘 470 GB 可用 |

---

## 2. 选型说明：Breeze TTS 2 为何以远程方式接入

原始需求指向 Breeze TTS 2。经核验官方 README 与本机环境，该方案在本机存在三重阻塞，因此在确认后改用 IndexTTS-2.5：

| 阻塞项 | 官方要求 | 本机实测 | 结论 |
|---|---|---|---|
| 操作系统 | Linux + CUDA，无 Windows 原生路径、无 CPU 回退 | Windows 11 | 不满足 |
| 显存 | eager 模式约 7.7 GiB，官方推荐 12 GB 起；`--fast-all` 需 14.4 GiB | 8188 MiB（桌面已占约 1 GB） | 不足 |
| WSL2 替代 | 可作为 Linux 兼容层 | `wsl.exe` 被安全策略黑名单拦截且不可绕过 | 不可用 |
| 许可 | 推理代码 Apache-2.0，权重为「研究与非商用」许可，自托管输出亦不可商用 | — | 商用受限 |

IndexTTS-2.5 在本机可稳定运行，且中文自然度、零样本克隆质量均处于开源第一梯队。二者能力对照：

| 维度 | Breeze TTS 2 | IndexTTS-2.5（本方案） |
|---|---|---|
| 架构 | 3B 稠密，24 kHz | GPT + DiT + BigVGAN，22.05 kHz |
| 语言 | 中英双语 | 中英日西阿多语种 |
| 克隆输入 | 参考音频 + 精确转写文本 | 仅参考音频（无需转写） |
| 显存 | 12 GB 起 | 约 5 GB（bf16） |
| 采样本机可跑 | 否 | 是 |

结论是「本机跑不动」，而不是「不能接入」。因此后续把 BreezeBlue 加进可选列表时，采用**远程推理服务**形态：本机只做客户端，权重跑在能启动官方服务的机器上（Linux + 12 GB CUDA，或云 GPU）。这样既不违反显存与平台约束，也能在同一个界面里用同一套音色库调用它。

---

## 2.5 多模型支持与切换

模型清单由 `app/models.py` 的注册表定义，启动时扫描权重目录，把「已安装 / 未安装」状态与能力矩阵一并下发给界面。顶栏的「当前模型」下拉列出全部可用模型，每张卡片显示名称、厂商、简介、显存占用、支持语种、是否支持情感/语速、商用许可与当前状态。

| 模型 | 架构 / 规模 | 实测显存 | 语种 | 情感提示 | 语速因子 | 许可 | 适用场景 |
|---|---|---|---|---|---|---|---|
| **IndexTTS 2.5** | GPT + S2Mel DiT + BigVGAN | 4.96 GB | 中英日西阿 | ✓ | ✓ | bilibili 自定义（附条件商用） | 中文有声内容、需要情绪与节奏控制 |
| **IndexTTS 1.5** | GPT + BigVGAN，权重 1.9 GB | 1.66 GB | 中英 | — | — | bilibili 自定义（附条件商用） | 显存紧张、追求速度、批量草稿 |
| **F5-TTS v1 Base** | 0.34B 非自回归 Flow Matching + Vocos | 0.69 GB | 中英 | — | ✓（映射 speed） | **MIT** | 需要可商用授权、长文本稳定输出 |
| **Breeze TTS 2** | 3B dense + Qwen3-TTS 音频分词器（远程服务） | 服务端 7.7 GB / 本机 0 | 中英 | ✓（映射 instruction） | — | 权重非商用；托管 API 需订阅 | 实时交互、情绪导演、需要语音事件 |

### 切换机制

```
选择模型 → 任务入队（与推理共用一把 GPU 锁）
        → 卸载当前模型（删引用 + gc.collect + empty_cache + ipc_collect）
        → 写入 config.json（重启后保持上次选择）
        → 懒加载目标模型 → 上报加载耗时
```

- **显存严格互斥**：同一时刻只有一份模型驻留。实测 2.5（4.96 GB）与 1.5（1.66 GB）互切无残留、无 OOM。切换即重新加载，2.5 约 22–33 s、1.5 约 22–34 s、F5 约 12 s。
- **参数不丢失**：切换只改变 `active_model`，前端不会清空文本、情感、语速与当前音色；服务端 `config.json` 记录选择，重启后自动恢复。
- **能力差异显式化**：目标模型不支持的参数仍保留在界面上，但输入被禁用并给出提示；合成结果里通过 `parameters_ignored` 明确列出「被忽略的参数」，不做静默丢弃。
- **音色按模型记状态**：`meta.json` 的 `extra.model_state` 记录每个模型对该音色的提取结果，卡片徽章据此显示「就绪 / 待提取」。切到新模型后首次合成会自动补做一次特征提取。
- **远程模型不占本地显存**：Breeze TTS 2 的 `transport` 为 `remote`，切换时不涉及权重加载，只做一次可达性探测；卸载也是解除绑定。它的 `installed` 语义改为「服务地址已配置」，另由 `online` 表示服务是否真的可达。

### 实测对比（同一段 22 字文本，同一参考音频）

| 模型 | 音频时长 | 耗时 | RTF | 备注 |
|---|---|---|---|---|
| IndexTTS 2.5 | 4.41 s（语速 1.4×） | 2.82 s | 0.64 | 情感提示因未启用 QwenEmotion 被忽略并提示 |
| IndexTTS 1.5 | 4.57 s | 2.41 s | 0.53 | 语速 1.6× 与情感提示被忽略并提示 |
| F5-TTS v1 | 2.60 s | 1.07 s | 0.41 | 语速随参考音频节奏，输出后做 −1 dBFS 限峰 |


### 2.6 Breeze TTS 2：远程服务是怎么接的

官方仓库提供了两样东西：本地推理脚本 `infer.py`，以及一个 HTTP 服务 `python -m breeze_infer.api`。后者暴露的是**单请求自带参考音频**的接口，与本应用「参考音频即音色」的模型完全对齐，因此适配器直接对接它：

```
POST {endpoint}/v1/audio/speech      multipart/form-data
  text        要合成的文本（可内联 [叹气]、(sigh) 等语音事件）
  ref_audio   参考音频文件
  ref_text    参考音频的准确文字稿
  instruction 情绪/语气指令（Voice Direction，可选）
  cfg_scale   1 = 纯克隆；4 = 加强指令遵循
  seed        随机种子
→ 24 kHz 单声道 s16le PCM（也兼容 audio/wav）
```

参数映射关系：

| 界面参数 | 发送给 Breeze | 说明 |
|---|---|---|
| 音色（参考音频） | `ref_audio` + `ref_text` | 每次请求内联上传；`ref_text` 由音色库的 ASR 结果自动补齐 |
| 情感提示 | `instruction`，并把 `cfg_scale` 提到 4 | 语义是「用自然语言导演」，不是 IndexTTS 那种情感向量 |
| 语速 | 不下发 | 接口无语速参数，结果里会明确写「语速因子已忽略」 |
| 文本内 `[叹气]` | 原样透传 | 官方支持句内语音事件 |

启用步骤（三步，无需改代码）：

1. 在能跑 GPU 的机器上启动官方服务：`python -m breeze_infer.api <模型目录> --host 0.0.0.0 --port 7860`；
2. 若服务不在本机，设置环境变量 `BREEZE_TTS_ENDPOINT=http://<该机器IP>:7860`（写进 `启动.bat` 或用系统环境变量均可）；
3. 打开界面 → 顶栏「当前模型」→ Breeze TTS 2。卡片显示「服务地址」与「使用中 · 已加载」即接通。

没有 GPU 机器时可先用协议自检桩验证链路（**只产生测试音，不是人声**）：

```
python tools/breeze_stub.py --port 7861 --log tmp/breeze_stub.jsonl
set BREEZE_TTS_ENDPOINT=http://127.0.0.1:7861
```

服务不可达时，卡片显示「未连接」并置灰，点击会提示具体地址；不会静默失败，也不会误判成「已安装」。

### 2.7 新增一个模型要改哪里

三个文件、四处改动，前端与接口层都不用动：

| 位置 | 改什么 | 说明 |
|---|---|---|
| `app/models.py` → `MODEL_SPECS` | 追加一条 `ModelSpec` | 唯一的数据源：`id / name / version / vendor / description / strengths / tradeoffs / languages / vram_gb / params / license_name / commercial_use / backend / model_dir + required_files`（本地模型）或 `transport="remote" + endpoint_env + default_endpoint`（远程模型） |
| `app/models.py` → `capabilities` | 声明能力矩阵 | `emotion_text` / `duration_factor` / `inline_events` / `ref_text_required` / `languages`。界面据此禁用控件、结果区据此产出 `parameters_ignored` |
| `app/engines/<name>.py` | 新建适配器 | 继承 `TTSEngineBase`，实现 `_load_model()`、`_infer(...)`、`_cached_prompt()`；加载状态、显存统计、串行锁、进度回调由基类统一处理 |
| `app/engine_manager.py` → `BACKENDS` | 注册 backend 名 | `{"<spec.backend>": <EngineClass>}`，键必须与 `ModelSpec.backend` 一致 |

`config.json` 只保存 `{"active_model": "<id>"}`，由界面切换时自动写入，不需要手工编辑；`meta.json` 里每个音色的 `extra.model_state` 也由服务端按模型自动累积。


---

## 3. 整体技术方案

### 3.1 分层结构

```
┌──────────────────────────────────────────────┐
│ 桌面外壳  app/desktop.py                     │
│ pywebview 原生窗口 → 内嵌 HTTP 指向本机服务   │
└───────────────┬──────────────────────────────┘
                │ 127.0.0.1:随机空闲端口
┌───────────────▼──────────────────────────────┐
│ 服务层  app/server.py（FastAPI）              │
│ 音色 CRUD · 任务队列 · 进度上报 · 静态页面托管 │
└──────┬─────────────────────┬─────────────────┘
       │                     │
┌──────▼─────────┐   ┌───────▼────────────────┐
│ app/asr.py     │   │ app/engine_manager.py  │
│ faster-whisper │   │ 模型注册 · 切换 · 串行锁│
│ 自动转写       │   │ ↓ app/engines/         │
│                │   │ 2.5 / 1.5 / F5 适配器   │
└──────┬─────────┘   └───────┬────────────────┘
       │                     │
┌──────▼─────────────────────▼──────────────────┐
│ 存储层  app/voice_store.py                    │
│ voices/<id>/ref.wav + meta.json，长文本切分   │
└───────────────────────────────────────────────┘
```

### 3.2 关键设计决策

**单例 + 串行队列。** 8 GB 显存只够放一份模型，并发推理必然 OOM。所有 GPU 任务（模型切换、音色提取、语音合成）经同一把互斥锁排队，前端以任务 ID 轮询进度，而不是阻塞式等待。

**模型注册表 + 适配器。** 每个模型是一个 `ModelSpec`（元信息、能力矩阵、权重路径、必需文件）加一个 `TTSEngineBase` 子类（只实现加载与单次推理）。管理器统一负责显存回收、切换与持久化，新增模型不改动服务层与前端。

**音色条件缓存。** IndexTTS2 内部以参考音频路径为键缓存说话人条件（`cache_spk_cond` / `cache_s2mel_style` / `cache_s2mel_prompt`）。因此「提取音色」被实现为一次极短合成的预热：既完成真实的特征计算，又让后续同音色合成跳过编码环节。实测同一音色连续合成，耗时从 9.5 s 降至 3.4 s。

**参考音频规范化。** 上传后统一处理为 22.05 kHz 单声道 PCM16、峰值 −3 dBFS；超过 20 秒时按 1 秒窗滑动搜索能量最高片段截取，避免把静音或噪声带入音色编码。

**文本自动转写。** 使用 faster-whisper（small，CPU int8，VAD 过滤）识别参考音频内容，结果用于界面展示与语种自动判定；识别失败时降级为手工填写，不阻塞音色注册。

**情感控制按需开启。** QwenEmotion 情感模型常驻约 1.2 GB 显存。默认关闭（`BREEZE_QWEN_EMO=0`），界面上的情感提示会被静默忽略并在结果区注明原因；显存充裕的机器可通过环境变量开启。

---

## 4. 模型加载与运行方式

### 4.1 权重构成

主模型 `IndexTeam/IndexTTS-2.5`（ModelScope，约 4.3 GB）：

| 文件 | 大小 | 作用 |
|---|---|---|
| `gpt.pth` | 3.26 GB | 自回归语音 token 生成主干 |
| `codec.pth` | 607 MB | 语义编解码器 |
| `s2mel.pth` | 415 MB | 语义到梅尔频谱的流匹配模型 |
| `config.yaml` | 2.9 KB | 模型结构与环境配置 |
| `feat1.pt` / `feat2.pt` | 0.4 MB | 说话人特征统计 |
| `multilingual_zh_ja_yue_char_del.tiktoken` | 0.9 MB | 多语种词表 |

辅助模型自动下载至 `checkpoints/hf_cache/`：Wav2Vec2-BERT 2.0（约 2.3 GB，语义特征）、CAMPPlus（192 维音色嵌入）、BigVGAN v2（22 kHz 声码器）、semantic_codec。合计占用约 8.4 GB 磁盘。

### 4.2 加载流程

1. 进程启动后在后台线程预加载（`BREEZE_PRELOAD=1`，默认开启），避免首次操作等待；
2. `TTSEngine.load()` 以 bf16 精度构建 `IndexTTS2`，依次恢复 GPT、语义编解码器、s2mel、CAMPPlus、BigVGAN；
3. 检测到显存小于 10 GB 时，上游自动开启 `low_vram` 模式，长文本按标点切分为 ≤40 字片段分块合成后拼接，间隔 200 ms 静音。

实测：加载耗时 **40.4–48.7 s**，加载后显存分配 **4.94 GB**，剩余约 1.6–1.9 GB。

### 4.3 推理性能（RTX 4060 Laptop 8 GB，bf16）

| 场景 | 输入 | 音频时长 | 耗时 | RTF |
|---|---|---|---|---|
| 首次合成（含音色编码） | 22 字 | 3.31 s | 9.50 s | 2.87 |
| 同音色再次合成 | 22 字 | 3.19 s | 3.41 s | 1.07 |
| 长文本分块 | 56 字 | 9.09 s | 22.67 s | 2.49 |

RTF 为耗时 / 音频时长，数值越小越快。分块合成因每段都要重新进入推理流程，RTF 高于短文本，属预期行为。

---

## 5. 界面模块与交互流程

### 5.1 模块划分

| 区域 | 元素 | 职责 |
|---|---|---|
| 顶栏 | 引擎状态胶囊、显存胶囊 | 每 5 秒轮询 `/api/status`，显示就绪 / 加载中 / 缺权重 / 显存占用 |
| 左栏·上传区 | 拖拽区、名称输入、语种下拉、「提取音色」 | 接收音频文件，校验类型与大小，提交后进入克隆任务 |
| 左栏·音色库 | 音色卡片（首字头像、名称、状态徽章、时长、语种、试听、删除） | 展示与管理音色，点击卡片切换当前音色 |
| 右栏·当前音色条 | 名称、时长、采样率、语种 | 明确合成将使用哪个音色 |
| 右栏·文本区 | 多行输入 + 字数计数 + 清空 | 上限 2000 字，实时刷新合成按钮可用性 |
| 右栏·参数区 | 情感提示、语速滑块（0.5×–2.0×） | 映射到 `emo_text` 与 `duration_factor` |
| 右栏·操作区 | 开始合成、卸载模型 | 提交任务；释放显存 |
| 右栏·进度与结果 | 进度条、阶段文字、播放器、下载、在文件夹中显示 | 轮询任务至完成，自动播放并支持下载 |
| 右栏·合成语音 | 打开目录、刷新、全选、批量播放/重命名/删除、单条播放/重命名/删除 | 管理 `outputs/` 下的成品音频 |

### 5.2 流程一：上传音频 → 提取音色（克隆）

```
选择/拖入文件
   → 前端校验扩展名与 MIME，回填文件名与大小
   → POST /api/voices (multipart: file, name, language)
       ├─ 落盘临时文件，librosa 解码、重采样、切片、归一化
       ├─ 生成 voices/<id>/ref.wav 与 meta.json（status=processing）
       └─ 立即返回 voice + job_id
   → 后台任务：
       ├─ faster-whisper 转写 → 回写 transcript、判定语种
       ├─ 引擎未加载则先加载（显示「加载语音模型」）
       └─ 以「你好。」预热合成，完成说话人条件提取并缓存
   → 前端每 600 ms 轮询 /api/jobs/{id}，刷新徽章为「就绪」
```

### 5.3 流程二：选择目标音色

点击卡片 → 更新 `selectedId` → 卡片高亮并在左侧显示强调条 → 右栏顶部同步名称、时长、采样率、语种 → 「试听」按钮单独播放 `/api/voices/{id}/ref`（不触发 GPU 任务）。

### 5.4 流程三：文本合成

```
输入文本（按钮在「有音色 + 有文本 + 空闲」时才可用）
   → POST /api/synthesize {voice_id, text, emotion_text, duration_factor}
       ├─ 校验音色与文本（2000 字上限）
       ├─ 创建 job，返回 job_id
       └─ 后台：绑定上游 gr_progress 回调 → 推理 → 写 outputs/*.wav
   → 前端轮询进度（0–99% + 阶段文字），完成后：
       ├─ 播放器加载 /api/outputs/<file>
       ├─ 显示耗时、音频时长、RTF
       ├─ 生成下载链接
       └─ 刷新下方「合成语音」列表
```

### 5.4.1 合成语音的存储与文件管理

合成结果统一落在项目根的 `outputs/`，每个成品包含两个文件：`<名>.wav` 与同名 `<名>.json`（记录音色、模型、原文、生成时间）。

界面下方的「合成语音」面板就是这套目录的可视化管理：

| 能力 | 交互 | 说明 |
|---|---|---|
| 打开存储目录 | 面板右上「打开目录」 | 若目录被删除会先重建再打开，并提示「存储目录不存在，已自动创建并打开」 |
| 定位单条文件 | 结果区「在文件夹中显示」 | 在资源管理器中直接选中该 wav |
| 浏览 | 列表 | 按修改时间倒序，显示文件名、时长、生成时间、模型、体积；目录缺失或无文件时给出引导文案 |
| 播放 | 行首 ▶ / 点行空白 | 单条播放；选中多条后「批量播放」按列表顺序连播 |
| 重命名 | 行尾 ✎ | 弹窗改名，扩展名自动保留，非法字符会被替换，重名给出明确提示 |
| 批量重命名 | 勾选后「批量重命名」 | 统一前缀 + 两位序号（前缀-01、前缀-02…），重名自动顺延 |
| 删除 | 行尾 × / 勾选后「删除选中」 | 先弹确认框列出待删文件，确认后连 sidecar 一起删除，并提示删除数量 |

删除与重命名都走服务端文件操作（`app/audio_library.py`），并做了三层防护：文件名必须是单段名称（拒绝 `../` 越界）、扩展名白名单（wav/mp3/flac/m4a/ogg）、Windows 非法字符与保留名替换。

### 5.5 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 引擎、显存、ASR、音色数量、全部模型与当前模型 |
| GET | `/api/models` | 模型注册表（含安装状态、能力、许可） |
| POST | `/api/models/activate` | 切换模型，返回 job_id（卸载→回收→加载） |
| GET | `/api/voices` | 音色列表 |
| POST | `/api/voices` | 上传参考音频，返回 voice + job_id |
| PATCH | `/api/voices/{id}` | 改名 / 修改情感提示 / 修改转写 |
| DELETE | `/api/voices/{id}` | 删除音色目录 |
| GET | `/api/voices/{id}/ref` | 参考音频试听 |
| POST | `/api/voices/{id}/extract` | 为指定模型重新提取音色特征 |
| POST | `/api/synthesize` | 提交合成任务（可传 `model_id` 指定模型） |
| GET | `/api/jobs/{id}` | 任务状态与进度 |
| GET | `/api/outputs` | 合成音频列表（`limit` / `offset` / `q` 搜索，含时长、体积、模型） |
| GET | `/api/outputs/{file}` | 下载 / 播放合成音频 |
| POST | `/api/outputs/open` | 在系统文件管理器中打开存储目录（不存在则创建），可传 `file_name` 定位文件 |
| PATCH | `/api/outputs/{file}` | 重命名单条（`{name}`，扩展名保留） |
| DELETE | `/api/outputs/{file}` | 删除单条（含 sidecar） |
| POST | `/api/outputs/batch-delete` | 批量删除（`{file_names: []}`，逐条汇报结果） |
| POST | `/api/outputs/batch-rename` | 批量重命名（`{file_names: [], prefix}`） |
| GET | `/api/history` | 最近生成记录（兼容旧前端，字段与 `/api/outputs` 对齐） |
| POST | `/api/engine/load` · `/api/engine/unload` | 手动加载 / 释放显存 |
| GET | `/api/app/runtime` | 窗口能力与界面设置（是否在桌面窗口中、托盘是否可用、退出清理范围） |
| PUT | `/api/app/settings` | 保存界面设置（`minimize_to_tray`），立即生效 |
| POST | `/api/app/hide` | 把窗口收进系统托盘（浏览器直接打开时返回不可用） |
| POST | `/api/app/exit` | **完全退出并关闭**：结束窗口、后端、复用后端与托盘，清理实例记录 |

---

## 5.6 窗口与退出（托盘 / 完全退出）

顶栏最右侧的齿轮按钮打开「窗口与退出」面板：

| 操作 | 位置 | 行为 |
|---|---|---|
| 最小化/关闭时收进托盘 | 面板复选框（默认开启） | 点窗口 X 或最小化 → 窗口隐藏，**后端与已加载模型继续运行**；设置存 `ui-settings.json` |
| 立即收进托盘 | 面板按钮 | 同上，不经过开关 |
| 完全退出并关闭 | 面板红色按钮 | 二次确认（列出清理范围）后结束全部相关进程 |
| 打开主窗口 / 完全退出并关闭 | 托盘图标右键（双击图标=打开主窗口） | 恢复窗口 / 完全退出 |

两种关闭的区别：

| 对象 | 普通关闭（X / 最小化） | 完全退出并关闭 |
|---|---|---|
| 窗口 | 隐藏 | 销毁 |
| 本进程（窗口 + 自带 uvicorn） | 保留 | 结束 |
| 复用模式下由 `启动.bat` / 启动器起的后端 | 保留 | 结束 |
| 其他残留的 `desktop.py` 进程 | 保留 | 结束（扫描兜底） |
| 托盘图标 / `logs/instance.json` / 端口 | 保留 | 清除 |

实现分三层：`app/runtime.py` 存放设置与宿主回调（FastAPI 不需要反向 import 桌面模块）；`app/shell_tray.py` 提供托盘（缺 `pystray` 自动降级）；`app/desktop.py` 的 `DesktopShell` 接 `closing` / `minimized` 事件并负责清理。`app/proc_utils.py` 里的 `ancestors()` 很关键——venv 的 `pythonw.exe` 是启动器、真实解释器是它的子进程，两者命令行相同，不排除祖先会在清理时把自己一起杀掉。

---

## 6. 依赖环境

| 组件 | 版本 | 说明 |
|---|---|---|
| 操作系统 | Windows 10/11 x64 | 需 WebView2 运行时（Win11 自带） |
| GPU | NVIDIA，≥ 6 GB 显存 | 本方案实测 8 GB；bf16 需 Turing 以上算力 |
| 驱动 | 支持 CUDA 12.8 | 实测 616.92 |
| Python | 3.11.13（由 uv 安装） | 上游要求 `>=3.10,<3.12` |
| PyTorch | 2.8.0+cu128 | Windows PyPI 版为 CPU-only，必须用 CUDA 专用 wheel |
| IndexTTS | 2.0.0（仓库 `index-tts/index-tts`） | 推理主栈 |
| 关键依赖 | transformers 4.52.1、transformers 生态、descript-audiotools 0.7.2、wetext 0.2.1 | 上游锁定版本 |
| F5 推理栈 | f5-tts 1.1.22（`--no-deps` 安装）、vocos 0.1.0、x-transformers 2.31.11、torchdiffeq 0.2.5、datasets 3.6.0 | 与 IndexTTS 共享同一 torch，互不干扰 |
| 重要约束 | `huggingface_hub==0.36.2` | datasets 5.x 会把它升到 2.x，直接破坏 transformers 4.52，必须锁版本 |
| 应用层 | fastapi、uvicorn、python-multipart、pywebview 6.2.1、faster-whisper 1.2.1 | 见 `requirements-app.txt` |
| 磁盘 | 约 24 GB | 环境 9 GB + 2.5 权重 8.4 GB + 1.5 权重 1.9 GB + F5 权重 1.35 GB + 输出音频 |

---

## 7. 启动步骤

### 7.1 日常启动

双击项目根目录的 **`启动.bat`**。程序会：

1. 用 `pythonw` 静默启动（无命令行窗口）；
2. 若检测到已有实例在运行，直接开一个新窗口复用同一后端，不会重复占用显存；
3. 否则在 8765 起的空闲端口启动本地服务，后台预加载模型；
4. 约 40–50 秒后弹出桌面窗口，顶栏显示「模型就绪」。

需要看控制台输出或排查问题时，改用 **`启动-调试.bat`**，窗口关闭后命令行会停留并显示日志。

### 7.1.1 方式二：双击 exe 启动器（可选，不影响上述流程）

项目根目录的 `BreezeVoiceStudio.exe` 是新增的图形启动入口，`app\` 与两个 bat **一行未改**：

1. 先构建一次：双击 **`构建启动器.bat`**（首次会自动安装 PyInstaller，约 1–3 分钟）；
2. 之后直接双击 `BreezeVoiceStudio.exe`：自检项目根 / 解释器 / 核心文件 / 读写权限 → 自动拉起 `app\desktop.py` → 窗口内实时滚动加载日志；
3. 关闭启动器时可选「收进托盘（保持运行）」或「停止并退出」；即使强杀启动器，Job Object 也会连带终止子进程，不留后台占用。

两种方式共用同一个 `desktop.py`、同一个后端与音色库。先开 `启动.bat` 再开 exe，只会多一个窗口，不会重复加载模型。完整说明（启动目标、参数、退出行为、异常处理、构建命令、共存验证点）见 [`launcher/README.md`](launcher/README.md)。

### 7.2 从零复现环境

```bash
# 1) 安装 Python 3.11（uv 会自动下载）
pip install -U uv
uv python install 3.11

# 2) 获取上游代码
git clone https://github.com/index-tts/index-tts.git vendor/index-tts
cd vendor/index-tts

# 3) 创建环境（Windows 上 DeepSpeed 不可用，跳过 extras）
uv venv --python 3.11 .venv

# 4) 安装 CUDA 版 PyTorch（PyPI 的 Windows 包是 CPU-only）
.venv/Scripts/python.exe -m pip install "torch==2.8.0+cu128" "torchaudio==2.8.0+cu128" \
  --index-url https://mirrors.nju.edu.cn/pytorch/whl/cu128

# 5) 安装上游依赖
.venv/Scripts/python.exe -m pip install -e . -i https://mirrors.aliyun.com/pypi/simple

# 6) 安装应用层依赖
.venv/Scripts/python.exe -m pip install -r ../../requirements-app.txt \
  -i https://mirrors.aliyun.com/pypi/simple

# 7) 下载主权重（约 4.3 GB）
#    模型目录：checkpoints/，来源 https://www.modelscope.cn/models/IndexTeam/IndexTTS-2.5

# 8) 首次加载会自动拉取辅助模型到 checkpoints/hf_cache/

# 9) 可选：IndexTTS 1.5 权重（约 1.9 GB，推理只需 gpt/bigvgan_generator/dvae + 词表）
#    目录：checkpoints-1.5/，来源 https://www.modelscope.cn/models/IndexTeam/IndexTTS-1.5

# 10) 可选：F5-TTS 权重（约 1.35 GB）
#     目录：models/f5tts/，来源 https://hf-mirror.com/SWivid/F5-TTS/tree/main/F5TTS_v1_Base
#     pip install --no-deps f5-tts==1.1.22
#     pip install vocos x-transformers ema-pytorch cached-path torchdiffeq pypinyin rjieba unidecode pydub hydra-core datasets
#     pip install "huggingface_hub==0.36.2"   # 必须回退，否则 transformers 不可用

cd ../.. && 启动-调试.bat
```

> 网络提示：辅助模型默认走 ModelScope（国内直连）；如需回退 HF，可设置 `HF_ENDPOINT=https://hf-mirror.com`（`启动.bat` 已设置）。

### 7.3 关键环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `BREEZE_PRELOAD` | `1` | 启动即预加载模型；设为 `0` 改为首次使用时加载 |
| `BREEZE_QWEN_EMO` | `0` | 设为 `1` 启用 QwenEmotion 情感提示（额外约 1.2 GB 显存） |
| `BREEZE_TTS_ENDPOINT` | `http://127.0.0.1:7860` | Breeze TTS 2 远程推理服务地址（官方 `breeze_infer.api` 默认端口） |
| `BREEZE_ASR_MODEL` | `small` | faster-whisper 模型规格（tiny/base/small/medium） |
| `BREEZE_DEBUG` | `0` | 设为 `1` 打开 pywebview 调试工具 |

---

## 8. 目录结构

```
E:\breezeblue\
├─ 启动.bat                 日常启动（静默）
├─ 启动-调试.bat            调试启动（保留控制台）
├─ 构建启动器.bat           双击构建 exe 启动器
├─ BreezeVoiceStudio.exe    exe 启动入口（构建产物，可直接双击）
├─ launcher\                exe 启动器源码：main / core / ui / tray / assets
├─ requirements-app.txt     应用层依赖
├─ app\
│  ├─ desktop.py            桌面外壳：单实例检测、服务线程、原生窗口
│  ├─ server.py             FastAPI 路由、任务队列、进度上报
│  ├─ models.py             模型注册表：元信息、能力矩阵、权重路径、安装检测
│  ├─ engine_manager.py     模型切换、显存回收、选择持久化、统一推理入口
│  ├─ engines\              base.py + indextts25.py + indextts15.py + f5tts.py + breeze.py
│  ├─ config.json           当前激活模型（运行时生成，位于项目根目录）
│  ├─ voice_store.py        参考音频规范化与音色元数据
│  ├─ asr.py                faster-whisper 自动转写
│  └─ web\                  index.html / style.css / app.js
├─ voices\<id>\             ref.wav + meta.json
├─ outputs\                 合成结果
├─ tools\breeze_stub.py     Breeze 远程契约自检桩（只出测试音，不产生人声）
├─ logs\                    desktop.log、instance.json
├─ checkpoints-1.5\         IndexTTS 1.5 推理权重
├─ models\f5tts\            F5-TTS 权重与声码器缓存
├─ vendor\index-tts\        上游代码与 .venv、checkpoints（2.5 权重）
└─ wheels\                  离线构建的 wheel（argbind、unidic-lite）
```

---

## 9. 已知限制与调优建议

1. **单并发。** 一把锁串行处理所有 GPU 任务，界面在合成期间无法并行提取音色。8 GB 卡上这是必要约束。
2. **首次加载慢。** 40–50 秒主要是 8.4 GB 权重从磁盘读入。换成 NVMe 或保持常驻（不点「卸载模型」）可避免反复等待。
3. **长文本 RTF 偏高。** 低于 10 GB 显存会触发分块，每块独立推理。建议单次控制在 200 字以内，长文分段合成再拼接。
4. **参考音频质量决定上限。** 建议 3–15 秒、单人、无混响无背景音乐的干声；录音底噪会被音色编码一并学习。
5. **情感提示默认不生效。** 需显存余量 ≥ 2 GB 时设置 `BREEZE_QWEN_EMO=1`，并确保 `checkpoints/qwen0.6bemo4-merge/` 存在（约 1.2 GB，未包含在默认下载清单中）。
6. **切换有加载成本。** 每次切换都要重新读盘构建模型（12–34 s），并清空该模型的音色条件缓存，切回后首次合成会多花 3–6 s 重新编码。频繁 A/B 对比时建议把文本与音色固定，一次切一轮。
7. **模型能力不同。** IndexTTS 1.5 不响应情感提示与语速，F5-TTS 依赖参考音频转写（本应用自动用 ASR 补齐）。被忽略的参数会在结果区明确列出，不会静默丢弃。
8. **Breeze TTS 2 依赖外部服务。** 本机 8 GB 显存与 Windows 平台都跑不动它的权重（需 Linux + 12 GB 起），因此该选项是远程客户端：没有可达的 `BREEZE_TTS_ENDPOINT` 时卡片显示「未连接」且不可切换。它的克隆效果取决于 `ref_text` 是否与参考音频**逐字一致**，ASR 结果只是近似，长参考音频建议人工校对一次。
9. **许可。** IndexTTS-2.5 / 1.5 权重适用 bilibili 自定义模型许可协议，允许自托管与商业使用但附有条件（不得用于改进其他 AI 模型、衍生作品需附声明、禁止高风险场景）。F5-TTS 为 MIT 许可，商用无附加条款。Breeze TTS 2 的推理代码是 Apache-2.0，但权重与自托管输出适用 BreezeBlue Research and Non-Commercial License（仅托管订阅可商用）。对外分发或商用前请分别通读 `vendor/index-tts/LICENSE`、`f5_tts` 包内 LICENSE 与 BreezeBlue 的权重许可。
