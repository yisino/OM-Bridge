"""命令行交付面 —— ``om-bridge``。

设计原则
--------
**命令行选项不是手写的，是从注册表现场生成的。**

原实现里 ``argparse`` 有一份完整的 ``--steps`` / ``--lora`` / ``--ref-image``
声明，MCP 的 ``inputSchema`` 里又有一份几乎一样的，构建函数里还有第三份
``if steps not in (4, 8): raise``。三份声明必然漂移 —— 这是维护成本的真实来源。

这里的做法：``--width``、``--steps``、``--ref-image-size`` … 全部由
遍历 :class:`~om_bridge.core.registry.Registry` 里所有方案的
:class:`~om_bridge.core.schema.ParamSchema` 与 ``media_slots`` 产生。
于是**新增一个方案，CLI 自动获得它的全部选项**，一行 argparse 都不用写。

副作用（有意的）
----------------
所有动态选项都按字符串接收，然后交给 :meth:`ParamSchema.resolve` 做类型还原与校验。
这样类型错误会走统一的 :class:`~om_bridge.core.models.Issue` 报错路径
（带参数名、范围、合法取值），而不是 argparse 那句干巴巴的
``invalid int value``。代价是 ``--help`` 里看不到 Python 类型，
但帮助文本里写了约束（``864 的倍数``、``栅格 17k+5``），实际更好用。

命令一览
--------
========================  ==================================================
``generate``              执行一次生成（最常用）
``validate``              只校验不提交
``graph``                 物化计算图 JSON（给需要"文件 + 节点 ID"的外部框架）
``run``                   执行一份外部给的作业载荷
``resume``                续等一个超时作业
``probe``                 探测后端连通性与能力盘点
``list``                  列出所有后端、方案与别名
``describe``              某个方案/后端的完整自描述
``config``                配置自省（list / explain / report）
``doctor``                全面体检，给出结论与可执行建议
========================  ==================================================

退出码
------
沿用 :mod:`om_bridge.core.errors` 里定义的契约（0 成功、1 用法/配置、
2 不可达、3 作业非法、4 被拒、5 超时、6 渲染失败、7 配置冲突）。
脚本与文档依赖这组数字，**改动即破坏兼容性**。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .. import __version__
from ..core import logging as log
from ..core.config import ALL_SETTINGS, Config, default_env_candidates, load_config
from ..core.errors import EXIT_BAD_WORKFLOW, EXIT_OK, EXIT_RENDER, EXIT_USAGE, OmBridgeError
from ..core.models import (
    GenerationRequest,
    MediaAsset,
    MediaKind,
    Severity,
    blocking_issues,
)
from ..core.registry import Registry, get_registry
from ..core.session import Session

PROG = "om-bridge"

# 这些名字已经被"通用选项"占用，因此不再为它们生成动态参数选项。
# 若某个方案恰好声明了同名参数，可以用 `--set 名字=值` 传（见 _parse_kv）。
RESERVED_FLAGS: frozenset[str] = frozenset({
    "prompt", "output_dir", "out", "timeout", "validate", "preflight",
    "label", "kind", "format", "payload", "graph", "job_id", "output_node",
    "media", "media_remote", "set", "node_override", "json", "help",
    "env_file", "workspace", "backend", "solution", "verbose", "quiet",
    "skip_probe", "solution_or_backend", "force", "dry_run",
})

# 媒体槽位的常见别名。存在的唯一理由是"手快"：
# 日常输入 --ref-image 比 --reference-image 舒服，而两者指同一个槽位。
MEDIA_ALIASES: dict[str, tuple[str, ...]] = {
    "reference_image": ("--ref-image",),
    "first_frame": ("--first-image", "--image"),
    "last_frame": ("--last-image",),
}


# ===========================================================================
# 小工具
# ===========================================================================
def _parse_kv(raw: str, *, what: str) -> tuple[str, str]:
    """解析 ``键=值``。缺失 ``=`` 时给出带上下文的错误，而不是静默忽略。"""
    if "=" not in raw:
        raise SystemExit(f"错误：{what} 期望 <键>=<值> 形式，收到 {raw!r}")
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise SystemExit(f"错误：{what} 的键不能为空（{raw!r}）")
    return key, value.strip()


def _json_or_str(raw: str) -> Any:
    """优先按 JSON 解析，失败则当字符串。

    用途是 ``--node-override 15.steps=8`` 这类覆盖：用户写 ``8`` 想要数字，
    写 ``"a b"`` 想要字符串，写 ``[1,2]`` 想要列表。全按字符串传会让
    后端收到 ``"8"`` 而类型不符，全按 JSON 又会逼用户给字符串加引号。
    """
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _emit_json(payload: Any) -> None:
    """把结果写到 stdout。

    ``ensure_ascii=False`` 是必要的：提示词与错误信息里大量中文，
    转义成 ``\\uXXXX`` 之后人几乎没法读日志。

    ``default=str`` 兜底任何非 JSON 类型，避免"结果里有个 Path 就整个输出失败"
    这种极不划算的失败模式。
    """
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _write_text(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()


def _describe_issues(issues: Iterable[Any], *, show_info: bool = False) -> str:
    """把问题列表渲染成人读文本。"""
    icons = {Severity.ERROR: "✖", Severity.WARNING: "!", Severity.INFO: "·"}
    lines: list[str] = []
    for issue in issues:
        if issue.severity is Severity.INFO and not show_info:
            continue
        location = f" [{issue.location}]" if issue.location else ""
        lines.append(f"  {icons.get(issue.severity, '?')} {issue.message}{location}")
        if issue.hint:
            lines.append(f"      ↳ {issue.hint}")
    return "\n".join(lines)


def _report_error(exc: OmBridgeError, *, as_json: bool) -> int:
    """统一的异常出口：JSON 模式下输出结构化错误，否则输出人读文本。"""
    if as_json:
        _emit_json(exc.as_dict())
    else:
        print(f"错误（{exc.stage}）：{exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"建议：{exc.hint}", file=sys.stderr)
        issues = getattr(exc, "issues", None)
        if issues:
            print(_describe_issues(issues), file=sys.stderr)
    return exc.exit_code


# ===========================================================================
# 参数与媒体的动态生成
# ===========================================================================
def _all_param_specs(registry: Registry) -> dict[str, Any]:
    """汇总所有方案声明过的参数（同名只保留第一个）。

    同名参数的约束在不同方案间可能不同（A 方案 width 上限 1024、B 方案 2048），
    因此这里**只借用一个**来生成 argparse 选项 —— 真正的校验发生在
    :meth:`ParamSchema.validate`，用的是**该方案自己的**声明。
    argparse 这一层只负责"接收字符串、并在帮助里提示存在这个选项"。
    """
    specs: dict[str, Any] = {}
    for cls in registry.solutions().values():
        for spec in cls.schema:
            if spec.name in RESERVED_FLAGS:
                continue
            specs.setdefault(spec.name, spec)
    return specs


def _all_media_roles(registry: Registry) -> dict[str, MediaKind]:
    """汇总所有方案声明过的媒体角色。"""
    roles: dict[str, MediaKind] = {}
    for cls in registry.solutions().values():
        for slot in cls.media_slots:
            roles.setdefault(slot.role, slot.kind)
    return roles


_PARAM_NAMES_CACHE: frozenset[str] | None = None


def _param_names() -> frozenset[str]:
    """全部已声明的方案参数名（惰性计算并缓存）。

    惰性而不是模块级常量：模块级会在 **import 时**触发注册表发现，
    而 import 本模块的调用方（测试、集成脚本）未必想付这个代价，
    也不该因为某个插件坏了而连 import 都失败。
    """
    global _PARAM_NAMES_CACHE
    if _PARAM_NAMES_CACHE is None:
        _PARAM_NAMES_CACHE = frozenset(_all_param_specs(get_registry()))
    return _PARAM_NAMES_CACHE


def _add_param_options(parser: argparse.ArgumentParser, registry: Registry) -> dict[str, str]:
    """为每个已知参数加一个动态选项。返回 ``dest -> 参数名`` 映射。"""
    mapping: dict[str, str] = {}
    group = parser.add_argument_group(
        "方案参数（由注册表中的方案声明自动生成）",
        "全部按字符串接收后统一做类型还原与范围校验；"
        "未在此列出的自定义参数可用 --set 名字=值 传入。",
    )
    for name, spec in sorted(_all_param_specs(registry).items()):
        flag = f"--{name.replace('_', '-')}"
        help_text = spec.to_cli_help()
        if spec.type in ("list", "path_list"):
            group.add_argument(flag, dest=name, action="append", default=None,
                               metavar="值", help=f"{help_text}（可重复）")
        elif spec.type == "boolean":
            # 用 nargs="?" + const 而不是 store_true：后者会把"没传"和
            # "显式传 false"混为一谈，导致配置文件里的默认值被无声覆盖。
            group.add_argument(flag, dest=name, nargs="?", const="true", default=None,
                               metavar="true|false",
                               help=f"{help_text}（不带值时视为 true）")
        else:
            group.add_argument(flag, dest=name, default=None, metavar="值", help=help_text)
        mapping[name] = name
    return mapping


def _add_media_options(parser: argparse.ArgumentParser, registry: Registry) -> None:
    """为每个媒体角色加一个可重复选项，另加 ``--media`` / ``--media-remote`` 通用形式。"""
    group = parser.add_argument_group(
        "媒体输入",
        "--<角色名> 传本地文件（会自动上传）；"
        "--media-remote <角色>=<远端名> 用于文件已在后端素材目录里的情形。",
    )
    for role, kind in sorted(_all_media_roles(registry).items()):
        flag = f"--{role.replace('_', '-')}"
        flags = [flag, *MEDIA_ALIASES.get(role, ())]
        group.add_argument(*flags, dest=role, action="append", default=None,
                           metavar="文件", help=f"{role}（{kind.value}），可重复")

    group.add_argument("--media", action="append", default=None, metavar="角色=路径",
                       help="通用形式，用于未被任何方案声明过的媒体角色")
    group.add_argument("--media-remote", dest="media_remote", action="append", default=None,
                       metavar="角色=远端名",
                       help="引用后端素材目录里已有的文件，跳过上传")


def _collect_params(args: argparse.Namespace) -> dict[str, Any]:
    """把 argparse 收集到的动态选项还原成方案的参数字典。

    **白名单而非黑名单。** 只取注册表里真实声明过的参数名，不使用
    "遍历 argparse 命名空间、排除掉已知的非参数键"这种写法。
    理由是后者已经真实咬过一次：``set_defaults(func=handler)`` 会把处理函数
    放进命名空间，于是它被当成参数一路带到 ``json.dumps`` 才炸
    （``Object of type function is not JSON serializable``）——
    而错误出现在离根因很远的地方，排查成本远高于这里多写一行白名单。

    只收集**显式给出**的选项（默认都是 None）：默认值该由配置层与方案声明决定，
    CLI 不该抢这个职责 —— 否则"这台机器该用什么权重/分辨率"会从配置文件漂回命令行。
    """
    params: dict[str, Any] = {}
    for name in _param_names():
        value = getattr(args, name, None)
        if value is not None:
            params[name] = value

    # 声明之外的扩展参数：走 --set，原样透传（方案自己不认识的键会由
    # ParamSchema.validate 报一条"未登记"的告警，而不是静默丢弃）
    for raw in getattr(args, "set", None) or []:
        key, value = _parse_kv(raw, what="--set")
        params[key] = _json_or_str(value)
    return params


def _collect_media(args: argparse.Namespace, registry: Registry) -> list[MediaAsset]:
    """把各媒体选项还原成 :class:`MediaAsset` 列表。

    **顺序即语义**：ref2v 的参考图顺序决定提示词里 ``<Picture 1>``、``<Picture 2>``
    指哪张图。argparse 的 ``action="append"`` 保留了命令行顺序，
    因此这里只需按字典顺序稳定地拼接不同角色的列表，不打乱同一角色内部次序。
    """
    roles = _all_media_roles(registry)
    assets: list[MediaAsset] = []

    for role in sorted(roles):
        for raw in getattr(args, role, None) or []:
            assets.append(MediaAsset(role=role, kind=roles[role], path=raw))

    for raw in getattr(args, "media", None) or []:
        role, path = _parse_kv(raw, what="--media")
        kind = roles.get(role, MediaKind.IMAGE)
        assets.append(MediaAsset(role=role, kind=kind, path=path))

    for raw in getattr(args, "media_remote", None) or []:
        role, name = _parse_kv(raw, what="--media-remote")
        kind = roles.get(role, MediaKind.IMAGE)
        assets.append(MediaAsset(role=role, kind=kind, name=name))

    return assets


def _collect_node_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for raw in getattr(args, "node_override", None) or []:
        key, value = _parse_kv(raw, what="--node-override")
        overrides[key] = _json_or_str(value)
    return overrides


# ===========================================================================
# 会话构造
# ===========================================================================
def _build_session(args: argparse.Namespace) -> Session:
    """按命令行参数装配一个 Session（含配置分层与后端选择）。"""
    workspace = getattr(args, "workspace", None)
    overrides: dict[str, Any] = {}
    if workspace:
        # 命令行给的 workspace 优先级最高，且必须显式传下去：
        # 配置文件探测也依赖它（会去找 <workspace>/.env）
        overrides["global.workspace"] = workspace

    config = load_config(
        env_file=getattr(args, "env_file", None),
        overrides=overrides or None,
        workspace=workspace,
    )
    return Session(
        config,
        backend_name=getattr(args, "backend", None),
        solution_name=getattr(args, "solution", None),
    )


def _build_request(args: argparse.Namespace, *, validate_default: bool = True) -> GenerationRequest:
    registry = get_registry()
    return GenerationRequest(
        solution=getattr(args, "solution", None) or "",
        prompt=getattr(args, "prompt", None) or "",
        params=_collect_params(args),
        media=_collect_media(args, registry),
        output_dir=getattr(args, "output_dir", None),
        timeout_s=getattr(args, "timeout", None),
        validate=(validate_default and not getattr(args, "no_validate", False)),
        preflight=not getattr(args, "no_preflight", False),
        node_overrides=_collect_node_overrides(args),
    )


def _resolve_solution_key(args: argparse.Namespace, session: Session) -> str:
    """确定本次实际使用的方案键（含"只有一个方案时自动选中"的兜底）。"""
    explicit = getattr(args, "solution", None)
    if explicit:
        return explicit
    try:
        solution, _preset = session.solution_of(GenerationRequest(solution=""))
        return solution.key
    except OmBridgeError:
        return ""


# ===========================================================================
# 各命令实现
# ===========================================================================
def _cmd_generate(args: argparse.Namespace) -> int:
    """执行一次生成。``--dry-run`` 时只解析不提交。"""
    session = _build_session(args)
    request = _build_request(args)

    with session:
        if getattr(args, "dry_run", False):
            prepared = session.prepare(request)
            payload = {"dry_run": True, **prepared.to_dict()}
            if getattr(args, "json", False):
                _emit_json(payload)
            else:
                print(f"方案      {prepared.solution.key}（模式 {prepared.mode}）", file=sys.stderr)
                print(f"后端      {prepared.backend.name} @ {prepared.backend.endpoint()}",
                      file=sys.stderr)
                print(f"参数      {json.dumps(prepared.params, ensure_ascii=False)}", file=sys.stderr)
                for asset in prepared.media:
                    target = asset.name or asset.path
                    print(f"媒体      {asset.role} = {target}", file=sys.stderr)
                print(f"载荷      {payload['job']['payload_nodes']} 个节点，"
                      f"输出节点 {prepared.job.output_selector}", file=sys.stderr)
                print(f"指纹      {payload['job']['fingerprint'][:16]}", file=sys.stderr)
                if prepared.issues:
                    print("\n校验结论：", file=sys.stderr)
                    print(_describe_issues(prepared.issues), file=sys.stderr)
                else:
                    print("校验结论：  无问题", file=sys.stderr)
            return EXIT_OK

        result = session.run(request)

    if getattr(args, "json", False):
        _emit_json(result.to_dict())
    else:
        _print_result(result)
    return EXIT_OK if result.ok else EXIT_RENDER


def _print_result(result: Any) -> None:
    """人读结果摘要。刻意简洁 —— 详细内容用 ``--json``。"""
    status = "✔ 完成" if result.ok else "✖ 未成功"
    print(f"{status}  solution={result.solution}  job_id={result.job_id}  "
          f"耗时 {result.elapsed_s:.1f}s")
    for artifact in result.artifacts:
        size = f"{artifact.bytes_written / 1048576:.1f} MB" if artifact.bytes_written else "?"
        print(f"  · {artifact.local_path or artifact.filename}  ({size})")
    if result.issues:
        blocking = blocking_issues(result.issues)
        rest = len(result.issues) - len(blocking)
        print(f"  校验：{len(blocking)} 项阻断，{rest} 项告警")
        print(_describe_issues(result.issues))
    if result.errors:
        print("  后端错误：")
        for error in result.errors:
            print(f"    {json.dumps(error, ensure_ascii=False, default=str)}")


def _cmd_validate(args: argparse.Namespace) -> int:
    """只校验不提交。永远返回 0，除非校验本身抛异常 ——
    问题清单本身是**结果**，不是命令失败。

    退出码恒 0，但报告里的 ``blocking`` / ``ok`` 用的是与 ``generate``
    **同一条**判定规则（见 :meth:`Session.blocking`）：开启严格模式后，
    告警也会计入 blocking，于是 CI 里 ``validate --json | jq .ok`` 的结论
    与真实提交的行为一致。
    """
    session = _build_session(args)
    request = _build_request(args)
    with session:
        issues = session.validate(request)

    blocking = session.blocking(issues)
    if getattr(args, "json", False):
        _emit_json({
            "ok": not blocking,
            "blocking": len(blocking),
            "total": len(issues),
            "issues": [issue.to_dict() for issue in issues],
        })
    else:
        if not issues:
            print("校验通过，无任何问题。")
        else:
            print(f"共 {len(issues)} 项（阻断 {len(blocking)} 项）：")
            print(_describe_issues(issues, show_info=True))
    return EXIT_OK


def _graph_default_path(session: Session, solution_key: str, mode: str | None) -> Path:
    """决定物化图落盘的默认路径。

    目录来源：方案的 ``graph_dir`` 配置 -> 否则 ``<工作区>/var/graphs``。

    为什么由 CLI 自己决定、而不是让调用方拼路径：调用方（外部框架的 shell 脚本）
    拼路径就意味着它必须**自己再解析一遍配置**（去读 .env、处理相对路径、
    处理 workspace 回退）。那份逻辑一旦与 Python 侧不一致，
    就会出现"文件写到了 A，框架去 B 找"这种极难定位的问题。
    让唯一持有 Config 对象的那一侧决定路径，是消灭这类问题的根本办法。
    """
    configured = session.config.get(f"solution.{solution_key}.graph_dir")
    directory = Path(configured).expanduser() if configured \
        else session.config.workspace / "var" / "graphs"
    directory = directory if directory.is_absolute() else (session.config.workspace / directory)
    # 文件名自带方案与模式：同一目录下可以并存多套图，且名字本身说明了它是什么
    return directory / f"{solution_key}_{mode or 'default'}_api.json"


def _cmd_graph(args: argparse.Namespace) -> int:
    """物化计算图 JSON —— 给"只认文件路径 + 节点 ID"的外部框架使用。

    这就是原实现里 ``COMFYUI_MINIMAX_H3_WORKFLOW_PATH`` / ``_OUTPUT_NODE``
    两个变量的替代品：不再预先存一份可能与代码脱节的图，
    而是**需要时现场生成**，生成即最新。

    刻意**不联网**：物化一张图的语义是"把代码里的构造结果写出来"，
    与后端是否可达无关。需要连带校验就再跑一次 ``om-bridge validate`` ——
    把两件事合成一件，只会让离线场景（CI 生成产物、跨机部署预生成）没法用。

    ``-o`` 省略时写到配置指定的图目录（见 :func:`_graph_default_path`），
    ``-o -`` 则打到 stdout（给人看或管道用）。
    """
    args.no_preflight = True  # 见上方说明：graph 恒为离线路径
    session = _build_session(args)
    request = _build_request(args)
    with session:
        prepared = session.prepare(request)

    graph = prepared.job.payload
    if not isinstance(graph, dict):
        print(f"错误：方案 {prepared.solution.key} 的载荷不是图结构（type="
              f"{type(graph).__name__}），无法物化", file=sys.stderr)
        return EXIT_BAD_WORKFLOW

    target = getattr(args, "out", None)
    envelope = {
        "ok": True,
        "solution": prepared.solution.key,
        "mode": prepared.mode,
        "output_node": prepared.job.output_selector,
        "nodes": len(graph),
        "fingerprint": prepared.job.fingerprint(),
    }

    if target == "-":
        # `-o -` 的语义是"把图给我"。此时**只**输出图本身，
        # 即便同时给了 --json 也不再输出信封 —— 两个 JSON 文档连在一起
        # 会让任何下游解析器拿到"额外数据"，而这类错误极难定位。
        # 信封信息改走 stderr，需要时仍可看到。
        _emit_json(graph)
        print(f"输出节点 {prepared.job.output_selector}（共 {len(graph)} 个节点）",
              file=sys.stderr)
        return EXIT_OK

    path = (Path(target).expanduser() if target
            else _graph_default_path(session, prepared.solution.key, prepared.mode))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    envelope["path"] = str(path)

    if getattr(args, "json", False):
        _emit_json(envelope)
    else:
        print(f"已写入 {path}")
        print(f"输出节点 {prepared.job.output_selector}（共 {len(graph)} 个节点）")
    return EXIT_OK


def _cmd_run(args: argparse.Namespace) -> int:
    """执行一份外部给的作业载荷（不经方案）。"""
    raw = getattr(args, "payload", None)
    if not raw:
        print("错误：run 需要 --payload <文件|->（- 表示从 stdin 读）", file=sys.stderr)
        return EXIT_USAGE

    if raw == "-":
        text = sys.stdin.read()
    else:
        path = Path(raw).expanduser()
        if not path.is_file():
            print(f"错误：找不到载荷文件 {path}", file=sys.stderr)
            return EXIT_USAGE
        text = path.read_text(encoding="utf-8")

    try:
        payload = json.loads(text)
    except ValueError as exc:
        print(f"错误：载荷不是合法 JSON：{exc}", file=sys.stderr)
        return EXIT_BAD_WORKFLOW

    session = _build_session(args)
    with session:
        result = session.run_raw(
            payload,
            output_selector=getattr(args, "output_node", None),
            output_dir=getattr(args, "output_dir", None),
            timeout=getattr(args, "timeout", None),
            validate=not getattr(args, "no_validate", False),
            label=getattr(args, "label", None) or f"payload:{raw}",
        )

    if getattr(args, "json", False):
        _emit_json(result.to_dict())
    else:
        _print_result(result)
    return EXIT_OK if result.ok else EXIT_RENDER


def _cmd_resume(args: argparse.Namespace) -> int:
    """续等超时作业。

    这条命令存在的唯一理由是：**超时不等于失败**。
    重提会产生重复作业与 GPU 争抢，是新手最常见的误操作，
    因此 CLI 在超时错误里直接把这条命令拼好给人看（见 session.wait）。
    """
    job_id = getattr(args, "job_id", None)
    if not job_id:
        print("错误：resume 需要 --job-id", file=sys.stderr)
        return EXIT_USAGE

    session = _build_session(args)
    with session:
        result = session.resume(
            job_id,
            timeout=getattr(args, "timeout", None),
            output_dir=getattr(args, "output_dir", None),
        )

    if getattr(args, "json", False):
        _emit_json(result.to_dict())
    else:
        _print_result(result)
    return EXIT_OK if result.ok else EXIT_RENDER


def _cmd_probe(args: argparse.Namespace) -> int:
    """探测后端：连通性 + 能力盘点 + 方案就绪度。"""
    session = _build_session(args)
    with session:
        report = session.probe_report()

    if getattr(args, "json", False):
        _emit_json(report)
        return EXIT_OK

    reachable = report.get("reachable")
    print(f"{'✔' if reachable else '✖'} 后端 {report.get('backend')} "
          f"{'可达' if reachable else '不可达'}")
    for key in ("version", "device", "vram_total_gb", "vram_free_gb"):
        if report.get(key) is not None:
            print(f"  {key}: {report[key]}")
    if report.get("errors"):
        for error in report["errors"]:
            print(f"  ! {error}")

    readiness = report.get("readiness") or {}
    for mode, entry in readiness.items():
        missing = entry.get("missing") or []
        flag = "✔" if entry.get("ready") else "✖"
        print(f"  {flag} 模式 {mode}：{len(missing)} 项缺失")
        for item in missing:
            print(f"      - {item}")
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    """列出后端、方案与别名。"""
    registry = get_registry()
    payload = registry.describe()

    if getattr(args, "json", False):
        _emit_json(payload)
        return EXIT_OK

    print("后端：")
    for backend in payload["backends"]:
        caps = ", ".join(backend["capabilities"]) or "—"
        print(f"  · {backend['name']:<12} {backend['display_name']}")
        print(f"      能力：{caps}")
        if backend["solutions"]:
            print(f"      方案：{', '.join(backend['solutions'])}")
    print("\n方案：")
    for solution in payload["solutions"]:
        modes = "/".join(solution["modes"]) or "—"
        print(f"  · {solution['key']:<16} [{solution['kind']}] 后端={solution['backend']}")
        print(f"      模式：{modes}")
        if solution["aliases"]:
            print(f"      别名：{', '.join(solution['aliases'])}")
    if payload["load_errors"]:
        print("\n加载问题：")
        for error in payload["load_errors"]:
            print(f"  ! {error}")
    return EXIT_OK


def _cmd_describe(args: argparse.Namespace) -> int:
    """输出方案或后端的完整自描述（含参数 schema 与资产需求）。

    刻意**不探测后端** —— 这个命令的定位是"离线查手册"，
    加一次网络往返会让它在无网环境、CI、以及被频繁调用的场景下都变得难用。
    想知道"这台机器现在能不能跑"，那是 ``om-bridge probe`` 的职责。
    """
    registry = get_registry()

    target = getattr(args, "solution_or_backend", None) or getattr(args, "solution", None)
    payload: dict[str, Any]

    if target:
        try:
            solution, _preset = registry.resolve(target)
            payload = solution.describe()
        except OmBridgeError:
            try:
                backend_cls = registry.backend(target)
                payload = backend_cls(load_config(env_file=getattr(args, "env_file", None))).describe()
            except OmBridgeError as exc:
                return _report_error(exc, as_json=getattr(args, "json", False))
    else:
        config = load_config(env_file=getattr(args, "env_file", None))
        payload = {
            "solutions": [registry.instance(key).describe() for key in sorted(registry.solutions())],
            "backends": [cls(config).describe() for cls in registry.backends().values()],
        }

    if getattr(args, "json", False):
        _emit_json(payload)
        return EXIT_OK

    _print_describe(payload)
    return EXIT_OK


def _print_describe(payload: dict) -> None:
    def one(entry: dict) -> None:
        print(f"{entry.get('display_name') or entry.get('key') or entry.get('name')}")
        if entry.get("description"):
            print(f"  {entry['description']}")
        if entry.get("docs"):
            print(f"  文档：{entry['docs']}")
        if entry.get("modes"):
            print(f"  模式：{', '.join(entry['modes'])}（默认 {entry.get('default_mode')}）")
        if entry.get("capabilities"):
            print(f"  能力：{', '.join(entry['capabilities'])}")
        params = entry.get("param_details") or []
        if params:
            print("  参数：")
            for spec in params:
                scope = "/".join(spec["applies_to"])
                default = "" if spec["default"] is None else f" 默认={spec['default']}"
                print(f"    --{spec['name'].replace('_', '-')}  [{scope}]{default}")
                if spec["description"]:
                    print(f"        {spec['description']}")
        slots = entry.get("media_slots") or []
        if slots:
            print("  媒体：")
            for slot in slots:
                modes = "/".join(slot.get("modes") or ["*"])
                need = "必填" if slot["required"] else "可选"
                extra = f" 最多 {slot['max_count']}" if slot["multiple"] else ""
                print(f"    --{slot['role'].replace('_', '-')}  [{modes}] {need}{extra}")
        assets = entry.get("assets") or []
        if assets:
            print("  模型资产：")
            for asset in assets:
                mark = "" if asset["required"] else "（可选）"
                print(f"    {asset['role']:<16} {asset['category']:<8} {asset['name']}{mark}")
        readiness = entry.get("readiness") or {}
        if readiness:
            print("  就绪度：")
            for mode, status in readiness.items():
                missing = status.get("missing") or []
                print(f"    {'✔' if status.get('ready') else '✖'} {mode}"
                      + (f"  缺失：{', '.join(missing)}" if missing else ""))

    if "solutions" in payload:
        for solution in payload["solutions"]:
            one(solution)
            print()
        for backend in payload["backends"]:
            one(backend)
            print()
    else:
        one(payload)


def _cmd_config(args: argparse.Namespace) -> int:
    """配置自省。三个子动作共用一个入口，因为它们读的是同一份报告。"""
    config: Config = _build_session(args).config
    action = getattr(args, "config_action", None) or "report"

    if action == "explain":
        key = getattr(args, "key", None)
        if not key:
            print("错误：config explain 需要 <键>", file=sys.stderr)
            return EXIT_USAGE
        entry = config.explain(key)
        if getattr(args, "json", False):
            _emit_json(entry)
            return EXIT_OK
        if not entry.get("known"):
            print(f"未知配置键：{key}")
            print("提示：用 `om-bridge config list` 查看全部已知键")
            return EXIT_USAGE
        print(f"{entry['key']}")
        print(f"  当前值    {entry['value']!r}")
        print(f"  来源      {entry['source']}")
        print(f"  规范变量  {entry['env']}")
        if entry["aliases"]:
            print(f"  兼容旧名  {', '.join(entry['aliases'])}")
        print(f"  内置默认  {entry['default']!r}")
        print(f"  说明      {entry['description']}")
        return EXIT_OK

    if action == "list":
        if getattr(args, "json", False):
            _emit_json([setting.to_dict() for setting in ALL_SETTINGS])
            return EXIT_OK
        print(f"{'键':<46} {'当前值':<24} 来源")
        for setting in ALL_SETTINGS:
            value = config.get(setting.key)
            shown = "***" if setting.secret and value else repr(value)
            if len(shown) > 22:
                shown = shown[:19] + "..."
            print(f"{setting.key:<46} {shown:<24} {config.source(setting.key)}")
        if config.warnings:
            print("\n注意：")
            for warning in config.warnings:
                print(f"  ! {warning}")
        return EXIT_OK

    # 默认：完整报告
    report = config.report(include_unknown_env=True)
    if getattr(args, "json", False):
        _emit_json(report)
        return EXIT_OK
    print(f"配置文件：{report['env_file']}")
    print(f"共 {len(report['entries'])} 项配置。用 `om-bridge config list` 看全量取值，"
          f"`om-bridge config explain <键>` 看单项来源。")
    by_source: dict[str, int] = {}
    for entry in report["entries"]:
        source = entry["source"].split(":")[0]
        by_source[source] = by_source.get(source, 0) + 1
    print("取值来源分布：" + "，".join(f"{k} {v}" for k, v in sorted(by_source.items())))
    if report["warnings"]:
        print("\n注意：")
        for warning in report["warnings"]:
            print(f"  ! {warning}")
    if report.get("unregistered_env"):
        print("\n已设置但未被任何配置项登记的 COMFYUI_*/OM_BRIDGE_* 变量：")
        for name in report["unregistered_env"]:
            print(f"  · {name}")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    """全面体检：配置 → 注册表 → 连通性 → 就绪度 → 结论。

    输出刻意分成"采集事实"与"给出结论"两段：前者的格式稳定，便于人和脚本读；
    后者带优先级排序的建议 —— 出问题时需要的是"先修哪个"，不是一堆原始数据。
    """
    session = _build_session(args)
    registry = get_registry()
    config = session.config

    facts: dict[str, Any] = {
        "version": __version__,
        "env_file": config.env_file or "(未加载)",
        "registry": registry.describe(),
        "config_warnings": config.warnings,
    }
    if not getattr(args, "skip_probe", False):
        with session:
            facts["probe"] = session.probe_report()
    else:
        facts["probe"] = {"skipped": True}

    verdict, advice = _judge(facts)
    facts["verdict"] = verdict
    facts["advice"] = advice

    if getattr(args, "json", False):
        _emit_json(facts)
        return EXIT_OK if verdict == "ok" else EXIT_USAGE

    print("=" * 62)
    print(f"OM-Bridge 体检报告  版本 {facts['version']}")
    print("=" * 62)
    print(f"配置文件      {facts['env_file']}")

    load_errors = facts["registry"]["load_errors"]
    print(f"注册表        {len(facts['registry']['backends'])} 个后端，"
          f"{len(facts['registry']['solutions'])} 个方案"
          + (f"，{len(load_errors)} 个加载问题" if load_errors else ""))
    for error in load_errors:
        print(f"                ! {error}")

    for warning in facts["config_warnings"]:
        print(f"配置注意      ! {warning}")

    probe = facts["probe"]
    if probe.get("skipped"):
        print("后端探测      已跳过（--skip-probe）")
    else:
        print(f"后端探测      {'可达' if probe.get('reachable') else '不可达'}"
              f"  后端={probe.get('backend')}")
        for key in ("version", "device", "vram_free_gb", "vram_total_gb"):
            if probe.get(key) is not None:
                print(f"              {key}: {probe[key]}")
        for error in probe.get("errors") or []:
            print(f"              ! {error}")
        readiness = probe.get("readiness") or {}
        for mode, entry in readiness.items():
            missing = entry.get("missing") or []
            print(f"  模式 {mode:<7} {'就绪' if entry.get('ready') else '缺 ' + str(len(missing)) + ' 项'}"
                  + (f"  缺：{', '.join(missing)}" if missing else ""))

    print("-" * 62)
    print(f"结论：{verdict}")
    for line in advice:
        print(f"  · {line}")
    print("=" * 62)
    return EXIT_OK if verdict == "ok" else EXIT_USAGE


def _judge(facts: dict[str, Any]) -> tuple[str, list[str]]:
    """把采集到的事实归纳成结论与带优先级的建议。

    顺序即优先级：**先修基础设施，再看模型资产**。
    这条顺序来自实际排障经验 —— 后端不可达时去补模型文件是白费的。
    """
    advice: list[str] = []
    fatal = False
    degraded = False

    registry = facts["registry"]
    if registry["load_errors"]:
        degraded = True
        advice.append("查看注册表加载问题：`om-bridge list`，"
                      "通常是某个方案的 import 失败（缺依赖或路径错）")

    if not registry["backends"]:
        fatal = True
        advice.append("一个后端都没有注册成功 —— 检查安装是否完整（`pip show om-bridge`）")

    if facts["config_warnings"]:
        degraded = True
        advice.append("配置存在冲突项：`om-bridge config report` 查看细节")

    probe = facts["probe"]
    if probe.get("skipped"):
        advice.append("未探测后端。确认部署是否正常请去掉 --skip-probe")
    elif not probe.get("reachable"):
        fatal = True
        advice.append("后端不可达 —— 先确认服务在跑："
                      f"`curl {probe.get('backend') and ''}` 或检查 OM_BRIDGE_COMFYUI_SERVER_URL / "
                      "旧名 COMFYUI_SERVER_URL 是否指向正确地址")
        advice.append("本机验通最快的方式：`om-bridge probe --json`，看 errors 字段里的具体原因")
    else:
        readiness = probe.get("readiness") or {}
        missing_modes = {mode: entry.get("missing") for mode, entry in readiness.items()
                         if not entry.get("ready")}
        if missing_modes:
            degraded = True
            names = sorted({name for missing in missing_modes.values() for name in (missing or [])})
            advice.append(f"有 {len(names)} 项模型资产缺失：{', '.join(names[:6])}"
                          + ("…" if len(names) > 6 else ""))
            advice.append("补齐后重跑 `om-bridge doctor`；"
                          "参考 `docs/solutions/minimax-h3.md` 的资产清单与下载地址")
        else:
            advice.append("一切就绪，可以直接生成："
                          "`om-bridge generate --solution minimax_h3.t2v --prompt \"...\"`")

    if fatal:
        return "failed", advice
    if degraded:
        return "degraded", advice
    return "ok", advice


# ===========================================================================
# 参数解析
# ===========================================================================
def _common_options() -> argparse.ArgumentParser:
    """所有命令共用的选项。

    用 ``default=argparse.SUPPRESS`` 而不是常规默认值：这些选项同时挂在
    主解析器与子解析器上（这样 ``--json`` 放在子命令前后都合法），
    若用普通默认值，子解析器的默认会**覆盖**主解析器已解析出的值 ——
    这是 argparse 的经典陷阱，SUPPRESS 是干净的解法。

    代价是访问时必须用 ``getattr(args, name, fallback)``，全文件统一遵守。
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="以 JSON 输出结果（stdout 只放 JSON，便于脚本解析）")
    parser.add_argument("--env-file", dest="env_file", default=argparse.SUPPRESS,
                        metavar="路径", help="显式指定配置文件")
    parser.add_argument("--workspace", default=argparse.SUPPRESS, metavar="目录",
                        help="工作区根目录（同时影响配置文件探测与产物落盘）")
    parser.add_argument("--backend", default=argparse.SUPPRESS, metavar="名称",
                        help="指定后端（默认取配置 global.default_backend）")
    parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="输出调试日志")
    parser.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="关闭进度输出（只保留最终结果）")
    return parser


