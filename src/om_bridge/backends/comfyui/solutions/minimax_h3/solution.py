"""MiniMax H3 方案实现 —— 把 manifest 里的模型事实接进 OM-Bridge 契约。

这个类是"方案"这一概念的完整示例，值得当作新增方案的模板来读：

* 它**不碰网络** —— 上传、提交、轮询全在 ComfyUI 后端里；
* 它**不读环境变量** —— 所有部署事实都通过 ``config`` 参数注入（易于测试）；
* 它**不知道自己会被谁调用** —— CLI / MCP / SDK / OpenMontage 走的是同一条路径。

新增一个方案时，照抄这四件事就够了：

1. :attr:`schema` —— 声明参数（一次声明同时喂 CLI / MCP / 校验 / 文档）；
2. :attr:`media_slots` —— 声明要哪些素材；
3. :meth:`resolve_assets` —— 声明要哪些模型文件（就绪度判断据此自动生成）；
4. :meth:`build_job` —— 把参数摊平成后端载荷。
"""

from __future__ import annotations

from typing import Any

from .....core.assets import AssetRequirement
from .....core.backend import Backend
from .....core.models import (
    GenerationRequest,
    Issue,
    JobSpec,
    MediaAsset,
    MediaKind,
    Provenance,
)
from .....core.schema import (
    TYPE_BOOL,
    TYPE_ENUM,
    TYPE_INT,
    TYPE_STR,
    ParamSchema,
    ParamSpec,
)
from .....core.solution import MediaSlot, Solution
from . import builder, manifest
from .manifest import MODE_NAMES, MODES


