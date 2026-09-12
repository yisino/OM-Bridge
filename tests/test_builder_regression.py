"""结构回归：把 ``builder`` 的产出与 5 份**已通过真机验证**的模板逐字节比对。

为什么这是本项目最重要的测试
============================

这 5 份模板是历史产物：它们被真机端到端跑通过（包括音频轨与参考图生效的 A/B 验证），
并且**外部框架（OpenMontage）依赖其中的节点 ID**（`output_node` = 15/16/17）。

所以"改了 `builder.py` 之后还能不能跑"不是靠推理保证的，而是靠这个测试：
只要产出的 JSON 与模板不再**逐字节相同**（除提示词外），就说明行为变了 ——
哪怕改动看起来只是"调整了一下节点顺序"。

比对范围包含**键的顺序**：因此这里用序列化后的字符串比较，而不是 ``dict ==``
（后者忽略顺序，会漏掉真实的 JSON 结构变化）。

提示词是唯一被归一化的字段：模板里烘焙的是"海边灯塔"那段示例词，
而构建时传什么由调用方决定，与图结构无关。
"""

from __future__ import annotations

import json

import pytest
from conftest import TEMPLATES

from om_bridge.backends.comfyui.solutions.minimax_h3 import builder, manifest

PLACEHOLDER = "<PROMPT-NORMALIZED>"

# 模板名 → (build_h3_graph 的关键字参数, 期望的 output_node)
CASES: dict[str, tuple[dict, str]] = {
    "minimax_h3_t2v_api.json": ({"mode": "t2v"}, "15"),
    "minimax_h3_i2v_api.json": (
        {"mode": "i2v", "first_frame_image": "丹华_角色模型.png"}, "16"),
    "minimax_h3_flf2v_api.json": (
        {"mode": "flf2v", "first_frame_image": "丹华_角色模型.png",
         "last_frame_image": "赤焰_角色模型.png"}, "17"),
    "minimax_h3_ref2v_api.json": (
        {"mode": "ref2v", "ref_images": ["丹华_角色模型.png"]}, "16"),
    "minimax_h3_ref2v_2ref_api.json": (
        {"mode": "ref2v", "ref_images": ["丹华_角色模型.png", "赤焰_角色模型.png"]}, "17"),
}

# 模板生成时使用的参数（从模板文件里读出的事实，不是猜测）
BASE_PARAMS = {"width": 864, "height": 480, "length": 124, "seed": 0, "fps": 24}


def normalize(graph: dict) -> str:
    """归一化提示词后序列化。

    ``json.dumps`` 保持**插入顺序**，所以字符串相等同时验证了结构、取值与键序。
    """
    clone = json.loads(json.dumps(graph, ensure_ascii=False))
    for node in clone.values():
        inputs = node.get("inputs")
        if isinstance(inputs, dict) and "prompt" in inputs:
            inputs["prompt"] = PLACEHOLDER
    return json.dumps(clone, ensure_ascii=False, indent=2)


def load_template(name: str) -> dict:
    return json.loads((TEMPLATES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("template_name", sorted(CASES))
def test_builder_matches_verified_template(template_name: str) -> None:
    kwargs, expected_output = CASES[template_name]
    template = load_template(template_name)

    graph, output_node = builder.build_h3_graph(
        prompt="（提示词不参与结构比对）", **BASE_PARAMS, **kwargs
    )

    assert output_node == expected_output, (
        f"{template_name}: output_node 变了（{output_node} != {expected_output}）。"
        "外部框架依赖这个值，猜错会导致「跑完了但找不到产物」。"
    )

    actual_text = normalize(graph)
    expected_text = normalize(template)
    if actual_text != expected_text:
        # 给出可读的差异位置（逐行 diff 比"两个大 JSON 不相等"有用得多）
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                expected_text.splitlines(), actual_text.splitlines(),
                fromfile=f"template:{template_name}", tofile="builder-output", lineterm="",
            )
        )
        pytest.fail(
            f"{template_name}: 图结构与已验证模板不一致。\n"
            "这意味着改了构图行为 —— 请确认是有意的，并同步更新模板。\n" + diff
        )