def _add_generation_inputs(
    parser: argparse.ArgumentParser,
    registry: Registry,
    *,
    needs_solution: bool = True,
    include_toggles: bool = True,
) -> None:
    """给一个子命令挂上"生成输入"相关选项：方案、提示词、动态参数、媒体。

    :param include_toggles: 是否包含 ``--no-validate`` / ``--no-preflight``。
        对 ``validate``（它存在的意义就是校验）与 ``graph``（离线物化，
        校验由 ``validate`` 命令负责）这两个命令来说，这两个开关只会误导人，
        因此关掉。
    """
    if needs_solution:
        parser.add_argument("-s", "--solution", metavar="方案[.模式]",
                            help="方案键或别名，如 minimax_h3 / minimax_h3.ref2v")
    parser.add_argument("-p", "--prompt", metavar="提示词",
                        help="提示词。可用 -p - 从 stdin 读取（长提示词更舒服）")
    parser.add_argument("--output-dir", dest="output_dir", metavar="目录",
                        help="产物落盘目录（默认取配置 global.output_dir）")
    parser.add_argument("--timeout", type=float, metavar="秒",
                        help="等待超时。⚠ 超时后请用 resume 续等，不要重提")
    if include_toggles:
        parser.add_argument("--no-validate", dest="no_validate", action="store_true",
                            help="跳过参数与媒体校验（不推荐；会省掉最有价值的一层保护）")
        parser.add_argument("--no-preflight", dest="no_preflight", action="store_true",
                            help="跳过需要联网的载荷静态校验（离线物化计算图时用）")
    parser.add_argument("--set", action="append", metavar="名字=值",
                        help="传入未被方案声明登记的扩展参数（可重复）")
    parser.add_argument("--node-override", dest="node_override", action="append",
                        metavar="节点ID.输入名=值",
                        help="直接改写载荷里某个节点的输入（逃生舱，绕过类型检查；可重复）")
    _add_param_options(parser, registry)
    _add_media_options(parser, registry)


