"""MiniMax H3 计算图构建器 —— 把参数摊平成一张 ComfyUI **API 格式**的图。

为什么必须自己构建，而不能直接用官方模板
----------------------------------------
ComfyUI 随包提供的 H3 模板（``video_minimax_h3_*.json``）有两个问题：

1. 它们是 **UI 格式**（顶层有 ``nodes`` / ``links``），后端只吃 API 格式；
2. 它们还用了**纯前端便利节点** —— 子图（subgraph）、``ComfySwitchNode``、
   ``PrimitiveInt`` / ``PrimitiveBoolean`` / ``ComfyMathExpression``。
   这些东西在提交给后端时无法解析，它们的唯一作用是让界面上的连线好看。

所以这里**直接构建扁平图**：没有子图、没有便利节点、所有值内联。
结果是提交时没有任何需要展开的东西，也就没有"展开失败"这类故障模式。

节点编号（不要随意改动）
------------------------
编号是**顺序推导**出来的，不是硬编码，但它决定了产出的"产出节点 ID"：

======  ====================================  ==========================
模式    图像输入节点                          产出节点（SaveVideo）
======  ====================================  ==========================
t2v     —                                     15
i2v     ``first_frame`` = 6                    16
flf2v   ``first_frame`` = 6, ``last_frame``=7  17
ref2v   ``ref_image_0`` = 6 （…最多到 14）      16（单参考）/ 17（双参考）
======  ====================================  ==========================

因为多了图像加载节点会把后续节点整体往后推，所以 ``output_node`` 必须
**由构建器返回**，调用方不该自己猜。外部框架（OpenMontage）把这个返回值
当作 ``output_node`` 参数使用，猜错会导致"跑完了但找不到产物"。

参考图槽位用**点号动态键**（``ref_images.ref_image_0``），依据见
:mod:`om_bridge.backends.comfyui.graph` 的模块文档。
"""

from __future__ import annotations

from typing import Any

from .....core.errors import ValidationError
from .....core.models import Issue
from . import manifest
from .manifest import MODE_NAMES, MODES, ModeSpec


