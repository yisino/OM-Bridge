# 扩展指南：新增后端与新增方案

> 本项目的核心卖点就是"新增实现不用改核心"。本文给出**可照做的步骤**与**检查清单**。
> 动手前请先读 [architecture.md](architecture.md) §3（双层模型）——90% 的设计错误来自把两层搞混。

## 目录

1. [先判断：我该加哪一层？](#1-先判断我该加哪一层)
2. [新增一个后端（Backend）](#2-新增一个后端backend)
3. [新增一个方案（Solution）](#3-新增一个方案solution)
4. [让方案可以在多个后端上跑](#4-让方案可以在多个后端上跑)
5. [发布为独立包（entry point）](#5-发布为独立包entry-point)
6. [检查清单](#6-检查清单)
7. [反模式](#7-反模式)

---

## 1. 先判断：我该加哪一层？

| 你面对的变化 | 加哪层 | 例子 |
|---|---|---|
| 换执行引擎（怎么提交、怎么轮询、怎么取件都变了） | **Backend** | ComfyUI → 某云视频 API |
| 换模型/图结构/参数集 | **Solution** | H3 → LTX-2 |
| 只是换权重文件、步数、分辨率 | **都不是**，改配置 | `.env` 里改 `..._STEPS=4` |
| 同一个模型换个引擎跑 | **都不要**，只需方案能兼容新后端 | H3 从 ComfyUI 换到别的引擎 |

**判断口诀**：如果"参数名"要变，是 Solution；如果"参数怎么送达"要变，是 Backend；
如果只是"参数的值"要变，是配置。

---

## 2. 新增一个后端（Backend）

### 2.1 目录结构

```
src/om_bridge/backends/<engine>/
├── __init__.py          # 导出后端类（注册表按模块扫描，务必导出）
├── client.py            # 与引擎通信的客户端（HTTP/SDK/子进程都行）
├── backend.py           # Backend 抽象基类的实现
├── validation.py        # （可选）载荷静态校验
└── solutions/           # 本引擎原生的方案（见 §3）
    └── __init__.py
```

**没有任何中心文件需要改。** 注册表用 `pkgutil.walk_packages` 扫描 `om_bridge.backends`，
发现 `Backend` 的子类即自动登记。所以不要去找"注册表该加在哪"——没有那个地方。

> **先读现成的参考实现**：`src/om_bridge/backends/mock/` 是一个能跑的完整最小后端
> （四个必选动作 + upload + 能力声明，零配置、零网络），配套方案在它的
> `solutions/echo/`。动手写自己的后端前，把它通读一遍比什么都快 ——
> 它的存在本身也是对双层契约的一次检验（见 [solutions/echo.md](solutions/echo.md)）。

### 2.2 实现 Backend（必需）

```python
from om_bridge.core.backend import Backend, Capability
from om_bridge.core.models import Artifact, JobHandle, JobSpec, JobStatus, ProbeReport


class MyEngineBackend(Backend):
    # --- 类属性：注册表与 UI 靠它们展示 ---
    name = "myengine"                      # 唯一，小写
    display_name = "My Engine"
    capabilities = frozenset({Capability.VIDEO, Capability.MEDIA_UPLOAD})
    description = "一句话说明这个后端适合什么场景。"

    # --- 必需 ---
    def probe(self, refresh: bool = False) -> ProbeReport:
        """盘点能力。**失败也要返回报告**（reachable=False + errors），不要抛异常。"""

    def submit(self, job: JobSpec) -> JobHandle:
        """受理即返回。被拒时抛 JobRejectedError（带后端的原始错误）。"""

    def poll(self, handle: JobHandle) -> JobStatus:
        """一次**非阻塞**查询。返回 JobState（PENDING/RUNNING/SUCCEEDED/FAILED...）。"""

    def fetch(self, status: JobStatus) -> list[Artifact]:
        """把产物取回本地并写成 Artifact（含 local_path）。"""

    # --- 可选（按需实现；不实现则相应能力不可用）---
    def upload(self, path: str, kind: str) -> str: ...      # Capability.MEDIA_UPLOAD
    def preflight(self, job, probe=None) -> list[Issue]: ...  # 载荷静态校验
    def resume(self, job_id: str, timeout=None) -> JobStatus: ...  # Capability.RESUME
    def close(self) -> None: ...                             # 释放连接
```

### 2.3 三条容易踩错的契约

**① `probe()` 失败也要返回报告。**

```python
def probe(self, refresh=False) -> ProbeReport:
    report = ProbeReport(backend=self.name)
    try:
        stats = self.client.system_stats()
    except Exception as exc:
        report.reachable = False
        report.errors.append(f"{type(exc).__name__}: {exc}")
        return report                     # ✅ 返回，不是 raise
    report.reachable = True
    report.version = stats.get("system", {}).get("comfyui_version")
    ...
    return report
```

理由：调用方需要的是"不可达 + 原因"，以便打印**可执行的建议**。
抛异常的探针会逼上层到处写 try/except，最后总会有一处忘写。

**② 产能盘点的字段语义要分清**（`ProbeReport`）：

| 字段 | 含义 | 进 JSON 输出？ |
|---|---|---|
| `nodes: dict[str, bool]` | 你关心的**关键**节点/组件是否存在 | 是 |
| `node_classes: set[str]` | **全部**可用的类名（供"某东西在不在"查询） | 否（可能上万项） |
| `assets: dict[str, list[str]]` | 按类别列出的可用资产（模型文件等） | 是 |

`assets[category]` 若是 `None`/缺失，表示"**这一类没被报告**"，与"报告了但为空"
是不同含义 —— `assets.py` 的 `AssetStatus.declared` 就是为区分这两者存在的。

**③ `poll()` 要让上层能发现"快得不正常"**。若你的引擎有结果缓存，
在耗时异常短时打一条 diagnostic（参考 ComfyUI 后端的 `CACHE_SUSPECT_SECONDS`）。
用户看到"改了参数结果没变"时，这条日志能省掉半天排查。

### 2.4 用 `Capability` 声明能力，别用 if

```python
capabilities = frozenset({Capability.VIDEO, Capability.MEDIA_UPLOAD, Capability.RESUME})
```

可选能力：`VIDEO` / `IMAGE` / `AUDIO` / `MEDIA_UPLOAD` / `RAW_JOB` / `RESUME`。

方案在 `check_compatible(backend)` 里声明自己需要什么，`Session` 会据此**早失败**
（在提交前，而不是渲染到一半）。**不要在方案里写 `if backend.name == "comfyui"`** ——
那就是把两层重新粘在一起了。

---

## 3. 新增一个方案（Solution）

### 3.1 目录结构

```
src/om_bridge/backends/<engine>/solutions/<name>/
├── __init__.py      # 必须导出 Solution 子类
├── manifest.py      # 常量与声明：模式、资产需求、约束（**无逻辑**）
├── builder.py       # 从参数构造载荷（可单测，无 IO）
├── solution.py      # Solution 子类：schema / 槽位 / build_job / provenance
└── templates/       # （可选）作为结构回归基准的载荷样本
```

**把 `manifest` / `builder` / `solution` 分开**是为了可测：`builder` 是纯函数，
可以离线单测与回归比对；`solution` 只做"装配"。本项目 H3 方案用 5 份模板做结构回归，
改 `builder.py` 后能立刻发现无意中的行为改变。

### 3.2 声明（`manifest.py`）—— 尽量只放数据

```python
SOLUTION_KEY = "my_model"

@dataclass(frozen=True)
class ModeSpec:
    name: str            # t2v / i2v / ...
    output_node: str     # 产物所在节点（**字符串**）
    steps: int
    lora: str | None
    slots: tuple[str, ...]   # 该模式接受的媒体槽位

MODES: dict[str, ModeSpec] = {...}

WIDTH_MULTIPLE = 32
LENGTH_GRID = (17, 5)        # value % 17 == 5

def asset_requirements(names=None) -> list[AssetRequirement]: ...
```

**约束用常量表达，别散落在代码里**。`WIDTH_MULTIPLE` / `LENGTH_GRID` 这类值会被
`ParamSchema`（`multiple_of`）、`builder`（吸附）和文档同时引用；散开写就一定会不一致。

**资产需求是声明的，不是硬编码的**：

```python
AssetRequirement(
    role="diffusion_model",
    category=COMFYUI_UNET,
    name="my_model_v1.safetensors",
    modes=("t2v", "i2v"),
    required=True,
    source_url="https://...",      # 缺资产时能直接告诉用户去哪下
)
```

节点存在性也用资产表达：`category=NODE_CATEGORY, name="MyModelNode"`。
这样"缺一个节点"与"缺一个权重"在下游是同一种结构，不需要两套处理。

### 3.3 构造载荷（`builder.py`）—— 纯函数，无 IO

```python
def build_graph(mode, prompt, width, height, length, seed, steps, ...) -> tuple[dict, str]:
    _validate_inputs(...)          # 非法输入抛 ValidationError（不要静默纠正）
    graph = {...}
    return graph, output_node
```

要求：

- **纯函数**：不读文件、不发请求、不看环境。这样才可测、可比对。
- **节点 ID 稳定**：模板里是什么就是什么。ID 漂移会让"输出节点=15"这类外部契约失效。
- **约束在此处也校验一遍**（参数栅格、LoRA 与步数配对）。`ParamSchema` 已经拦了一层，
  但 `builder` 可能被直接调用（如集成层），不能假设上游一定校验过。

### 3.4 实现 `Solution`（`solution.py`）

```python
class MyModelSolution(Solution):
    key = "my_model"
    backend_name = "myengine"          # 绑定到哪个后端
    kind = "video"
    modes = ("t2v", "i2v")
    default_mode = "t2v"

    schema = ParamSchema([...])        # ★ 单一声明处，驱动 CLI + MCP + 校验 + 文档
    media_slots = (MediaSlot(role="first_frame", kind=MediaKind.IMAGE, modes=("i2v",)),)
    assets = (...)                     # 或实现 resolve_assets(config)

    def config_defaults(self, config, mode) -> dict:
        """从配置取本方案的默认值。★ 会覆盖 schema 的内置 default。"""

    def build_job(self, request, backend, *, resolved_params, media, config=None) -> JobSpec:
        """产出载荷 + 输出选择器。**权重/资产从 config 取，不从 request.params 取。**"""
```

**`config_defaults` 与 `schema.default` 的关系**（很容易搞错）：
优先级是 `config_defaults` **高于** `schema.default`。设计意图是"这台机器该用什么"
是**部署事实**，应该由配置决定。所以：把**代码里合理**的值写进 `schema`，
把**部署时会变**的值（权重文件名、步数、分辨率）通过 `config_defaults` 从配置读。

**为什么权重从 config 取而不是从 params 取？**
因为"这台机器上文件叫什么"是环境事实，不是每次调用的参数。
把它做成参数会诱导用户手写文件名，然后拼错、然后在提交时被拒。
（需要临时覆盖时，`--set unet=...` 仍然可用，且会被校验拦错。）

### 3.5 `provenance()` —— 记录"这次到底用了什么"

```python
def provenance(self, *, resolved_params, media, backend, artifacts=None) -> Provenance:
    return Provenance(
        solution=self.key, mode=..., backend=backend.name,
        params=resolved_params,
        extra={"weights": {...}, "reference_tags": [...]},
    )
```

这条信息是**复现实验的唯一依据**。请把所有影响输出的输入都记录下来：
权重名、LoRA、步数、seed、参考图文件名。少了任何一项，三个月后就无法复现。

---

## 4. 让方案可以在多个后端上跑

`Solution` 只依赖 `Backend` **抽象**，所以原则上可以换挂。要做到这点：

1. `build_job` 里**不要**出现后端专有类名。若载荷格式本身是后端专有的，
   那就说明这个方案不该跨后端 —— 把 `backend_name` 设死，并在 `check_compatible` 里明确拒绝。
2. 用 `Capability` 表达依赖，让 `Session` 早失败：

```python
def check_compatible(self, backend) -> list[Issue]:
    issues = super().check_compatible(backend)
    if Capability.RESUME not in backend.capabilities:
        issues.append(Issue.warning(
            "该后端不支持续等：超时后只能重新提交",
            code="solution.no_resume",
            hint="把 global.timeout 调大以降低超时概率",
        ))
    return issues
```

---

## 5. 发布为独立包（entry point）

不想把实现放进 OM-Bridge 仓库时，做成独立 PyPI 包：

```toml
# 你的包 pyproject.toml
[project]
name = "om-bridge-myengine"
dependencies = ["om-bridge>=0.1"]

[project.entry-points."om_bridge.backends"]
myengine = "om_bridge_myengine.backend:MyEngineBackend"
```

安装后注册表自动发现。**不需要**改 OM-Bridge 任何一行代码，也不需要它知道你的存在。

> 这是本项目对"可扩展"的定义：**扩展方零侵入，核心零感知**。
> 如果哪天你为了接一个实现而去改 `core/`，请先回头看 [architecture.md](architecture.md) §2 ——
> 多半是分层放错了。

---

## 6. 检查清单

新增后端：

- [ ] `name` 唯一、小写，且在 `list` 里能出现
- [ ] `probe()` 在**后端不可达**时返回报告而非抛异常
- [ ] `capabilities` 与真实实现一致（声明了 `upload` 就必须实现 `upload`）
- [ ] `poll()` 非阻塞，且能在状态里反映失败原因
- [ ] `fetch()` 落盘并填 `local_path`
- [ ] 若引擎有缓存，`poll()` 对"异常快"有诊断输出

新增方案：

- [ ] `key` 唯一；`modes` 与 `default_mode` 正确；别名 `key.mode` 能解析
- [ ] `schema` 覆盖全部参数，含 `multiple_of` / 范围 / `choices`
- [ ] `media_slots` 的数量上限与顺序语义与后端一致
- [ ] `assets` 或 `resolve_assets()` 覆盖所有模式的必需资产，**按模式区分**
- [ ] `config_defaults()` 读配置（权重、步数、分辨率），不让部署事实留在代码里
- [ ] `build_job()` 是确定性的：同输入同输出（`fingerprint()` 才有意义）
- [ ] `provenance()` 记录了所有影响输出的输入
- [ ] **结构回归**：与模板比对通过（若有模板）
- [ ] 文档：`docs/solutions/<key>.md` + `config/om-bridge.env.example` 的新变量

完整自测：

```bash
om-bridge list                       # 新东西出现
om-bridge describe <key>             # schema/槽位/资产/就绪度都合理
om-bridge validate -s <key> -p "x"   # 参数校验路径
om-bridge graph -s <key> -p "x" -o /tmp/g.json    # 离线物化成功
om-bridge generate -s <key> -p "x" --dry-run      # 不联网的端到端
om-bridge doctor                     # 就绪度结论正确
```

---

## 7. 反模式

| 反模式 | 为什么错 | 正确做法 |
|---|---|---|
| 在 `core/` 里 import 具体后端 | 分层当场失效，扩展性归零 | 只 import 抽象与 `models` |
| 方案里 `if backend.name == "comfyui"` | 重新把两层粘死 | 用 `Capability` + `check_compatible` |
| 把某方案的权重名单硬编码进后端 | 装别的模型时该方案被误判"不可用"，Agent 会转投**收费云端** | 名单放方案的 `manifest.py`（声明式） |
| `probe()` 抛异常 | 上层到处 try，总有一处漏 | 返回 `reachable=False` 的报告 |
| 在 `build_job` 里读环境变量 | 不可测、不可复现 | 通过 `config` 传入 |
| 手写第二份参数表（如给 CLI 单独写 help） | 必然与 schema 漂移 | 用 `ParamSchema.to_cli_help()` / `to_json_schema()` |
| 静默纠正非法输入 | 用户以为生效了，实际参数被改 | 抛 `ValidationError`，或给明确 `Issue` 告警 |
| `print()` 到 stdout | 破坏 MCP stdio 协议 | `progress()` / `diagnostic()` |
| 用 `--node-override` 当成常规配置手段 | 绕过全部类型检查，是逃生舱 | 把参数补进 `ParamSchema` |
