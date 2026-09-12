"""图操作的行为测试。

重点是三条"不报错但结果是错的"的坑：
* `SaveVideo` 的 mp4 挂在 `images` 键下（不是 `video`）；
* 动态槽位是点号键（写成别的名字会被后端 400 拒绝，但只是"看起来像后端问题"）；
* 覆盖项必须严格校验（否则多余输入被 400 拒绝，报错里看不出是哪条覆盖导致的）。
"""

from __future__ import annotations

import pytest

from om_bridge.backends.comfyui import graph as g
from om_bridge.core.errors import ValidationError
from om_bridge.core.models import MediaKind


# ---------------------------------------------------------------------------
# 连线与格式识别
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (["1", 0], True),
    ([1, 2], True),
    ([], False),
    (["1"], False),
    (["1", 0, 3], False),
    ("1", False),
    (5, False),
    (["1", "0"], False),      # 槽位序号必须是整数
    (["1", True], False),     # bool 是 int 的子类，必须排除
    (None, False),
])
def test_is_link(value: object, expected: bool) -> None:
    assert g.is_link(value) is expected


def test_is_api_format_accepts_api_graph() -> None:
    ok, reason = g.is_api_format({"1": {"class_type": "UNETLoader", "inputs": {}}})
    assert ok and reason == ""


@pytest.mark.parametrize("marker", ["nodes", "links", "last_node_id", "groups"])
def test_is_api_format_rejects_ui_format(marker: str) -> None:
    """UI 格式顶层带 `nodes`/`links` 等键 —— 必须明确拒绝并给出指引。

    静默接受会一路走到后端才失败，而那时的报错与真实原因相距很远。
    """
    ok, reason = g.is_api_format({marker: [], "1": {"class_type": "X"}})
    assert not ok
    assert "API" in reason or "api" in reason


def test_is_api_format_rejects_nodes_without_class_type() -> None:
    ok, reason = g.is_api_format({"1": {"inputs": {}}})
    assert not ok and "class_type" in reason


def test_is_api_format_rejects_empty_and_non_dict() -> None:
    assert g.is_api_format({})[0] is False
    assert g.is_api_format([])[0] is False


def test_load_graph_missing_file_raises_validation_error(tmp_path) -> None:
    with pytest.raises(ValidationError):
        g.load_graph(tmp_path / "nope.json")


def test_load_graph_rejects_invalid_json(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError):
        g.load_graph(bad)


def test_dump_and_load_round_trip(tmp_path) -> None:
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "h3.safetensors"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen.safetensors"}},
    }
    path = g.dump_graph(graph, tmp_path / "sub" / "out.json")
    assert path.is_file()
    assert g.load_graph(path) == graph


def test_dump_graph_writes_readable_chinese(tmp_path) -> None:
    """产物要能人工比对，所以中文不转义。"""
    path = g.dump_graph(
        {"1": {"class_type": "LoadImage", "inputs": {"image": "丹华_角色模型.png"}}},
        tmp_path / "g.json",
    )
    assert "丹华_角色模型.png" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 节点查询
# ---------------------------------------------------------------------------
def test_find_nodes_by_class_type() -> None:
    """`find_nodes` 返回**节点 ID 列表**，不是 ``(ID, 节点)`` 元组。

    刻意只返回 ID：调用方接下来多半要改写节点（``graph[nid]["inputs"][...] = ...``），
    拿着 ID 回查比拿着一份可能已经过期的节点引用更安全。
    """
    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
        "3": {"class_type": "SaveVideo", "inputs": {}},
    }
    assert sorted(g.find_nodes(graph, "LoadImage")) == ["1", "2"]
    assert g.find_nodes(graph, "SaveVideo") == ["3"]
    assert g.find_nodes(graph, "NoSuchNode") == []


