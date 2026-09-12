"""静态校验器 —— 在提交之前把"必然会被拒绝的图"挡下来。

为什么值得单独做一层
--------------------
``POST /prompt`` 的校验失败会返回一个**结构化但难读**的 JSON，
而且这次提交已经消耗了一次排队。更糟的是失败信息里通常只有一个节点 ID 和
Python 异常栈，定位要来回好几轮。

静态校验拿 ``/object_info`` 当事实来源，可以在本地一次性报出**全部**问题：
类不存在、输入名拼错、必填项缺失、枚举越界、数值越界、连线悬空、输出槽位越界……
每一条都带位置和修改建议。零成本，纯收益。

识别动态槽位（关键）
--------------------
``COMFY_AUTOGROW_V3`` 类型（如 ``ref_images``）在 ``/object_info`` 里
**只暴露父名**，看不到 ``ref_images.ref_image_0`` 这些槽位。
不特殊处理的话，一个完全合法的 ref2v 图会被报成"存在未知输入"，
而用户会去"修"一个本来就对的东西 —— 这是最糟糕的一类假错误。
处理办法见 :func:`_resolve_dynamic_input`。
"""

from __future__ import annotations

from typing import Any

from ...core.models import Issue
from .graph import SAVE_NODE_CLASSES, is_api_format, is_link
from .inventory import Inventory

# 这些类型在 /object_info 里没有可枚举的取值，跳过取值检查（它们的合法性
# 由服务端在展开时判断，本地无从验证）
OPAQUE_TYPES = frozenset(
    {"COMFY_AUTOGROW_V3", "COMFY_MULTITYPE_WIDGET", "COMFY_DYNAMICCOMBO_V3_ROOT"}
)


def resolve_dynamic_input(
    inventory: Inventory, class_type: str, name: str
) -> tuple[tuple[Any, dict] | None, str | None]:
    """解析 ``父名.前缀序号`` 形式的动态输入。

    返回 ``(spec, None)`` 表示解析成功；
    返回 ``(None, 原因)`` 表示"确实是个动态输入，但槽位名非法"（应报错）；
    返回 ``(None, None)`` 表示"这根本不是动态输入"（交给常规未知输入处理）。
    """
    if "." not in name:
        return None, None
    parent, _, child = name.partition(".")
    found = Inventory.input_spec(inventory.object_info, class_type, parent)
    if found is None:
        return None, None
    type_token, options = found
    if type_token != "COMFY_AUTOGROW_V3":
        return None, None

    template = options.get("template") or {}
    if "prefix" in template and "max" in template:
        legal = [f"{template['prefix']}{i}" for i in range(int(template["max"]))]
    elif template.get("names"):
        legal = list(template["names"])
    else:
        return None, f"无法确定动态输入 {parent!r} 的槽位名（模板中既无 prefix/max 也无 names）"

    if child not in legal:
        return None, (
            f"输入 {name!r} 不是 {parent!r} 的合法槽位"
            f"（合法取值：{legal[0]} … {legal[-1]}，共 {len(legal)} 个）"
        )
    return _template_input_spec(options), None


def _template_input_spec(options: dict) -> tuple[Any, dict] | None:
    """从 AUTOGROW 模板里取出单个槽位的类型定义。"""
    template_input = (options.get("template") or {}).get("input") or {}
    for kind in ("required", "optional"):
        for _name, entry in (template_input.get(kind) or {}).items():
            type_token = entry[0] if isinstance(entry, list) and entry else entry
            opts = (
                entry[1]
                if isinstance(entry, list) and len(entry) > 1 and isinstance(entry[1], dict)
                else {}
            )
            return type_token, opts
    return None


def _enum_values(type_token: Any, options: dict) -> list | None:
    """取枚举类输入的全部合法取值。"""
    if isinstance(type_token, list):
        return list(type_token)
    if type_token in ("COMBO", "COMFY_DYNAMICCOMBO_V3"):
        raw = options.get("options")
        if isinstance(raw, list):
            if raw and isinstance(raw[0], dict) and "key" in raw[0]:
                return [entry["key"] for entry in raw]
            return list(raw)
    return None


