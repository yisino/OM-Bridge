# 排障手册

> **按症状查**。每条都先给"怎么确认"，再给"为什么"，最后给"怎么修"。

## 0. 排障永远的第一步

```bash
om-bridge doctor              # 配置 → 注册表 → 连通性 → 就绪度 → 结论
om-bridge doctor --skip-probe # 离线时用
om-bridge config report       # 含"未登记变量"（你写的变量没被读取时会出现在这里）
om-bridge -v <命令>            # 要堆栈时加 -v
```

`doctor` 的输出已经按"优先级建议"排好。**先读它，再往下翻本文**。

---

## 1. 连不上后端

### 症状：`EXIT_UNREACHABLE` / `probe` 报 `reachable: false`

**确认**：

```bash
om-bridge config explain backend.comfyui.server_url   # 值是多少？哪来的？
curl --noproxy '*' -m 5 http://<那个地址>/system_stats
```

**先看三条**（按命中率排序）：

| 检查 | 说明 |
|---|---|
| ① `server_url` 的值真是你想要的吗 | 最常见的错误是"我改了 `.env` 但程序读的是别的文件"。`config explain` 会告诉你来源 |
| ② 代理劫持 | 主机上有 HTTP 代理时，代理会拦掉局域网流量。**必须在 env 里设 `NO_PROXY=<后端IP>,127.0.0.1,localhost`**，用 curl 验证时加 `--noproxy '*'` |
| ③ 后端监听地址 | ComfyUI 若是 `--listen 127.0.0.1`，外部连不上。在**后端机器上**跑 `ss -ltnp \| grep 8188` 看监听地址 |

**不要**用 `curl` 不带 `--noproxy` 去测 —— 你会看到 502 并误以为是网络不通
（本项目历史上就被这个误导过一次，差点得出"需要跨网段隧道"的错误结论）。

**跨网段？先确认是不是真的跨**：

```bash
ip route get <后端IP>     # 去程走哪个网卡、源地址是什么
ip -brief addr            # 本机有哪些地址
```

很多"跨网段"其实是**本机另有一张同网段网卡**，直连就通。

---

## 2. 参数/校验相关

### 症状：`validate` 报一堆问题，但不知道怎么改

每条 `Issue` 都带 `code` / `hint` / `location`：

```bash
om-bridge validate -s minimax_h3.ref2v -p "..." --json \
  | python3 -c 'import json,sys
for i in json.load(sys.stdin).get("issues", []):
    print(f"[{i[\"severity\"]}] {i.get(\"code\")} @ {i.get(\"location\")}\n  {i[\"message\"]}\n  → {i.get(\"hint\")}")'
```

`hint` 是给"下一步该做什么"的。Agent 场景下尤其重要——它可以直接照做。

### 症状：给 t2v 传了 `--ref-image`，被忽略了

这是**正确的行为**：模式不使用的参数会被丢弃并给出**告警**（不是静默忽略）。
若你没看到告警，说明是 `--set` 传的（`--set` 用于未登记参数，不走模式过滤）。

确认某个参数在当前模式下是否有效：

```bash
om-bridge describe minimax_h3     # 看 schema 里每个参数的 applies_to
```

### 症状：`--set` 传的值没生效

`--set` 的值会做 JSON 解析（`"4"` → `4`，`"true"` → `true`，`"[1,2]"` → list）。
但**它不校验、也不过模式过滤**，只是塞进 `params`。若方案的 `resolve()` 不认这个键，
它会被丢弃。请改用 `om-bridge describe` 查有没有对应参数。

### 症状：ComfyUI 报 400，`Unknown input` / `Required input is missing`

载荷与后端实际能力不符。**先跑**：

```bash
om-bridge validate -s <方案> -p "..."     # 含载荷静态校验
om-bridge run --payload bad.json --output-node 15    # 用 run 复现时，别加 --no-validate
```

常见原因，按概率排序：

1. **模型文件名写错**（`--set unet=...` 或配置里改了权重名）→ `code = "graph.bad_enum"`，
   报错会**列出合法取值**（来自后端实际扫描到的文件列表）。
2. **自定义节点的动态槽位名写错**。如 `ref_images.ref_image_0`：
   父名 + `.` + `ref_image_<i>`。这是 `COMFY_AUTOGROW_V3` 的规则
   （依据 `comfy_api/latest/_io.py` 的 `finalize_prefix()`）。
   ⚠ `/object_info` **只暴露父名** `ref_images`，看不到槽位 —— 别试图从那里推断。
3. **必填输入缺失**（如 `ref_image_size`）。

---

## 3. 生成结果不对

### 症状：结果与预期无关 / 参考图似乎没生效

