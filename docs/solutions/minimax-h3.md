# 方案说明：MiniMax H3

> `minimax_h3` —— 本地开源的音视频联合生成模型，跑在 ComfyUI 后端上。
> 本文件是该方案的实现说明与参数详解。通用用法见 [../user-guide.md](../user-guide.md)。

## 1. 这是什么

H3 是**音视频联合生成**：一次推理同时产出视频与音频（不是后期配音）。
在 ComfyUI 上通过官方节点的本地权重实现，`backend_name = "comfyui"`。

```bash
om-bridge list                  # 看有没有 minimax_h3 及其别名
om-bridge describe minimax_h3   # 看完整 schema、槽位、资产与就绪度
```

> ⚠ 上游工具里 `minimax_h3` 有**两条完全不同的路**：`minimax_h3_local`（本地权重，
> 即本方案）与 `minimax_h3_api`（走 ComfyUI Partner Node 的云端推理，需要 Comfy 账号与额度）。
> 本方案只实现 **local**。选错路的症状是"生成了但计费了"。

## 2. 四种模式

| 模式 | 别名 | 用途 | H3 节点 | 扩散模型 | Turbo LoRA | steps | output_node |
|---|---|---|---|---|---|---|---|
| t2v | `minimax_h3` / `minimax_h3.t2v` | 文生视频 | `MiniMaxH3ImageToVideo` | `fl2va` 系列 | 8 步版 | **8** | `15` |
| i2v | `minimax_h3.i2v` | 首帧生视频 | 同上 | 同上 | 同上 | **8** | `16` |
| flf2v | `minimax_h3.flf2v` | 首尾帧生视频 | 同上 | 同上 | 同上 | **8** | `17` |
| ref2v | `minimax_h3.ref2v` | **参考图**生视频 | `MiniMaxH3ReferenceToVideo` | **`ref2va` 系列** | **4 步版** | **4** | `16`（单参考）/ `17`（双参考） |

### ref2v 与其余模式只差两点

1. H3 节点换成 `MiniMaxH3ReferenceToVideo`（输出同样是 `[CONDITIONING, LATENT]`，下游全部复用）
2. 多出若干 `LoadImage` 接到 `ref_images.ref_image_<i>`

⚠ **权重不可混用。** ref2v 必须配 `ref2va` 扩散模型 + 4 步 ref2v LoRA；其余模式用 `fl2va` + 8 步 LoRA。
混用能跑但结果不对，所以 `builder.py` 在混用时**直接抛错**（不给你"能跑但不对"的机会）。

`output_node` 为**字符串**（`"15"` 而非 `15`）—— 部分外部框架对此敏感。

## 3. 参数

| 参数 | CLI | 约束 | 说明 |
|---|---|---|---|
| `mode` | `-s minimax_h3.<模式>` | 四选一 | 生成形态 |
| `prompt` | `-p` | — | 提示词（支持 `<Picture N>`，见 §5） |
| `width` | `--width` | **32 的倍数**，≤1344 | 默认 864 |
| `height` | `--height` | **32 的倍数** | 默认 480 |
| `length` | `--length` | **满足 `n % 17 == 5`**，5…3600 | 帧数，24fps。默认 124（≈5.2s） |
| `fps` | `--fps` | — | 封装帧率，默认 24 |
| `steps` | `--steps` | 与 LoRA 配对 | `0`=按模式自动（ref2v→4，其余→8） |
| `seed` | `--set seed=N` | — | **每次都要换**，否则命中后端整图缓存 |
| `turbo` | `--turbo` | bool | 是否挂 Turbo LoRA |
| `ref_image_size` | `--ref-image-size` | `match` / `max` | 仅 ref2v。`match` 缩到生成分辨率（快）；`max` 保留 2048px 短边（身份更保真、**慢数倍**） |
| `filename_prefix` | `--filename-prefix` | — | 后端侧文件名前缀 |
| `sampler` / `scheduler` | 同名选项 | — | 高级覆盖 |