def validate_graph(graph: dict[str, Any], inventory: Inventory, label: str = "graph") -> list[Issue]:
    """校验一张 API 格式的图，返回**全部**问题（含非阻断告警）。"""
    issues: list[Issue] = []

    ok, reason = is_api_format(graph)
    if not ok:
        return [Issue.error(f"{label}: {reason}", code="graph.format", location=label)]

    object_info = inventory.object_info

    for node_id, node in graph.items():
        class_type = node.get("class_type")
        if class_type not in inventory.node_classes:
            issues.append(Issue.error(
                f"{label}: 节点 {node_id} 的类 {class_type!r} 在本后端上不存在",
                code="graph.unknown_class", location=str(node_id),
                hint="该节点所需的插件/模型包没有装，或类名拼错。",
            ))
            continue

        provided = node.get("inputs") or {}
        node_def = object_info.get(class_type) or {}
        required = set(((node_def.get("input") or {}).get("required") or {}).keys())
        optional = set(((node_def.get("input") or {}).get("optional") or {}).keys())

        for missing in sorted(required - set(provided)):
            issues.append(Issue.error(
                f"{label}: 节点 {node_id}（{class_type}）缺少必填输入 {missing!r}",
                code="graph.missing_input", location=f"{node_id}.{missing}",
            ))

        for name, value in provided.items():
            found = Inventory.input_spec(object_info, class_type, name)
            dynamic_reason = None
            if found is None:
                found, dynamic_reason = resolve_dynamic_input(inventory, class_type, name)
            if found is None:
                if dynamic_reason:
                    issues.append(Issue.error(
                        f"{label}: 节点 {node_id}（{class_type}）{dynamic_reason}",
                        code="graph.bad_dynamic_slot", location=f"{node_id}.{name}",
                    ))
                    continue
                close = [c for c in (required | optional)
                         if name.lower() in c.lower() or c.lower() in name.lower()]
                issues.append(Issue.error(
                    f"{label}: 节点 {node_id}（{class_type}）存在未知输入 {name!r}",
                    code="graph.unknown_input", location=f"{node_id}.{name}",
                    hint=f"是否想写 {close[:3]}？" if close else "",
                ))
                continue

            type_token, options = found
            issues.extend(
                _validate_value(graph, inventory, label, node_id, class_type, name,
                                value, type_token, options)
            )

    issues.extend(_validate_topology(graph, label))
    return issues


