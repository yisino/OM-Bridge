"""ComfyUI 计算图（API 格式）的操作工具。

这个模块只处理**图的结构**：节点、连线、输入覆盖、动态槽位、
以及从 ``/history`` 里把产物捞出来。它不认识 MiniMax H3、也不认识任何具体模型 ——
方案负责构造图，这里负责操作图。

两个必须记住的 ComfyUI 事实
---------------------------
**1. API 格式与 UI 格式长得完全不同。**

* UI 格式（ComfyUI 界面导出的 ``.json``）：顶层有 ``nodes`` 与 ``links`` 两个键。
* API 格式（"Export (API)" 导出的）：就是一个扁平的 ``{节点ID: {"class_type": ..., "inputs": {...}}}``。

后端只吃 API 格式。判错格式的后果不是报错，而是**一个难以理解的执行异常**，
所以 :func:`is_api_format` 会被放在校验链最前面。

**2. ``COMFY_AUTOGROW_V3`` 动态输入在 API 格式里是"点号键"。**

节点定义里只声明父输入名（如 ``ref_images``），看不到具体槽位。
服务端展开时会把父名与 ``prefix + index`` 用 ``.`` 连起来::

    ref_images.ref_image_0
    ref_images.ref_image_1
    ...

依据是 ``comfy_api/latest/_io.py`` 的 ``finalize_prefix()``（用 ``"."`` 连接）。
这条规则对 ``ref_images`` / ``ref_videos`` / ``ref_audios`` / ``ref_video_audios`` 都成立。

**写错槽位名不会静默忽略，而是被 ``POST /prompt`` 以 400 拒绝** ——
所以 :mod:`om_bridge.backends.comfyui.validation` 必须能识别动态键，
否则会把合法的槽位误报成"未知输入"。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ...core.errors import ValidationError
from ...core.models import Artifact, Issue

# 判定图格式时，UI 格式的特征键
UI_FORMAT_MARKERS = ("nodes", "links")

# 可能的产出节点类名（用于"这个图有没有出口"的检查）
SAVE_NODE_CLASSES = frozenset(
    {"SaveVideo", "SaveImage", "SaveWEBP", "SaveAnimatedWEBP", "SaveAudio", "SaveLatent"}
)


def is_link(value: Any) -> bool:
    """判断一个输入值是不是节点连线。

    API 格式里连线的表示是 ``[来源节点ID, 输出槽位序号]``。
    注意节点 ID 可能是字符串也可能是整数（取决于导出工具），两种都要接受。
    """
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def is_api_format(graph: Any) -> tuple[bool, str]:
    """判断是不是 API 格式，并给出原因（便于直接展示给用户）。"""
    if not isinstance(graph, dict):
        return False, f"顶层不是 JSON 对象（实际是 {type(graph).__name__}）"
    if not graph:
        return False, "图是空的"
    for marker in UI_FORMAT_MARKERS:
        if marker in graph:
            return False, (
                f"检测到顶层 {marker!r} 键，说明这是 **UI 格式**而非 API 格式。"
                "请在 ComfyUI 里用 Workflow → Export (API) 重新导出。"
            )
    missing = [str(k) for k, v in graph.items() if not isinstance(v, dict) or "class_type" not in v]
    if missing:
        return False, f"这些节点缺少 class_type，不像 API 格式：{missing[:5]}"
    return True, ""


def load_graph(path: str | Path) -> dict:
    """读取一个 API 格式的图。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise ValidationError(f"图文件不存在：{file_path}")
    try:
        graph = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"图文件不是合法 JSON：{file_path}（{exc}）") from None
    ok, reason = is_api_format(graph)
    if not ok:
        raise ValidationError(f"{file_path} 不可用：{reason}")
    return graph