def test_find_nodes_accepts_predicate() -> None:
    """谓词收到 ``(节点ID, 节点字典)``；与类名同时给时取**交集**。"""
    graph = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
        "3": {"class_type": "LoadImage", "inputs": {}},
    }
    asked: list[str] = []

    def wants_a(node_id: str, node: dict) -> bool:
        asked.append(node_id)
        return node["inputs"].get("image") == "a.png"

    assert g.find_nodes(graph, "LoadImage", wants_a) == ["1"]
    assert sorted(asked) == ["1", "2", "3"], "谓词应对每个候选节点都调用一次"
    assert g.find_nodes(graph, predicate=wants_a) == ["1"]


def test_node_class_lookup() -> None:
    graph = {"1": {"class_type": "UNETLoader", "inputs": {}}}
    assert g.node_class(graph, "1") == "UNETLoader"
    assert g.node_class(graph, "99") == ""


def test_next_node_id_ignores_non_numeric_ids() -> None:
    graph = {"1": {}, "7": {}, "abc": {}}
    assert g.next_node_id(graph) == 8


def test_next_node_id_on_empty_graph() -> None:
    assert g.next_node_id({}) == 1


# ---------------------------------------------------------------------------
# 动态槽位（点号键）
# ---------------------------------------------------------------------------
def test_autogrow_slot_key_uses_dot_separator() -> None:
    """依据 `comfy_api/latest/_io.py` 的 `finalize_prefix()`：用 `.` 连接。

    这条如果写错（例如用 `_` 或 `/`），ComfyUI 会以 **400** 拒绝 ——
    而报错看起来像"后端不认识这个输入"，很容易被误判成版本问题。
    """
    assert g.autogrow_slot_key("ref_images", "ref_image_", 0) == "ref_images.ref_image_0"
    assert g.autogrow_slot_key("ref_images", "ref_image_", 8) == "ref_images.ref_image_8"
    assert g.autogrow_slot_key("ref_videos", "ref_video_", 2) == "ref_videos.ref_video_2"


def test_autogrow_slots_lists_bound_slots() -> None:
    graph = {
        "6": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
        "7": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
        "8": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "prompt": "x",
            "ref_images.ref_image_0": ["6", 0],
            "ref_images.ref_image_1": ["7", 0],
        }},
    }
    slots = g.autogrow_slots(graph, "8", "ref_images", "ref_image_")
    assert slots == {"ref_images.ref_image_0": "6", "ref_images.ref_image_1": "7"}


def test_autogrow_slots_empty_for_unrelated_node() -> None:
    graph = {"8": {"class_type": "X", "inputs": {"prompt": "x"}}}
    assert g.autogrow_slots(graph, "8", "ref_images", "ref_image_") == {}


# ---------------------------------------------------------------------------
# 覆盖（逃生舱）—— 严格校验
# ---------------------------------------------------------------------------
def test_apply_overrides_from_dict() -> None:
    graph = {"6": {"class_type": "H3", "inputs": {"prompt": "old", "width": 864}}}
    assert g.apply_overrides(graph, {"6.prompt": "new"}) == 1
    assert graph["6"]["inputs"]["prompt"] == "new"


def test_apply_overrides_from_cli_strings_coerces_types() -> None:
    graph = {"6": {"class_type": "H3", "inputs": {"width": 864, "turbo": False,
                                                  "scale": 1.0}}}
    g.apply_overrides(graph, ["6.width=608", "6.turbo=true", "6.scale=1.5"])
    assert graph["6"]["inputs"]["width"] == 608
    assert graph["6"]["inputs"]["turbo"] is True
    assert graph["6"]["inputs"]["scale"] == pytest.approx(1.5)


def test_apply_overrides_rejects_unknown_node() -> None:
    with pytest.raises(ValidationError) as excinfo:
        g.apply_overrides({"6": {"class_type": "H3", "inputs": {}}}, {"99.prompt": "x"})
    assert "99" in str(excinfo.value)


def test_apply_overrides_rejects_unknown_input() -> None:
    """必须严格：塞进多余输入会被后端 400 拒绝，且报错看不出是哪条覆盖导致的。"""
    graph = {"6": {"class_type": "H3", "inputs": {"prompt": "x"}}}
    with pytest.raises(ValidationError) as excinfo:
        g.apply_overrides(graph, {"6.widht": "x"})
    assert "widht" in str(excinfo.value)


