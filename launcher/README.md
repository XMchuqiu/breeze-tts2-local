# Breeze Voice Studio 启动器（Windows .exe）

一个可以双击运行的图形启动入口。它**不是**新的后端，也不复制任何业务逻辑：只负责检查环境、拉起 `app/desktop.py`、把日志显示出来、退出时收干净。

原有的 `启动.bat` / `启动-调试.bat` 一行未改，三种入口可以同时存在、互不影响。

---

## 1. 启动目标（明确到每一层）

| 项目 | 取值 | 说明 |
|---|---|---|
| 目标程序 | `app\desktop.py` | 桌面外壳：内嵌 uvicorn + pywebview 原生窗口 |
| 解释器 | `vendor\index-tts\.venv\Scripts\pythonw.exe` | 无控制台版本，避免黑色命令行窗口 |
| 工作目录 | 项目根（当前为 `E:\breezeblue`） | 相对路径全部以它为基准 |
| 启动参数 | 默认 `[]`（无） | 可被 `launcher.json` 的 `args` 追加 |
| 标准输出 | 管道 → 界面日志 + `logs\launcher.log` | 实时逐行转发，不缓冲 |
| 退出方式 | 见第 4 节 | 默认「启动器关 → 工作室关」 |

**为什么是 `desktop.py` 而不是 `server.py`？**
`desktop.py` 已经实现了启动器需要的全部语义：单实例复用（`logs/instance.json`）、端口自动避让（8765 起向后找 40 个）、内嵌服务 + 原生窗口、窗口关闭时释放显存。启动器直接复用它，就不存在两套启动逻辑各自演化、行为不一致的问题。

**解释器探测顺序**（第一个存在的即采用）：

1. `launcher.json` 里手工指定的 `python_exe`
2. `vendor\index-tts\.venv\Scripts\pythonw.exe`（项目自带，可信）
3. 同目录 `python.exe`
4. `.venv\Scripts\pythonw.exe` / `.venv\Scripts\python.exe`
5. `venv\Scripts\...`
6. 系统 `PATH` 上的 `pythonw.exe` / `python.exe`（跳过 Microsoft Store 的存根，避免弹应用商店）

第 6 种属于「外来解释器」，会先执行一次 `import webview, uvicorn, fastapi` 做验证，缺依赖时禁用启动按钮并给出提示，而不是让用户看到一个一闪而过的错误窗口。

**注入的环境变量**（可在 `launcher.json` 的 `env` 中覆盖）：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `HF_ENDPOINT` | `https://hf-mirror.com` | 与 `启动.bat` 一致，走国内镜像 |
| `TOKENIZERS_PARALLELISM` | `false` | 抑制 tokenizer 并发告警 |
| `PYTHONUTF8` / `PYTHONIOENCODING` | `1` / `utf-8` | 保证中文日志不乱码 |
| `PYTHONUNBUFFERED` | `1` | 日志实时到达界面 |

---

## 2. 界面需求：需要什么、为什么、不要什么

| 需求 | 结论 | 理由 |
|---|---|---|
| 窗口 | **需要** | 模型加载要 20–40 秒。双击后如果只有一个托盘图标，用户无法判断是「在加载」还是「已经挂了」。窗口负责展示进度、失败原因和补救建议。 |
| 图标 | **需要** | `assets\app.ico`（多尺寸 16–256）。用于 exe 图标、窗口左上角、托盘。 |
| 菜单 | **需要（托盘右键）** | 窗口收起后仍要能「打开工作室 / 重启 / 看日志 / 退出」。 |
| 托盘 | **可选，缺失自动降级** | 依赖 `pystray`。没装或初始化失败时，按钮改为「最小化到任务栏」并说明原因，不影响启动。 |
| 内嵌网页界面 | **不要** | 那是 `app/desktop.py` 的职责。启动器只做入口，避免出现两套 UI 壳。 |

窗口默认 780×560，最小 700×500，深色配色与主界面一致（`#0a0e13`）。内容自上而下：