`length` 的栅格最容易记错：合法值是 `5, 22, 39, 56, …, 124, …`（**不是** 17 的倍数，
而是 `17k + 5`）。传 120 会被吸附到最近的合法值并给出告警。

查最新可选项：

```bash
om-bridge generate -s minimax_h3.ref2v --help     # 只显示该模式相关的参数
```

## 4. 媒体槽位与资产

| 槽位 | CLI | 模式 | 说明 |
|---|---|---|---|
| `first_frame` | `--first-frame` / `--first-image` / `--image` | i2v / flf2v | 首帧 |
| `last_frame` | `--last-frame` / `--last-image` | flf2v | 末帧 |
| `reference_image` | `--reference-image` / `--ref-image` | ref2v | 参考图，**可重复，顺序有意义** |

上限：参考图 ≤ 9，参考视频 ≤ 3，独立音频 ≤ 3（本方案只用到图）。

必需资产（按模式区分，`probe` 会逐模式报告缺什么）：

| 类别 | t2v/i2v/flf2v | ref2v |
|---|---|---|
| 扩散模型 | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| Turbo LoRA | `minimax_h3_fl2v_turbo_8step_v1.0_..._bf16.safetensors` | `minimax_h3_ref2v_turbo_4step_v0.1_..._bf16.safetensors` |
| 文本编码器 | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 同 |
| 视频 VAE | `minimax_h3_video_vae_fp16.safetensors` | 同 |
| 音频 VAE | `minimax_h3_audio_vae_fp32.safetensors` | 同 |
| 节点 | `MiniMaxH3ImageToVideo` | `MiniMaxH3ReferenceToVideo` |

权重文件名可用配置覆盖（`OM_BRIDGE_SOLUTION_MINIMAX_H3_UNET` 等），
**配置的值优先于代码内置默认**。

**"t2v 能跑但 ref2v 报错"** 最常见的原因就是缺 `ref2va` 权重 —— `om-bridge probe` 会逐模式告诉你。

## 5. 提示词语法

参考媒体用**连接顺序**编号：

- `<Picture 1>` / `<Picture 2>` … 对应 `--reference-image` 的第 1、2 张
- `<Video N>` / `<Audio N>` 同理（本方案未用到）

```bash
om-bridge generate -s minimax_h3.ref2v \
    -p "<Picture 1> 中的角色抬起头，背景保持不变，缓慢推镜" \
    --reference-image A.png --width 608 --height 352 --length 5
```

ref2v **对措辞敏感**，务必明说"哪张参考驱动镜头的哪一部分"。
`describe` 与 MCP 返回里都带 `ref_tag_hint`，按它写最稳。

多参考示例：

```bash
om-bridge generate -s minimax_h3.ref2v \
    -p "<Picture 1> 的人物走进 <Picture 2> 的场景" \
    --reference-image 人物.png --reference-image 场景.png
```

## 6. 实测性能（供估算）

| 配置 | 耗时 | 产物 |
|---|---|---|
| 608×352 / 5 帧 / 8 步（t2v） | ≈52s | h264 + **AAC 双声道**，0.208s，32KB |
| 864×480 / 124 帧 / 8 步（t2v） | ≈100s | 5.167s，425–796KB |
| 608×352 / 5 帧 / 4 步（ref2v） | ≈42.5s | 34KB |
| 864×480 / 124 帧 / 4 步（ref2v） | ≈95s | 1.07MB @1.65Mbps |
| 608×352 / 5 帧（SER 跨机，含上传参考图） | ≈30s | 模型已常驻故更快 |

参考硬件：RTX 3080 20GB。**帧数是线性成本**，时长砍半基本就是耗时砍半。

⚠ **验性能前必须换 `seed`**：同样的载荷会被后端缓存秒回（0.4s），
你会得出"改了参数没变化"的错误结论。

## 7. 实现结构