def test_apply_overrides_rejects_malformed_entry() -> None:
    with pytest.raises(ValidationError):
        g.apply_overrides({"6": {"inputs": {}}}, ["6prompt"])
    with pytest.raises(ValidationError):
        g.apply_overrides({"6": {"inputs": {}}}, ["prompt=x"])


# ---------------------------------------------------------------------------
# 产物收集 —— 这里藏着最有价值的一个坑
# ---------------------------------------------------------------------------
def test_collect_outputs_finds_video_under_images_key() -> None:
    """⚠ ComfyUI 的坑：`SaveVideo` 产出的 mp4 挂在 **`images`** 键下，不是 `video`。

    历史上按 `kind == "video"` 筛选，于是"跑完了但找不到产物"。
    """
    entry = {
        "outputs": {
            "15": {
                "images": [{
                    "filename": "MiniMax_H3_00001_.mp4",
                    "subfolder": "video",
                    "type": "output",
                    "animated": [True],
                }],
            }
        }
    }
    artifacts = g.collect_outputs(entry)
    assert len(artifacts) == 1
    assert artifacts[0].filename == "MiniMax_H3_00001_.mp4"
    assert artifacts[0].kind == "images"          # 键名是实情，不粉饰
    assert artifacts[0].is_video is True          # 判断类型请用它
    assert artifacts[0].origin["node"] == "15"
    assert artifacts[0].origin["subfolder"] == "video"


def test_collect_outputs_skips_animated_marker() -> None:
    """`animated` 不是产物，只是一条标记，不能当文件收进来。"""
    entry = {"outputs": {"15": {"images": [{"filename": "a.mp4"}], "animated": [True]}}}
    artifacts = g.collect_outputs(entry)
    assert [a.filename for a in artifacts] == ["a.mp4"]


def test_collect_outputs_handles_multiple_nodes_and_keys() -> None:
    entry = {
        "outputs": {
            "12": {"images": [{"filename": "frame_00001_.png"}]},
            "15": {"images": [{"filename": "clip.mp4"}]},
        }
    }
    names = {a.filename for a in g.collect_outputs(entry)}
    assert names == {"frame_00001_.png", "clip.mp4"}


def test_collect_outputs_ignores_entries_without_filename() -> None:
    entry = {"outputs": {"15": {"images": [{"subfolder": "x"}, "not-a-dict"]}}}
    assert g.collect_outputs(entry) == []


def test_collect_outputs_tolerates_missing_outputs() -> None:
    assert g.collect_outputs({}) == []
    assert g.collect_outputs({"outputs": None}) == []


def test_artifact_video_detection_by_extension() -> None:
    """有些后端把 mp4 放在别的键下 —— 扩展名是更可靠的判据。"""
    from om_bridge.core.models import Artifact

    assert Artifact(filename="a.mp4", kind="images").is_video is True
    assert Artifact(filename="a.MP4", kind="images").is_video is True
    assert Artifact(filename="a.png", kind="images").is_video is False


# ---------------------------------------------------------------------------
# 错误提取与终态判断
# ---------------------------------------------------------------------------
def test_extract_errors_pulls_execution_error_messages() -> None:
    status = {
        "status_str": "error",
        "messages": [
            ["execution_start", {"prompt_id": "p1"}],
            ["execution_error", {"node_id": 6, "exception_message": "CUDA out of memory"}],
        ],
    }
    errors = g.extract_errors(status)
    assert errors, "应当抽出执行错误"
    assert "CUDA out of memory" in repr(errors)


def test_extract_errors_empty_when_clean() -> None:
    status = {"status_str": "success", "messages": [["execution_start", {}]]}
    assert g.extract_errors(status) == []


def test_is_completed_recognises_success() -> None:
    assert g.is_completed({"status": {"completed": True, "status_str": "success"}}) is True


def test_is_completed_recognises_failure_as_terminal() -> None:
    """失败也是终态 —— 否则轮询会一直等到超时，白等几分钟。"""
    assert g.is_completed({"status": {"completed": False, "status_str": "error"}}) is True


def test_is_completed_false_while_running() -> None:
    assert g.is_completed({"status": {"completed": False, "status_str": "running"}}) is False


