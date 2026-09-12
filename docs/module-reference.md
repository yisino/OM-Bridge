# 模块参考（代码说明）

> 逐模块的职责、关键 API 与契约。设计取舍见 [architecture.md](architecture.md) 与 [design/](design/)；
> 本文只讲"这块代码是什么、怎么用、边界在哪"。

## 目录

- [core.errors —— 错误与退出码](#coreerrors)
- [core.models —— 数据契约](#coremodel)
- [core.schema —— 参数单一声明](#coreschema)
- [core.assets —— 资产就绪度](#coreassets)
- [core.config —— 配置分层](#coreconfig)
- [core.backend —— Backend 抽象](#corebackend)
- [core.solution —— Solution 抽象](#coresolution)
- [core.registry —— 发现与解析](#coreregistry)
- [core.session —— 编排](#coresession)
- [core.logging —— 日志](#corelogging)
- [backends.comfyui.* —— ComfyUI 后端](#backendscomfyui)
- [backends.mock.* —— 离线模拟后端](#backendsmock)
- [solutions.minimax_h3.* —— H3 方案](#solutionsminimax_h3)
- [solutions.echo —— 演示方案](#solutionsecho)
- [interfaces.* —— 交付面](#interfaces)
- [配置项总表](#配置项总表)

---

## core.errors

**职责**：把"失败"表达成**有分类、有退出码、能给建议**的对象，而不是裸 `Exception`。

`OmBridgeError` 基类携带三个额外字段：

| 字段 | 语义 | 谁在用 |
|---|---|---|
| `exit_code` | 进程退出码（CLI 直接返回它） | Shell / CI |
| `stage` | 失败阶段（`config`/`registry`/`validate`/`upload`/`submit`/`poll`/`fetch`） | 日志与 Agent 判断"卡在哪" |
| `as_dict()` | 结构化输出 | CLI `--json` / MCP |

退出码分配（**约定，不要随意改动，脚本会依赖**）：

| 码 | 常量 | 含义 |
|---|---|---|
| 0 | `EXIT_OK` | 成功 |
| 1 | `EXIT_USAGE` | 命令行/入参错误（用户改一下就能过） |
| 2 | `EXIT_UNREACHABLE` | 后端不可达 |
| 3 | `EXIT_BAD_WORKFLOW` | 载荷/计算图非法（提交前就能判定） |
| 4 | `EXIT_REJECTED` | 后端拒绝受理（`POST /prompt` 返回错误） |
| 5 | `EXIT_TIMEOUT` | 超时 —— **可恢复**，应 `resume` 而非重提 |
| 6 | `EXIT_RENDER` | 渲染过程失败（后端内部出错） |
| 7 | `EXIT_CONFIG` | 配置错误（缺失/非法） |

异常族：`ConfigError`(`ConfigMissingError`) ⊂ 、`RegistryError`(`UnknownBackendError`/`UnknownSolutionError`)、
`BackendError`(`BackendUnavailableError`/`JobRejectedError`/`UploadError`)、
`ValidationError`（带 `issues` 列表）、`JobTimeoutError`（带 `recoverable=True` 与 `job_id`）、`RenderFailedError`。

> **为什么超时要单独一个类并且带 `job_id`？** 因为它是唯一"正确应对方式不是重试"的失败。
> 重提会向 ComfyUI 队列塞入第二个渲染任务，两个任务抢同一块 GPU，结果两边都变慢。
> 把 `job_id` 挂在异常上，调用方才有办法回到"续等"这条路。

---

## core.models

**职责**：定义跨层传递的全部数据结构。**唯一**被所有层 import 的模块。

所有结构都提供 `to_dict()`，可直接 `json.dumps`。`Severity`（`INFO`/`WARNING`/`ERROR`）用于 `Issue`；
`blocking_issues()` 是唯一判断"这批问题是否阻断提交"的地方（**不要在别处自行判断 severity**）。

`JobSpec.fingerprint()` 对载荷做稳定哈希，用于日志与"是否同一次作业"的比对（**不用于缓存**，见 architecture §11）。

`ProbeReport` 里有两个容易混淆的字段，务必分清：

| 字段 | 语义 | 是否进 `to_dict()` |
|---|---|---|
| `nodes: dict[str, bool]` | **关键节点清单**（当前后端的核心节点是否存在） | 是（简短） |
| `node_classes: set[str]` | **全量节点类清单**（`/object_info` 的所有键） | **否**（1261 项，进 JSON 会淹没有效信息） |

问"某个节点类在不在"必须用 `probe.has_node_class(cls)`，**不要**查 `probe.nodes`。
（历史上这里出过一次误报：H3 节点实际存在，却因为只查了"关键节点清单"而被判缺失。）

---

## core.schema

**职责**：参数的**单一声明处**。`ParamSpec` 描述单个参数，`ParamSchema` 描述一组。

```python
ParamSpec(
    name="width", type=TYPE_INT,
    default=864, minimum=32, maximum=1344, multiple_of=32,
    applies_to=("t2v", "i2v", "flf2v", "ref2v"),   # 只在该模式下出现
    description="输出宽度，32 的倍数。",
)
```

三类消费方（全部自动派生，**不要手写第二份**）：

| 方法 | 产出 | 消费方 |
|---|---|---|
| `to_cli_help()` | 帮助文本（含默认值与约束） | `interfaces/cli.py` |
| `to_json_schema()` | JSON Schema | `interfaces/mcp.py` 的 `inputSchema` |
| `validate()` | `Issue` 列表 | 调用前校验 |

`ParamSchema.resolve(values, mode, default_overrides=...)` 的语义要注意：
**优先级是 `default_overrides`（来自配置）> 内置 default**，即"部署机上配置的值"会覆盖代码里的默认。
同时它会**丢掉与当前模式无关的键**（如给 t2v 传 `ref_image_size` 会被丢弃并产生告警）。

> **为什么默认值要有"配置优先"这一层？** 因为"这台机器该用哪个权重、哪个分辨率"是**部署事实**，
> 不是代码常量。如果代码默认值优先级更高，用户改了 `.env` 却不生效，只能去改代码——
> 这正是这类项目最常见的可用性灾难。

---

## core.assets

**职责**：声明式描述"某个模式需要哪些模型/节点"，并对照 `ProbeReport` 算出就绪度。

```python
AssetRequirement(role="diffusion_model", category=COMFYUI_UNET,
                 name="minimax_h3_..._int8_convrot.safetensors",
                 modes=("t2v", "i2v"), required=True, source_url="...")
```

- `NODE_CATEGORY = "node"`：**节点存在性也用资产来表达**（见 architecture §5）。
- `evaluate_assets(reqs, probe)` → `list[AssetStatus]`，每项带 `present` 与 `declared`
  （`declared=False` 表示"后端没报告这一类"，与"报告了但里面没有"是不同情况，不要混为一谈）。
- `readiness_by_mode(reqs, probe)` → `{mode: {"ready": bool, "missing": [...]}}`，**按模式分别报告**。
- `asset_issues(reqs, probe, mode, ...)` → 转成 `Issue` 列表。

> 区分 `present` 与 `declared` 的价值：当探针没能取到清单（后端部分不可用）时，
> 结论应该是"无法判定"而不是"缺资产"。把它们混起来会让人去下载本来就有的权重。

---

## core.config

**职责**：把"环境变量 / `.env` / 默认值"合成一个可自省、可解释的配置对象。

- `Setting(key, env, default, type, description, aliases, secret, deprecated_alias)`：一个配置项的定义。
  `key`（如 `backend.comfyui.server_url`）是**程序内名字**，`env` 是**环境变量名**，两者分开是为了
  让程序内引用稳定、而部署侧可以改名。
- `parse_env_file(path)`：自带解析器（引号、行内注释、`export` 前缀、空值），无第三方依赖。
- `Config` 的关键方法：

| 方法 | 用途 |
|---|---|
| `.get(key, default=None)` / `.require(key)` | 取标量（`require` 缺失时抛 `ConfigMissingError`） |
| `.get_bool(key)` / 类型自动转换 | 由 `Setting.type` 驱动 |
| `.backend(name)` / `.solution(key)` | 取该命名空间下的全部键（返回 `{短名: 值}`） |
| `.workspace` / `.output_dir()` / `.resolve_path(p)` | 路径统一（相对路径按 workspace 展开） |
| `.explain(key)` | **该键最终值的来源**（内置/文件/环境/override） |
| `.report()` | 全量报告，含"环境里存在但未登记"的变量 |

`.report()` 的"未登记变量"那部分是排障利器：它能告诉你"你写的那个变量名根本没被程序读取"
（即 no-op 变量），而不是让你困惑为什么改了不生效。

---

## core.backend

`Backend` 抽象基类。类属性：`name`、`display_name`、`capabilities: set[Capability]`、`description`。

`Capability` = `VIDEO` / `IMAGE` / `AUDIO` / `MEDIA_UPLOAD` / `RAW_JOB` / `RESUME`。

必须实现：

| 方法 | 契约 |
|---|---|
| `probe(refresh=False) -> ProbeReport` | **失败也要返回报告**（`reachable=False` + `errors`），不要抛异常 |
| `submit(job: JobSpec) -> JobHandle` | 受理即返回；被拒抛 `JobRejectedError` |
| `poll(handle) -> JobStatus` | 一次非阻塞查询，返回 `JobState` + 可选 `progress` |
| `fetch(status) -> list[Artifact]` | 取回产物到本地 |

可选实现：`upload(path, kind) -> str`、`preflight(job, probe) -> list[Issue]`、
`resume(job_id, timeout) -> JobStatus`、`close()`。

> `probe()` "失败也返回报告"这条是刻意的：调用方需要的不是异常，而是
> "不可达 + 原因"，这样才能打印出可执行的建议。抛异常的探针只会让上层到处写 try。

---

## core.solution

`Solution` 抽象基类。类属性：`key`、`backend_name`、`kind`（video/image/audio）、
`modes`、`default_mode`、`schema`、`assets`、`media_slots`。

必须实现 `build_job(request, backend, *, resolved_params, media, config=None) -> JobSpec`
（可选返回 `(payload, output_selector)` 形式的 `JobSpec`）。

其余方法都有合理默认实现，但通常需要覆写：`resolve_assets(config)`、`config_defaults(config, mode)`、
`default_media(config, mode)`、`validate_params(params, mode)`、`validate_media(media, mode)`、
`provenance(...)`、`describe()`。

`MediaSlot(role, kind, required, multiple, max_count, modes)` 描述方案能接受哪些素材槽位。
**槽位顺序有意义** —— 例如 H3 的参考图顺序决定提示词里 `<Picture N>` 指哪一张。

---

## core.registry

| 方法 | 说明 |
|---|---|
| `discover()` | 扫描内置包 + entry points，构建索引（幂等） |
| `backend(name)` | 返回**类**；不存在抛 `UnknownBackendError` |
| `solution(key)` | 返回**类**；不存在抛 `UnknownSolutionError` |
| `instance(key)` | 返回已实例化对象（需要 config 的场合） |
| `resolve(name) -> (solution_instance, preset)` | 解析别名：`"minimax_h3.ref2v"` → 实例 + `{"mode": "ref2v"}` |
| `create_backend(name)` | 按配置实例化后端 |
| `describe()` | 全部后端与方案的元信息（供 `list` / `describe`） |

`get_registry()` 返回模块级单例。**发现是惰性且幂等的**，但别在热路径里反复构造新 `Registry`。

---

## core.session

唯一编排者。方法：

| 方法 | 是否联网 | 是否抛异常 | 用途 |
|---|---|---|---|
| `probe()` / `probe_report()` | 是 | 否 | 能力盘点 |
| `readiness(mode)` | 是 | 否 | 某模式就绪度 |
| `prepare(request)` | 视 `preflight` 而定 | **是**（有阻断项时） | 生成前的完整准备 |
| `validate(request)` | 视 `preflight` 而定 | **否（保证）** | 一次列全部问题 |
| `submit(prepared)` | 是 | 是 | 只提交 |
| `wait(handle, timeout=None)` | 是 | 超时抛 `JobTimeoutError` | 只等待 |
| `collect(handle, status=None)` | 是 | 是 | 只取件 + 落盘 |
| `run(request)` | 是 | 是 | 全流程（prepare→submit→wait→collect） |
| `resume(job_id, timeout=None)` | 是 | 是 | **续等**一个超时作业 |

`run()` 的等价展开（想在中间插入自定义逻辑时照这个写）：

```python
with Session(config) as session:
    prepared = session.prepare(request)      # 校验失败在这里抛
    handle   = session.submit(prepared)      # 记下 handle.job_id —— 超时后要用它 resume
    status   = session.wait(handle)
    result   = session.collect(handle, status)   # 落盘并返回 GenerationResult
```

---

## core.logging

两个通道，**刻意分开**：

| 函数 | 面向 | 输出 | 受日志级别影响 |
|---|---|---|---|
| `progress(msg)` | 人 | stderr | **否**（总是输出） |
| `diagnostic(logger, msg, ...)` | 排查者 | stderr | 是 |

`configure_logging(level)` 强制 `propagate=False` 且 handler 指向 stderr。

**约束**：任何写 stdout 的日志都会破坏 MCP 的 stdio 协议。新增代码请用这两个函数，**别用 `print()`**。

---

## backends.comfyui

| 模块 | 职责 | 关键点 |
|---|---|---|
| `client.py` | HTTP 客户端（`urllib`，无第三方依赖） | `system_stats`/`object_info`/`queue`/`history`/`submit_prompt`/`view_bytes`/`upload_image`；上传遇 **HTTP 409 重名**自动改用内容哈希名重试 |
| `graph.py` | 图操作 | `load_graph`/`apply_overrides`（严格）/`autogrow_slot_key`/`autogrow_slots`/`bind_media_slots`/`collect_outputs`/`extract_errors`/`is_completed` |
| `inventory.py` | 能力盘点 | `Inventory.from_object_info()`；`combo_values()` 处理 `COMBO` 与 `COMFY_DYNAMICCOMBO_V3` |
| `validation.py` | 载荷静态校验 | `validate_graph`、`resolve_dynamic_input`（`COMFY_AUTOGROW_V3`）、`validate_output_selector` |
| `backend.py` | `ComfyUIBackend` | URL 优先级、探针、提交/轮询/取件、上传、预检；`CACHE_SUSPECT_SECONDS = 2.0` |

三个必须知道的实现事实：

1. **`SaveVideo` 的产物在 `/history` 里挂在 `images` 键下**（还带 `"animated": [true]`），
   **不是 `video` 键**。按 `kind == "video"` 筛会漏掉产物。`collect_outputs()` 已处理。
2. **整图缓存**：提交与上次**逐字节相同**的载荷会秒回（< 2s）并复用原文件名。
   `poll()` 在耗时 < `CACHE_SUSPECT_SECONDS` 时会打缓存疑点日志 —— 看到这条就该换 `seed` 重测。
3. **`COMFY_AUTOGROW_V3` 的动态槽位在 API 格式里是"点号键"**：
   `ref_images.ref_image_0` … `ref_images.ref_image_8`。依据是
   `comfy_api/latest/_io.py` 的 `finalize_prefix()`（用 `.` 连接父名与 `prefix+index`）。
   `/object_info` **只暴露父名**，看不到槽位 —— 别试图从那里推断。写错槽位名会导致
   `POST /prompt` **直接 400**（ComfyUI 对未知输入是报错，不是忽略）。

---

## solutions.minimax_h3

| 模块 | 职责 |
|---|---|
| `manifest.py` | 常量与声明：`MODES`（`ModeSpec`）、`ASSET_DEFAULTS`/`ASSET_ALTERNATIVES`、栅格约束、`asset_requirements()` |
| `builder.py` | `build_h3_graph(...) -> (graph, output_node_id)`；输入合法性校验 |
| `solution.py` | `MinimaxH3Solution`：schema / media_slots / config_defaults / build_job / provenance / describe |
| `templates/*.json` | **5 份已核对的计算图**（t2v/i2v/flf2v/ref2v/ref2v_2ref），用作结构回归基准 |

四种模式与关键差异见 [solutions/minimax-h3.md](solutions/minimax-h3.md)。
**回归护栏**：`tests/test_builder_regression.py` 会把 `builder` 的产出与这 5 份模板逐节点比对。
改动 `builder.py` 后必须让这个测试保持通过，否则说明你无意中改变了已验证的行为。

---

## backends.mock

**职责**：离线模拟后端 —— 双层契约的**第二实现**（检验 [ADR-0001](design/0001-two-layer-backend-solution.md)
的抽象是否真的与 ComfyUI 无关），同时是"新增后端"的可运行参考样例与 CI 的离线端到端载体。

| 行为 | 约定 |
|---|---|
| `probe()` | 恒 `reachable=True`（不依赖外部服务）；节点/资产清单留空 = "未报告"而非"缺失" |
| `upload()` | 复制进 `<workspace>/var/mock-uploads/`，远端名 = 文件名 |
| `submit()` | 同步形态：受理即完成；`job_id = mock-<载荷指纹前12位>`（同载荷同 id） |
| `poll()` | 立即 `SUCCEEDED`；**未知 job_id 返回 FAILED**（mock 台账不跨进程，绝不让 wait 挂死） |
| `fetch()` | 写出 `<job_id>.transcript.json`（提示词、最终参数、媒体远端名、完整载荷） |

能力声明全量（video/image/audio/media_upload/raw_job/resume）。**没有配置项** ——
顺便证明后端可以零配置接入。

---

## solutions.echo

绑定 `mock` 的最小合法方案（单文件，无模式、无资产 —— 与 H3 互补覆盖注册分支）。
参数 `frames` / `case` / `simulate_failure` 与失败路径演练见
[solutions/echo.md](solutions/echo.md)。
测试：`tests/test_mock_backend.py`（Session 全路径）与
`tests/test_cli.py::test_mock_backend_end_to_end_via_cli`（CLI 交付面）。

---

## interfaces

| 模块 | 入口 | 说明 |
|---|---|---|
| `cli.py` | `om-bridge` | 10 个子命令；选项由注册表与 `ParamSchema` 动态生成 |
| `mcp.py` | `om-bridge-mcp` | stdio JSON-RPC；7 个工具 |
| `__main__.py` | `python -m om_bridge` | 等价于 CLI；也支持直接执行文件（自动把 `src` 加进 `sys.path`） |

CLI 子命令：`generate`、`validate`、`graph`、`run`、`resume`、`probe`、`list`、`describe`、
`config {list,explain,report}`、`doctor`。

MCP 工具：`om_bridge_list`、`om_bridge_describe`、`om_bridge_check`、`om_bridge_validate`、
`om_bridge_generate_video`、`om_bridge_run_workflow`、`om_bridge_resume`。

**`graph` 与 `generate` 的区别**：`graph` 只把载荷物化成 JSON 文件（**离线**，不探测后端），
给"只认文件路径 + 节点 ID"的外部框架用；`generate` 走完整流程并真的渲染。

---

## 配置项总表

规范名以 `OM_BRIDGE_` 前缀，括号内为该键的**旧名别名**（等同优先级）。

### global

| 键 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `global.default_backend` | `OM_BRIDGE_DEFAULT_BACKEND` | `comfyui` | 默认后端名 |
| `global.default_solution` | `OM_BRIDGE_DEFAULT_SOLUTION` | `minimax_h3` | 默认方案键 |
| `global.workspace` | `OM_BRIDGE_WORKSPACE` | `.` | 工作区根目录 |
| `global.output_dir` | `OM_BRIDGE_OUTPUT_DIR` | 空 → `<ws>/var/output` | 产物落盘目录 |
| `global.connect_timeout` | `OM_BRIDGE_CONNECT_TIMEOUT` | `15` | 连接/轻量探测超时（秒） |
| `global.timeout` | `OM_BRIDGE_TIMEOUT` | `900` | 作业总超时（秒）。**超时≠失败** |
| `global.poll_interval` | `OM_BRIDGE_POLL_INTERVAL` | `5` | 轮询间隔（秒），被后端专属值覆盖 |
| `global.validate` | `OM_BRIDGE_VALIDATE` | `true` | 提交前静态校验 |
| `global.strict` | `OM_BRIDGE_STRICT` | `false` | 告警也视为阻断。`validate` 报告与 `generate` 放行共用同一条判定（CI 用） |
| `global.log_level` | `OM_BRIDGE_LOG_LEVEL` | `INFO` | 日志级别。⚠ 只认进程环境变量（日志先于 `.env` 初始化），写在 `.env` 里不生效 |
| `global.env_file` | `OM_BRIDGE_ENV_FILE` | 空 | 显式指定配置文件 |

### backend.comfyui

| 键 | 环境变量（旧名） | 默认 | 说明 |
|---|---|---|---|
| `backend.comfyui.server_url` | `OM_BRIDGE_COMFYUI_SERVER_URL` (`COMFYUI_SERVER_URL`) | `http://localhost:8188` | 基地址 |
| `backend.comfyui.video_server_url` | `OM_BRIDGE_COMFYUI_VIDEO_SERVER_URL` (`COMFYUI_VIDEO_SERVER_URL`) | 空 → 继承 | 视频能力专用地址 |
| `backend.comfyui.image_server_url` | `..._IMAGE_SERVER_URL` (`COMFYUI_IMAGE_SERVER_URL`) | 空 → 继承 | 图像能力专用地址 |
| `backend.comfyui.music_server_url` | `..._MUSIC_SERVER_URL` (`COMFYUI_MUSIC_SERVER_URL`) | 空 → 继承 | 音频能力专用地址 |
| `backend.comfyui.base_url` | `OM_BRIDGE_COMFYUI_BASE_URL` (`COMFYUI_BASE_URL`) | 空 | ⚠ **已废弃别名**，优先级最低 |
| `backend.comfyui.connect_timeout` | `OM_BRIDGE_COMFYUI_CONNECT_TIMEOUT` (`COMFYUI_CONNECT_TIMEOUT`) | `15` | 轻量请求超时 |
| `backend.comfyui.read_timeout` | `OM_BRIDGE_COMFYUI_READ_TIMEOUT` (`COMFYUI_READ_TIMEOUT`) | `900` | 单次渲染总超时 |
| `backend.comfyui.poll_interval` | `OM_BRIDGE_COMFYUI_POLL_INTERVAL` (`COMFYUI_POLL_INTERVAL`) | `5` | `/history` 轮询间隔，**按后端覆盖**全局值；显式置 `0` 视为未配置 |
| `backend.comfyui.api_token` | `OM_BRIDGE_COMFYUI_API_TOKEN` (`COMFYUI_API_TOKEN`) | 空 | Bearer token（**secret**） |
| `backend.comfyui.auth_user` | `OM_BRIDGE_COMFYUI_AUTH_USER` (`COMFYUI_AUTH_USER`) | 空 | Basic 用户名（**secret**） |
| `backend.comfyui.auth_password` | `OM_BRIDGE_COMFYUI_AUTH_PASSWORD` (`COMFYUI_AUTH_PASSWORD`) | 空 | Basic 密码（**secret**） |
| `backend.comfyui.upload_timeout` | `OM_BRIDGE_COMFYUI_UPLOAD_TIMEOUT` | `300` | 单次上传超时 |
| `backend.comfyui.object_info_timeout` | `OM_BRIDGE_COMFYUI_OBJECT_INFO_TIMEOUT` | `180` | 拉取 `/object_info` 超时 |

> ⚠ `COMFYUI_BASE_URL` 是**为了兼容而保留的废弃名**：OpenMontage 从来不读它。
> 新部署请只用 `server_url`。工具（如 `doctor` / `config report`）会对它给出告警。

### solution.minimax_h3

| 键 | 环境变量（旧名） | 默认 | 说明 |
|---|---|---|---|
| `..._WIDTH` | `OM_BRIDGE_SOLUTION_MINIMAX_H3_WIDTH` (`COMFYUI_MINIMAX_H3_WIDTH`) | `864` | 宽度（32 的倍数） |
| `..._HEIGHT` | `..._HEIGHT` (`COMFYUI_MINIMAX_H3_HEIGHT`) | `480` | 高度（32 的倍数） |
| `..._LENGTH` | `..._LENGTH` (`COMFYUI_MINIMAX_H3_LENGTH`) | `124` | 帧数（24fps，吸附 17k+5 栅格） |
| `..._FPS` | `..._FPS` (`COMFYUI_MINIMAX_H3_FPS`) | `24` | 封装帧率 |
| `..._STEPS` | `..._STEPS` (`COMFYUI_MINIMAX_H3_STEPS`) | `0` | 0=按模式自动（ref2v→4，其余→8） |
| `..._UNET` | `..._UNET` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | t2v/i2v/flf2v 扩散模型 |
| `..._CLIP` | `..._CLIP` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 文本编码器 |
| `..._VIDEO_VAE` | `..._VIDEO_VAE` | `minimax_h3_video_vae_fp16.safetensors` | 视频 VAE |
| `..._AUDIO_VAE` | `..._AUDIO_VAE` | `minimax_h3_audio_vae_fp32.safetensors` | 音频 VAE（缺则**没有音轨**） |
| `..._TURBO_LORA` | `..._TURBO_LORA` | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | 8 步 LoRA |
| `..._REF2V_UNET` | `..._REF2V_UNET` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | ref2v 专用（**不可与 fl2va 互换**） |
| `..._REF2V_TURBO_LORA` | `..._REF2V_TURBO_LORA` | `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | ref2v 4 步 LoRA |
| `..._REF2V_IMAGE_SIZE` | `..._REF2V_IMAGE_SIZE` | `match` | `match`=缩到生成分辨率（快）/ `max`=保留 2048px（身份保真、慢数倍） |
| `..._REF2V_IMAGES` | `..._REF2V_IMAGES` | 空 | 默认参考图文件名（list，逗号分隔） |
| `..._PREFLIGHT_MODELS` | `..._PREFLIGHT_MODELS` (`COMFYUI_H3_LOCAL_MODELS`) | 空 | 对外声明"本机 H3 就绪"的权重名（供 OpenMontage 集成） |
| `..._GRAPH_DIR` | `..._GRAPH_DIR` | 空 → `<ws>/var/graphs` | 物化计算图落盘目录 |

> ⚠ **已移除、不再读取**的旧变量：`COMFYUI_MINIMAX_H3_WORKFLOW_PATH`、`_OUTPUT_NODE`、
> `COMFYUI_MINIMAX_H3_REF2V_WORKFLOW_PATH`、`_REF2V_OUTPUT_NODE`。
> 它们存在的唯一理由是"外部框架要一个文件路径 + 节点 ID"，现在由 `om-bridge graph` 现场物化替代。
> 如果你在 `.env` 里看到它们，可以删掉（`config report` 会把它们列为"未登记变量"）。