```
solutions/minimax_h3/
├── manifest.py     # 常量与声明：MODES / 资产需求 / 栅格约束 / steps_for()
├── builder.py      # build_h3_graph(...) -> (graph, output_node)；纯函数
├── solution.py     # MinimaxH3Solution：schema / slots / config_defaults / build_job / provenance
└── templates/      # 5 份已核对载荷，用作结构回归基准
    ├── minimax_h3_t2v_api.json
    ├── minimax_h3_i2v_api.json
    ├── minimax_h3_flf2v_api.json
    ├── minimax_h3_ref2v_api.json
    └── minimax_h3_ref2v_2ref_api.json
```

**`templates/` 是回归护栏**：`tests/test_builder_regression.py` 会把 `builder` 的产出与它们
逐节点/逐键序比对。改动 `builder.py` 后这个测试必须仍然通过 —— 否则说明你无意中改变了
已经过真机验证的行为（节点数、节点 ID、键序都可能被外部框架依赖）。

## 8. 必须知道的三个实现事实

### ① `COMFY_AUTOGROW_V3` 的动态槽位是「点号键」

`MiniMaxH3ReferenceToVideo` 的 `ref_images` 是 `COMFY_AUTOGROW_V3`（`prefix="ref_image_"`, `max=9`），
在 API 格式载荷里的合法键是：

```
ref_images.ref_image_0  …  ref_images.ref_image_8
```

依据：`comfy_api/latest/_io.py` 的 `finalize_prefix()` 用 `"."` 连接父名与 `prefix+index`。

⚠ `/object_info` **只暴露父名** `ref_images`，看不到槽位 —— 不要试图从那里推断。
写错槽位名会导致 `POST /prompt` **直接 400**（ComfyUI 对未知输入是报错而非忽略）。

同类：`ref_videos.ref_video_{0..2}`、`ref_video_audios.ref_video_audio_{0..2}`、`ref_audios.ref_audio_{0..2}`。

### ② 产物在 `/history` 里挂在 `images` 键下

ComfyUI 的 `SaveVideo` 节点返回的 mp4 在 `outputs` 里以 **`images`** 键出现
（还带 `"animated": [true]`），**不是 `video` 键**。按 `kind == "video"` 筛会漏掉产物。
本项目的 `graph.collect_outputs()` 已处理这个特例。

### ③ 后端有整图缓存

提交与上次**逐字节相同**的载荷会秒回（< 2s）并复用原文件名。
`poll()` 在耗时 < `CACHE_SUSPECT_SECONDS`（2.0s）时会打诊断日志。

## 9. 参考图是否生效：怎么证明

**提交成功 ≠ 参考图被消费。** 唯一可靠的证明是 **A/B 对照**：
固定 `seed` / `prompt` / 尺寸 / `steps`，**只替换参考图**，逐字节比对输出。

本项目实测过一次：固定其余全部条件，`丹华` 与 `赤焰` 两张参考图的两份输出
raw RGB 有 **97.45% 字节不同、平均绝对差 37.7/255** → 参考图确实贯穿每一步采样
（辅以抽帧肉眼判读：红袍金纹、凤冠、金色光剑，身份锁定成立）。

反过来，若两份输出**逐字节相同** → 参考图没生效。此时检查：
模式是否为 `ref2v`、权重是否误用 `fl2va`、槽位键是否写错。

## 10. 溯源（provenance）

每次生成都会记录影响输出的全部输入：

```json
{
  "solution": "minimax_h3", "mode": "ref2v", "backend": "comfyui",
  "params": { "width": 608, "height": 352, "length": 5, "steps": 4, "seed": 42 },
  "extra": {
    "weights": { "diffusion_model": "...ref2va...", "turbo_lora": "...4step...", "clip": "...qwen3vl...", "video_vae": "...", "audio_vae": "..." },
    "reference_tags": ["<Picture 1>"]
  }
}
```

**这是复现实验的唯一依据。** 少了任何一项，三个月后就无法复现。
用 `--json` 拿它，或在 SDK 里读 `result.provenance.to_dict()`。