def _build_parser(registry: Registry) -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog=PROG,
        parents=[common],
        description="OM-Bridge —— 可扩展的 AI 生成能力桥接层。"
                    "把「算力来源」（Backend）与「模型语义」（Solution）解耦，"
                    "新增实现无需改动核心。",
        epilog="全部命令都支持 --json 以获得机器可读输出。"
               "参数与媒体选项由注册表中的方案自动生成，"
               "用 `om-bridge describe` 查看某个方案的完整选项。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="<命令>")

    # --- generate ---
    gen = subparsers.add_parser(
        "generate", parents=[common], help="执行一次生成",
        description="执行一次生成。用 --dry-run 可以先看清将要提交什么，不实际排队。",
    )
    _add_generation_inputs(gen, registry)
    gen.add_argument("--dry-run", dest="dry_run", action="store_true",
                     help="只解析与校验，打印将要提交的内容，不提交")
    gen.set_defaults(func=_cmd_generate)

    # --- validate ---
    val = subparsers.add_parser(
        "validate", parents=[common], help="只校验不提交",
        description="跑完全部本地校验与资产就绪度检查，输出全部问题（含告警）。",
    )
    _add_generation_inputs(val, registry, include_toggles=False)
    val.set_defaults(func=_cmd_validate)

    # --- graph ---
    graph = subparsers.add_parser(
        "graph", parents=[common], help="物化计算图 JSON（供外部框架使用）",
        description="现场生成计算图并写出 JSON，同时给出输出节点 ID。"
                    "这是「外部框架需要文件路径 + 节点 ID」这一需求的官方答案："
                    "不再预存可能与代码脱节的图。此命令不联网。",
    )
    _add_generation_inputs(graph, registry, include_toggles=False)
    graph.add_argument("-o", "--out", metavar="文件",
                       help="写出到文件。省略则写到配置的图目录"
                            "（solution.<方案>.graph_dir，未配置则为 <工作区>/var/graphs）；"
                            "- 表示打到 stdout")
    graph.set_defaults(func=_cmd_graph)

    # --- run ---
    run = subparsers.add_parser(
        "run", parents=[common], help="执行一份外部给的作业载荷",
        description="把一个现成的 API 格式图跑起来，不经过任何方案。"
                    "没有方案层的参数校验，只有后端静态校验 —— 所以别关掉它。",
    )
    run.add_argument("--payload", metavar="文件|-", help="载荷 JSON 文件，- 表示从 stdin 读")
    run.add_argument("--output-node", dest="output_node", metavar="节点ID",
                     help="产出成品所在节点（决定收集哪个文件）")
    run.add_argument("--label", metavar="标签", help="给这次作业起个名字（出现在日志与溯源里）")
    run.add_argument("--output-dir", dest="output_dir", metavar="目录", help="产物落盘目录")
    run.add_argument("--timeout", type=float, metavar="秒", help="等待超时（秒）")
    run.add_argument("--no-validate", dest="no_validate", action="store_true",
                     help="跳过提交前的载荷校验（强烈不推荐）")
    run.set_defaults(func=_cmd_run)

    # --- resume ---
    resume = subparsers.add_parser(
        "resume", parents=[common], help="续等一个超时作业",
        description="对一个已提交的作业继续等待。⚠ 超时后请用这个，不要重新提交 —— "
                    "重提会排出重复作业并与原作业争抢同一块 GPU。",
    )
    resume.add_argument("--job-id", dest="job_id", metavar="ID", help="作业 ID（如 ComfyUI 的 prompt_id）")
    resume.add_argument("--output-dir", dest="output_dir", metavar="目录", help="产物落盘目录")
    resume.add_argument("--timeout", type=float, metavar="秒", help="再等多久（秒）")
    resume.set_defaults(func=_cmd_resume)

    # --- probe ---
    probe = subparsers.add_parser(
        "probe", parents=[common], help="探测后端连通性与能力盘点",
        description="一次探测同时回答「能不能连上」与「连上之后有什么」。",
    )
    probe.set_defaults(func=_cmd_probe)

    # --- list ---
    listing = subparsers.add_parser(
        "list", parents=[common], help="列出所有后端、方案与别名",
        description="列出注册表内容。发现哪些方案可用、哪些别名指向它。",
    )
    listing.set_defaults(func=_cmd_list)

    # --- describe ---
    describe = subparsers.add_parser(
        "describe", parents=[common], help="某个方案或后端的完整自描述",
        description="输出参数 schema、媒体槽位、资产需求与就绪度。"
                    "不带参数时输出全部。",
    )
    describe.add_argument("solution_or_backend", nargs="?", metavar="方案|后端",
                          help="方案键/别名，或后端名；省略则全部")
    describe.set_defaults(func=_cmd_describe)

    # --- config ---
    config = subparsers.add_parser(
        "config", parents=[common], help="配置自省",
        description="查看全部配置项、取值来源与冲突提示。",
    )
    config_sub = config.add_subparsers(dest="config_action", metavar="<动作>")
    config_sub.add_parser("list", parents=[common], help="列出全部配置项与当前取值")
    explain = config_sub.add_parser("explain", parents=[common], help="解释单个配置项的取值来源")
    explain.add_argument("key", metavar="键", help="规范键，如 backend.comfyui.server_url")
    config_sub.add_parser("report", parents=[common], help="完整报告（含未登记的变量）")
    config.set_defaults(func=_cmd_config)

    # --- doctor ---
    doctor = subparsers.add_parser(
        "doctor", parents=[common], help="全面体检并给出结论",
        description="按「配置 → 注册表 → 连通性 → 就绪度 → 结论」采集事实并给出优先级建议。",
    )
    doctor.add_argument("--skip-probe", dest="skip_probe", action="store_true",
                        help="跳过联网探测（离线排障时用）")
    doctor.set_defaults(func=_cmd_doctor)

    return parser


