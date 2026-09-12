# 使用手册

> 面向"把生成能力用起来"的人。安装与部署细节见 [deployment.md](deployment.md)；
> 出问题看 [troubleshooting.md](troubleshooting.md)。

## 目录

1. [五分钟上手](#1-五分钟上手)
2. [三个交付面](#2-三个交付面)
3. [概念速览](#3-概念速览)
4. [CLI 完整用法](#4-cli-完整用法)
5. [MCP 用法（给 Agent / 框架）](#5-mcp-用法给-agent--框架)
6. [Python SDK](#6-python-sdk)
7. [典型任务配方](#7-典型任务配方)
8. [退出码与错误处理](#8-退出码与错误处理)
9. [选项速查](#9-选项速查)

---

## 1. 五分钟上手

```bash
# 0) 确认能用（不需要联网）
om-bridge list

# 1) 体检：能不能连上后端、模型齐不齐
om-bridge doctor

# 2) 先看清将要提交什么，不排队
om-bridge generate -s minimax_h3 -p "一只猫在草地上打滚" --dry-run

# 3) 真的跑一次（短视频，验证链路）
om-bridge generate -s minimax_h3 -p "一只猫在草地上打滚" \
    --width 608 --height 352 --length 5

# 4) 参考图生视频（ref2v）
om-bridge generate -s minimax_h3.ref2v -p "<Picture 1> 中的角色抬起头" \
    --reference-image ./丹华_角色模型.png --width 608 --height 352 --length 5
```

没装成命令时，把 `om-bridge` 换成 `PYTHONPATH=<repo>/src python -m om_bridge` 即可。

> **为什么第 2 步值得做？** `--dry-run` 会把"解析后的参数、素材、载荷规模、
> 输出节点、就绪度问题"全列出来。一次错参数提交到 ComfyUI 会占用队列并可能触发模型换入换出
> （几十秒到几分钟），`--dry-run` 一秒就能发现。

---

## 2. 三个交付面

同一套 `Session`，三种入口。**行为一致**，选最贴合你环境的那个：

| 面 | 什么时候用 | 入口 |
|---|---|---|
| **CLI** | 人在终端、Shell 脚本、CI | `om-bridge <命令>` |
| **MCP** | Agent（Claude/Cursor/自研）或支持 MCP 的框架 | `om-bridge-mcp`（stdio） |
| **SDK** | 你自己的 Python 程序 | `from om_bridge import Session` |

三者的关系：

```
             ┌──────────── 同一注册表 + 同一 Session ────────────┐
   CLI  ─────┤                                                  ├───── 后端
   MCP  ─────┤  解析 → 校验 → 上传 → 构图 → 预检 → 提交 → 轮询 → 取件
   SDK  ─────┤                                                  ├───── 产物
             └──────────────────────────────────────────────────┘
```

新增一个方案后，**三个面同时获得它** —— 不需要分别改三处。

---

## 3. 概念速览

| 词 | 含义 | 例子 |
|---|---|---|
| **Backend** | 算力来源 / 执行引擎 | `comfyui` |
| **Solution** | 用什么模型、怎么跑 | `minimax_h3` |
| **Mode** | 方案内的一种生成形态 | `t2v` / `i2v` / `flf2v` / `ref2v` |
| **别名** | `方案.模式` 的写法 | `minimax_h3.ref2v` |
| **素材 / 槽位** | 方案接受的输入文件及其角色 | `reference_image`、`first_frame` |
| **载荷（payload）** | 提交给后端的图（API 格式 JSON） | — |
| **产物（Artifact）** | 后端产出的文件 | `mp4` |
| **探测（probe）** | 盘点后端连通性与可用资产 | — |
| **就绪度（readiness）** | 某模式下资产是否齐备 | `ref2v: ready` |

查当前有哪些后端/方案/别名：`om-bridge list`。
查某个方案的参数、槽位、资产：`om-bridge describe minimax_h3`。

---

## 4. CLI 完整用法

### 4.1 通用选项（所有命令都可加）

| 选项 | 说明 |
|---|---|
| `--json` | stdout 只输出一份 JSON，便于脚本解析 |
| `--env-file <路径>` | 显式指定配置文件 |
| `--workspace <目录>` | 工作区根目录（影响配置探测与产物落盘） |
| `--backend <名称>` | 指定后端（默认取配置） |
| `-v` / `--verbose` | 调试日志（始终写 stderr）。并先输出**启动诊断**：发现了哪些方案/后端、最终读的是哪个配置文件 —— "改了配置没生效"先看这个 |
| `-q` / `--quiet` | 关掉进度输出，只留最终结果 |
| `--version` | 版本 |

### 4.2 `generate` —— 执行一次生成

```bash
om-bridge generate -s <方案[.模式]> -p <提示词> [参数...] [媒体...]
```

关键选项：

| 选项 | 说明 |
|---|---|
| `-s, --solution` | `minimax_h3` 或 `minimax_h3.ref2v` 等 |
| `-p, --prompt` | 提示词。**`-p -` 从 stdin 读**（长提示词强烈建议） |
| `--dry-run` | 只解析与校验，打印将要提交的内容，不提交 |
| `--output-dir <目录>` | 产物落盘目录（默认取 `global.output_dir`） |
| `--timeout <秒>` | 等待超时。**超时后请 `resume`，不要重提** |
| `--no-validate` | 跳过参数/媒体校验（不推荐） |
| `--no-preflight` | 跳过需要联网的载荷静态校验 |
| `--set 名字=值` | 传未登记的扩展参数（可重复） |
| `--node-override 节点ID.输入名=值` | 直接改写载荷里某节点的输入（**逃生舱**，可重复） |

示例：

```bash
# 从文件读长提示词
om-bridge generate -s minimax_h3.ref2v -p - --reference-image ref.png < prompt.txt

# 覆盖权重与步数（临时实验，不改配置文件）
om-bridge generate -s minimax_h3 --set steps=20 --set turbo=false -p "..." 

# 精确改写某个节点输入（绕过类型检查，慎用）
om-bridge generate -s minimax_h3 \
    --node-override "3.noise_seed=12345" -p "..."

# 机器可读
om-bridge generate -s minimax_h3 -p "..." --json | jq '.artifacts[].local_path'
```

> `--node-override` 是**逃生舱**：它绕过参数 schema 的类型与范围检查，
> 直接改载荷。用途是"方案还没声明某个参数，但你现在就要试"。
> 用它试出结论后，请把参数补进方案的 `ParamSchema`，别长期依赖。

### 4.3 `validate` —— 只校验不提交

```bash
om-bridge validate -s minimax_h3.ref2v -p "..." --reference-image a.png
om-bridge validate -s minimax_h3.t2v -p "x" --json
```

**这个命令保证不抛异常**，一次列出**全部**问题（含告警），并给出退出码：

- `0` 无阻断项（可能仍有告警）
- 非 `0` 有阻断项

适合放在 CI 或提交前的 pre-commit 里。它比 `generate --dry-run` 多检查**资产就绪度**
（会联网盘点后端）。

### 4.4 `graph` —— 物化计算图（给外部框架）

```bash
# 写到配置的图目录（solution.minimax_h3.graph_dir，未配置则 <工作区>/var/graphs）
om-bridge graph -s minimax_h3.t2v -p "..."

# 指定路径
om-bridge graph -s minimax_h3.ref2v --ref-image a.png --ref-image b.png -o /tmp/h3.json

# 拿到路径 + 输出节点（脚本里用）
om-bridge graph -s minimax_h3 -p "..." --json
# → {"ok":true,"mode":"t2v","output_node":"15","nodes":15,"path":"...","fingerprint":"..."}
```

**为什么需要这个命令？** 有些框架（如 OpenMontage）只接受"一个工作流文件路径 + 一个输出节点 ID"，
无法接受内存里的图。这个命令现场生成文件，所以**永远不会出现"仓库里的 JSON 与代码脱节"**。

⚠ 此命令**不联网**：物化是"把代码里的构造结果写出来"，与后端是否可达无关。

### 4.5 `run` —— 执行外部现成的载荷

```bash
om-bridge run --payload /tmp/graph.json --output-node 15 --label my-job
cat graph.json | om-bridge run --payload - --output-node 15
```

用于"图是别处生成的"。它**不经过方案层**，所以没有参数级校验，只有后端静态校验 ——
**别关掉它**（没有 `--no-preflight`，只有 `--no-validate`）。

### 4.6 `resume` —— 续等超时作业

```bash
om-bridge resume --job-id <prompt_id> --timeout 1800
```

⚠ **超时不是失败。** 超时后正确做法是 `resume`，而不是重新 `generate`：
重提会向 ComfyUI 队列塞入第二个任务，两个任务抢同一块 GPU，两边都变慢。
`JobTimeoutError` 会带上 `job_id`，`--json` 输出里也有。

### 4.7 `probe` / `list` / `describe` —— 自省

```bash
om-bridge probe                 # 连通性 + 节点/资产盘点（含按模式就绪度）
om-bridge probe --json          # 机器可读
om-bridge list                  # 后端、方案、别名
om-bridge describe              # 全部方案与后端的完整自描述
om-bridge describe minimax_h3   # 某个方案：参数 schema + 槽位 + 资产需求 + 就绪度
```

### 4.8 `config` —— 配置自省

```bash
om-bridge config list                                # 全部配置项与当前取值
om-bridge config explain backend.comfyui.server_url  # 这个值是哪来的？
om-bridge config report                              # 完整报告（含"未登记变量"）
```

> `config report` 里的**未登记变量**一栏最有价值：它会指出"你写的这个环境变量
> 程序根本不读"。遇到"我改了配置却没生效"，先跑这个，别靠猜。

### 4.9 `doctor` —— 体检

```bash
om-bridge doctor
om-bridge doctor --skip-probe       # 离线
```

按**配置 → 注册表 → 连通性 → 就绪度 → 结论**采集事实，最后给出优先级建议。
排障第一步永远是它。

---

## 5. MCP 用法（给 Agent / 框架）

### 5.1 客户端配置

stdio 服务，命令就是可执行名（未安装时用 python 绝对路径 + 模块名）：

```json
{
  "mcpServers": {
    "om-bridge": {
      "command": "om-bridge-mcp",
      "args": [],
      "env": {
        "OM_BRIDGE_COMFYUI_SERVER_URL": "http://192.168.3.3:8188",
        "NO_PROXY": "192.168.3.3,127.0.0.1,localhost"
      }
    }
  }
}
```

未安装时：

```json
{
  "mcpServers": {
    "om-bridge": {
      "command": "/path/to/python3",
      "args": ["-m", "om_bridge.interfaces.mcp"],
      "env": { "PYTHONPATH": "/path/to/OM-Bridge/src" }
    }
  }
}
```

> ⚠ `NO_PROXY` 这一条不是可选项。主机上有 HTTP 代理时，代理会劫持局域网流量，
> 症状是"后端明明在同网段却连不上"。

### 5.2 工具清单（7 个）

| 工具 | 用途 | 何时调 |
|---|---|---|
| `om_bridge_list` | 列出后端/方案/别名 | 想知道"有什么可用" |
| `om_bridge_describe` | 某方案/后端的完整自描述 | 想确认参数与槽位 |
| `om_bridge_check` | 连通性 + 资产盘点 + **按模式就绪度** | **生成之前**（避免排队后才发现缺模型） |
| `om_bridge_validate` | 只校验，返回全部问题 | 拿不准参数时 |
| `om_bridge_generate_video` | 执行视频生成 | 主入口 |
| `om_bridge_run_workflow` | 执行现成载荷 | 图是别处生成的 |
| `om_bridge_resume` | 续等超时作业 | 收到超时后 |

`om_bridge_generate_video` 的 `inputSchema` **由注册表自动合成**：所有 `kind == "video"` 的方案的参数
会并集进来，`solution` 字段带上可选值枚举。所以新增视频方案后，Agent 会自动看到新参数。

### 5.3 Agent 推荐调用序列

```
1. om_bridge_check                                   # 先确认环境
2. om_bridge_generate_video {solution, prompt, ...}  # 生成
   ├─ 若返回校验问题 → 修参数后重试
   ├─ 若超时 → om_bridge_resume {job_id}
   └─ 若成功 → 拿到 artifacts 路径与 provenance
```

`om_bridge_check` 的返回里含 `readiness`（按模式），Agent 应据此选模式：
`ref2v` 显示缺 `ref2va` 权重时，应改用 `t2v`，而不是硬跑一遍再失败。

---

## 6. Python SDK

```python
from om_bridge import Session, GenerationRequest, MediaAsset, MediaKind, load_config

request = GenerationRequest(
    solution="minimax_h3.ref2v",
    prompt="<Picture 1> 中的角色抬起头",
    params={"width": 608, "height": 352, "length": 5, "seed": 42},
    media=[MediaAsset(role="reference_image", kind=MediaKind.IMAGE,
                      path="丹华_角色模型.png")],
)

with Session(load_config()) as session:
    result = session.run(request)          # 全流程
    if result.ok:
        for art in result.artifacts:
            print(art.local_path, art.kind)
        print(result.provenance.to_dict())  # 这次到底用了什么权重/参考
    else:
        for issue in result.issues:
            print(issue.severity.value, issue.message, issue.hint)
```

需要更细的控制（在提交前后插入逻辑）时用分步 API：

```python
from om_bridge import Session, load_config
from om_bridge.core.errors import JobTimeoutError

with Session(load_config()) as session:
    prepared = session.prepare(request)     # 校验失败在这里抛
    handle = session.submit(prepared)
    print("job_id =", handle.job_id)         # ★ 记下来，超时后要用
    try:
        status = session.wait(handle)
    except JobTimeoutError as exc:
        status = session.resume(exc.job_id, timeout=1800)   # ✅ 续等
    result = session.collect(handle, status)
```

**只想看问题不想跑**：

```python
with Session(load_config()) as session:
    issues = session.validate(request)       # 保证不抛异常，返回全部问题
    for i in issues:
        print(i.severity.value, i.code, i.message, "→", i.hint)
```

---

## 7. 典型任务配方

### 配方 A：跨机生成（后端在另一台机器，模型已常驻）

```bash
export OM_BRIDGE_COMFYUI_SERVER_URL=http://192.168.3.3:8188
export NO_PROXY=192.168.3.3,127.0.0.1,localhost
om-bridge doctor
om-bridge generate -s minimax_h3.ref2v -p "..." --reference-image ref.png
```

### 配方 B：为只认"文件 + 节点 ID"的框架准备载荷

```bash
OUT=$(om-bridge graph -s minimax_h3.t2v -p "..." --json | python3 -c \
      'import json,sys; d=json.load(sys.stdin); print(d["path"], d["output_node"])')
echo "workflow_path + output_node = $OUT"
```

⚠ 生成后若要**重复提交同一份图**，必须换 `seed` 重新物化，否则会命中后端整图缓存秒回旧片。

### 配方 C：CI 里做提交前门禁

```bash
set -e
om-bridge validate -s "$SOLUTION" -p "$PROMPT" --json > /tmp/report.json || {
    python3 -c 'import json;d=json.load(open("/tmp/report.json"));
print("\n".join(f"[{i[\"severity\"]}] {i[\"message\"]}" for i in d.get("issues",[])))'
    exit 1
}
```

### 配方 D：批量跑不同参数

```bash
for W in 608 864; do
  for SEED in 1 2 3; do
    om-bridge generate -s minimax_h3.t2v -p "$PROMPT" \
        --width $W --height $((W*9/16)) --length 5 --set seed=$SEED \
        --output-dir "out/${W}_${SEED}" --json
  done
done
```

> 每次都必须换 `seed`：同样的载荷会被后端缓存秒回，看起来"快得可疑"。

### 配方 E：只改配置就能换权重（不改代码）

```bash
# 在 .env 里
OM_BRIDGE_SOLUTION_MINIMAX_H3_STEPS=4
OM_BRIDGE_SOLUTION_MINIMAX_H3_TURBO_LORA=minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors
```

配置的默认值**优先于**代码内置默认值（见 [core.schema](module-reference.md#coreschema)），
所以"这台机器该用哪套权重"是部署事实，不需要动代码。

---

## 8. 退出码与错误处理

| 码 | 含义 | 你该做什么 |
|---|---|---|
| `0` | 成功 | — |
| `1` | 入参/命令行错误 | 看 `--help`，修参数 |
| `2` | 后端不可达 | 查地址、网络、`NO_PROXY`；跑 `doctor` |
| `3` | 载荷非法 | 看报出的具体节点/输入名 |
| `4` | 后端拒绝受理 | 载荷与后端实际能力不符（多半是缺节点或模型名写错） |
| `5` | **超时** | 用 `resume --job-id` **续等**，别重提 |
| `6` | 渲染失败 | 看后端日志；多半是显存/权重不匹配 |
| `7` | 配置错误 | `config explain` / `config report` |

所有错误都带 `stage`（`config`/`registry`/`validate`/`upload`/`submit`/`poll`/`fetch`），
`--json` 输出里也有 —— 用它定位"卡在哪一段"，比读堆栈快。

---

## 9. 选项速查

### 媒体输入（由注册表自动生成）

| 槽位 | 可用写法 | 含义 |
|---|---|---|
| `reference_image` | `--reference-image` / `--ref-image` | 参考图，**可重复，顺序有意义** |
| `first_frame` | `--first-frame` / `--first-image` / `--image` | 首帧 |
| `last_frame` | `--last-frame` / `--last-image` | 末帧 |
| 任意 | `--media 角色=路径` | 通用形式（未声明过的角色） |
| 任意 | `--media-remote 角色=远端名` | 文件已在后端素材目录里，跳过上传 |

> **顺序为什么有意义？** 参考图的顺序决定提示词里 `<Picture N>` 指哪一张。
> 传两张参考图时，`--reference-image A.png --reference-image B.png`
> 对应 `<Picture 1>` = A、`<Picture 2>` = B。

### 方案参数（以 minimax_h3 为例，`om-bridge describe` 可查最新）

```bash
om-bridge generate -s minimax_h3 -p "..." --help    # 该方案的全部参数选项
```

常见：`--width`、`--height`、`--length`（帧数）、`--fps`、`--steps`、`--seed`、
`--turbo`、`--ref-image-size`、`--filename-prefix`、`--sampler`、`--scheduler`。

参数的含义与约束见 [solutions/minimax-h3.md](solutions/minimax-h3.md)。
