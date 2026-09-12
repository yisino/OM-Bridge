# 架构总览

> 本文是**代码说明**的入口：读完它应当能回答"这套东西分成几块、每块的职责边界在哪、
> 我要加东西该动哪里、以及为什么不能反过来动"。

## 1. 问题与约束

要解决的问题是：**让外部框架/流水线稳定地调用生成式模型能力（视频/图像/音频），
并且在底下的执行引擎或模型换代时，上层不用跟着改。**

这不是一个"再包一层 HTTP 客户端"的问题，因为真实环境里有四条硬约束：

| 约束 | 来源（真实场景） | 架构后果 |
|---|---|---|
| **执行引擎会换** | 今天是 ComfyUI，明天可能是别的工作流引擎或云 API | 必须抽象 Backend |
| **模型会不断加/换** | 今天 H3，明天 LTX-2、Wan、Seedance…… | 必须抽象 Solution |
| **部署机可能装不了依赖** | GPU 主机常无外网、无 pip 镜像 | 运行时**零第三方依赖** |
| **上层已有存量配置** | OpenMontage 的 `.env` 里已写死一批变量名 | 配置名必须能**向后兼容** |

其中第三条和第四条是很多"看起来更优雅"的方案会翻车的地方，务必先认清。

## 2. 分层结构

```
                     ┌─────────────────────────────────────────────┐
   交付面            │  interfaces/                                │
   (Surface)         │    cli.py   → om-bridge      (人 / Shell)   │
                     │    mcp.py   → om-bridge-mcp  (Agent / 框架)  │
                     │    (SDK)    → import om_bridge (Python 调用) │
                     └───────────────────┬─────────────────────────┘
                                         │  只依赖 ↓，三者共享同一注册表
                     ┌───────────────────▼─────────────────────────┐
   编排层            │  core/session.py                            │
   (Orchestration)   │    解析 → 校验 → 上传素材 → 构图 → 预检      │
                     │    → 提交 → 轮询 → 取件 → 溯源              │
                     └───────────────────┬─────────────────────────┘
                                         │
                     ┌───────────────────▼─────────────────────────┐
   契约层            │  core/   纯抽象 + 数据结构，**不含任何后端代码** │
   (Contract)        │    models  数据契约（唯一跨层语言）          │
                     │    backend Backend 抽象基类                 │
                     │    solution Solution 抽象基类                │
                     │    schema  ParamSchema（参数单一声明）        │
                     │    assets  资产就绪度的声明式描述             │
                     │    config  配置分层与解析                    │
                     │    registry 发现与解析                      │
                     │    errors  错误与退出码                     │
                     │    logging 日志（只走 stderr）               │
                     └───────────────────┬─────────────────────────┘
                                         │  实现 ↑ 的抽象；只 import 契约层
                     ┌───────────────────▼─────────────────────────┐
   实现层            │  backends/comfyui/                          │
   (Implementation)  │    client      HTTP 客户端（stdlib urllib）  │
                     │    graph       图操作 / 动态槽位 / 产物收集   │
                     │    inventory   后端能力盘点                  │
                     │    validation  载荷静态校验                  │
                     │    backend     ComfyUIBackend               │
                     │    solutions/minimax_h3/  ← 一个"方案"       │
                     └─────────────────────────────────────────────┘
```

**依赖方向严格单向向下。** `core` 不知道 `comfyui` 存在，`comfyui` 不知道 `minimax_h3` 之外的方案存在，
`interfaces` 只通过注册表拿到实例、不直接 import 任何实现类。

> 这条规则的价值在于：新增实现时**不需要读核心代码**，只需要实现抽象基类并让注册表能发现它。
> 一旦某天有人在 `core` 里 `import om_bridge.backends.comfyui`，扩展性就当场失效了。

## 3. 双层扩展模型（本项目的核心设计）

这是与"写一个 ComfyUI 封装"最大的区别。看下面这张表：

| 变更内容 | 应改哪一层 | 举例 |
|---|---|---|
| 换了执行引擎（HTTP 协议、提交/轮询语义都变了） | **Backend** | 从 ComfyUI 换成某云厂商 API |
| 换了模型 / 变了图结构 / 变了参数集 | **Solution** | 从 H3 换成 LTX-2 |
| 只是换了权重文件或步数 | **配置** | `H3_STEPS=8` → `4`（不碰代码） |

