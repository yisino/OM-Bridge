"""Solution —— 生成方案的契约（"跑什么模型、怎么跑"）。

职责边界
--------
**Solution 只负责模型语义，不碰网络。**

它知道：要哪些权重、参数怎么约束、某个模式需要几张参考图、
提示词里的 ``<Picture 1>`` 对应第几张上传的图、怎么把参数摊平成一张计算图。

它**不知道**：后端地址是什么、怎么上传、作业怎么轮询、超时怎么续等。

于是同一条 MiniMax H3 t2v 方案，既可以在本机 ComfyUI 上跑，
也可以在将来某个云端 ComfyUI 集群上跑，方案代码一行不改。

为什么"模式（mode）"是方案内部的参数，而不是独立方案
----------------------------------------------------
t2v / i2v / flf2v / ref2v 共享 95% 的图结构（同一个 Qwen3-VL 文本编码器、
同一套 VAE、同一个采样器链），只有三处分叉：H3 节点类、是否挂参考图、权重与步数。

把它们做成四个独立 Solution 会导致那 95% 复制四份；做成一个带 ``mode`` 参数的
Solution 则只有一处分叉判断。**同时**为了命令行与 agent 的易用性，
注册表会为每个模式额外注册一个别名键（``minimax_h3.ref2v``），
所以"一个实现"和"四个入口"可以同时成立。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .assets import (
    AssetRequirement,
    AssetStatus,
    asset_issues,
    evaluate_assets,
    readiness_by_mode,
)
from .backend import Backend, Capability
from .models import (
    GenerationRequest,
    Issue,
    JobSpec,
    MediaAsset,
    MediaKind,
    ProbeReport,
    Provenance,
)
from .schema import ParamSchema


@dataclass
class MediaSlot:
    """方案声明的一个媒体输入槽位。

    这是"方案告诉外界它要什么素材"的唯一渠道。CLI 用它生成 ``--ref-image``
    这类可重复选项，MCP 用它生成 ``array`` 类型的入参，校验层用它检查必填。

    没有它的话，媒体要求只能写死在 CLI 的 argparse 和 MCP 的手工 schema 里 ——
    那正是原实现里两处重复的来源。
    """

    role: str
    """方案内部角色名（``first_frame`` / ``reference_image`` …），与 ``mode`` 内的语义绑定。"""
    kind: MediaKind = MediaKind.IMAGE
    required: bool = False
    multiple: bool = False
    """是否可重复（如参考图最多 9 张 -> ``multiple=True, max_count=9``）。"""
    max_count: int = 1
    description: str = ""
    modes: list[str] = field(default_factory=list)
    """限定该槽位只在某些模式下需要。空 = 所有模式。"""

    def applies_to(self, mode: str | None) -> bool:
        if not self.modes:
            return True
        if mode is None:
            return True
        return mode in self.modes

    def to_dict(self) -> dict:
        payload = {
            "role": self.role,
            "kind": self.kind.value,
            "required": self.required,
            "multiple": self.multiple,
            "max_count": self.max_count,
        }
        if self.modes:
            payload["modes"] = self.modes
        if self.description:
            payload["description"] = self.description
        return payload


class Solution(ABC):
    """生成方案抽象基类。"""

    key: ClassVar[str] = ""
    """方案唯一标识，小写 + 下划线。CLI 里写作 ``--solution <key>[.<mode>]``。"""

    display_name: ClassVar[str] = ""
    backend_name: ClassVar[str] = ""
    """需要哪一类后端。注册表据此校验"方案与后端是否匹配"。"""

    kind: ClassVar[str] = "video"
    """产出物大类，取值建议与 :class:`Capability` 一致（video / image / audio）。"""

    modes: ClassVar[list[str]] = []
    """支持的子模式。空列表表示无模式概念（单一形态）。"""

    default_mode: ClassVar[str | None] = None

    description: ClassVar[str] = ""
    docs: ClassVar[str] = ""
    """相对文档路径，供 CLI 的 ``describe`` 输出指向详细说明。"""

    schema: ClassVar[ParamSchema] = ParamSchema()
    assets: ClassVar[list[AssetRequirement]] = []
    media_slots: ClassVar[list[MediaSlot]] = []

    # -- 元信息 -------------------------------------------------------------
    def describe(self) -> dict:
        """自描述：参数 schema、媒体槽位、资产需求一次全给。

        这个方法的输出同时是 CLI ``describe`` 的内容与 MCP 工具的 schema 来源，
        因此新增方案**不需要**额外写文档或工具声明。
        """
        return {
            "key": self.key,
            "display_name": self.display_name or self.key,
            "backend": self.backend_name,
            "kind": self.kind,
            "description": self.description,
            "docs": self.docs,
            "modes": list(self.modes),
            "default_mode": self.default_mode,
            "params": {spec.name: spec.to_json_schema() for spec in self.schema},
            "param_details": [
                {
                    "name": spec.name,
                    "type": spec.type,
                    "default": spec.default,
                    "required": spec.required,
                    "applies_to": spec.applies_to or ["*"],
                    "description": spec.description,
                }
                for spec in self.schema
            ],
            "media_slots": [slot.to_dict() for slot in self.media_slots],
            "assets": [
                {
                    "role": req.role,
                    "category": req.category,
                    "name": req.name,
                    "required": req.required,
                    "modes": req.modes or ["*"],
                    "note": req.note,
                }
                for req in self.assets
            ],
        }

    # -- 模式 ---------------------------------------------------------------
    def resolve_mode(self, params: dict[str, Any]) -> str | None:
        """从参数里取出模式名。默认读名为 ``mode`` 的参数。"""
        if not self.modes:
            return None
        return params.get("mode") or self.default_mode or self.modes[0]

    def config_defaults(self, config: Any, mode: str | None = None) -> dict[str, Any]:
        """从配置里取出该方案的参数默认值，用于覆盖声明里的内置默认。

        存在的意义是让"这台机器该用什么权重/分辨率"住在配置文件里，
        而不是把部署事实硬编码进方案代码。默认实现返回空字典 ——
        不覆写的话，参数就只由 :attr:`schema` 的声明默认值决定。

        约定：返回的键是**参数名**（与 ``schema`` 一致），不是配置键。
        """
        return {}

    # -- 校验 ---------------------------------------------------------------
    def validate_params(self, params: dict[str, Any], mode: str | None = None) -> list[Issue]:
        """参数校验。默认走 :class:`ParamSchema`；复杂约束可在子类追加。"""
        return self.schema.validate(params, mode)

    def default_media(self, config: Any, mode: str | None) -> list[MediaAsset]:
        """从配置里补出媒体槽位的默认值。

        典型用途：某些方案在部署机上固定使用一组素材（例如固定的角色参考图），
        这种"部署事实"该住在配置里，而不是逼调用方每次都传。

        约定：返回的媒体只在对应槽位**为空**时才会被采用，
        因此不会覆盖调用方显式给出的素材。默认实现不补任何东西。
        """
        return []

    def validate_media(self, media: list[MediaAsset], mode: str | None) -> list[Issue]:
        """媒体校验：必填槽位是否给了、可重复槽位是否超量。"""
        issues: list[Issue] = []
        for slot in self.media_slots:
            if not slot.applies_to(mode):
                continue
            matched = [m for m in media if m.role == slot.role]
            if slot.required and not matched:
                issues.append(Issue.error(
                    f"模式 {mode} 需要至少一个 {slot.role}（{slot.kind.value}）",
                    code="media.missing_role", location=slot.role,
                    hint=slot.description,
                ))
            if slot.multiple and len(matched) > slot.max_count:
                issues.append(Issue.error(
                    f"{slot.role} 最多 {slot.max_count} 个，收到 {len(matched)} 个",
                    code="media.too_many", location=slot.role,
                ))
            if not slot.multiple and len(matched) > 1:
                issues.append(Issue.error(
                    f"{slot.role} 只接受 1 个，收到 {len(matched)} 个",
                    code="media.too_many", location=slot.role,
                ))
            for item in matched:
                if item.needs_upload and slot.kind is not MediaKind.IMAGE:
                    # ComfyUI 的 /upload/image 只吃图片；视频/音频需另走通道。
                    # 在方案层就拦下来，避免调用方以为传了就开始等渲染。
                    issues.append(Issue.warning(
                        f"{slot.role} 是 {slot.kind.value} 类型且只给了本地路径，"
                        "当前后端的自动上传仅支持图片，请先手动放入后端素材目录后用远端名引用",
                        code="media.upload_unsupported", location=slot.role,
                    ))
        undeclared = {m.role for m in media} - {s.role for s in self.media_slots}
        for role in sorted(undeclared):
            issues.append(Issue.warning(
                f"媒体角色 {role} 未在方案声明中登记，将原样透传",
                code="media.undeclared", location=role,
            ))
        return issues

    def resolve_assets(self, config: Any = None) -> list[AssetRequirement]:
        """产出**本次运行**实际生效的资产需求列表。

        与类属性 :attr:`assets` 的区别：:attr:`assets` 是静态声明（含内置默认名），
        而本方法允许用配置里的实际文件名覆盖它们 —— 例如部署机换了一套量化权重，
        只需要改配置，不必改代码。

        默认实现直接返回静态声明。需要"名字来自配置"的方案应覆写它。
        """
        return list(self.assets)

    def readiness(self, probe: ProbeReport, config: Any = None) -> dict[str, Any]:
        """按模式给出就绪度与缺失清单（声明式，见 :mod:`om_bridge.core.assets`）。"""
        statuses = evaluate_assets(self.resolve_assets(config), probe)
        modes = list(self.modes) or ["default"]
        report = readiness_by_mode(statuses, modes)
        for mode, entry in report.items():
            entry["assets"] = [s.to_dict() for s in statuses if s.requirement.applies_to(mode)]
        return report

    def asset_issues(
        self, probe: ProbeReport, mode: str | None = None, config: Any = None
    ) -> list[Issue]:
        """把缺失资产转成问题列表，并按模式过滤。"""
        statuses: list[AssetStatus] = evaluate_assets(self.resolve_assets(config), probe)
        if mode is not None:
            statuses = [s for s in statuses if s.requirement.applies_to(mode)]
        return asset_issues(statuses)

    def check_compatible(self, backend: Backend) -> list[Issue]:
        """方案与后端的静态兼容性自检。

        在真正提交前暴露"方案要求上传、后端不支持上传"这类结构性不匹配，
        比让它在渲染中途失败好得多。
        """
        issues: list[Issue] = []
        if self.backend_name and backend.name != self.backend_name:
            issues.append(Issue.error(
                f"方案 {self.key} 需要后端 {self.backend_name!r}，"
                f"当前后端是 {backend.name!r}",
                code="solution.backend_mismatch",
            ))
        if self.kind and not backend.supports(self.kind):
            issues.append(Issue.error(
                f"后端 {backend.name!r} 未声明 {self.kind!r} 能力",
                code="solution.capability_missing",
            ))
        if any(slot.kind is MediaKind.IMAGE for slot in self.media_slots):
            if not backend.supports(Capability.MEDIA_UPLOAD):
                issues.append(Issue.info(
                    "后端不支持媒体上传，媒体槽位只能使用已在后端素材目录中的文件（用 name 指定）",
                    code="solution.upload_limited",
                ))
        return issues

    # -- 构造作业 -----------------------------------------------------------
    @abstractmethod
    def build_job(
        self,
        request: GenerationRequest,
        backend: Backend,
        *,
        resolved_params: dict[str, Any],
        media: list[MediaAsset],
        config: Any = None,
    ) -> JobSpec:
        """构造后端可执行的作业。

        :param resolved_params: 已由 schema 填好默认值、做完类型还原的参数集，
            方案实现**不应**再自己查默认值。
        :param media: 已完成上传的媒体列表（``name`` 字段可直接写进载荷）。
        :param config: 配置对象。载荷里往往需要**部署事实**（用哪套权重、
            写到哪个目录），这些不该是"参数"，但必须能读到。
            显式传进来而不是让方案去翻后端的 config，是为了保持"方案不依赖后端内部"。
        """

    def preflight(
        self,
        job: JobSpec,
        backend: Backend,
        *,
        probe: ProbeReport | None = None,
    ) -> list[Issue]:
        """提交前对**载荷**的校验。默认委托后端（后端最懂自己的载荷格式）。"""
        return backend.preflight(job, probe=probe)

    def provenance(
        self,
        request: GenerationRequest,
        *,
        resolved_params: dict[str, Any],
        media: list[MediaAsset],
        job: JobSpec | None = None,
    ) -> Provenance:
        """装配溯源信息。方案可以覆写以补充模型特有的字段。"""
        return Provenance(
            backend=self.backend_name,
            solution=self.key,
            payload_fingerprint=job.fingerprint() if job else "",
            resolved_params=resolved_params,
            assets={req.role: req.name for req in self.assets if req.name},
            media=[m.to_dict() for m in media],
        )