# ---------------------------------------------------------------------------
# 媒体槽位绑定
# ---------------------------------------------------------------------------
def _ref2v_graph_with_two_slots() -> dict:
    return {
        "6": {"class_type": "LoadImage", "_meta": {"title": "ref_image_0 (old.png)"},
              "inputs": {"image": "old.png"}},
        "7": {"class_type": "LoadImage", "_meta": {"title": "ref_image_1 (old2.png)"},
              "inputs": {"image": "old2.png"}},
        "8": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "prompt": "x",
            "ref_images.ref_image_0": ["6", 0],
            "ref_images.ref_image_1": ["7", 0],
        }},
    }


def test_bind_media_slots_creates_loader_nodes_on_empty_graph() -> None:
    graph: dict = {"8": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"prompt": "x"}}}
    bound = g.bind_media_slots(
        graph, "8", parent="ref_images", prefix="ref_image_",
        names=["丹华.png", "赤焰.png"], max_slots=9,
    )
    assert list(bound) == ["ref_images.ref_image_0", "ref_images.ref_image_1"]
    node_a, node_b = bound.values()
    assert graph[node_a]["inputs"]["image"] == "丹华.png"
    assert graph[node_b]["inputs"]["image"] == "赤焰.png"
    assert graph[node_a]["class_type"] == "LoadImage"


def test_bind_media_slots_recycles_extra_slots() -> None:
    """**正确性要求**：槽位变少时必须清掉多余的。

    否则提示词里的 `<Picture 2>` 仍能引用到一张"用户以为已经不用了"的图，
    身份锁定会静默出错 —— 没有任何报错，但结果不对。
    """
    graph = _ref2v_graph_with_two_slots()
    bound = g.bind_media_slots(
        graph, "8", parent="ref_images", prefix="ref_image_",
        names=["only.png"], max_slots=9,
    )
    assert list(bound) == ["ref_images.ref_image_0"]
    inputs = graph["8"]["inputs"]
    assert "ref_images.ref_image_1" not in inputs
    assert "7" not in graph, "孤儿加载节点应当被回收"
    assert graph[bound["ref_images.ref_image_0"]]["inputs"]["image"] == "only.png"


def test_bind_media_slots_reuses_existing_loaders() -> None:
    """同数量重绑不该不断新增节点（模板会被反复复用）。"""
    graph = _ref2v_graph_with_two_slots()
    before = set(graph)
    bound = g.bind_media_slots(
        graph, "8", parent="ref_images", prefix="ref_image_",
        names=["a.png", "b.png"], max_slots=9,
    )
    assert set(graph) == before
    assert set(bound.values()) == {"6", "7"}


def test_bind_media_slots_rejects_overflow() -> None:
    from om_bridge.backends.comfyui.solutions.minimax_h3 import manifest

    graph: dict = {"8": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {}}}
    too_many = [f"r{i}.png" for i in range(manifest.MAX_REFERENCE_IMAGES + 1)]
    with pytest.raises(ValidationError) as excinfo:
        g.bind_media_slots(
            graph, "8", parent="ref_images", prefix="ref_image_",
            names=too_many, max_slots=manifest.MAX_REFERENCE_IMAGES,
        )
    assert "graph.too_many_slots" in {i.code for i in excinfo.value.issues}


def test_bind_media_slots_order_is_preserved() -> None:
    """顺序即语义：第 i 个名字对应提示词里的 `<Picture i+1>`。"""
    graph: dict = {"8": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {}}}
    names = ["first.png", "second.png", "third.png"]
    bound = g.bind_media_slots(
        graph, "8", parent="ref_images", prefix="ref_image_",
        names=names, max_slots=9,
    )
    ordered = [bound[f"ref_images.ref_image_{i}"] for i in range(3)]
    assert [graph[nid]["inputs"]["image"] for nid in ordered] == names


def test_media_kind_enum_values() -> None:
    assert MediaKind.IMAGE.value == "image"
    assert MediaKind.VIDEO.value == "video"
    assert MediaKind.AUDIO.value == "audio"