**为什么必须分成两层？** 如果只有一层（比如"每个模型一个独立工具类"），会出现两种坏情况：

- 两个模型跑在同一个引擎上 → 客户端代码、上传逻辑、轮询逻辑被复制两份，改一处忘一处。
- 同一个模型要在两个引擎上跑 → 模型逻辑被绑死在引擎实现里，无法复用。

分成两层后，`ComfyUIBackend` 只管"怎么跟 ComfyUI 说话"，`MinimaxH3Solution` 只管
"H3 的图长什么样、参数是什么、需要哪些权重"。二者通过 `JobSpec`（载荷 + 产物选择器）交换信息。

### 二者如何交互（唯一的耦合点）

```
Session.run(request)
  │
  ├─ registry.resolve("minimax_h3.ref2v")
  │     → (MinimaxH3Solution 实例, preset={"mode": "ref2v"})
  │     → 由 solution.backend_name 找到 ComfyUIBackend 类并实例化
  │
  ├─ solution.check_compatible(backend)      ← 能力协商，不通过就早失败
  ├─ solution.validate_params(...)           ← 方案自己的参数约束（如步数与 LoRA 配对）
  ├─ solution.validate_media(...)            ← 方案自己的素材约束（如参考图 ≤ 9）
  ├─ backend.upload(...)                     ← 后端负责"怎么把本地文件送进去"
  ├─ solution.build_job(...) → JobSpec       ← 方案产出载荷 + 输出选择器
  ├─ backend.preflight(job)                  ← 后端做静态校验（可选，可离线跳过）
  ├─ backend.submit(job) → JobHandle
  ├─ backend.poll(handle) → JobStatus  (循环)
  ├─ backend.fetch(status) → list[Artifact]
  └─ solution.provenance(...) → Provenance   ← 记录"这次到底用了什么权重/参考"
```

注意 `Solution` **只依赖 `Backend` 的抽象**，拿到的是 `Backend` 类型而非 `ComfyUIBackend`。
所以一个 Solution 理论上可以挂到任意具备所需 `Capability` 的后端上。

## 4. 数据契约（`core/models.py`）

`models.py` 是**唯一**允许被所有层 import 的东西。所有跨层传递的结构都在这里定义，
且都实现 `to_dict()`，保证"能直接 `json.dumps`"——这是 CLI 的 `--json` 与 MCP 的返回值基础。

关键结构：

| 结构 | 语义 | 关键字段 |
|---|---|---|
| `GenerationRequest` | 调用方的意图（输入） | `solution`、`prompt`、`params`、`media`、`node_overrides`、`validate`、`preflight` |
| `MediaAsset` | 一个待用的素材 | `role`（槽位角色）、`kind`、`path`/`name`、`needs_upload` |
| `JobSpec` | 后端的可提交载荷 | `payload`、`output_selector`、`kind`、`validate_before_submit`、`fingerprint()` |
| `JobHandle` | 提交凭据 | `job_id`、`backend`、`submitted_at` |
| `JobStatus` | 一次轮询结果 | `state`、`progress`、`artifacts`、`raw` |
| `ProbeReport` | 后端能力盘点 | `reachable`、`node_classes`、`assets`、`vram_*`、`errors` |
| `GenerationResult` | 最终结果（输出） | `ok`、`artifacts`、`issues`、`errors`、`provenance` |
| `Issue` | 一条问题 | `severity`、`message`、`code`、`location`、`hint` |

**设计取舍：为什么 `Issue` 要带 `hint` 与 `code`？**
因为使用这套东西的常常是 Agent（而非人）。`message` 用于人读，`code` 供程序分支判断
（例如"缺权重"与"参数越界"要触发不同的补救动作），`hint` 是给 Agent 的下一步指令。
把三者合在一条消息里会让 Agent 只能靠关键词猜。

## 5. 参数与资产：单一声明，多处消费

`core/schema.py` 的 `ParamSchema` 是**参数的唯一声明处**。一处声明，同时驱动：

1. CLI 的 `--width` / `--steps` 等选项（含类型、默认值、帮助文本、choices）
2. MCP 工具的 `inputSchema`（JSON Schema，供 Agent 理解可传什么）
3. 调用前的类型/范围校验
4. `om-bridge describe` 的自描述输出（也是文档素材）

