"""MiniMax H3 的**事实清单**（manifest）。

这个模块只有一个职责：把"H3 到底是什么、需要什么、有哪些形态"写成数据。

为什么要独立成一份 manifest，而不是散在构建代码里
--------------------------------------------------
原实现把同一批模型文件名、步数配对规则、输出节点编号写在两个地方
（``make_h3_workflow.py`` 的模块常量 + ``comfyui_mcp_server.py`` 的 ``DEFAULT_*``），
于是"换个量化版本"要改两处，且极易漂移。

现在：**任何关于 H3 的事实都只在这里出现一次**。构建器读它、方案声明读它、
文档引用它、测试断言它。改一处即全局一致。

已核实的来源
------------
* 模型文件名与节点类：对着运行中的 ComfyUI 0.35.1 的 ``GET /object_info`` 核对；
* 参考槽位序列化方式（点号动态键）：读 ``comfy_api/latest/_io.py`` 的 ``finalize_prefix``；
* 步数与 LoRA 的配对、参数栅格：官方模板 + 实测。
"""

from __future__ import annotations

from dataclasses import dataclass

from .....core.assets import (
    COMFYUI_CLIP,
    COMFYUI_LORA,
    COMFYUI_UNET,
    COMFYUI_VAE,
    NODE_CATEGORY,
    AssetRequirement,
)

# ===========================================================================
# 身份
# ===========================================================================
SOLUTION_KEY = "minimax_h3"
DISPLAY_NAME = "MiniMax H3（本地开源权重）"
BACKEND_NAME = "comfyui"
KIND = "video"
DESCRIPTION = (
    "MiniMax H3 音视频联合生成。一次推理同时产出画面与立体声音轨，"
    "支持文生视频、首帧生视频、首尾帧生视频、以及参考媒体生视频四种形态。"
)
DOCS = "docs/solutions/minimax-h3.md"

# ⚠ 与"云端 Partner Node"路线的区分，值得在代码里也写明：
#   minimax_h3_local（本方案）= 开源权重，跑在自己的 GPU 上，无外部费用与额度限制
#   minimax_h3_api            = ComfyUI Partner Node 云端托管，需 Comfy 账号 + 预付费额度
# 两条路的名字很像，混淆会导致"以为在用本地卡、其实在花钱"。

# ===========================================================================
# 模型资产的内置默认名（部署机可用配置覆盖）
# ===========================================================================
ASSET_DEFAULTS: dict[str, str] = {
    "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "ref2v_unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "turbo_lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    "ref2v_turbo_lora": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
}

ASSET_ALTERNATIVES: dict[str, list[str]] = {
    "ref2v_unet": ["minimax_h3_ref2va_pruned_w4a8_mixed.safetensors"],
    "turbo_lora": ["minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"],
}

ASSET_SOURCE_URL = "https://huggingface.co/Comfy-Org/MiniMax-H3"

# ===========================================================================
# 参数约束（实测值，不是估计值）
# ===========================================================================
WIDTH_MULTIPLE = 32
"""H3 要求分辨率是 32 的倍数。"""

LENGTH_GRID = (17, 5)
"""帧数栅格：``17k + 5``，即 5、22、39、…、124。未对齐时后端会自行吸附。"""

LENGTH_RANGE = (5, 3600)
"""帧数范围。实测训练区间约 124–362，更长未经验证，故上限给到节点声明的 3600。"""

MAX_REFERENCE_IMAGES = 9
"""参考图上限（来自 ``COMFY_AUTOGROW_V3`` 的 ``max``）。"""

REF_IMAGE_SIZE_CHOICES = ["match", "max"]
"""参考图缩放模式：``match`` 缩到生成分辨率（快）；``max`` 保留 2048px 短边（更保真、慢数倍）。"""

# ===========================================================================
# 模式（形态）
# ===========================================================================
@dataclass(frozen=True)
class ModeSpec:
    """一种生成形态的完整定义。

    四种形态共享同一套编码器/VAE/采样器链，只有三处分叉：
    H3 节点类、权重与 LoRA、是否挂参考媒体。这个 dataclass 就是把那三处集中起来。
    """

    name: str
    h3_class: str
    title: str
    weight_role: str
    """扩散模型用哪个资产角色（见 :data:`ASSET_DEFAULTS`）。"""
    lora_role: str | None
    default_steps: int
    description: str
    media_roles: tuple[str, ...] = ()
    """该形态需要的媒体角色，顺序即提示词里 ``<Picture N>`` 的编号顺序。"""
    sample_prompt: str = ""
    output_node_hint: str = ""
    """仅在**生成的图结构固定**时才有意义的参考编号。

    ⚠ 注意：真正的 ``output_node`` 是构建器按节点数**算出来**的，不是常量。
    这里写的编号只用于文档与人工核对（对应我们生成的五种模板）。
    """


