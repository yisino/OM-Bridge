"""MCP 交付面 —— ``om-bridge-mcp``。

一套核心，两个协议
------------------
这个模块是 **JSON-RPC 2.0 over stdio** 的极简服务端（MCP 的 stdio 传输就是
"一行一条 JSON-RPC 消息"）。它自己实现协议循环而**不引入 mcp SDK**，
理由和核心层一样：目标机是没有 pip 镜像的生产 GPU 主机，
"装不上依赖"不该变成"用不了"。协议这一层的复杂度远低于它的依赖成本。

工具清单同样是**自动生成**的
----------------------------
``om_bridge_generate_video`` 的 ``inputSchema`` 由注册表里所有 ``kind == "video"``
的方案的 :class:`~om_bridge.core.schema.ParamSchema` 与 ``media_slots`` 现场拼出来。
于是新增一个视频方案，agent 那边**立刻**能看到它的参数 —— 不需要改本文件，
更不需要在别处再维护一份 schema。

stdout 是协议的，不能碰
-----------------------
JSON-RPC 消息走 stdout，日志与进度一律走 stderr（见
:mod:`om_bridge.core.logging`）。一次误写就会让宿主报"invalid JSON"，
而那条报错**不会**指向真正的原因，排查代价极高。

错误分两类，这是有意的
----------------------
* **协议级错误**（方法不存在、参数结构不对）→ JSON-RPC ``error`` 对象；
* **业务级错误**（后端不可达、校验不过、渲染失败）→ 正常 result，
  但 ``isError: true``，内容里带结构化的错误详情。

第二类刻意不用协议错误：MCP 宿主把协议错误当作"服务坏了"，
而"校验没通过"是**有效答案**——agent 需要读到它、据此改参数再试。
把它降级成协议错误等于剥夺了 agent 的自我修正能力。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .. import __version__
from ..core import logging as log
from ..core.config import load_config
from ..core.errors import EXIT_OK, OmBridgeError
from ..core.models import GenerationRequest, MediaAsset, MediaKind, blocking_issues
from ..core.registry import Registry, get_registry
from ..core.session import Session

SERVER_NAME = "om-bridge"
DEFAULT_PROTOCOL = "2024-11-05"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ===========================================================================
# 工具 schema 生成
# ===========================================================================
def _solutions_of_kind(registry: Registry, kind: str) -> list[type]:
    return [cls for cls in registry.solutions().values() if cls.kind == kind]


def _solution_keys(registry: Registry, kind: str) -> list[str]:
    """某一大类下的全部方案键 + 别名（别名让人/agent 可以直接指定模式）。"""
    keys = {cls.key for cls in _solutions_of_kind(registry, kind)}
    aliases = {
        alias for alias, (key, _preset) in registry._aliases.items() if key in keys
    }  # noqa: SLF001 —— 注册表与交付面同属一个包，直接读别名表比再加一层 API 更实在
    return sorted(keys | aliases)


def _merged_params(registry: Registry, kind: str) -> dict[str, Any]:
    """合并某一大类下所有方案的参数声明（同名取第一个，与新声明顺序无关）。"""
    merged: dict[str, Any] = {}
    for cls in _solutions_of_kind(registry, kind):
        for spec in cls.schema:
            merged.setdefault(spec.name, spec)
    return merged


def _merged_slots(registry: Registry, kind: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for cls in _solutions_of_kind(registry, kind):
        for slot in cls.media_slots:
            merged.setdefault(slot.role, slot)
    return merged


def _generation_tool(registry: Registry, *, kind: str, name: str, title: str) -> dict:
    """按类别生成一个"生成"工具（视频 / 图像 / 音频共用同一套构造）。"""
    params = _merged_params(registry, kind)
    slots = _merged_slots(registry, kind)

    properties: dict[str, Any] = {
        "solution": {
            "type": "string",
            "enum": _solution_keys(registry, kind),
            "description": "方案键或别名。带模式后缀的别名（如 minimax_h3.ref2v）"
                           "会自动带上该模式，等价于同时传 mode。",
        },
        "prompt": {
            "type": "string",
            "description": "提示词。ref2v 模式下用 <Picture 1>、<Picture 2> 指代参考图，"
                           "顺序与 reference_image 数组一致。",
        },
        "output_dir": {
            "type": "string",
            "description": "产物落盘目录。省略则用配置里的默认产物目录。",
        },
        "timeout_s": {
            "type": "number",
            "description": "等待超时（秒）。超时**不等于失败**：作业通常仍在跑，"
                           "应改用 om_bridge_resume 续等，不要重复提交。",
        },
        "node_overrides": {
            "type": "object",
            "description": "逃生舱：直接改写载荷里某个节点的输入，键为 <节点ID>.<输入名>。"
                           "绕过类型检查，仅在方案尚未支持某参数时使用。",
            "additionalProperties": True,
        },
        "media_remote": {
            "type": "object",
            "description": "引用后端素材目录里**已经存在**的文件（跳过上传）。"
                           "键为媒体角色名，值为远端文件名数组。"
                           "在素材已预先同步好的批处理场景下，这比传路径更快。",
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
        },
    }
    for name_, spec in sorted(params.items()):
        properties[name_] = spec.to_json_schema()
    for role, slot in sorted(slots.items()):
        properties[role] = {
            "type": "array",
            "items": {"type": "string"},
            "description": f"{slot.description or role}"
                           f"（本地文件路径，会自动上传；"
                           f"{'必填' if slot.required else '可选'}"
                           f"{f'，最多 {slot.max_count} 个' if slot.multiple else ''}）",
        }

    return {
        "name": name,
        "title": title,
        "description": f"生成{title}。参数与媒体槽位来自方案声明，"
                       "先用 om_bridge_check 确认后端就绪可少走一次无效排队。",
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": ["solution", "prompt"],
        },
    }


def build_tools(registry: Registry) -> list[dict]:
    """构造全部工具声明。新增后端/方案会自动反映到这里。"""
    tools: list[dict] = [
        {
            "name": "om_bridge_list",
            "title": "列出可用后端与方案",
            "description": "列出本桥支持的全部后端、方案、模式与别名。"
                           "在不确定有哪些方案可用、或需要方案键时先调用它。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "om_bridge_describe",
            "title": "查看方案详情",
            "description": "给出某个方案的完整自描述：参数（含类型、默认值、取值范围）、"
                           "媒体槽位、所需模型资产、以及各模式的默认步数与权重角色。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "solution": {
                        "type": "string",
                        "description": "方案键或别名。省略则输出全部方案。",
                    },
                },
            },
        },
        {
            "name": "om_bridge_check",
            "title": "体检后端与就绪度",
            "description": "探测后端连通性、盘点可用节点与模型资产，并给出**按模式**的就绪度"
                           "与缺失清单。生成之前调用它可以避免「排了队才发现缺模型」。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "refresh": {
                        "type": "boolean",
                        "description": "是否绕过缓存强制重新探测（默认 true）。",
                        "default": True,
                    },
                },
            },
        },
        {
            "name": "om_bridge_validate",
            "title": "校验一次请求（不提交）",
            "description": "跑完全部本地校验、资产就绪度与载荷静态校验，返回全部问题。"
                           "不会提交任何作业，因此是零成本的 —— 适合在正式生成前先自检。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "solution": {"type": "string"},
                    "prompt": {"type": "string"},
                    "params": {
                        "type": "object",
                        "description": "方案参数（与生成工具同名的一批键，这里用对象聚拢）。",
                        "additionalProperties": True,
                    },
                    "media": {
                        "type": "object",
                        "description": "媒体输入：键为角色名，值为本地文件路径数组。",
                        "additionalProperties": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "required": ["solution"],
            },
        },
        {
            "name": "om_bridge_run_workflow",
            "title": "执行一份现成的工作流",
            "description": "把一份 API 格式的工作流 JSON 直接跑起来，不经过任何方案。"
                           "用于复用别人给的图、或验证方案尚未支持的新节点参数。"
                           "⚠ 没有方案层的参数校验，只有后端静态校验，因此别关掉 validate。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": "API 格式的工作流（节点ID -> {class_type, inputs}）。",
                        "additionalProperties": True,
                    },
                    "payload_path": {
                        "type": "string",
                        "description": "工作流 JSON 文件路径。与 payload 二选一。",
                    },
                    "output_node": {
                        "type": "string",
                        "description": "产出成品所在节点 ID（决定收集哪个文件）。",
                    },
                    "validate": {
                        "type": "boolean",
                        "description": "提交前是否做后端静态校验（默认 true，强烈建议保持）。",
                        "default": True,
                    },
                    "timeout_s": {"type": "number"},
                },
                "required": [],
            },
        },
        {
            "name": "om_bridge_resume",
            "title": "续等一个超时作业",
            "description": "对一个**已提交**的作业继续等待并收集产物。"
                           "⚠ 等待超时后必须用这个，不要重新提交 —— "
                           "重提会排出重复作业并与原作业争抢同一块 GPU。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "作业 ID（如 ComfyUI 的 prompt_id）。"},
                    "timeout_s": {"type": "number", "description": "再等多久（秒）。"},
                    "output_dir": {"type": "string"},
                },
                "required": ["job_id"],
            },
        },
    ]

    # 每个"生成大类"都生成一个工具。当前只有 video（H3），
    # 将来接入图像/音频方案时，这里会自动多出对应工具。
    for kind, tool_name, title in (
        ("video", "om_bridge_generate_video", "视频"),
        ("image", "om_bridge_generate_image", "图像"),
        ("audio", "om_bridge_generate_audio", "音频"),
    ):
        if _solutions_of_kind(registry, kind):
            tools.append(_generation_tool(registry, kind=kind, name=tool_name, title=title))
    return tools


# ===========================================================================
# 工具实现
# ===========================================================================
def _text(payload: Any) -> dict:
    """把返回值包成 MCP 的 content 结构（JSON 文本，agent 好读也好解析）。"""
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": body}], "isError": not _is_ok(payload)}


def _is_ok(payload: Any) -> bool:
    """判定一次工具调用是否成功。

    ``ok`` 字段优先 —— 它是 :class:`~om_bridge.core.models.GenerationResult`
    这类"业务结果"的显式判定，比任何启发式都可靠。
    没有 ``ok`` 时才去看 ``error`` / ``errors``（探测报告走这条路：
    它没有 ``ok``，但不可达时会带上 ``errors``）。
    """
    if isinstance(payload, dict):
        if "ok" in payload:
            return bool(payload["ok"])
        if payload.get("error") or payload.get("errors"):
            return False
    return True


def _to_media(per_role: dict[str, Any], kind: str) -> list[MediaAsset]:
    """把「角色 -> 路径数组」的映射还原成 MediaAsset 列表。

    **顺序有意义**：ref2v 的参考图顺序决定提示词里 ``<Picture 1>`` 指哪张，
    因此这里按角色名稳定排序、角色内保持给定顺序，绝不去重排。

    传入的名字若已经是后端上存在的文件，用 ``media_remote`` 那条通道
    （见 :func:`_split_generation_arguments`）—— 本函数只处理本地路径。
    """
    registry = get_registry()
    roles = {role: slot.kind for role, slot in _merged_slots(registry, kind).items()}
    media: list[MediaAsset] = []
    for role in sorted(per_role or {}):
        for path in per_role[role] or []:
            if path:
                media.append(MediaAsset(role=role, kind=roles.get(role, MediaKind.IMAGE), path=path))
    return media


def _remote_media(per_role: dict[str, Any], kind: str) -> list[MediaAsset]:
    """``media_remote`` 通道：引用后端素材目录里**已经存在**的文件，跳过上传。"""
    registry = get_registry()
    roles = {role: slot.kind for role, slot in _merged_slots(registry, kind).items()}
    media: list[MediaAsset] = []
    for role in sorted(per_role or {}):
        for name in per_role[role] or []:
            if name:
                media.append(MediaAsset(role=role, kind=roles.get(role, MediaKind.IMAGE), name=name))
    return media


def _split_generation_arguments(
    arguments: dict[str, Any], kind: str
) -> tuple[dict[str, Any], list[MediaAsset]]:
    """把工具参数拆成"方案参数"与"媒体"两部分。

    这个拆分必须由**注册表**决定，而不是写死一串键名 —— 否则新增一个媒体角色
    （比如参考视频）时，它会悄悄被当成方案参数透传，表现为"传了但没生效"，
    而这类问题极难从报错里看出来。

    声明之外的键**刻意保留**在 params 里：那可能是方案自己认识的扩展项，
    也可能是调用方在试新东西。ParamSchema 会为它们报一条"未登记"的告警，
    比在这里静默丢弃好得多。
    """
    registry = get_registry()
    roles = set(_merged_slots(registry, kind))
    reserved = {
        "solution", "prompt", "output_dir", "timeout_s", "node_overrides", "media_remote",
    } | roles

    params = {k: v for k, v in arguments.items() if k not in reserved and v is not None}
    media = _to_media({role: arguments.get(role) for role in roles}, kind)
    media.extend(_remote_media(arguments.get("media_remote") or {}, kind))
    return params, media


def call_tool(name: str, arguments: dict[str, Any]) -> dict:
    """执行一个工具。返回 MCP 的 ``result`` 结构。"""
    registry = get_registry()
    arguments = arguments or {}

    if name == "om_bridge_list":
        describe = registry.describe()
        return _text({
            "backends": describe["backends"],
            "solutions": describe["solutions"],
            "aliases": describe["aliases"],
            "load_errors": describe["load_errors"],
        })

    if name == "om_bridge_describe":
        target = arguments.get("solution")
        if target:
            solution, _preset = registry.resolve(target)
            return _text(solution.describe())
        return _text({
            "solutions": [registry.instance(key).describe() for key in sorted(registry.solutions())]
        })

    if name == "om_bridge_check":
        # 日志只走 stderr；MCP 宿主通常会把它收集进日志面板
        log.configure_logging()
        with Session(load_config(), backend_name=arguments.get("backend")) as session:
            return _text(session.probe_report())

    if name == "om_bridge_validate":
        solution = arguments.get("solution")
        kind = _kind_of(registry, solution)
        media = _to_media(arguments.get("media") or {}, kind)
        request = GenerationRequest(
            solution=solution or "",
            prompt=arguments.get("prompt") or "",
            params=dict(arguments.get("params") or {}),
            media=media,
            validate=True,
        )
        with Session(load_config()) as session:
            issues = session.validate(request)
        blocking = blocking_issues(issues)
        return _text({
            "ok": not blocking,
            "blocking": len(blocking),
            "total": len(issues),
            "issues": [issue.to_dict() for issue in issues],
        })

    if name == "om_bridge_run_workflow":
        payload = arguments.get("payload")
        if payload is None and arguments.get("payload_path"):
            path = Path(str(arguments["payload_path"])).expanduser()
            if not path.is_file():
                return _text({"ok": False, "error": f"工作流文件不存在：{path}"})
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                return _text({"ok": False, "error": f"工作流不是合法 JSON：{exc}"})
        if not isinstance(payload, dict):
            return _text({"ok": False, "error": "缺少 payload（对象）或 payload_path（文件路径）"})

        with Session(load_config()) as session:
            result = session.run_raw(
                payload,
                output_selector=arguments.get("output_node"),
                timeout=arguments.get("timeout_s"),
                validate=bool(arguments.get("validate", True)),
                label="mcp:workflow",
            )
        return _text(result.to_dict())

    if name == "om_bridge_resume":
        job_id = arguments.get("job_id")
        if not job_id:
            return _text({"ok": False, "error": "缺少 job_id"})
        with Session(load_config()) as session:
            result = session.resume(
                str(job_id),
                timeout=arguments.get("timeout_s"),
                output_dir=arguments.get("output_dir"),
            )
        return _text(result.to_dict())

    if name.startswith("om_bridge_generate_"):
        kind = name.rsplit("_", 1)[-1]
        if kind not in ("video", "image", "audio"):
            return _text({"ok": False, "error": f"未知工具 {name}"})
        if not arguments.get("solution"):
            return _text({"ok": False, "error": "缺少 solution；先用 om_bridge_list 查看可用方案"})

        params, media = _split_generation_arguments(arguments, kind)
        request = GenerationRequest(
            solution=str(arguments["solution"]),
            prompt=arguments.get("prompt") or "",
            params=params,
            media=media,
            output_dir=arguments.get("output_dir"),
            timeout_s=arguments.get("timeout_s"),
            node_overrides=dict(arguments.get("node_overrides") or {}),
        )
        with Session(load_config()) as session:
            result = session.run(request)
        return _text(result.to_dict())

    return _text({"ok": False, "error": f"未知工具 {name}"})


def _kind_of(registry: Registry, solution: str | None) -> str:
    """推断某个方案属于哪个大类（用于选对媒体角色的类型）。"""
    if solution:
        try:
            cls = registry.solution(solution)
            return cls.kind
        except OmBridgeError:
            try:
                _solution, _preset = registry.resolve(solution)
                return _solution.kind
            except OmBridgeError:
                pass
    return "video"


# ===========================================================================
# JSON-RPC 循环
# ===========================================================================
class McpServer:
    """stdio 上的 JSON-RPC 服务端。

    刻意只实现 MCP 里真正用到的四个方法（initialize / tools/list / tools/call / ping），
    并把其余一概回 ``METHOD_NOT_FOUND``。原因：一个"什么都答一点"的实现
    比"明确不支持"更难排查 —— 宿主会以为某项能力可用，然后再也别想不通为什么没效果。
    """

    def __init__(self, registry: Registry | None = None) -> None:
        self.registry = registry or get_registry()
        self.tools = build_tools(self.registry)
        self.initialized = False
        self.client_protocol = DEFAULT_PROTOCOL

    # -- 分发 -------------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条消息。返回 ``None`` 表示这是通知，不该有响应。"""
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        if msg_id is None:
            # 通知：按协议一律不响应。但仍要处理 initialized，
            # 否则后续请求会被当成"没握手"。
            if method == "notifications/initialized":
                self.initialized = True
            return None

        try:
            if method == "initialize":
                return self._ok(msg_id, self._initialize(params))
            if method == "ping":
                return self._ok(msg_id, {})
            if method == "tools/list":
                return self._ok(msg_id, {"tools": self.tools})
            if method == "tools/call":
                # 未知工具名是**结构性**错误（调用方拼错了名字），按规范回 -32602。
                # 这与"业务失败"必须分开：后者要回到 agent 手里让它自我修正，
                # 前者应该让调用方立刻发现是自己写错了。
                requested = params.get("name")
                known = {tool["name"] for tool in self.tools}
                if not requested or requested not in known:
                    return self._err(
                        msg_id, INVALID_PARAMS,
                        f"未知工具 {requested!r}", {"available": sorted(known)},
                    )
                return self._ok(msg_id, self._call(params))
            if method == "resources/list":
                # 明确回答"没有资源"，比返回错误更有用：
                # 宿主据此不再重试，也不会把它记成服务异常。
                return self._ok(msg_id, {"resources": []})
            if method == "prompts/list":
                return self._ok(msg_id, {"prompts": []})
            return self._err(msg_id, METHOD_NOT_FOUND, f"不支持的方法：{method}")
        except OmBridgeError as exc:
            # 业务异常也返回成功响应 + isError，让 agent 能读到并自我修正
            payload = exc.as_dict()
            issues = getattr(exc, "issues", None)
            if issues:
                payload["issues"] = [
                    issue.to_dict() if hasattr(issue, "to_dict") else issue for issue in issues
                ]
            return self._ok(msg_id, {
                "content": [{"type": "text",
                             "text": json.dumps(payload, ensure_ascii=False, indent=2, default=str)}],
                "isError": True,
            })
        except Exception as exc:  # noqa: BLE001
            # 用 exception() 而不是 debug()：未预期异常必须带栈，
            # 否则排查时只剩一句没有信息量的类型名。
            log.get_logger("mcp").exception("处理 %s 时抛出未预期异常", method)
            return self._err(msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOLS:
            self.client_protocol = requested
        return {
            # 回一个自己支持、且对方大概率也接受的版本
            "protocolVersion": self.client_protocol if requested in SUPPORTED_PROTOCOLS
            else DEFAULT_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": (
                "OM-Bridge 把「算力来源」与「模型语义」解耦，统一暴露生成能力。"
                "建议流程：先 om_bridge_check 确认后端就绪，再 om_bridge_generate_video 生成；"
                "等待超时时用 om_bridge_resume 续等，**不要**重复提交。"
            ),
        }

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            return {
                "content": [{"type": "text", "text": "缺少工具名"}],
                "isError": True,
            }
        known = {tool["name"] for tool in self.tools}
        if name not in known:
            return {
                "content": [{
                    "type": "text",
                    "text": f"未知工具 {name}。可用：" + ", ".join(sorted(known)),
                }],
                "isError": True,
            }
        return call_tool(name, arguments)

    @staticmethod
    def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _err(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _write(message: dict[str, Any]) -> None:
    """输出一条 JSON-RPC 消息。

    ⚠ 必须用 ``separators=(",", ":")`` 且不带换行 —— stdio 传输以**换行**分帧，
    消息体里出现换行会把一条消息劈成两条，宿主那里表现为"JSON 解析失败"。
    ``ensure_ascii=True``（默认）在这里反而是对的：转义后的内容不含裸换行，
    而 stdout 编码未定时也不会因中文而抛 UnicodeEncodeError。
    """
    sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    """MCP stdio 入口。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(f"{SERVER_NAME}-mcp {__version__}")
        return EXIT_OK
    if "--list-tools" in argv:
        # 便于人工/脚本检查工具清单，不必先跑一遍握手
        for tool in build_tools(get_registry()):
            print(f"{tool['name']}: {tool.get('title', '')}")
        return EXIT_OK

    log.configure_logging()
    # 进度行会污染宿主日志面板，且对 agent 无用 —— 关掉，只保留诊断日志
    log.set_progress_enabled(False)

    server = McpServer()
    log.diagnostic("mcp", "启动 %s %s，共 %d 个工具", SERVER_NAME, __version__, len(server.tools))

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": PARSE_ERROR, "message": f"JSON 解析失败：{exc}"}})
            continue
        if not isinstance(message, dict):
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": INVALID_REQUEST, "message": "消息必须是 JSON 对象"}})
            continue

        response = server.handle(message)
        if response is not None:
            _write(response)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