**为什么必须单一来源？** 历史上这类项目最常见的腐坏方式是：CLI 加了参数、MCP 忘了加；
或校验放宽了、帮助文本还是旧约束。只要声明处唯一，就不可能漂移。

`core/assets.py` 处理**资产就绪度**（"这台机器上模型文件齐不齐"），同样是声明式的：
`AssetRequirement(role, category, name, modes, ...)` 描述"某模式下需要什么"，
`evaluate_assets()` 拿 `ProbeReport` 一算就知道缺什么，并且能**按模式分别报告**
（这正是"t2v 能跑但 ref2v 报缺权重"这类问题的正确表达方式）。

> **节点也被当作资产**（`NODE_CATEGORY`）。因为对使用者来说"缺一个自定义节点"和
> "缺一个权重文件"是同一类问题：环境没准备好。合成同一种结论，下游只需要处理一种结构。

## 6. 发现机制

`core/registry.py` 负责"找出所有可用的 Backend 与 Solution"：

| 来源 | 机制 | 用途 |
|---|---|---|
| 内置 | `pkgutil.walk_packages` 扫描 `om_bridge.backends`、`om_bridge.backends.*.solutions` | 随包发布的实现 |
| 第三方 | `importlib.metadata.entry_points(group="om_bridge.backends")` | **独立的 PyPI 包**接入 |

新增一个独立发布的实现包，只需在它自己的 `pyproject.toml` 里声明：

```toml
[project.entry-points."om_bridge.backends"]
my_engine = "my_pkg.backend:MyBackend"
```

**不做的事**：没有中心注册表文件、没有装饰器注册、没有 `__init__.py` 里手写 import 清单。
理由是这三种方式都要求"新增实现时改动既有文件"，而改动既有文件正是 merge 冲突与遗忘的源头。

注册表还会自动生成**模式别名**：若 `MinimaxH3Solution.key == "minimax_h3"` 且 `modes` 含 `ref2v`，
则 `minimax_h3.ref2v` 成为可解析的名字。所以 `--solution minimax_h3.ref2v` 无需任何额外登记。

## 7. 配置分层

`core/config.py` 的解析顺序（**后者被前者覆盖**，从上到下优先级递减）：

```
1. 构造时显式传入的 overrides      （代码调用方说了算）
2. 进程环境变量   OM_BRIDGE_*       （部署时说了算）
3. 进程环境变量   旧名 alias         （兼容存量部署）
4. .env 文件      OM_BRIDGE_*       （本地开发）
5. .env 文件      旧名 alias
6. 内置默认值                        （代码里的合理兜底）
```

两个刻意的决定：

- **alias 与规范名同层，而不是"alias 优先级更低"**。因为 alias 存在的理由是"存量部署已经在用它"，
  如果在同一个 `.env` 文件里规范名和旧名同时存在、且旧名优先，会让人无法用规范名覆盖旧值。
- **自带 `.env` 解析器（`parse_env_file`），不引 `python-dotenv`**。为省一个依赖写 30 行解析代码，
  换来"GPU 主机上零 pip 也能跑"，这个交换非常划算。

`config.explain(key)` 会给出某个键**最终取值的来源**（内置/文件/环境/override），
排查"我改了 env 为什么没生效"时直接看它，不要靠猜。

## 8. 会话编排（`core/session.py`）

`Session` 是唯一编排者。它有两条路径，**共享同一段解析逻辑**（`_resolve`）：

- `prepare(request)` → 解析 + 校验（**有阻断项就抛异常**），返回 `PreparedJob`
- `validate(request)` → 同一条路径，但把校验降级，**永不抛异常**，一次返回全部问题

**为什么 `validate` 必须不抛异常？** 因为校验命令的产出**就是**那份问题清单。
如果它在第一个阻断项上抛错，用户一次只能看到一个问题——最坏的使用体验。
这个约束写在代码注释里，请不要"顺手"把它改成抛异常。

三个阶段的超时语义也在这里定义：

```
submit ──► poll（每次 poll 有超时） ──► fetch
                │
                └─ 超时 → JobTimeoutError(recoverable=True, job_id=...)
                          ↑ 调用方应调 Session.resume(job_id) **续等**
                            ⚠ 绝不要重新 submit：会排入重复渲染，抢同一块 GPU
```