1. **标题栏区域**：产品名、项目根路径、右上角状态徽标（未启动 / 就绪 / 启动中 / 运行中 / 异常退出）
2. **环境自检**：6 行检查结果，每行「圆点 + 名称 + 详情」，失败时附「处理建议」
3. **运行日志**：子进程输出实时滚动，错误行红色、警告行黄色，右上角显示日志文件路径
4. **状态行 + 按钮**：启动工作室 / 停止 / 重启 ｜ 选择项目目录 / 打开目录 / 查看日志 / 收进托盘 / 退出

---

## 3. 环境自检的 6 项

| 检查项 | 判定方式 | 失败后的处理 |
|---|---|---|
| 项目根目录 | 从 exe 同目录、上一级、上两级、当前工作目录、配置、`--root` 依次找含 `app\desktop.py` 的目录 | 弹「选择项目目录」，选中后写入配置并立即重查 |
| 核心文件 | `app\desktop.py`、`app\server.py`、`app\web\index.html`、`app\engines\base.py` 是否都在 | 列出缺哪些；提示可能是没下载完整或被杀软隔离 |
| 启动脚本 | `launcher.json` 的 `script` 是否存在 | 提示检查配置字段 |
| Python 解释器 | 按上文顺序探测；外来解释器再做依赖验证 | 提示安装依赖或手工指定 |
| 读写权限 | 试写 `voices/`、`outputs/`、`logs/` | 非致命：日志自动改写到 `%LOCALAPPDATA%\BreezeVoiceStudio\logs` |
| 运行实例 | 读 `logs\instance.json` 并访问一次 `/api/status` | 检测到就说明「本次只开新窗口、不重复加载模型」 |

---

## 4. 退出行为

退出分两种语义，界面上是两个不同的入口，不要混为一谈：

### 4.1 普通关闭（收进托盘）

| 触发方式 | 行为 |
|---|---|
| 点窗口 X / 点最小化按钮（勾选「最小化后收进系统托盘」时） | 窗口隐藏，**启动器、工作室、后端、已加载的模型全部继续运行**，显存不释放 |
| 点「收进托盘」按钮 | 同上，不经过任何询问 |
| 托盘图标双击 / 右键「打开主窗口」 | 窗口恢复显示，状态行从「已收进托盘」切回运行态 |

### 4.2 完全退出并关闭

| 触发方式 | 行为 |
|---|---|
| 点界面右下角「完全退出」 | 若有工作室在跑，先弹确认框；确认后结束全部相关进程再关启动器 |
| 托盘图标右键「退出（完全退出）」 | 有工作室在跑时先确认一次，然后同上 |
| 点窗口 X（**未**勾选托盘设置时） | 等同于「完全退出」 |

清理范围（这就是它与普通关闭的区别）：

| 对象 | 普通关闭 | 完全退出 |
|---|---|---|
| 启动器窗口 | 隐藏 | 关闭 |
| 启动器进程 | 保留 | 结束 |
| 启动器拉起的子进程 | 保留 | 结束（`terminate` → 8 秒 → `taskkill /T /F`） |
| **其他入口**启动的后端（`启动.bat` / exe 复用到的实例） | 保留 | 结束（先校验是 python 进程再杀，避免 PID 复用误杀） |
| 命令行里带本项目 `desktop.py` 的残留进程 | 保留 | 结束（PowerShell CIM 扫描兜底） |
| 托盘图标 | 保留 | 移除 |
| `logs\instance.json` | 保留 | 删除 |
| 端口 8765 / 显存 | 占用 | 释放（等待端口真正释放，最长 6 秒） |

实现要点：`launcher/core.py` 的 `terminate_app_processes()` 汇集三类来源（子进程 PID、`instance.json` 记录的 PID、扫描结果），去重后逐个用 `QueryFullProcessImageNameW` 确认是 `python/pythonw` 再动手，最后把结果写进日志（「已结束进程：PID x」「端口已释放，无后台残留」）。即使 worker 卡住，也有 15 秒兜底关窗 + Job Object 收尸。