@pytest.mark.parametrize("template_name", sorted(CASES))
def test_output_node_for_agrees_with_builder(template_name: str) -> None:
    """``output_node_for()`` 只是"预先推算"用的辅助函数，必须与真实构建结果一致。

    它容易腐坏（两处独立推导同一个编号），所以用参数化测试钉住。
    """
    kwargs, expected_output = CASES[template_name]
    mode = kwargs.pop("mode")

    counts: dict[str, int] = {}
    if "ref_images" in kwargs:
        counts["reference_image"] = len(kwargs["ref_images"])
    for role in ("first_frame_image", "last_frame_image"):
        if role in kwargs:
            counts[role.replace("_image", "")] = 1

    assert builder.output_node_for(mode, counts) == expected_output


def test_ref2v_rejects_fl2va_weights() -> None:
    """ref2v 用 fl2va 权重是"能跑但结果不对"的陷阱，构建期必须直接拒绝。"""
    from om_bridge.core.errors import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        builder.build_h3_graph(
            mode="ref2v", prompt="x", **BASE_PARAMS,
            ref_images=["a.png"],
            unet_name=manifest.ASSET_DEFAULTS["unet"],   # fl2va —— 错的
        )
    assert "h3.wrong_weights" in {issue.code for issue in excinfo.value.issues}


def test_ref_images_not_allowed_outside_ref2v() -> None:
    """把参考图传给 t2v 是常见的误解，应当报错而不是静默忽略。"""
    from om_bridge.core.errors import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        builder.build_h3_graph(
            mode="t2v", prompt="x", **BASE_PARAMS, ref_images=["a.png"]
        )
    assert "h3.refs_not_applicable" in {i.code for i in excinfo.value.issues}


def test_ref2v_requires_at_least_one_reference() -> None:
    from om_bridge.core.errors import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        builder.build_h3_graph(mode="ref2v", prompt="x", **BASE_PARAMS)
    assert "h3.missing_ref" in {i.code for i in excinfo.value.issues}


def test_ref2v_rejects_more_than_max_references() -> None:
    from om_bridge.core.errors import ValidationError

    too_many = [f"r{i}.png" for i in range(manifest.MAX_REFERENCE_IMAGES + 1)]
    with pytest.raises(ValidationError) as excinfo:
        builder.build_h3_graph(
            mode="ref2v", prompt="x", **BASE_PARAMS, ref_images=too_many
        )
    assert "h3.too_many_refs" in {i.code for i in excinfo.value.issues}


def test_unknown_mode_is_rejected() -> None:
    from om_bridge.core.errors import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        builder.build_h3_graph(mode="nope", prompt="x", **BASE_PARAMS)
    assert "h3.bad_mode" in {i.code for i in excinfo.value.issues}


def test_builder_is_deterministic() -> None:
    """确定性是 ``fingerprint()`` 与"模板比对"成立的前提。"""
    args = {"mode": "t2v", "prompt": "同样的输入", **BASE_PARAMS}
    first, node_a = builder.build_h3_graph(**args)
    second, node_b = builder.build_h3_graph(**args)
    assert node_a == node_b
    assert normalize(first) == normalize(second)


def test_ref2v_uses_dotted_dynamic_slot_keys() -> None:
    """动态槽位必须是点号键。

    依据 ``comfy_api/latest/_io.py`` 的 ``finalize_prefix()``（用 ``"."`` 连接父名与前缀）。
    写成 ``ref_image_0`` 会被 ComfyUI 以 **400** 拒绝 —— 不是静默忽略。
    """
    graph, _ = builder.build_h3_graph(
        mode="ref2v", prompt="x", **BASE_PARAMS, ref_images=["a.png", "b.png"]
    )
    h3_node = next(
        node for node in graph.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo"
    )
    keys = [k for k in h3_node["inputs"] if k.startswith("ref_images.")]
    assert keys == ["ref_images.ref_image_0", "ref_images.ref_image_1"]


def test_audio_vae_is_wired_so_output_has_audio_track() -> None:
    """H3 是音视频联合生成 —— 忘了接音频 VAE 会静默产出无声视频。"""
    graph, save_id = builder.build_h3_graph(mode="t2v", prompt="x", **BASE_PARAMS)
    classes = {node["class_type"] for node in graph.values()}
    assert "VAEDecodeAudio" in classes
    # CreateVideo 必须同时拿到视频与音频两路
    create = next(n for n in graph.values() if n["class_type"] == "CreateVideo")
    assert set(create["inputs"]) >= {"images", "audio", "fps"}
    assert graph[save_id]["class_type"] == "SaveVideo"