## 9. 三个交付面

| 面 | 入口 | 适用 | 关键约束 |
|---|---|---|---|
| CLI | `om-bridge <cmd>` | 人、Shell 脚本、CI | 退出码有意义（见 `errors.py`） |
| MCP | `om-bridge-mcp`（stdio） | Agent / 支持 MCP 的框架 | **stdout 只能放 JSON-RPC**，日志一律 stderr |
| SDK | `import om_bridge` | 同进程 Python 调用 | 与 CLI 走同一 `Session`，行为一致 |

`interfaces/mcp.py` 的工具清单**不是手写的**：`om_bridge_generate_video` 的 `inputSchema`
由注册表里所有 `kind == "video"` 的方案合成。所以新增一个视频方案后，MCP 工具自动获得该方案的参数——
不需要改 `mcp.py`。

> 唯一的硬约束：**stdio 类传输的 stdout 必须是干净的协议流。**
> 这就是 `core/logging.py` 强制 `stderr` + `propagate=False` 的原因。
> 任何 `print()` 到 stdout 的调试代码都会破坏 MCP 协议，且症状是"框架侧报 JSON 解析失败"，
> 极难定位。改 `mcp.py` 时请用 `progress()` / `diagnostic()`，不要用 `print()`。

## 10. 外部框架集成（`integrations/`）

`integrations/` 是**顶层目录，不属于安装的包**。理由：集成逻辑依赖具体的第三方项目结构，
把它放进包里会让包被迫感知外部世界；而放在顶层，则是"外部世界的胶水"这一正确归属。

`integrations/openmontage/publish.py` 做一件事：把 OM-Bridge 的配置与**现场物化的计算图**
发布到 OpenMontage 的 `.env` 托管块里。它是幂等的，且支持 `--remove`。

**它取代了什么**：原先散落的 `patch_h3_preflight.py`（猴子补丁修 OpenMontage 的就绪度误判）
与 `merge_env_example_comfyui.py`（合并变量表）。这两个脚本都是"对着别人仓库做外科手术"，
且各自有独立的幂等逻辑；合并为一个"托管块发布器"后，只有一个地方需要维护边界标记与回滚。

## 11. 明确不做的设计（Negative Architecture）

写下来是为了防止后来者"顺手加上"：

| 不做 | 为什么 |
|---|---|
| 不做任务队列 / 调度器 | 后端（ComfyUI）自己就有队列；再加一层会引入状态一致性问题 |
| 不做结果缓存 | 后端有整图缓存，语义微妙（同样的图会秒回旧产物）。桥接层缓存会让"缓存命中"与"真的算得快"无法区分 |
| 不做模型下载 | 权重体积以百 GB 计、来源与许可各异，属于环境准备而非桥接职责。只做**就绪度检查**并给出下载来源 |
| 不把 `prompt` 拼装成模板 | 提示词质量取决于模型与场景，桥接层只做透传与语法提示（如 `<Picture N>` 的顺序含义） |
| 不在 `core` 里 import 任何具体实现 | 见 §2 |

## 12. 扩展点速查

| 我想…… | 动这里 | 参考 |
|---|---|---|
| 接一个新执行引擎 | 新建 `backends/<engine>/`，实现 `Backend` | [adding-a-backend.md](adding-a-backend.md) |
| 接一个新模型方案 | 在对应后端下新建 `solutions/<name>/`，实现 `Solution` | [adding-a-backend.md](adding-a-backend.md) |
| 加一个方案参数 | 改该方案的 `ParamSchema` 声明（CLI/MCP/校验/文档会同步） | [module-reference.md](module-reference.md) §schema |
| 加一个环境变量 | `core/config.py` 加 `Setting` + `config/om-bridge.env.example` 加条目 | [deployment.md](deployment.md) §配置 |
| 加一个 CLI 子命令 | `interfaces/cli.py` 加 subparser | [user-guide.md](user-guide.md) |
| 改就绪度判断逻辑 | 改方案的 `assets.py` 声明，**不要**在后端里硬编码某方案的权重名单 | [design/0008](design/0008-avoid-hardcoded-readiness.md) |