_MODE_TABLE: dict[str, ModeSpec] = {}


def _register(spec: ModeSpec) -> ModeSpec:
    _MODE_TABLE[spec.name] = spec
    return spec


T2V = _register(ModeSpec(
    name="t2v",
    h3_class="MiniMaxH3ImageToVideo",
    # 节点标题保持英文：它会被写进图的 _meta.title，是 ComfyUI 界面里显示的文字，
    # 与该图中其它节点（"Load H3 diffusion model"、"Sample"…）保持一致风格。
    # 中文说明放在 description 里。
    title="MiniMax H3 conditioning + latent",
    weight_role="unet",
    lora_role="turbo_lora",
    default_steps=8,
    description="纯文本生成。给运镜、光线、氛围与音轨描述，不需要任何素材。",
    sample_prompt=(
        "电影感远景，缓慢推镜。黄昏时分玄武岩悬崖上的灯塔，下方涌浪拍岸，"
        "海鸥自左向右掠画，灯室里暖光外溢，体积雾，自然胶片颗粒。\n\n"
        "音频：深海低频轰鸣、风拍打麦克风、远处鸥鸣，场景下方一条持续的低音弦乐。\n\n"
        "画面中不得出现任何文字、字幕、标志或水印。"
    ),
    output_node_hint="15",
))

I2V = _register(ModeSpec(
    name="i2v",
    h3_class="MiniMaxH3ImageToVideo",
    title="MiniMax H3 conditioning + latent",
    weight_role="unet",
    lora_role="turbo_lora",
    default_steps=8,
    description="从一张首帧图出发继续运动。适合把已有的关键帧变成镜头。",
    media_roles=("first_frame",),
    sample_prompt=(
        "从给定首帧继续：画面向右缓慢平移，主体保持原有光照关系，"
        "空气中漂浮的尘埃被侧逆光打亮，浅景深，自然胶片颗粒。\n\n"
        "音频：环境底噪、轻微的风声，一条不抢戏的低音持续音。\n\n"
        "画面中不得出现任何文字、字幕、标志或水印。"
    ),
    output_node_hint="16",
))

FLF2V = _register(ModeSpec(
    name="flf2v",
    h3_class="MiniMaxH3ImageToVideo",
    title="MiniMax H3 conditioning + latent",
    weight_role="unet",
    lora_role="turbo_lora",
    default_steps=8,
    description="给定首帧与尾帧，让模型补出中间的运动。适合可控的转场与镜头设计。",
    media_roles=("first_frame", "last_frame"),
    sample_prompt=(
        "从首帧过渡到末帧：中段加入一次自然的呼吸式手持晃动，"
        "光线随时间由暖转冷，胶片颗粒保持一致，结尾在末帧构图处稳定住。\n\n"
        "音频：空间底噪随镜头推进逐渐收紧，结尾处一条低音弦乐自然收束。\n\n"
        "画面中不得出现任何文字、字幕、标志或水印。"
    ),
    output_node_hint="17",
))

REF2V = _register(ModeSpec(
    name="ref2v",
    h3_class="MiniMaxH3ReferenceToVideo",
    title="MiniMax H3 reference conditioning + latent",
    weight_role="ref2v_unet",
    lora_role="ref2v_turbo_lora",
    default_steps=4,
    description=(
        "把参考图（≤9）、参考视频（≤3）、独立音频（≤3）织进生成过程，"
        "用于锁定角色身份、风格、运动、镜头或音色。"
    ),
    media_roles=("reference_image",),
    sample_prompt=(
        "严格沿用 <Picture 1> 中的人物：同一张脸、同一套红金刺绣长袍、"
        "同一顶嵌宝凤冠、同样的黑色长发与纤细身形。\n\n"
        "废墟月夜庭院中一个缓慢的电影感中景。她起初静止，随后转向镜头并"
        "举起一柄发光的金色长剑；余烬与浮尘在光柱中翻涌。浅景深，自然胶片颗粒，"
        "冷蓝月光下带暖色轮廓光。缓慢推近，画面不抖。\n\n"
        "音频：低沉的庭院风声、远处隐约的钟声、剑刃的嗡鸣，一条持续的低音弦乐。\n\n"
        "画面中不得出现任何文字、字幕、标志或水印。"
    ),
    output_node_hint="16",
))

MODES: dict[str, ModeSpec] = _MODE_TABLE
MODE_NAMES: tuple[str, ...] = ("t2v", "i2v", "flf2v", "ref2v")
DEFAULT_MODE = "t2v"

# 全模式共用的节点类
SHARED_NODE_CLASSES: tuple[str, ...] = (
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "LoraLoaderModelOnly",
    "RandomNoise",
    "KSamplerSelect",
    "BasicScheduler",
    "BasicGuider",
    "SamplerCustomAdvanced",
    "VAEDecode",
    "VAEDecodeAudio",
    "CreateVideo",
    "SaveVideo",
    "LoadImage",
)