def build_h3_graph(
    *,
    mode: str = manifest.DEFAULT_MODE,
    prompt: str,
    width: int,
    height: int,
    length: int,
    seed: int = 0,
    steps: int | None = None,
    lora: str | None = "auto",
    lora_strength: float = 1.0,
    filename_prefix: str = manifest.DEFAULT_FILENAME_PREFIX,
    unet_name: str | None = None,
    clip_name: str | None = None,
    video_vae: str | None = None,
    audio_vae: str | None = None,
    sampler: str = manifest.DEFAULT_SAMPLER,
    scheduler: str = manifest.DEFAULT_SCHEDULER,
    fps: int = 24,
    first_frame_image: str | None = None,
    last_frame_image: str | None = None,
    ref_images: list[str] | None = None,
    ref_image_size: str = "match",
    weight_dtype: str = manifest.DEFAULT_WEIGHT_DTYPE,
) -> tuple[dict[str, dict[str, Any]], str]:
    """构建 H3 图，返回 ``(graph, output_node_id)``。

    ``output_node_id`` 是产出成品的那个 ``SaveVideo`` 节点的 **字符串** ID。

    几个参数允许传 ``None`` 表示"按模式自动决定"，这样调用方不必知道
    ref2v 用的是另一套权重和另一个步数：``unet_name`` / ``clip_name`` /
    ``video_vae`` / ``audio_vae`` / ``lora`` / ``steps``。
    """
    mode = (mode or manifest.DEFAULT_MODE).lower()
    if mode not in MODE_NAMES:
        raise ValidationError(
            f"未知模式 {mode!r}",
            issues=[Issue.error(f"不支持的 {mode!r}", code="h3.bad_mode",
                                hint=f"可选：{' | '.join(MODE_NAMES)}")],
        )
    spec: ModeSpec = MODES[mode]

    _validate_inputs(mode, spec, first_frame_image, last_frame_image, ref_images, ref_image_size)

    defaults = manifest.ASSET_DEFAULTS
    unet_name = unet_name or defaults[spec.weight_role]
    clip_name = clip_name or defaults["clip"]
    video_vae = video_vae or defaults["video_vae"]
    audio_vae = audio_vae or defaults["audio_vae"]

    # ref2v 用另一套权重。混用不会报错，只会产出"能跑但不对"的结果，
    # 所以在构建期就拦住 —— 这是实测踩过的坑，值得一道硬闸。
    if mode == "ref2v" and "ref2v" not in unet_name.lower():
        raise ValidationError(
            f"ref2v 需要带参考能力的权重，但 unet_name={unet_name!r}",
            issues=[Issue.error(
                "权重与模式不匹配", code="h3.wrong_weights", location="unet_name",
                hint=f"应使用 {defaults['ref2v_unet']!r}；"
                     f"t2v/i2v/flf2v 用的 fl2va 是**另一套权重**",
            )],
        )

    if lora == "auto":
        lora = defaults[spec.lora_role] if spec.lora_role else None
    effective_steps = manifest.steps_for(mode, lora, steps)

    graph: dict[str, dict[str, Any]] = {}

    # --- 模型栈（节点 1–5） -------------------------------------------------
    graph["1"] = {
        "class_type": "UNETLoader",
        "_meta": {"title": "Load H3 diffusion model"},
        "inputs": {"unet_name": unet_name, "weight_dtype": weight_dtype},
    }
    graph["2"] = {
        "class_type": "CLIPLoader",
        "_meta": {"title": "Load Qwen3-VL text encoder (minimax)"},
        "inputs": {"clip_name": clip_name, "type": "minimax", "device": "default"},
    }
    graph["3"] = {
        "class_type": "VAELoader",
        "_meta": {"title": "Load H3 video VAE"},
        "inputs": {"vae_name": video_vae},
    }
    graph["4"] = {
        "class_type": "VAELoader",
        "_meta": {"title": "Load H3 audio VAE"},
        "inputs": {"vae_name": audio_vae},
    }

    # Turbo LoRA 是可选的。存在时它成为调度器与引导器的模型来源 ——
    # 注意是两者都改，只改一个会导致步数语义与实际去噪强度不一致。
    if lora:
        graph["5"] = {
            "class_type": "LoraLoaderModelOnly",
            "_meta": {"title": f"Turbo LoRA ({effective_steps}-step)"},
            "inputs": {
                "model": ["1", 0],
                "lora_name": lora,
                "strength_model": lora_strength,
            },
        }
        model_ref: list[Any] = ["5", 0]
    else:
        model_ref = ["1", 0]

    # --- 条件与潜变量（H3 节点同时产出两者） --------------------------------
    h3_inputs: dict[str, Any] = {
        "clip": ["2", 0],
        "prompt": prompt,
        "width": width,
        "height": height,
        "length": length,
    }

    next_id = 6
    if mode == "ref2v":
        # 参考节点上 vae / audio_vae 都是**可选**输入。
        # 两者都接（官方模板的做法）时参考图会同时影响视觉与音频条件；
        # 只接 vae 时参考图仅作用于文本编码器。这里跟随官方模板。
        h3_inputs["vae"] = ["3", 0]
        h3_inputs["audio_vae"] = ["4", 0]
        h3_inputs["ref_image_size"] = ref_image_size
        for index, ref in enumerate(ref_images or []):
            node_id = str(next_id)
            graph[node_id] = {
                "class_type": "LoadImage",
                "_meta": {"title": f"ref_image_{index} ({ref})"},
                "inputs": {"image": ref},
            }
            # 动态槽位：父名 + "." + 前缀 + 序号（COMFY_AUTOGROW_V3 的服务端展开形式）
            h3_inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]
            next_id += 1
    else:
        h3_inputs["vae"] = ["3", 0]
        if first_frame_image:
            graph[str(next_id)] = {
                "class_type": "LoadImage",
                "_meta": {"title": "first_frame"},
                "inputs": {"image": first_frame_image},
            }
            h3_inputs["first_frame"] = [str(next_id), 0]
            next_id += 1
        if last_frame_image:
            graph[str(next_id)] = {
                "class_type": "LoadImage",
                "_meta": {"title": "last_frame"},
                "inputs": {"image": last_frame_image},
            }
            h3_inputs["last_frame"] = [str(next_id), 0]
            next_id += 1

    h3_id = str(next_id)
    graph[h3_id] = {
        "class_type": spec.h3_class,
        "_meta": {"title": spec.title},
        "inputs": h3_inputs,
    }

    # --- 采样与输出（节点 H3+1 … H3+9） ------------------------------------
    def node_id(offset: int) -> str:
        return str(int(h3_id) + offset)

    noise_id, sampler_id = node_id(1), node_id(2)
    scheduler_id, guider_id = node_id(3), node_id(4)
    sample_id, video_decode_id = node_id(5), node_id(6)
    audio_decode_id, create_id, save_id = node_id(7), node_id(8), node_id(9)

    graph[noise_id] = {
        "class_type": "RandomNoise",
        "_meta": {"title": "Noise"},
        "inputs": {"noise_seed": seed},
    }
    graph[sampler_id] = {
        "class_type": "KSamplerSelect",
        "_meta": {"title": "Sampler"},
        "inputs": {"sampler_name": sampler},
    }
    graph[scheduler_id] = {
        "class_type": "BasicScheduler",
        "_meta": {"title": "Scheduler"},
        "inputs": {"model": model_ref, "scheduler": scheduler,
                   "steps": effective_steps, "denoise": 1.0},
    }
    graph[guider_id] = {
        "class_type": "BasicGuider",
        "_meta": {"title": "Guider"},
        "inputs": {"model": model_ref, "conditioning": [h3_id, 0]},
    }
    graph[sample_id] = {
        "class_type": "SamplerCustomAdvanced",
        "_meta": {"title": "Sample"},
        "inputs": {
            "noise": [noise_id, 0],
            "guider": [guider_id, 0],
            "sampler": [sampler_id, 0],
            "sigmas": [scheduler_id, 0],
            "latent_image": [h3_id, 1],
        },
    }
    graph[video_decode_id] = {
        "class_type": "VAEDecode",
        "_meta": {"title": "Decode video"},
        "inputs": {"samples": [sample_id, 0], "vae": ["3", 0]},
    }
    graph[audio_decode_id] = {
        "class_type": "VAEDecodeAudio",
        "_meta": {"title": "Decode audio"},
        "inputs": {"samples": [sample_id, 0], "vae": ["4", 0]},
    }
    graph[create_id] = {
        "class_type": "CreateVideo",
        "_meta": {"title": "Mux video + audio"},
        "inputs": {
            "images": [video_decode_id, 0],
            "audio": [audio_decode_id, 0],
            "fps": fps,
            "bit_depth": 8,
        },
    }
    graph[save_id] = {
        "class_type": "SaveVideo",
        "_meta": {"title": "Save video"},
        "inputs": {
            "video": [create_id, 0],
            "filename_prefix": filename_prefix,
            "format": "auto",
            "codec": "auto",
        },
    }

    return graph, save_id