> 注意：`keep_running_on_exit: true` 只影响**启动器自己**的正常退出路径；点「完全退出」时该选项被忽略——这个按钮的语义就是「一个都不留」。

---

## 4.3 托盘设置

状态行右侧有复选框 **「最小化后收进系统托盘」**，勾选状态即时写入 `launcher.json` 的 `minimize_to_tray`：

- 勾选（默认）：点 X 或最小化 → 收进托盘，服务继续跑；
- 取消：点 X 直接询问「完全退出」，不驻留托盘；
- 托盘组件缺失（`pystray` / `Pillow` 未装）时自动降级为最小化到任务栏，并在日志里说明原因，不会导致启动失败。

托盘右键菜单：**打开主窗口**（默认项，双击图标同效）、打开工作室界面、重启工作室、查看日志、打开项目目录、**退出（完全退出）**。

---

## 5. 构建（双击即可）

前提：项目虚拟环境已就绪（`vendor\index-tts\.venv` 里有 `python.exe`）。

**双击项目根目录的 `构建启动器.bat`**，脚本会依次完成：

1. 定位解释器，检查 `app\desktop.py` 是否存在；
2. 缺 `PyInstaller` / `pystray` / `pillow` 时自动安装（先清华镜像，失败回退默认源）；
3. 生成图标 `launcher\assets\app.ico`；
4. 清理 `build\launcher` 与旧 exe；
5. 用 PyInstaller 打单文件、无控制台、带版本信息与图标的 exe；
6. 把产物复制到项目根目录，双击即用。

等价的命令行（想自己调参数时用）：

```bat
vendor\index-tts\.venv\Scripts\python.exe launcher\build.py
vendor\index-tts\.venv\Scripts\python.exe launcher\build.py --skip-deps
vendor\index-tts\.venv\Scripts\python.exe launcher\build.py --onedir
```

`构建启动器.bat` 只做两件事：找到解释器、转发给 `launcher/build.py`。**全部构建逻辑与中文输出都在 Python 里**，原因很实际：cmd.exe 在 UTF-8 代码页下解析带中文的长命令行（尤其 `^` 续行）会错位，把参数拦腰截断。把 bat 写成仅含 ASCII 的转发壳，可以彻底绕开这一类问题，也让构建逻辑可读、可测、可加参数。

`build.py` 内部的 PyInstaller 参数同样值得注意：必须带 `--paths launcher`，否则分析阶段找不到 `core.py`，打出来的 exe 一启动就 `ModuleNotFoundError: No module named 'core'`。

产物：`dist\BreezeVoiceStudio.exe`（并复制一份到项目根）。onefile 模式启动时会解压到 `%TEMP%\_MEIxxxx`，因此**系统临时目录必须可写**；如果 `%TEMP%` 被安全策略锁死，改用 `--onedir` 打包即可。

启动器本身只需要标准库 + `tkinter` + 可选的 `pystray/Pillow`，与目标程序的 torch 依赖完全隔离，所以打包体积约 20–30 MB，且**不会**把 torch 打进去。

---

## 6. 配置：`launcher.json`

首次运行会在 exe 同目录生成；该目录不可写时落到 `%LOCALAPPDATA%\BreezeVoiceStudio\launcher.json`。

```json
{
  "project_root": "",
  "python_exe": "",
  "script": "app/desktop.py",
  "args": [],
  "env": {
    "HF_ENDPOINT": "https://hf-mirror.com",
    "TOKENIZERS_PARALLELISM": "false",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1"
  },
  "keep_running_on_exit": false,
  "autostart": true,
  "start_minimized": false,
  "minimize_to_tray": true,
  "window_width": 780,
  "window_height": 560
}
```