**提交成功 ≠ 参考图被消费。** 可靠的验证方式是 **A/B 对照**：
固定 `seed` / `prompt` / 尺寸 / `steps`，**只替换参考图**，比对输出。

```bash
om-bridge generate -s minimax_h3.ref2v -p "<Picture 1> 中的角色" \
    --reference-image A.png --set seed=42 --width 608 --height 352 --length 5 \
    --output-dir out/A
om-bridge generate -s minimax_h3.ref2v -p "<Picture 1> 中的角色" \
    --reference-image B.png --set seed=42 --width 608 --height 352 --length 5 \
    --output-dir out/B
```

两边输出**逐字节相同** → 参考图没生效；**明显不同** → 生效。
（本项目实测过一次：`丹华` vs `赤焰` 的两份输出 raw RGB 有 97.45% 字节不同、
平均绝对差 37.7/255，这才算证明了参考图贯穿采样。）

参考图没生效的常见原因：

- 用了 t2v 模式却传 `--ref-image`（该模式不用参考图）→ 应改 `-s minimax_h3.ref2v`。
- **权重混用**：ref2v 必须用 `ref2va` 系列权重 + 4 步 ref2v LoRA。
  用 `fl2va` 权重能跑但结果不对。方案会在参数层面直接拦（抛错），
  但如果你用 `--node-override` 硬改，就绕过了这层保护。

### 症状：提示词里的 `<Picture 1>` 指错了图

**参考图顺序 = 槽位顺序**，`--reference-image` 可重复，顺序即 `<Picture N>` 的编号：

```bash
--reference-image A.png --reference-image B.png   # A→Picture 1, B→Picture 2
```

`describe` 返回里带 `ref_tag_hint`（MCP 也有），按它写提示词最稳。
ref2v 对措辞敏感，要**明说"哪张参考驱动镜头的哪一部分"**。

### 症状：没有音轨

H3 是**音视频联合生成**。缺音轨八成是 `audio_vae` 没接或权重名不对：

```bash
om-bridge config explain solution.minimax_h3.audio_vae
om-bridge probe --json | grep -i audio_vae
```

---

## 4. 性能与"快得不正常"

### 症状：渲染只用了 0.4 秒，产物和上次一模一样

**命中了后端的整图缓存**：提交与上次**逐字节相同**的载荷会秒回并复用原文件名。

`poll()` 在耗时 < 2s 时会打一条缓存疑点日志。看到它就**换 `seed`**：

```bash
om-bridge generate -s minimax_h3 -p "..." --set seed=$RANDOM --json
```

**为什么这不只是"快"的问题**：你会误判性能、误判参考图是否生效、误判配置改动是否起作用。
"我改了参数结果没变"最常见的原因就是这个。

### 症状：第一次生成特别慢，之后快

首次要把权重从系统内存换入显存（`/system_stats` 里 `vram_free` 很小通常说明模型已常驻，
属正常现象）。**不要**把它当成显存泄漏。

优化方向（按收益排序）：

1. **让模型常驻**（别让后端在你两次调用之间释放模型）。
2. 调大 `OM_BRIDGE_POLL_INTERVAL`（后端慢时减少无谓请求）。
3. 降 `length`（帧数是线性成本）。
4. 只在需要身份保真时才把 `ref_image_size` 设成 `max`（保留 2048px，慢数倍）。

### 症状：超时了，我该重试吗？

**不该。** 超时是可恢复状态：

```bash
om-bridge resume --job-id <从错误或 --json 里拿到的 job_id> --timeout 1800
```

重提会向后端队列塞入第二个任务，两个任务**争抢同一块 GPU**，两边都变慢。
如果 `job_id` 丢了，用后端的队列接口查（`om-bridge probe` 会带队列摘要）。

---

## 5. 配置相关

### 症状：我改了配置，没生效

```bash
om-bridge config explain <键>     # 这个值最终从哪来
om-bridge config report           # 你写的变量是否被登记
```

三种经典原因：

1. **变量名写错**（未被登记）→ `config report` 会把它列在"未登记变量"里。
   本项目的原则是**不造 no-op 变量**：代码不读的变量不该出现在配置里。
2. **优先级搞反**：环境变量 > `.env`。你的 shell 里可能 export 了旧值（`env | grep COMFYUI`）。
3. **读的是另一个文件**：`config explain` 会显示来源文件路径。

### 症状：改了权重名，ComfyUI 说找不到

配置里的权重名必须与后端**实际扫描到的文件名**完全一致：

```bash
om-bridge probe --json | python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in ("diffusion_models","unet","loras","vae","text_encoders","clip"):
    if k in d.get("assets", {}):
        print(k, "→", ", ".join(d["assets"][k]))'
```

**文件名带不带扩展名？** 配置里默认值带 `.safetensors`，与后端枚举一致。改了名要整段照抄。