def _validate_inputs(
    mode: str,
    spec: ModeSpec,
    first_frame_image: str | None,
    last_frame_image: str | None,
    ref_images: list[str] | None,
    ref_image_size: str,
) -> None:
    """构建前的形态校验，一条错就停（这些都是"图根本画不出来"的错误）。"""
    issues: list[Issue] = []

    if mode in ("i2v", "flf2v") and not first_frame_image:
        issues.append(Issue.error(
            f"模式 {mode} 需要 first_frame_image",
            code="h3.missing_first_frame", location="first_frame_image",
            hint="填 ComfyUI 主机 input/ 目录里已存在的文件名（LoadImage 的取值是目录枚举，不是路径）",
        ))
    if mode == "flf2v" and not last_frame_image:
        issues.append(Issue.error(
            "模式 flf2v 需要 last_frame_image",
            code="h3.missing_last_frame", location="last_frame_image",
        ))

    if mode == "ref2v":
        if not ref_images:
            issues.append(Issue.error(
                "模式 ref2v 至少需要一张参考图",
                code="h3.missing_ref", location="ref_images",
                hint="用 --ref-image <本地文件> 自动上传，或用 --ref-name <后端已有文件名>",
            ))
        elif len(ref_images) > manifest.MAX_REFERENCE_IMAGES:
            issues.append(Issue.error(
                f"ref2v 最多接受 {manifest.MAX_REFERENCE_IMAGES} 张参考图，收到 {len(ref_images)} 张",
                code="h3.too_many_refs", location="ref_images",
            ))
        if ref_image_size not in manifest.REF_IMAGE_SIZE_CHOICES:
            issues.append(Issue.error(
                f"ref_image_size 只能是 {' 或 '.join(manifest.REF_IMAGE_SIZE_CHOICES)}，"
                f"收到 {ref_image_size!r}",
                code="h3.bad_ref_size", location="ref_image_size",
            ))
    elif ref_images:
        issues.append(Issue.error(
            f"ref_images 只对 ref2v 有意义，模式 {mode} 用不到",
            code="h3.refs_not_applicable", location="ref_images",
        ))

    if issues:
        raise ValidationError(f"无法构建 {mode} 图（{len(issues)} 项问题）", issues=issues)


def output_node_for(mode: str, media_counts: dict[str, int] | None = None) -> str:
    """在不真正建图的情况下推算产出节点 ID。

    仅在需要"预先知道编号"的场合使用（例如文档、外部框架的静态配置）。
    **实际使用时请用 :func:`build_h3_graph` 的返回值** —— 那才是权威。
    """
    media_counts = media_counts or {}
    spec = MODES.get(mode)
    if spec is None:
        raise ValidationError(f"未知模式 {mode!r}")
    if mode == "ref2v":
        image_nodes = max(1, media_counts.get("reference_image", 1))
    else:
        image_nodes = sum(
            1 for role in ("first_frame", "last_frame") if media_counts.get(role, 0) > 0
        )
        if mode in ("i2v", "flf2v") and image_nodes == 0:
            image_nodes = 1 if mode == "i2v" else 2
    return str(6 + image_nodes + 9)