# ===========================================================================
# 资产需求声明
# ===========================================================================
def asset_requirements(names: dict[str, str] | None = None) -> list[AssetRequirement]:
    """产出 H3 的资产需求列表。

    :param names: 覆盖内置默认名的映射（``role -> 实际文件名``）。
        这样"部署机上换了一套量化权重"只需改配置，不必改这份 manifest。
    """
    effective = {**ASSET_DEFAULTS, **(names or {})}
    fl2va_modes = ["t2v", "i2v", "flf2v"]

    return [
        # --- 扩散模型：两套权重，**不可互换** ---
        AssetRequirement(
            role="unet", category=COMFYUI_UNET, name=effective["unet"],
            modes=fl2va_modes,
            note="t2v/i2v/flf2v 的扩散模型（fl2va 系列）",
            source_url=ASSET_SOURCE_URL,
        ),
        AssetRequirement(
            role="ref2v_unet", category=COMFYUI_UNET, name=effective["ref2v_unet"],
            modes=["ref2v"],
            note=(
                "ref2v 专用扩散模型（ref2va 系列）。与 t2v/i2v/flf2v 用的 fl2va "
                "**不是同一套权重**，混用会得到「能跑但结果不对」的输出。"
                f"备选量化版本：{', '.join(ASSET_ALTERNATIVES['ref2v_unet'])}"
            ),
            source_url=ASSET_SOURCE_URL,
        ),
        # --- 文本编码器与两个 VAE：全模式共用 ---
        AssetRequirement(
            role="clip", category=COMFYUI_CLIP, name=effective["clip"],
            note="Qwen3-VL 文本编码器", source_url=ASSET_SOURCE_URL,
        ),
        AssetRequirement(
            role="video_vae", category=COMFYUI_VAE, name=effective["video_vae"],
            note="视频 VAE", source_url=ASSET_SOURCE_URL,
        ),
        AssetRequirement(
            role="audio_vae", category=COMFYUI_VAE, name=effective["audio_vae"],
            note="音频 VAE。缺它就没有音轨（H3 是音视频联合生成）",
            source_url=ASSET_SOURCE_URL,
        ),
        # --- Turbo LoRA：两组，步数必须配对 ---
        AssetRequirement(
            role="turbo_lora", category=COMFYUI_LORA, name=effective["turbo_lora"],
            modes=fl2va_modes,
            note="8 步 Turbo LoRA，必须配 steps=8",
            source_url=ASSET_SOURCE_URL,
        ),
        AssetRequirement(
            role="ref2v_turbo_lora", category=COMFYUI_LORA, name=effective["ref2v_turbo_lora"],
            modes=["ref2v"],
            note="ref2v 专用 4 步 Turbo LoRA，必须配 steps=4",
            source_url=ASSET_SOURCE_URL,
        ),
        # --- 节点类：把"插件在不在"并入同一套就绪度判断 ---
        AssetRequirement(
            role="node_h3_i2v", category=NODE_CATEGORY, name="MiniMaxH3ImageToVideo",
            modes=fl2va_modes,
            note="H3 首帧/首尾帧节点未被后端报告，检查 ComfyUI 版本与 H3 节点包",
        ),
        AssetRequirement(
            role="node_h3_ref2v", category=NODE_CATEGORY, name="MiniMaxH3ReferenceToVideo",
            modes=["ref2v"],
            note="H3 参考节点未被后端报告 —— 这是「t2v 能跑但 ref2v 报错」最常见的原因",
        ),
    ]


# 静态声明（用于 describe() 与文档；运行时以 asset_requirements() 的覆盖版为准）
STATIC_ASSETS: list[AssetRequirement] = asset_requirements()


# ===========================================================================
# 默认取样配置
# ===========================================================================
DEFAULT_SAMPLER = "res_multistep"
DEFAULT_SCHEDULER = "simple"
DEFAULT_WEIGHT_DTYPE = "default"
DEFAULT_FILENAME_PREFIX = "video/MiniMax_H3"

# 无 LoRA 时的推荐步数（质量优先，显著更慢）
STEPS_WITHOUT_LORA = 20


def steps_for(mode: str, lora: str | None, requested: int | None = None) -> int:
    """决定采样步数。

    配对规则（来自官方模板，写错会导致「能跑但质量异常」）：

    * 指定了步数 -> 用指定的；
    * 有 Turbo LoRA -> 用该模式的默认步数（ref2v 4 步、其余 8 步）；
    * 没有 LoRA -> 20 步。

    把它做成函数而不是散在构建器里的 if，是因为"步数与 LoRA 必须配对"
    是一条容易违反、且违反后不报错的规则 —— 集中一处才好加校验。
    """
    if requested:
        return int(requested)
    if lora is None:
        return STEPS_WITHOUT_LORA
    return MODES[mode].default_steps