def dump_graph(graph: dict, path: str | Path) -> Path:
    """写出一个图（保持中文可读，便于人工比对）。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return file_path


def node_class(graph: dict, node_id: str) -> str:
    node = graph.get(str(node_id))
    return node.get("class_type", "") if isinstance(node, dict) else ""


def find_nodes(
    graph: dict,
    class_type: str | None = None,
    predicate: Callable[[str, dict], bool] | None = None,
) -> list[str]:
    """按类名或自定义条件查找节点。

    ``predicate`` 收到 ``(节点ID, 节点字典)``，返回 True 表示命中。
    两个条件都给时取交集。
    """
    found: list[str] = []
    for node_id, node in graph.items():
        if not isinstance(node, dict):
            continue
        if class_type is not None and node.get("class_type") != class_type:
            continue
        if predicate is not None and not predicate(str(node_id), node):
            continue
        found.append(str(node_id))
    return found


def next_node_id(graph: dict, *, start: int = 1) -> int:
    """给出一个可用的新节点 ID（纯数字）。"""
    numeric = [int(k) for k in graph if str(k).isdigit()]
    return max(numeric, default=start - 1) + 1


def apply_overrides(graph: dict, overrides: dict[str, Any] | Iterable[str]) -> int:
    """把 ``<节点ID>.<输入名> = 值`` 形式的覆盖写进图。

    接受两种输入：字典（已是最终值）或字符串列表（``"6.prompt=hello"``，
    命令行传来的形态）。返回实际应用了几条。

    校验很严格（节点必须存在、输入名必须已存在），而不是"直接塞进去"：
    塞进去的多余输入会被后端以 400 拒绝，报错信息里看不出是哪个覆盖项导致的。
    """
    items: list[tuple[str, Any]] = []
    if isinstance(overrides, dict):
        items = list(overrides.items())
    else:
        for raw in overrides:
            if "=" not in raw:
                raise ValidationError(f"覆盖项缺少 '='：{raw!r}",
                                      hint="正确格式：<节点ID>.<输入名>=<值>")
            key, _, value = raw.partition("=")
            items.append((key, _coerce_cli_value(value)))

    applied = 0
    for dotted, value in items:
        if "." not in dotted:
            raise ValidationError(
                f"覆盖项 {dotted!r} 缺少输入名", hint="正确格式：<节点ID>.<输入名>=<值>")
        node_id, _, input_name = dotted.partition(".")
        node = graph.get(node_id)
        if not isinstance(node, dict):
            raise ValidationError(
                f"覆盖项 {dotted!r} 指向的节点不存在",
                hint=f"现有节点：{sorted(graph, key=str)}",
            )
        inputs = node.setdefault("inputs", {})
        if input_name not in inputs:
            raise ValidationError(
                f"节点 {node_id}（{node.get('class_type')}）没有输入 {input_name!r}",
                hint=f"可用输入：{sorted(inputs)}",
            )
        inputs[input_name] = value
        applied += 1
    return applied


def _coerce_cli_value(raw: str) -> Any:
    """把命令行字符串还原成图需要的类型。

    顺序很重要：先试 bool，再试 int，最后 float。
    若先试 int，``"1"`` 会被当成 1 而不是 True —— 对 ``denoise=True`` 这类
    输入无害，但对真正的布尔输入就是错的。
    """
    text = raw.strip()
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return raw


# ---------------------------------------------------------------------------
# 动态槽位（COMFY_AUTOGROW_V3）
# ---------------------------------------------------------------------------
def autogrow_slot_key(parent: str, prefix: str, index: int) -> str:
    """拼出动态槽位在 API 格式里的键名：``<父名>.<前缀><序号>``。

    见模块文档第 2 条：连接符是 ``"."``（依据 ``comfy_api/latest/_io.py``）。
    """
    return f"{parent}.{prefix}{index}"


def autogrow_slots(graph: dict, node_id: str, parent: str, prefix: str) -> dict[str, str]:
    """列出某个节点上已连线的动态槽位，返回 ``{槽位键: 来源节点ID}``。"""
    node = graph.get(str(node_id)) or {}
    inputs = node.get("inputs") or {}
    marker = f"{parent}."
    return {
        key: str(value[0])
        for key, value in inputs.items()
        if key.startswith(marker) and is_link(value)
    }


def bind_media_slots(
    graph: dict,
    node_id: str,
    *,
    parent: str,
    prefix: str,
    names: list[str],
    max_slots: int,
    loader_class: str = "LoadImage",
    image_input: str = "image",
    title_prefix: str = "",
    title_template: str = "{role}_{index}",
) -> dict[str, str]:
    """把一批"已上传的远端文件名"按顺序绑到动态槽位上。

    做三件事：

    1. 复用图中已存在的加载节点（不重复造节点，保持图干净）；
    2. 槽位不够时新建加载节点；
    3. **清掉多余的槽位**并回收其不再被引用的加载节点。

    第 3 点是关键的正确性要求：传 1 张参考图给一个原本有 2 个槽位的模板时，
    如果不清理，第 2 个槽位仍指向旧的图片 —— 提示词里的 ``<Picture 2>``
    就还能引用到一张"用户以为已经不用了"的图，身份锁定会静默出错。

    顺序即语义：第 i 个名字会成为提示词里的第 i+1 个引用。
    返回 ``{槽位键: 加载节点ID}``。
    """
    if len(names) > max_slots:
        raise ValidationError(
            f"{parent} 最多接受 {max_slots} 个槽位，收到 {len(names)} 个",
            issues=[Issue.error(
                f"{len(names)} > 最大槽位数 {max_slots}", code="graph.too_many_slots",
                location=parent)],
        )

    existing = autogrow_slots(graph, node_id, parent, prefix)
    inputs = graph[str(node_id)].setdefault("inputs", {})
    allocated = next_node_id(graph)
    bound: dict[str, str] = {}

    for index, name in enumerate(names):
        key = autogrow_slot_key(parent, prefix, index)
        candidate = existing.pop(key, None)
        role = title_template.format(role=prefix.rstrip("_"), index=index)
        if candidate and node_class(graph, candidate) == loader_class:
            graph[candidate].setdefault("inputs", {})[image_input] = name
            graph[candidate].setdefault("_meta", {})["title"] = f"{title_prefix}{role}"
            slot_node = candidate
        else:
            slot_node = str(allocated)
            allocated += 1
            graph[slot_node] = {
                "class_type": loader_class,
                "_meta": {"title": f"{title_prefix}{role}"},
                "inputs": {image_input: name},
            }
        inputs[key] = [slot_node, 0]
        bound[key] = slot_node

    # 回收多余槽位
    for key, orphan in existing.items():
        inputs.pop(key, None)
        still_referenced = any(
            is_link(value) and str(value[0]) == orphan
            for other_id, other in graph.items()
            if other_id != orphan
            for value in (other.get("inputs") or {}).values()
        )
        if not still_referenced and node_class(graph, orphan) == loader_class:
            graph.pop(orphan, None)

    return bound


# ---------------------------------------------------------------------------
# 产物收集
# ---------------------------------------------------------------------------
def collect_outputs(entry: dict) -> list[Artifact]:
    """从 ``/history/<id>`` 的一条记录里收集全部产物。

    ⚠ **ComfyUI 的坑**：``SaveVideo`` 节点产出的 mp4 挂在 ``images`` 键下，
    而不是 ``video`` 键（还会带一个 ``"animated": [true]``）。
    因此这里按"任何带 filename 的条目"收集，**不按 key 名过滤**，
    否则会漏掉视频 —— 这是实际踩过的坑。

    ``kind`` 只作为参考信息保留，判断类型请用 :attr:`Artifact.is_video` 等。
    """
    artifacts: list[Artifact] = []
    outputs = entry.get("outputs") or {}
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue
        for key, items in node_output.items():
            if key == "animated":
                continue
            candidates = items if isinstance(items, list) else [items]
            for item in candidates:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                artifacts.append(
                    Artifact(
                        filename=item["filename"],
                        kind=key,
                        origin={
                            "node": str(node_id),
                            "subfolder": item.get("subfolder", ""),
                            "type": item.get("type", "output"),
                        },
                    )
                )
    return artifacts


def extract_errors(status: dict) -> list[Any]:
    """从 ``/history`` 的 ``status`` 里抽出执行错误。

    ComfyUI 把每条消息编码成 ``["execution_error", {...}]`` 这样的二元组，
    失败原因藏在第二个元素里。
    """
    errors: list[Any] = []
    for message in status.get("messages") or []:
        if not isinstance(message, list) or len(message) != 2:
            continue
        kind = str(message[0])
        if kind in ("execution_error", "execution_interrupted"):
            errors.append(message[1] if isinstance(message[1], dict) else {"detail": message[1]})
    return errors


def is_completed(entry: dict) -> bool:
    """判断历史记录是否已到达终态。

    ComfyUI 有两个可用的信号：``status.completed`` 与 ``status.status_str``。
    两个都看，是因为某些版本只填其中一个。
    """
    status = entry.get("status") or {}
    if status.get("completed") is True:
        return True
    return status.get("status_str") in ("success", "error")