| 字段 | 作用 |
|---|---|
| `project_root` | 手工锁定项目根；留空则自动探测 |
| `python_exe` | 手工锁定解释器；留空则自动探测 |
| `script` | 启动哪个脚本，默认 `app/desktop.py` |
| `args` | 追加的命令行参数（`desktop.py` 目前不解析 argv，留给将来扩展） |
| `env` | 追加/覆盖环境变量 |
| `autostart` | 打开启动器后是否自动拉起工作室（默认是） |
| `start_minimized` | 启动成功后是否自动收进托盘 |
| `keep_running_on_exit` | 启动器**普通退出**时是否保留工作室进程（「完全退出」不受它影响） |
| `minimize_to_tray` | 最小化/关闭窗口时是否收进系统托盘；界面上的复选框改的就是它 |

---

## 7. 与原有启动方式共存

### 7.1 原有入口完全没动

| 原有入口 | 现状 |
|---|---|
| `启动.bat` | 内容一字未改，仍是 `pythonw app\desktop.py` |
| `启动-调试.bat` | 内容一字未改，仍带 `BREEZE_DEBUG=1` 与控制台 |
| `python app\desktop.py` | 仍可直接运行 |
| `app\` 下的业务代码 | 未修改；只有 `desktop.py` 的实例探测函数换了一种判断方式，见下方说明 |

启动器是**新目录 `launcher\` + 一个新 bat + 一个 exe**，属于纯增量，删掉 `launcher\` 就回到原样。

**关于 `desktop.py` 的那一处修改（`running_instance_port()`）**

原实现用 `urllib` 请求 `127.0.0.1:{port}/api/status` 来判断「是否已有实例」。实测发现它在两种情况下会失败：

1. 系统设置了 `HTTP_PROXY`（本机就存在，指向本地转发端口），回环请求被代理，必然超时；
2. 已有实例正在加载模型（20–40 秒），后端被 GIL 拖慢，1.5 秒的 HTTP 超时不够用。

两者都会让第二个实例误判「没有实例」，于是再起一个后端、重复加载模型——8 GB 显存直接吃紧，窗口互相抢。

修改只把判断方式从「HTTP 请求成功」换成「TCP 能连上」（端口在本进程写入 `instance.json` 之前就已开始监听，握手由内核完成），**行为语义不变**：有实例就复用，没有就新建。这个函数不被任何入口直接调用，因此对三种启动方式的表现没有影响——只是让「复用」在代理环境和加载期间也能真正生效。

### 7.2 三条路径最终汇到同一个程序

```
启动.bat ─────────┐
启动-调试.bat ────┼──▶ app\desktop.py ──▶ app\server.py ──▶ engines/
BreezeVoiceStudio.exe ─┘        （后端、音色库、模型全部只有一份）
```

差别只在「谁拉起 `desktop.py`」和「有没有一个窗口展示它的输出」。业务逻辑没有第二份实现，所以不存在「exe 版和行为不一致」的问题。

### 7.3 依赖方向是单向的

```
launcher/  ──依赖──▶  app/desktop.py（作为子进程，不 import）
app/       ──不依赖──▶ launcher/
```

启动器**不 import** `app` 里的任何模块，只用 `subprocess` 把它当黑盒启动。因此：

- 改 `app/` 的内部实现，不会让启动器编译失败；
- 删掉 `launcher/`，`app/` 毫发无损。

### 7.4 共享状态只有两处，且都是只读或已被 `desktop.py` 管好

| 文件 | 谁写 | 启动器怎么用 |
|---|---|---|
| `logs\instance.json` | `desktop.py` | **只读**，用来判断「是否已有实例」「端口是多少」 |
| `config.json` | `EngineManager`（经 `app/server.py`） | 不碰 |
| `voices\` / `outputs\` | `app/voice_store.py`、合成流程 | 不碰，只做可写性探测 |

启动器自己的写操作只有两个：`logs\launcher.log`（2 MB 轮转）和 `launcher.json`。

### 7.5 同时启动会不会打架？

| 场景 | 结果 |
|---|---|
| 先双击 `启动.bat`，再双击 exe | exe 的自检显示「已在 http://127.0.0.1:8765 运行」，`desktop.py` 走复用分支**只开一个新窗口**，不重复加载模型、不翻倍占显存 |
| 先双击 exe，再双击 `启动.bat` | 同理，`启动.bat` 起的进程检测到 `instance.json` 后只开窗口 |
| 两个都关掉其中一个窗口 | 只关窗口，后端仍在（复用分支不持有 server 对象），另一个窗口照常可用 |
| 关闭启动器 | 只终止**它自己创建的那个子进程**；由 `启动.bat` 起的后端不受影响 |
| 启动器在，工作室在，再双击 exe | 单实例互斥体拦截，把已有启动器窗口提到前台，不开第二个 |

关键点：Job Object 绑定的是**启动器自己 `Popen` 出来的 PID**，不会去枚举或杀掉别的 python 进程。所以「关启动器」和「关 bat 起的实例」是两件互不干扰的事。

### 7.6 已知的语义边界（说清楚，不藏着）

1. 如果启动器检测到「已有实例」并走了复用分支，那么它托管的那个进程**只是窗口进程**。此时点「停止」只会关掉这个窗口，原有后端继续跑——这是 `desktop.py` 复用分支的既定行为，启动器会把这句提示打进日志。
2. 启动器不接管「已经由 bat 启动的进程」。它对那些进程没有句柄，也不该有。
3. `keep_running_on_exit: false` 是默认值，意味着「关启动器 = 关它自己起的那个工作室」。要后台常驻，请用 `true`，或干脆用 `启动.bat`。

---

## 8. 后续开发规范（避免影响任一入口）

1. **依赖只能单向**：`launcher/` 可以调用 `app/`，`app/` 永不 import `launcher/`。给 `app/` 加功能时不要为了「方便启动器」而在 `app/` 里引入 GUI 启动相关代码。
2. **业务逻辑只放 `app/`**：新增模型、接口、音色处理，一律进 `app/`；`launcher/` 只允许增加「入口能力」（自检项、按钮、托盘菜单项）。
3. **不复制已有逻辑**：端口扫描、单实例判断、显存释放都已存在于 `desktop.py`，启动器只调用不重写。发现需要重复实现时，先考虑把这些能力下沉到 `app/`。
4. **新增必填文件 → 同步 `REQUIRED_FILES`**：`launcher/core.py` 顶部那张表决定自检项，漏改会导致「检查通过但启动就崩」。
5. **新增第三方依赖 → 装进 `vendor/index-tts/.venv`**：两种入口共用同一个解释器，装错环境会造成「bat 能用、exe 不能用」。
6. **`launcher/` 内部模块必须双模式导入**：源码跑（`python launcher/main.py`）和打包跑都要求可用，所以每个模块都用 `try: from . import core / except ImportError: import core` 的写法，不要只写相对导入。
7. **界面线程不做阻塞操作**：子进程启动、停止、网络探测、依赖验证一律丢到后台线程，结果经 `queue` 回到主线程。之前「点启动卡 3 秒」就是反例。
8. **改 `desktop.py` 的启动约定要同步三处**：`launcher.json` 的 `script`/`args` 说明、本 README 第 1 节、`launcher/core.py` 的 `REQUIRED_FILES`。
9. **发版前跑一遍自检**：`python launcher/main.py --check`，以及 `--run-for 8 --script <冒烟脚本>` 验证托管链路。
10. **版本号同步**：`launcher/__init__.py` 的 `__version__` 与 `launcher/version_info.txt` 的 `filevers/prodvers` 一起改。
11. **打包产物不进 `app/`**：exe 放项目根或 `dist/`，两者都会被根目录探测识别（会自动向上找一层）。

---

## 9. 验证点清单

| # | 验证项 | 操作 | 期望结果 |
|---|---|---|---|
| 1 | 原有入口未受影响 | 双击 `启动.bat` | 与改动前完全一致，模型照常加载 |
| 2 | 新入口可用 | 双击 `BreezeVoiceStudio.exe` | 出现启动器窗口，6 项自检全绿，自动拉起工作室 |
| 3 | 共存（先 bat 后 exe） | 先 `启动.bat`，模型就绪后再双击 exe | 自检显示「已在 8765 运行」，只新开一个窗口；显存占用不翻倍 |
| 4 | 共存（先 exe 后 bat） | 反过来操作 | `启动.bat` 打开的也是新窗口，不重复加载 |
| 5 | 停止清理 | 在启动器点「停止」 | 子进程退出，`logs\instance.json` 清除，8765 端口释放 |
| 6 | 强杀不残留 | 任务管理器结束启动器进程 | 工作室窗口一并消失，端口释放（Job Object 生效） |
| 7 | 普通关闭=收进托盘 | 勾选「最小化后收进系统托盘」后点窗口 X 或最小化 | 窗口隐藏、启动器与工作室进程仍在，托盘图标出现；右键「打开主窗口」可恢复 |
| 7.1 | 完全退出（子进程） | 点「完全退出」→ 确认 | 日志出现「已结束进程：PID x」「端口已释放」，无 python 进程残留 |
| 7.2 | 完全退出（复用后端） | 先 `启动.bat` 起后端，再开 exe 并点「完全退出」 | 由 `启动.bat` 起的后端一并结束，8765 释放，`instance.json` 删除 |
| 7.3 | 托盘退出 | 托盘右键「退出（完全退出）」 | 有工作室时先确认，确认后进程全部退出 |
| 8 | 缺失文件提示 | 把 `script` 改成不存在的路径 | 自检「启动脚本」标红，启动按钮禁用，给出处理建议 |
| 9 | 项目目录错误 | 把 exe 拷到别的目录双击 | 提示找不到项目，提供「选择项目目录…」，选中后立即恢复 |
| 10 | 只读目录降级 | 项目目录设为只读后启动 | 日志改写 `%LOCALAPPDATA%\BreezeVoiceStudio\logs`，启动不中断 |
| 11 | 自带文件完整 | `python launcher/main.py --check` | 全部 `[OK]`，退出码 0 |
| 12 | 构建可复现 | 双击 `构建启动器.bat` | 生成 `BreezeVoiceStudio.exe`，双击后与第 2 条一致 |
| 13 | 代理环境不重复加载 | 在设置了 `HTTP_PROXY` 的机器上，先 `启动.bat`，趁模型还在加载时再双击 exe | 仍复用同一个后端，日志显示「复用模式」，显存占用不翻倍 |
| 14 | 加载期也能识别 | 模型加载到一半时看启动器状态 | 3–5 秒内显示「运行中」，不会一直卡在「启动中」 |

---

## 10. 目录与文件

```
launcher\
├─ main.py            入口：DPI、单实例、命令行自检、崩溃兜底
├─ core.py            路径/解释器探测、配置、Job Object、子进程托管、日志
├─ ui.py              tkinter 界面（自检列表、日志、按钮状态机）
├─ tray.py            托盘封装（可选依赖，失败自动降级）
├─ build.py           构建逻辑（安装依赖、生成图标、调 PyInstaller、复制产物）
├─ make_icon.py       生成 assets\app.ico
├─ version_info.txt   exe 版本信息
└─ assets\app.ico     图标
构建启动器.bat        双击构建（仅转发给 build.py）
BreezeVoiceStudio.exe 构建产物（可直接双击）
```

命令行自检（排障用，不开界面）：

```bat
vendor\index-tts\.venv\Scripts\python.exe launcher\main.py --check
vendor\index-tts\.venv\Scripts\python.exe launcher\main.py --check --json
vendor\index-tts\.venv\Scripts\python.exe launcher\main.py --run-for 8 --script tmp\_smoke_target.py
```

其它参数：`--root <目录>` 指定项目根、`--script <相对路径>` 覆盖启动脚本、`--no-autostart` 只开窗口不启动、`--debug` 打开 pywebview 调试、`--allow-multi` 跳过单实例检查。
