"""模型资产的声明与就绪度判定。

解决的问题
----------
原实现里"要哪些模型权重"这件事**散落在两处硬编码**：
``make_h3_workflow.py`` 的模块常量一份、``comfyui_mcp_server.py`` 的 ``DEFAULT_*`` 又一份。
结果是同一批文件名要改两次，且"缺哪个权重"只能靠一句写死的判断。

这里把它变成**声明式**：方案用 :class:`AssetRequirement` 列出自己需要什么，
由 :func:`evaluate_readiness` 拿后端探测结果算就绪度。于是：

* 新增方案不需要写任何就绪度代码，声明即可；
* "哪个模式缺哪个文件"是自动推出来的，不再是手写的 if 链；
* 报错信息可以精确到"这个模式需要的这个角色缺这个文件"。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .models import Issue, ProbeReport


class AssetCategory(str, enum.Enum):
    """资产的大类，用于**给人看**的语义分组与报告排版。

    注意：真正决定"去后端哪个类别里找"的是 :attr:`AssetRequirement.category`
    （一个普通字符串），因为不同后端的类别命名不同，核心层不该假设存在哪些类别。
    这里的枚举只是给报告和 JSON Schema 提供一组推荐取值。
    """

    DIFFUSION_MODEL = "unet"
    TEXT_ENCODER = "clip"
    VAE = "vae"
    LORA = "lora"
    UPSCALER = "upscaler"
    OTHER = "other"


# ComfyUI 后端的资产类别键（与 /object_info 里各加载节点的下拉框一一对应）。
# 定义为常量而非枚举值，是为了强调"这是 ComfyUI 的事实，不是核心层的假设"。
COMFYUI_UNET = "unet"
COMFYUI_CLIP = "clip"
COMFYUI_VAE = "vae"
COMFYUI_LORA = "lora"

NODE_CATEGORY = "node"
"""特殊类别：把"节点类是否存在"也当作一种资产需求。

为什么要这样做，而不是另立一套"节点需求"机制：
就绪度的判断逻辑（required / modes 过滤 / 缺失清单汇总）与资产完全同构，
差别只在**去哪儿查**（``probe.nodes`` 而不是 ``probe.assets``）。
复用同一套机制意味着"某个模式的依赖"只有一个遍历点，
既不会漏报，也不必维护两条并行的判断链。
"""


@dataclass
class AssetRequirement:
    """方案对某个模型资产的**声明式**需求。

    ``role`` 是方案内部的角色名（如 ``ref2v_unet``），``category`` + ``name``
    指出在后端的哪个类别里找哪个文件名，``modes`` 说明哪些模式需要它。
    """

    role: str
    """方案内部角色名，同时作为 :attr:`Provenance.assets` 的键。"""
    category: str
    """后端资产类别键（ComfyUI 用 ``unet`` / ``clip`` / ``vae`` / ``lora``）。"""
    name: str
    """具体文件名。留空表示"该角色由方案参数动态给出"。"""
    modes: list[str] = field(default_factory=list)
    """需要该资产的模式列表。空列表表示**所有模式**都需要。"""
    required: bool = True
    """``False`` 表示可选（缺失不阻断，只在报告里提示）。"""
    note: str = ""
    source_url: str = ""

    def applies_to(self, mode: str | None) -> bool:
        """该需求是否作用于给定模式。"""
        if not self.modes:
            return True
        if mode is None:
            return True
        return mode in self.modes


@dataclass
class AssetStatus:
    """一个资产需求的**实测**就绪状态。"""

    requirement: AssetRequirement
    present: bool
    declared: bool = True
    """后端是否报告过该类别。``False`` 说明类别整体探测失败，而非文件缺失 ——
    区开这两者能避免把"探测失败"误报成"文件没下"。"""

    @property
    def label(self) -> str:
        return f"{self.requirement.category}:{self.requirement.name}"

    def to_dict(self) -> dict:
        return {
            "role": self.requirement.role,
            "category": self.requirement.category,
            "name": self.requirement.name,
            "present": self.present,
            "required": self.requirement.required,
            "modes": self.requirement.modes or ["*"],
        }


def evaluate_assets(
    requirements: list[AssetRequirement],
    probe: ProbeReport,
) -> list[AssetStatus]:
    """把资产声明与后端探测结果对账，逐条给出就绪状态。

    ``category == NODE_CATEGORY`` 的需求查 ``probe.has_node_class``（节点是否存在），
    其余查 ``probe.assets``（资产文件是否被后端扫描到）。
    两种来源合成同一种结论，让下游只需要处理一种结构。

    ⚠ 这里**必须**用 ``has_node_class`` 而不是 ``probe.nodes``：
    后者只是一份人工挑选的"关键节点"字典，拿它回答"某个节点类在不在"
    会把所有未被挑中的节点类一律报成缺失 —— 一个高噪声的假错误。
    """
    statuses: list[AssetStatus] = []
    for req in requirements:
        if req.category == NODE_CATEGORY:
            present = probe.has_node_class(req.name)
            # 全量清单为空说明"节点盘点整体失败"，与"这个节点确实没有"是两回事
            declared = bool(probe.node_classes or probe.nodes)
        else:
            values = probe.assets.get(req.category)
            declared = values is not None
            present = bool(values) and req.name in (values or [])
        statuses.append(AssetStatus(requirement=req, present=present, declared=declared))
    return statuses


def readiness_by_mode(
    statuses: list[AssetStatus],
    modes: list[str],
) -> dict[str, dict[str, Any]]:
    """按模式汇总就绪度，并列出各模式**具体缺什么**。

    这是把原先那段手写 ``h3_modes`` 判断泛化之后的产物：
    只要方案声明了资产与模式的对应关系，"t2v 能跑但 ref2v 报错"这类结论
    就是自动推理出来的，而且能直接给出缺失清单。

    返回结构::

        {
          "t2v":   {"ready": True,  "missing": []},
          "ref2v": {"ready": False, "missing": ["unet:minimax_h3_ref2va_....safetensors"]},
        }
    """
    report: dict[str, dict[str, Any]] = {}
    for mode in modes:
        missing: list[str] = []
        for status in statuses:
            req = status.requirement
            if not req.applies_to(mode):
                continue
            if not req.required:
                continue
            if status.present:
                continue
            # 类别整体没探测到 -> 用另一个措辞，避免把探测失败误报成文件缺失
            missing.append(status.label if status.declared else f"{status.label} (类别未探测到)")
        report[mode] = {"ready": not missing, "missing": missing}
    return report


def asset_issues(statuses: list[AssetStatus], modes: list[str] | None = None) -> list[Issue]:
    """把缺失资产转成 :class:`Issue` 列表，供统一的问题汇总使用。"""
    issues: list[Issue] = []
    for status in statuses:
        if status.present:
            continue
        req = status.requirement
        scope = f"（模式：{', '.join(req.modes)}）" if req.modes else ""
        severity = Issue.error if req.required else Issue.warning

        if req.note:
            hint = req.note
        elif req.category == NODE_CATEGORY:
            hint = "该节点类未被后端报告 —— 通常是缺少对应的自定义节点插件或模型包"
        elif req.source_url:
            hint = f"从 {req.source_url} 获取后放入后端 {req.category} 目录，并让后端重新扫描"
        else:
            hint = ""

        issues.append(severity(
            f"缺少资产 {req.name}{scope}",
            code="asset.missing",
            location=status.label,
            hint=hint,
        ))
    return issues
