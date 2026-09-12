"""资产就绪度的行为测试。

这是 [ADR-0008](../../docs/design/0008-avoid-hardcoded-readiness.md) 的直接护栏：
**可用性判断必须由实现声明驱动**。这里钉住三件事：
逐模式报告、`present`/`declared` 的区分、以及"节点存在性走全量清单"。
"""

from __future__ import annotations

from om_bridge.backends.comfyui.solutions.minimax_h3 import manifest
from om_bridge.core.assets import (
    NODE_CATEGORY,
    AssetRequirement,
    evaluate_assets,
    readiness_by_mode,
)
from om_bridge.core.models import ProbeReport

UNET = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
REF2V_UNET = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
LORA8 = manifest.ASSET_DEFAULTS["turbo_lora"]
LORA4 = manifest.ASSET_DEFAULTS["ref2v_turbo_lora"]


def probe_with(*, unets=(), loras=(), node_classes=(), assets_declared=True) -> ProbeReport:
    assets: dict[str, list[str]] = {}
    if assets_declared:
        assets["unet"] = list(unets)
        assets["lora"] = list(loras)
    return ProbeReport(
        backend="comfyui",
        reachable=True,
        assets=assets,
        node_classes=set(node_classes),
    )


# ---------------------------------------------------------------------------
# present 与 declared 是两件事
# ---------------------------------------------------------------------------
def test_present_and_declared_are_distinct_for_files() -> None:
    req = [AssetRequirement("diffusion_model", "unet", UNET)]

    # 类别被报告过，但里面没有这个文件 → 真的缺
    missing = evaluate_assets(req, probe_with(unets=["other.safetensors"]))[0]
    assert missing.present is False and missing.declared is True

    # 类别整体没被报告 → "无法判定"，不是"缺文件"
    unknown = evaluate_assets(req, probe_with(assets_declared=False))[0]
    assert unknown.present is False and unknown.declared is False


def test_missing_wording_differs_when_category_undeclared() -> None:
    """措辞要区分"缺文件"与"探测失败"。

    否则用户会去下载一个本来就有的权重 —— 这是纯粹浪费时间的误报。
    """
    reqs = [AssetRequirement("diffusion_model", "unet", UNET, modes=["t2v"])]

    really_missing = readiness_by_mode(
        evaluate_assets(reqs, probe_with(unets=[])), ["t2v"]
    )
    assert really_missing["t2v"]["ready"] is False
    assert "未探测到" not in really_missing["t2v"]["missing"][0]

    undeclared = readiness_by_mode(
        evaluate_assets(reqs, probe_with(assets_declared=False)), ["t2v"]
    )
    assert "未探测到" in undeclared["t2v"]["missing"][0]


# ---------------------------------------------------------------------------
# 逐模式报告 —— 这条直接对应"t2v 能跑但 ref2v 缺权重"的真实场景
# ---------------------------------------------------------------------------
def test_readiness_is_reported_per_mode() -> None:
    reqs = [
        AssetRequirement("diffusion_model", "unet", UNET, modes=["t2v", "i2v", "flf2v"]),
        AssetRequirement("diffusion_model", "unet", REF2V_UNET, modes=["ref2v"]),
    ]
    # 只装了 fl2va 权重（t2v 那套），没装 ref2va
    statuses = evaluate_assets(reqs, probe_with(unets=[UNET]))
    report = readiness_by_mode(statuses, ["t2v", "i2v", "flf2v", "ref2v"])

    assert report["t2v"]["ready"] is True
    assert report["i2v"]["ready"] is True
    assert report["ref2v"]["ready"] is False
    assert REF2V_UNET in report["ref2v"]["missing"][0]


def test_readiness_ready_for_all_modes_when_assets_present() -> None:
    m = manifest.asset_requirements()
    deps = manifest.ASSET_DEFAULTS
    probe = ProbeReport(
        backend="comfyui",
        reachable=True,
        assets={
            "unet": [deps["unet"], deps["ref2v_unet"]],
            "clip": [deps["clip"]],
            "vae": [deps["video_vae"], deps["audio_vae"]],
            "lora": [deps["turbo_lora"], deps["ref2v_turbo_lora"]],
        },
        node_classes={"MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"},
    )
    report = readiness_by_mode(evaluate_assets(m, probe), list(manifest.MODE_NAMES))
    for mode, entry in report.items():
        assert entry["ready"] is True, f"{mode} 缺 {entry['missing']}"