class MinimaxH3Solution(Solution):
    """MiniMax H3 本地开源权重（音视频联合生成）。"""

    key = manifest.SOLUTION_KEY
    display_name = manifest.DISPLAY_NAME
    backend_name = manifest.BACKEND_NAME
    kind = manifest.KIND
    description = manifest.DESCRIPTION
    docs = manifest.DOCS

    modes = list(MODE_NAMES)
    default_mode = manifest.DEFAULT_MODE

    # ------------------------------------------------------------------
    # 1. 参数声明
    # ------------------------------------------------------------------
    schema = ParamSchema([
        ParamSpec(
            "mode", TYPE_ENUM, manifest.DEFAULT_MODE,
            choices=list(MODE_NAMES),
            description="生成形态。t2v 文生视频 / i2v 首帧生视频 / "
                        "flf2v 首尾帧生视频 / ref2v 参考媒体生视频。",
        ),
        ParamSpec(
            "width", TYPE_INT, 864, minimum=64, maximum=2048,
            multiple_of=manifest.WIDTH_MULTIPLE,
            description="输出宽度（像素）。必须是 32 的倍数。",
        ),
        ParamSpec(
            "height", TYPE_INT, 480, minimum=64, maximum=2048,
            multiple_of=manifest.WIDTH_MULTIPLE,
            description="输出高度（像素）。必须是 32 的倍数。",
        ),
        ParamSpec(
            "length", TYPE_INT, 124,
            minimum=manifest.LENGTH_RANGE[0], maximum=manifest.LENGTH_RANGE[1],
            grid=manifest.LENGTH_GRID,
            description="帧数（24fps）。会对齐到 17k+5 栅格：5, 22, 39, … 124≈5.2 秒。",
        ),
        ParamSpec(
            "fps", TYPE_INT, 24, minimum=1, maximum=60,
            description="封装帧率。",
        ),
        ParamSpec(
            "steps", TYPE_INT, None, minimum=1, maximum=100,
            description="采样步数。留空按模式自动：ref2v 用 4，其余用 8（关闭 turbo 时为 20）。",
        ),
        ParamSpec(
            "seed", TYPE_INT, 0, minimum=0, maximum=2**63 - 1,
            description="随机种子。⚠ 复用同一份图与同一 seed 会命中 ComfyUI 整图缓存而秒回旧片。",
        ),
        ParamSpec(
            "turbo", TYPE_BOOL, True,
            description="启用 Turbo LoRA。关闭则不加 LoRA，改用 20 步（质量优先，显著更慢）。",
        ),
        ParamSpec(
            "ref_image_size", TYPE_ENUM, "match",
            choices=list(manifest.REF_IMAGE_SIZE_CHOICES),
            applies_to=["ref2v"],
            description="参考图缩放模式。match 缩到生成分辨率（快）；"
                        "max 保留 2048px 短边（身份更保真，但参考 token 会贯穿每一步采样，慢数倍）。",
        ),
        ParamSpec(
            "filename_prefix", TYPE_STR, manifest.DEFAULT_FILENAME_PREFIX,
            description="后端侧的文件名前缀（决定产物在 ComfyUI output/ 下的相对路径）。",
        ),
        ParamSpec(
            "sampler", TYPE_STR, manifest.DEFAULT_SAMPLER,
            description="采样器名（ComfyUI 的 KSamplerSelect 取值）。",
        ),
        ParamSpec(
            "scheduler", TYPE_STR, manifest.DEFAULT_SCHEDULER,
            description="调度器名（ComfyUI 的 BasicScheduler 取值）。",
        ),
    ])

    # ------------------------------------------------------------------
    # 2. 媒体槽位
    # ------------------------------------------------------------------
    media_slots = [
        MediaSlot(
            role="first_frame", kind=MediaKind.IMAGE, required=True,
            modes=["i2v", "flf2v"],
            description="首帧图。本地文件会被自动上传；也可以直接给后端 input 目录里已有的文件名。",
        ),
        MediaSlot(
            role="last_frame", kind=MediaKind.IMAGE, required=True,
            modes=["flf2v"],
            description="尾帧图。与首帧一起限定运动的两端。",
        ),
        MediaSlot(
            role="reference_image", kind=MediaKind.IMAGE, required=True,
            multiple=True, max_count=manifest.MAX_REFERENCE_IMAGES,
            modes=["ref2v"],
            description="参考图。按传入顺序对应提示词里的 <Picture 1>、<Picture 2>…",
        ),
    ]

    # 资产角色 → 配置键名（同名，故用映射表显式表达，便于将来改名）
    ASSET_CONFIG_KEYS = {
        "unet": "unet",
        "ref2v_unet": "ref2v_unet",
        "clip": "clip",
        "video_vae": "video_vae",
        "audio_vae": "audio_vae",
        "turbo_lora": "turbo_lora",
        "ref2v_turbo_lora": "ref2v_turbo_lora",
    }

    # ------------------------------------------------------------------
    # 3. 资产声明（名字来自配置，缺省回落到 manifest 内置默认）
    # ------------------------------------------------------------------
    assets = manifest.STATIC_ASSETS

    def resolve_assets(self, config: Any = None) -> list[AssetRequirement]:
        """把配置里的实际权重名套进资产声明。

        这一步让"部署机换了量化版本"完全不必改代码 ——
        只是配置里的一个字符串变了，就绪度检查、报错提示、文档一起跟着变。
        """
        if config is None:
            return list(self.assets)
        section = config.solution(self.key)
        names = {
            role: section.get(config_key)
            for role, config_key in self.ASSET_CONFIG_KEYS.items()
            if section.get(config_key)
        }
        return manifest.asset_requirements(names)

    # ------------------------------------------------------------------
    # 4. 配置默认值与默认素材
    # ------------------------------------------------------------------
    def config_defaults(self, config: Any, mode: str | None = None) -> dict[str, Any]:
        """从配置取参数默认值（优先级：调用方显式传参 > 配置 > 声明默认）。"""
        section = config.solution(self.key)
        defaults: dict[str, Any] = {
            "width": section.get("width"),
            "height": section.get("height"),
            "length": section.get("length"),
            "fps": section.get("fps"),
            "ref_image_size": section.get("ref_image_size"),
        }
        configured_steps = section.get_int("steps", 0)
        if configured_steps and configured_steps > 0:
            defaults["steps"] = configured_steps
        return {k: v for k, v in defaults.items() if v not in (None, "")}

    def default_media(self, config: Any, mode: str | None) -> list[MediaAsset]:
        """ref2v 未显式给参考图时，用配置里登记的默认参考图。

        这是"部署机上固定使用某组素材"的表达方式：把素材名写在配置里，
        调用方就不必每次重复传。
        """
        if mode != "ref2v":
            return []
        section = config.solution(self.key)
        names = section.get("ref_images") or []
        if isinstance(names, str):
            names = [n.strip() for n in names.split(",") if n.strip()]
        return [
            MediaAsset(role="reference_image", kind=MediaKind.IMAGE, name=name,
                       metadata={"source": "config"})
            for name in names
        ]

    # ------------------------------------------------------------------
    # 5. 校验补充
    # ------------------------------------------------------------------
    def validate_params(self, params: dict[str, Any], mode: str | None = None) -> list[Issue]:
        issues = super().validate_params(params, mode)

        # 步数必须与所选 LoRA 配对。写错不会报错，只会让画质悄悄变差 ——
        # 正是这种"静默降级"最值得给一条告警。
        steps = params.get("steps")
        turbo = params.get("turbo", True)
        if steps and mode:
            expected = MODES[mode].default_steps if turbo else manifest.STEPS_WITHOUT_LORA
            if turbo and int(steps) != expected:
                issues.append(Issue.warning(
                    f"步数 {steps} 与 Turbo LoRA 的推荐值 {expected} 不一致，可能出现画质异常",
                    code="h3.steps_lora_mismatch", location="steps",
                    hint=f"要么用 steps={expected}，要么把 turbo 关掉（此时推荐 "
                         f"{manifest.STEPS_WITHOUT_LORA} 步）",
                ))
            if not turbo and int(steps) < manifest.STEPS_WITHOUT_LORA:
                issues.append(Issue.warning(
                    f"未启用 Turbo LoRA 却只给了 {steps} 步，画质很可能不足",
                    code="h3.steps_too_low", location="steps",
                    hint=f"关闭 turbo 时建议 {manifest.STEPS_WITHOUT_LORA} 步",
                ))
        return issues

    # ------------------------------------------------------------------
    # 6. 构造作业
    # ------------------------------------------------------------------
    def build_job(
        self,
        request: GenerationRequest,
        backend: Backend,
        *,
        resolved_params: dict[str, Any],
        media: list[MediaAsset],
        config: Any = None,
    ) -> JobSpec:
        mode = resolved_params.get("mode") or self.default_mode

        def names_of(role: str) -> list[str]:
            return [a.name for a in media if a.role == role and a.name]

        def first_of(role: str) -> str | None:
            found = names_of(role)
            return found[0] if found else None

        # 权重与 LoRA 属于"部署事实"而非"创作参数"，因此来自配置而不是 request.params。
        # 想要临时换一套权重做对比，用 node_overrides 直接改图中节点（逃生舱）。
        section = (config or backend.config).solution(self.key)
        turbo = bool(resolved_params.get("turbo", True))
        if turbo:
            lora = section.get("ref2v_turbo_lora") if mode == "ref2v" else section.get("turbo_lora")
        else:
            lora = None

        graph, output_node = builder.build_h3_graph(
            mode=mode,
            prompt=request.prompt,
            width=int(resolved_params["width"]),
            height=int(resolved_params["height"]),
            length=int(resolved_params["length"]),
            seed=int(resolved_params.get("seed") or 0),
            steps=resolved_params.get("steps"),
            lora=lora or None,
            filename_prefix=resolved_params.get("filename_prefix")
            or manifest.DEFAULT_FILENAME_PREFIX,
            unet_name=section.get("ref2v_unet") if mode == "ref2v" else section.get("unet"),
            clip_name=section.get("clip"),
            video_vae=section.get("video_vae"),
            audio_vae=section.get("audio_vae"),
            sampler=resolved_params.get("sampler") or manifest.DEFAULT_SAMPLER,
            scheduler=resolved_params.get("scheduler") or manifest.DEFAULT_SCHEDULER,
            fps=int(resolved_params.get("fps") or 24),
            first_frame_image=first_of("first_frame"),
            last_frame_image=first_of("last_frame"),
            ref_images=names_of("reference_image") or None,
            ref_image_size=resolved_params.get("ref_image_size") or "match",
        )

        return JobSpec(
            payload=graph,
            output_selector=output_node,
            kind="graph",
            validate_before_submit=True,
            metadata={
                "label": f"minimax_h3.{mode}",
                "mode": mode,
                "output_node": output_node,
                "nodes": len(graph),
                # 提示词里的引用标签与实际参考数量必须一致，直接把对应关系记下来，
                # 避免调用方自己数错（ref2v 对措辞敏感，指错对象会锁错身份）
                "reference_tags": [
                    f"<Picture {i + 1}>" for i in range(len(names_of("reference_image")))
                ],
            },
        )

    # ------------------------------------------------------------------
    # 7. 溯源补充
    # ------------------------------------------------------------------
    def provenance(
        self,
        request: GenerationRequest,
        *,
        resolved_params: dict[str, Any],
        media: list[MediaAsset],
        job: JobSpec | None = None,
    ) -> Provenance:
        provenance = super().provenance(
            request, resolved_params=resolved_params, media=media, job=job
        )
        mode = resolved_params.get("mode") or self.default_mode
        provenance.extra = {
            "mode": mode,
            "output_node": (job.output_selector if job else None),
            "weights": {
                "diffusion": (
                    manifest.ASSET_DEFAULTS["ref2v_unet"] if mode == "ref2v"
                    else manifest.ASSET_DEFAULTS["unet"]
                ),
                "turbo_lora": (
                    manifest.ASSET_DEFAULTS["ref2v_turbo_lora"] if mode == "ref2v"
                    else manifest.ASSET_DEFAULTS["turbo_lora"]
                ),
                "turbo": bool(resolved_params.get("turbo", True)),
            },
            "reference_tags": (job.metadata.get("reference_tags") if job else []) or [],
        }
        return provenance

    # ------------------------------------------------------------------
    # 8. 自描述增强
    # ------------------------------------------------------------------
    def describe(self) -> dict:
        payload = super().describe()
        payload["modes_detail"] = [
            {
                "name": spec.name,
                "h3_node": spec.h3_class,
                "weight_role": spec.weight_role,
                "lora_role": spec.lora_role,
                "default_steps": spec.default_steps,
                "media_roles": list(spec.media_roles),
                "description": spec.description,
                "output_node_hint": spec.output_node_hint,
            }
            for spec in (MODES[name] for name in self.modes)
        ]
        payload["limits"] = {
            "width_multiple": manifest.WIDTH_MULTIPLE,
            "length_grid": f"{manifest.LENGTH_GRID[0]}k+{manifest.LENGTH_GRID[1]}",
            "length_range": list(manifest.LENGTH_RANGE),
            "max_reference_images": manifest.MAX_REFERENCE_IMAGES,
        }
        return payload