def _validate_value(
    graph: dict,
    inventory: Inventory,
    label: str,
    node_id: str,
    class_type: str,
    name: str,
    value: Any,
    type_token: Any,
    options: dict,
) -> list[Issue]:
    """校验单个输入取值。"""
    issues: list[Issue] = []
    location = f"{node_id}.{name}"

    # 连线：检查来源节点存在、输出槽位序号合法
    if is_link(value):
        source_id, slot = str(value[0]), value[1]
        if source_id not in graph:
            issues.append(Issue.error(
                f"{label}: 节点 {location} 连到了不存在的节点 {source_id!r}",
                code="graph.dangling_link", location=location,
            ))
            return issues
        source_class = (graph[source_id] or {}).get("class_type")
        outputs = (inventory.object_info.get(source_class) or {}).get("output") or []
        if slot >= len(outputs):
            issues.append(Issue.error(
                f"{label}: 节点 {location} 读取来源节点 {source_id}（{source_class}）"
                f"的输出槽位 {slot}，但它只有 {len(outputs)} 个输出",
                code="graph.bad_slot", location=location,
            ))
        return issues

    # ⚠ 顺序很关键。这里必须先判"是不是枚举"再判"是不是 INT/FLOAT"，
    # 且所有 `in` / `==` 比较都必须先确认 type_token 是**字符串**。
    #
    # 原因：ComfyUI 的 /object_info 对"组合型输入"会把**合法取值列表本身**
    # 当作类型标记给出（例如 `["euler", "heun", ...]`）。这时 type_token 是数据
    # 而不是标记，把它丢进 `x in frozenset(...)` 会直接抛
    # `TypeError: unhashable type: 'list'` —— 一个足以让整条校验链路崩掉的
    # 假错误。这个坑在真实实例上是必现的（任何带 COMBO 输入的节点都会触发）。
    if isinstance(type_token, str) and type_token in OPAQUE_TYPES:
        return issues

    legal = _enum_values(type_token, options)
    if legal is not None:
        if value not in legal:
            head = legal[:10]
            issues.append(Issue.error(
                f"{label}: 节点 {location} = {value!r} 不是合法取值，可选 {head}"
                f"{' …' if len(legal) > 10 else ''}",
                code="graph.bad_enum", location=location,
                hint="这类输入的合法值来自后端实际安装的文件列表，检查文件名是否拼对、"
                     "或该文件是否真的被后端扫描到。",
            ))
        return issues

    if not isinstance(type_token, str):
        # 既不是已知的字符串标记，也不是可枚举列表 —— 可能是后端新增的类型形态。
        # 本地无从判断，**跳过而不是报错**：把不认识的东西报成错误，
        # 会让"后端升级"变成"桥必须跟着改才能用"。
        return issues

    if type_token in ("INT", "FLOAT"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(Issue.error(
                f"{label}: 节点 {location} 期望 {type_token}，收到 {type(value).__name__}",
                code="graph.bad_type", location=location,
            ))
            return issues
        low, high = options.get("min"), options.get("max")
        if low is not None and value < low:
            issues.append(Issue.error(
                f"{label}: 节点 {location} = {value} 小于下限 {low}",
                code="graph.out_of_range", location=location,
            ))
        if high is not None and value > high:
            issues.append(Issue.error(
                f"{label}: 节点 {location} = {value} 超过上限 {high}",
                code="graph.out_of_range", location=location,
            ))
        step = options.get("step")
        if step and low is not None and isinstance(value, (int, float)):
            if abs(round((value - low) / step) * step + low - value) > 1e-6:
                issues.append(Issue.warning(
                    f"{label}: 节点 {location} = {value} 不落在步长 {step} 的栅格上"
                    f"（起点 {low}），后端会自行吸附",
                    code="graph.off_grid", location=location,
                ))
        return issues

    if type_token == "BOOLEAN" and not isinstance(value, bool):
        issues.append(Issue.error(
            f"{label}: 节点 {location} 期望 BOOLEAN，收到 {type(value).__name__}",
            code="graph.bad_type", location=location,
        ))
    elif type_token == "STRING" and not isinstance(value, str):
        issues.append(Issue.error(
            f"{label}: 节点 {location} 期望 STRING，收到 {type(value).__name__}",
            code="graph.bad_type", location=location,
        ))
    return issues


def _validate_topology(graph: dict, label: str) -> list[Issue]:
    """检查图的出口与可达性。"""
    issues: list[Issue] = []

    referenced = {
        str(value[0])
        for node in graph.values()
        if isinstance(node, dict)
        for value in (node.get("inputs") or {}).values()
        if is_link(value)
    }
    sinks = [
        node_id for node_id, node in graph.items()
        if isinstance(node, dict) and node.get("class_type") in SAVE_NODE_CLASSES
    ]

    if not sinks:
        issues.append(Issue.warning(
            f"{label}: 图中没有任何保存类节点（SaveVideo/SaveImage/…），可能产不出文件",
            code="graph.no_sink", location=label,
        ))
    for node_id, node in graph.items():
        if node_id not in referenced and node_id not in sinks:
            issues.append(Issue.warning(
                f"{label}: 节点 {node_id}（{(node or {}).get('class_type')}）没有人引用，"
                "不会被求值",
                code="graph.unreachable", location=str(node_id),
            ))
    return issues


def validate_output_selector(
    graph: dict, output_node: str | None, label: str = "graph"
) -> list[Issue]:
    """校验"产出节点"这个选择器是否指向一个真正会产出文件的节点。

    外部框架（如 OpenMontage）用 ``output_node`` 去 ``/history`` 里捞成品。
    它指错节点不会导致提交失败，只会导致**跑完之后找不到产物** ——
    那时渲染已经白跑了几分钟，所以必须提前查。
    """
    if not output_node:
        return []
    selector = str(output_node)
    if selector not in graph:
        return [Issue.error(
            f"{label}: output_node={selector!r} 不在图中",
            code="output.missing_node", location=selector,
            hint=f"现有节点：{sorted(graph, key=str)}",
        )]
    class_type = (graph[selector] or {}).get("class_type")
    if class_type not in SAVE_NODE_CLASSES:
        return [Issue.error(
            f"{label}: output_node={selector!r} 指向 {class_type!r}，它不是保存类节点",
            code="output.not_a_sink", location=selector,
            hint=f"保存类节点：{sorted(SAVE_NODE_CLASSES)}",
        )]
    return []