# ===========================================================================
# 入口
# ===========================================================================
def _log_startup_context(args: argparse.Namespace, registry: Registry) -> None:
    """``-v`` 时先说明"这次运行实际用了什么"，再干活。

    为什么值得专门做这件事：用户最常问的问题不是"怎么配"，而是
    **"我改了配置，为什么没生效"**。而这个问题的一半答案在程序选了什么，
    不在用户写了什么 —— 配置探测会依次尝试若干位置并**只取第一个存在的**，
    用户往往不知道最终落到哪个文件；注册表发现失败时也只有一条静默的
    ``load_errors``。

    特意放在**所有命令都经过**的位置，因此 ``list`` 这类纯离线查询也能回答
    "我的 ``.env`` 到底被加载了没有"。日志走 stderr（见 ADR-0007），
    所以 ``--json`` 的 stdout 依然只有一份 JSON。
    """
    logger = log.get_logger("cli")
    payload = registry.describe()
    logger.debug(
        "发现 %d 个后端、%d 个方案：%s",
        len(payload["backends"]),
        len(payload["solutions"]),
        ", ".join(solution["key"] for solution in payload["solutions"]) or "(无)",
    )
    for error in payload["load_errors"]:
        logger.warning("实现加载失败：%s", error)

    explicit = getattr(args, "env_file", None)
    if explicit:
        logger.debug("配置文件（--env-file 显式指定）：%s", explicit)
        return

    workspace = Path(getattr(args, "workspace", None) or ".")
    candidates = default_env_candidates(workspace)
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if existing:
        logger.debug(
            "配置文件：%s（探测顺序里的第一个存在者）", existing[0]
        )
    else:
        logger.debug(
            "未找到配置文件，已按顺序探测：%s",
            " → ".join(str(candidate) for candidate in candidates),
        )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。返回进程退出码。"""
    argv = list(sys.argv[1:] if argv is None else argv)

    # 先把日志配好再解析参数：注册表发现过程本身可能报加载问题，
    # 那些信息需要有个去处（stderr），否则会被静默吞掉。
    log.configure_logging()
    registry = get_registry()
    parser = _build_parser(registry)

    args = parser.parse_args(argv)

    if getattr(args, "quiet", False):
        log.set_progress_enabled(False)
    if getattr(args, "verbose", False):
        log.configure_logging("DEBUG", force=True)
        _log_startup_context(args, registry)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    # 提示词支持从 stdin 读：长提示词（H3 的多段式提示词尤其长）
    # 在命令行里转义引号是折磨，管道进来更实际。
    if getattr(args, "prompt", None) == "-":
        args.prompt = sys.stdin.read().strip()

    as_json = bool(getattr(args, "json", False))

    try:
        return args.func(args)
    except OmBridgeError as exc:
        return _report_error(exc, as_json=as_json)
    except KeyboardInterrupt:
        print("\n已中断。若作业已提交，它很可能仍在后端运行 —— "
              "用 `om-bridge resume --job-id <ID>` 续等，不要重提。", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # 被 `| head` 之类的下游提前关闭管道。不是错误，静默退出。
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001
        # 未预期异常必须带上类型，否则排查时只剩一句没有信息量的话
        print(f"内部错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc(file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
