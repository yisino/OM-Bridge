# Changelog

本文件格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **`mock` 后端**（`backends/mock/`）：确定性、离线、零网络的参考实现。
  产物是作业转写 JSON 而非媒体文件；`job_id` 由载荷指纹派生（同载荷同 id，
  复现整图缓存语义）；作业台账仅在进程内（未知 job 的 poll 立刻 FAILED 而非挂死）。
  用于离线端到端测试、CI，以及作为"新增后端"的可运行样例。
- **`echo` 方案**（`backends/mock/solutions/echo/`）：绑定 mock 的最小合法 Solution。
  无模式、无资产需求，覆盖与 H3 互补的注册分支；`simulate_failure` 参数提供
  结构化失败路径。Session / CLI 全链路均有测试（`tests/test_mock_backend.py`、
  `tests/test_cli.py::test_mock_backend_end_to_end_via_cli`）。

### 计划中

- `tests/` 覆盖率的补齐（当前重点是构图回归与配置分层）
- 旧变量名的**弃用告警**（需等上游 OpenMontage 完成迁移，见
  [ADR-0004](docs/design/0004-config-backward-compatible-aliases.md)）

## [0.1.0] — 2026-09-12

首个版本。把 SER 上原先分散的 `shared/` 与 `ser_comfyui/` 两个目录整合为一个独立可扩展工程。

### 新增

**架构**
- 双层扩展模型：`Backend`（在哪跑）× `Solution`（用什么模型），仅通过 `core/models` 数据契约通信。
  见 [ADR-0001](docs/design/0001-two-layer-backend-solution.md)。
- 注册表自动发现：内置走 `pkgutil.walk_packages`，第三方走 `importlib.metadata` entry points
  （组名 `om_bridge.backends`）。新增实现 = 新增目录，**不修改任何既有文件**。
  见 [ADR-0003](docs/design/0003-registry-autodiscovery.md)。
- 模式别名自动派生（`minimax_h3.ref2v`），无需登记。
- 参数单一声明（`ParamSchema`）驱动 CLI 选项、MCP `inputSchema`、校验与 `describe` 输出。
- 声明式资产就绪度（`AssetRequirement` + `readiness_by_mode`），**逐模式**报告缺什么；
  节点存在性也建模为资产。见 [ADR-0008](docs/design/0008-avoid-hardcoded-readiness.md)。

**核心（`src/om_bridge/core/`）**
- `models`：数据契约（`GenerationRequest` / `JobSpec` / `JobStatus` / `Artifact` / `ProbeReport` /
  `GenerationResult` / `Issue` / `Provenance` 等），全部 `to_dict()` 可序列化。
- `errors`：带**退出码**与**阶段**的异常族（`0`–`7`），其中超时是可恢复状态并携带 `job_id`。
- `config`：分层配置（构造参数 > 环境变量 > `.env` > 默认值）+ 自省（`explain` / `report`）。
- `registry` / `session`：发现与编排（解析 → 校验 → 上传 → 构图 → 预检 → 提交 → 轮询 → 取件 → 溯源）。
- `logging`：进度与诊断分离，**只写 stderr**。见 [ADR-0007](docs/design/0007-logs-to-stderr-only.md)。

**ComfyUI 后端（`src/om_bridge/backends/comfyui/`）**
- `client`：基于标准库 `urllib` 的 HTTP 客户端；上传遇 **HTTP 409 重名**时改用内容哈希名重试。
- `graph`：图操作、`COMFY_AUTOGROW_V3` 动态槽位（点号键）、产物收集
  （正确处理 `SaveVideo` 的 mp4 挂在 `images` 键下的特例）。
- `inventory`：`/object_info` 能力盘点（含 `COMBO` 与 `COMFY_DYNAMICCOMBO_V3`）。
- `validation`：提交前的载荷静态校验（节点存在性、输入名、必填项、枚举值、范围、连线引用）。
- `backend`：`ComfyUIBackend`（探针 / 提交 / 轮询 / 取件 / 上传 / 预检），
  并对"耗时异常短"给出**缓存疑点**诊断。

**MiniMax H3 方案（`backends/comfyui/solutions/minimax_h3/`）**
- 四种模式：`t2v` / `i2v` / `flf2v`（fl2va 权重 + 8 步 LoRA）与
  `ref2v`（ref2va 权重 + 4 步 LoRA，1–9 张参考图）。
- 权重混用时**直接拒绝**（避免"能跑但结果不对"）。
- 5 份已核对的计算图作为**结构回归基准**。
- 音视频联合生成（产物含 AAC 双声道）。

**交付面（`src/om_bridge/interfaces/`）**
- CLI `om-bridge`：10 个子命令（`generate` / `validate` / `graph` / `run` / `resume` /
  `probe` / `list` / `describe` / `config {list,explain,report}` / `doctor`）。
- MCP `om-bridge-mcp`：stdio JSON-RPC，7 个工具，schema 由注册表合成。
- Python SDK：`Session` / `GenerationRequest`，支持分步控制。