# ---------------------------------------------------------------------------
# 节点存在性必须查**全量**清单（这正是那次误报的根因）
# ---------------------------------------------------------------------------
def test_node_requirement_uses_full_class_list_not_curated_dict() -> None:
    """回归：`MiniMaxH3ImageToVideo` 明明存在，却被报成缺失。

    原因是当时用"关键节点清单"（``probe.nodes``，一份人工挑选的小字典）
    去回答"某节点类在不在"。修复是引入全量清单 ``node_classes`` / ``has_node_class()``。
    """
    req = [AssetRequirement("node", NODE_CATEGORY, "MiniMaxH3ImageToVideo")]

    # 全量清单里有它，而"关键节点清单"里没有 —— 必须判为存在
    probe = ProbeReport(
        backend="comfyui",
        reachable=True,
        nodes={"SomeOtherCoreNode": True},
        node_classes={"MiniMaxH3ImageToVideo", "SomeOtherCoreNode"},
    )
    status = evaluate_assets(req, probe)[0]
    assert status.present is True


def test_node_requirement_detects_absent_node_class() -> None:
    req = [AssetRequirement("node", NODE_CATEGORY, "NotInstalledNode")]
    probe = ProbeReport(backend="comfyui", reachable=True,
                        node_classes={"MiniMaxH3ImageToVideo"})
    assert evaluate_assets(req, probe)[0].present is False


def test_absent_node_is_reported_in_missing() -> None:
    reqs = [AssetRequirement("node", NODE_CATEGORY, "CustomRefNode", modes=["ref2v"])]
    probe = ProbeReport(backend="comfyui", reachable=True, node_classes=set())
    report = readiness_by_mode(evaluate_assets(reqs, probe), ["ref2v"])
    assert report["ref2v"]["ready"] is False
    assert "node:CustomRefNode" in report["ref2v"]["missing"][0]


# ---------------------------------------------------------------------------
# 模式过滤与可选资产
# ---------------------------------------------------------------------------
def test_requirement_applies_to_all_modes_when_modes_empty() -> None:
    req = AssetRequirement("clip", "clip", "x.safetensors")
    assert req.applies_to("t2v") and req.applies_to("ref2v") and req.applies_to(None)


def test_optional_asset_missing_does_not_break_readiness() -> None:
    reqs = [
        AssetRequirement("diffusion_model", "unet", UNET, modes=["t2v"]),
        AssetRequirement("extra", "lora", "optional.safetensors", modes=["t2v"],
                         required=False),
    ]
    report = readiness_by_mode(evaluate_assets(reqs, probe_with(unets=[UNET])), ["t2v"])
    assert report["t2v"]["ready"] is True


def test_mode_not_mentioning_requirement_is_unaffected() -> None:
    reqs = [AssetRequirement("diffusion_model", "unet", REF2V_UNET, modes=["ref2v"])]
    report = readiness_by_mode(evaluate_assets(reqs, probe_with()), ["t2v"])
    assert report["t2v"]["ready"] is True


def test_asset_status_label_shape() -> None:
    status = evaluate_assets(
        [AssetRequirement("diffusion_model", "unet", UNET)], probe_with()
    )[0]
    assert status.label == f"unet:{UNET}"


# ---------------------------------------------------------------------------
# asset_issues
# ---------------------------------------------------------------------------
def test_asset_issues_flags_missing_required_asset() -> None:
    from om_bridge.core.assets import asset_issues
    from om_bridge.core.models import blocking_issues

    reqs = [AssetRequirement("node", NODE_CATEGORY, "MissingNode", modes=["t2v"])]
    statuses = evaluate_assets(reqs, ProbeReport(backend="comfyui", node_classes=set()))
    issues = asset_issues(statuses, ["t2v"])
    assert issues and blocking_issues(issues)


def test_asset_issues_clean_when_everything_present() -> None:
    from om_bridge.core.assets import asset_issues

    reqs = [AssetRequirement("node", NODE_CATEGORY, "PresentNode", modes=["t2v"])]
    statuses = evaluate_assets(
        reqs, ProbeReport(backend="comfyui", node_classes={"PresentNode"})
    )
    assert asset_issues(statuses, ["t2v"]) == []