### 症状：`COMFYUI_BASE_URL` 报了废弃告警

它是**为兼容而保留的废弃别名**，且 OpenMontage 从不读这个名字。改用
`OM_BRIDGE_COMFY_SERVER_URL`（旧名 `OM_BRIDGE_COMFYUI_SERVER_URL` /
`COMFYUI_SERVER_URL` 仍有效）。
`config report` 会提示它，删掉即可。

### 症状：`.env` 里有 `COMFYUI_MINIMAX_H3_WORKFLOW_PATH` 之类的变量

**已移除，不再读取**。理由：它们存在的唯一意义是"外部框架要一个文件路径 + 节点 ID"，
现在由 `om-bridge graph` **现场物化**替代（生成即最新，永不脱节）。
直接删掉这些行；`config report` 会把它们列为未登记变量。

---

## 6. MCP 相关

### 症状：框架报"JSON 解析失败" / 工具列表为空

**99% 是 stdout 被污染。** MCP over stdio 要求 **stdout 只放 JSON-RPC**，日志必须走 stderr。

```bash
# 手动验证：往 stdin 喂一条 initialize，看 stdout 是不是纯净的 JSON
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  | om-bridge-mcp 2>/dev/null
```

输出应当是**单行 JSON**。若夹杂别的内容，找出谁在 `print()` 到 stdout ——
`core/logging.py` 保证了框架自身的日志走 stderr，所以问题在新增代码里。

### 症状：工具调用返回 `-32602` / `-32601` / `-32700`

| 码 | 含义 | 修法 |
|---|---|---|
| `-32602` | 未知工具名 | 用 `om_bridge_list` 看真实工具名 |
| `-32601` | 未知方法 | 只支持 `initialize` / `tools/list` / `tools/call` / `ping` |
| `-32700` | 消息不是合法 JSON | 检查客户端是否按行分隔（JSON-RPC 一行一条） |

服务端在这些情况下**不会崩**，会继续读下一行 —— 这是刻意设计（客户端不该因一次坏消息失联）。

### 症状：Agent 说"环境不可用"但明明能用

这是**就绪度误判**，本项目最危险的一类问题：错误的"不可用"判断会让 Agent
放弃可用的本地 GPU 路径，转而去调**收费的云端服务**。

```bash
om-bridge probe --json        # 看 readiness 逐模式结论
om-bridge describe minimax_h3 # 看资产需求与实际盘点
```

排查方向：某个模式报缺资产，但 `probe` 的 `assets` 里明明有 → 说明**权重名单与现实不符**。
名单在方案的 `manifest.py`（声明式），改那里，**不要**在后端里硬编码某方案的权重名。

---

## 7. 已知的坑（本项目踩过，别再踩）

| 坑 | 现象 | 真相 |
|---|---|---|
| **产物筛选** | 找不到视频产物 | ComfyUI 的 `SaveVideo` 把 mp4 挂在 `/history` 的 **`images`** 键下（还带 `"animated": [true]`），**不是 `video` 键**。按 `kind=="video"` 筛会漏 |
| **整图缓存** | 0.4s 返回、文件名不变 | 载荷逐字节相同即命中缓存。验真实性能必须换 `seed` |
| **动态槽位** | `POST /prompt` 直接 400 | `COMFY_AUTOGROW_V3` 的键是**点号形式**（`ref_images.ref_image_0`），`/object_info` 看不到槽位 |
| **代理劫持** | 同网段却连不上 | 主机代理拦掉 LAN 流量。设 `NO_PROXY`，curl 加 `--noproxy '*'` |
| **探针语义** | 节点明明存在却报缺失 | `probe.nodes` 是"关键节点清单"，不是"全量节点类清单"。问存在性要用 `has_node_class()` |
| **参数收集** | `Object of type function is not JSON serializable` | argparse 的 `set_defaults(func=...)` 会把函数塞进命名空间。CLI 用**白名单**收集参数 |
| **校验命令抛异常** | 只看到第一个问题 | `validate` 保证不抛异常、一次列全。别把它改成抛异常 |
| **管道的 stdout** | 脚本解析 JSON 失败 | `--json` 下 stdout 只有一份 JSON。`graph -o -` 会独占 stdout，别与 `--json` 同用 |

---

## 8. 还是不行？收集这些信息

```bash
{
  echo "=== version ===";      om-bridge --version
  echo "=== list ===";         om-bridge list --json
  echo "=== config ===";       om-bridge config report --json
  echo "=== doctor ===";       om-bridge doctor --json
  echo "=== failing cmd ===";  om-bridge -v <你的命令>
} > /tmp/om-bridge-diag.txt 2>&1
```

注：`config report` 对 `secret` 项做掩码，可以安全分享；但**分享前仍建议扫一眼**。