**配置 / 部署 / 脚本 / 集成**
- `config/om-bridge.env.example`（权威模板，逐项标注来源与陷阱）；
  `config/profiles/{same-lan,through-tunnel}.env`。
- `deploy/install.sh`（`--mode=path` 零依赖 / `--mode=venv`）、`uninstall.sh`（默认预演）、
  `verify.sh`（含逐模式就绪度检查）。
- `scripts/gen-workflow.sh`、`validate-workflow.py`、`mcp-smoke.py`、`bootstrap.sh`。
- `integrations/openmontage/publish.py`：幂等的 `.env` 托管块发布器
  （取代原先两个各自独立的补丁/合并脚本）。

**文档**
- `docs/design/`：8 份 ADR（含"考虑过的其它选项"与"我们放弃了什么"）。
- `docs/architecture.md`、`module-reference.md`、`user-guide.md`、`deployment.md`、
  `adding-a-backend.md`、`troubleshooting.md`、`solutions/minimax-h3.md`。

### 变更（相对此前分散的实现）

- 计算图不再预存为 JSON 文件，改为 `om-bridge graph` **现场物化**；
  相应地**移除** 4 个配置变量：`COMFYUI_MINIMAX_H3_WORKFLOW_PATH` / `_OUTPUT_NODE` /
  `COMFYUI_MINIMAX_H3_REF2V_WORKFLOW_PATH` / `_REF2V_OUTPUT_NODE`。
  见 [ADR-0006](docs/design/0006-materialize-graph-on-demand.md)。
- 配置采用**规范名 + 兼容旧名**：`OM_BRIDGE_*` 为规范名，旧 `COMFYUI_*` 作为同层别名保留，
  存量部署无需改动。见 [ADR-0004](docs/design/0004-config-backward-compatible-aliases.md)。
- `COMFYUI_BASE_URL` 标记为**废弃别名**（上游从不读取该名字），使用时会给出告警。
- 参数默认值的优先级为**配置 > 代码内置默认值**：换权重/步数不再需要改代码。

### 修复

相对此前实现，修掉了 4 个在真实环境实测暴露的缺陷：

- **组合型输入导致崩溃**：ComfyUI 对某些输入会把**取值列表本身**当类型标记，
  原先的类型哈希比较会抛 `TypeError: unhashable type: 'list'`（真实实例上必现）。
- **节点存在性误报**：探针的 `nodes` 字段是"关键节点清单"，却被用来回答"某节点类在不在"，
  导致确实存在的 `MiniMaxH3ImageToVideo` 被报缺失。新增 `node_classes` 与 `has_node_class()`。
- **CLI 参数收集把函数对象当参数**：`set_defaults(func=...)` 让处理函数进入 argparse 命名空间，
  一路带到 `json.dumps` 才报 `Object of type function is not JSON serializable`。
  改为**白名单**收集。
- **`validate` 命令会抛异常**：校验命令的产出应当是"问题清单"，不该在第一个阻断项抛错。
  抽公共解析路径 + issue 去重后，`validate` 保证**不抛异常、一次列全部问题**。

另修：`graph -o -` 与 `--json` 同用会把两份 JSON 文档连在一起，现在 `-o -` 独占 stdout。

测试收尾阶段又发现并修掉 3 个"静默失效"类缺陷：

- **校验结论在 JSON 输出里退化成 `repr` 文本**：`ValidationError.as_dict()` 探测的是
  `issue.as_dict()`，而 `Issue` 定义的是 `to_dict()` —— `hasattr` 静默为 False，
  模型对象原样进了 payload，最后被 `json.dump(default=str)` 渲染成
  `Issue(severity=...)` 字符串。"结构化错误"的承诺在不报错的情况下失效，
  调用方按 `code` 分流的逻辑全部失灵。修复后 `issues` 是字典列表，
  并新增 `tests/test_errors.py` 把异常契约（退出码、`as_dict` 形状、超时可恢复标记）整体钉住。
- **两个 no-op 配置项**：`OM_BRIDGE_STRICT`（登记了"告警也视为失败"却无人读取）
  与 `backend.<name>.poll_interval`（实际轮询只读全局值）。分别落实为
  `Session.blocking()`（`validate` 报告与 `generate` 放行共用同一条判定）与
  按后端覆盖轮询间隔。顺带修掉实现时的一个坑：登记过的键永远有值，
  "用户是否真的给了值"必须问 `is_default()` 而不是用自带默认值试探。
- **`-v` 无输出时"stdout 纯净"是空话**：`--verbose` 现在会先输出启动诊断
  （发现了哪些方案、最终读了哪个配置文件），全部走 stderr ——
  既是"改了配置没生效"的第一手线索，也让日志去 stderr 的护栏测试有了真实的检验对象。

### 已知限制

- 只有 ComfyUI 一个后端实现 —— 双层抽象的"够不够用"尚未被第二个实现检验
  （这正是 Unreleased 里那条计划项的目的）。
- 未做任务队列/调度、结果缓存、模型下载（理由见
  [architecture.md §11](docs/architecture.md)）。
- 旧变量名尚无弃用告警（需等上游迁移完成）。
