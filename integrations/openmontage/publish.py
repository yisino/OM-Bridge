"""把 OM-Bridge 的现状**发布**给 OpenMontage —— 一个幂等的托管块写入器。

它取代了什么
------------
原实现里有两个各管一半的脚本：

* ``merge_env_example_comfyui.py`` —— 把变量并进 ``.env.example``；
* ``patch_h3_preflight.py`` —— 让 OpenMontage 的前置检查知道"本机 H3 是可用的"。

两者都在"改别人的文件"，用的是逐行查找 + 替换的写法。这种写法的问题不在正确性，
而在**幂等性**：跑两次会留下两份、改名之后会留下孤儿行、失败中断会留下半截状态。
而"配置漂移"正是最难查的一类故障 —— 它不报错，只是行为与预期不符。

这里的做法是**托管块**：在目标文件里用一对固定标记圈出一段内容，每次发布
整段替换。于是：

* 重复执行结果一致（幂等）；
* 想撤销只需删掉整段（``--remove``）；
* 块外的内容**一个字节都不动**，用户自己写的东西永远安全；
* 任何时刻都能一眼看出"哪些变量是 OM-Bridge 管的"。

为什么是"发布"而不是"同步"
--------------------------
单向：OM-Bridge 是这些变量的**事实来源**，OpenMontage 是消费者。
不做双向同步，因为双向必然产生"谁赢"的歧义，而歧义会在最不方便的时候
（比如线上出问题时）才暴露出来。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from om_bridge.core.config import load_config  # noqa: E402
from om_bridge.core.models import GenerationRequest, MediaAsset, MediaKind  # noqa: E402
from om_bridge.core.registry import get_registry  # noqa: E402
from om_bridge.core.session import Session  # noqa: E402

BEGIN_MARKER = "# >>> OM-Bridge managed block >>> 由 om-bridge 集成脚本生成，请勿手工编辑"
END_MARKER = "# <<< OM-Bridge managed block <<<"


@dataclass
class PublishedVar:
    """一个要发布给外部框架的变量。"""

    name: str
    """**外部框架**读取的变量名。注意它可能与我们自己的规范名不同 ——
    这里是兼容层，不是别名读取层。"""
    source: str
    """值来自哪里（配置键或现场生成的产物），写进注释里便于溯源。"""
    value: str
    note: str = ""
    secret: bool = False


def build_vars(
    *,
    env_file: str | None,
    graph_dir: str | None,
    materialize: bool,
    extra: dict[str, str],
) -> list[PublishedVar]:
    """算出要发布的内容。

    分成三类，理由各不相同：

    1. **后端地址** —— OpenMontage 用它直连 ComfyUI 跑自己的通道。
       必须与 OM-Bridge 看到的**同一个地址**，否则会出现
       "用 om-bridge 测通了，用 OpenMontage 却连不上"。
    2. **就绪度声明（``COMFYUI_H3_LOCAL_MODELS``）** —— 让 OpenMontage 的
       前置检查知道本机 H3 可用。这份列表来自 OM-Bridge 的资产声明，
       因此本机换了量化权重时，重跑一次发布即可，不必改两处。
    3. **物化图路径 + 输出节点** —— OpenMontage 的原生 H3 通道只接受
       「文件路径 + 节点 ID」。这里**现场物化**再写路径，
       于是它在结构上不可能与构图代码脱节。
    """
    config = load_config(env_file=env_file)
    comfy = config.backend("comfyui")
    h3 = config.solution("minimax_h3")

    variables: list[PublishedVar] = []

    # --- 1. 后端地址 ---
    server_url = comfy.get("server_url", "")
    variables.append(PublishedVar(
        "COMFYUI_SERVER_URL", "backend.comfyui.server_url", str(server_url),
        "OpenMontage 读取的 ComfyUI 地址。与 OM-Bridge 使用同一个值，避免两套配置分叉。",
    ))
    for capability, name in (
        ("video", "COMFYUI_VIDEO_SERVER_URL"),
        ("image", "COMFYUI_IMAGE_SERVER_URL"),
        ("music", "COMFYUI_MUSIC_SERVER_URL"),
    ):
        value = comfy.get(f"{capability}_server_url", "")
        if value:
            # 只在显式配置时才发布：留空等于"继承基准地址"，
            # 发布一个空值反而会让消费者把"继承"误解成"没有"。
            variables.append(PublishedVar(
                name, f"backend.comfyui.{capability}_server_url", str(value),
                f"{capability} 能力专用地址。",
            ))

    # --- 2. 就绪度声明 ---
    models = h3.get("preflight_models") or []
    if isinstance(models, str):
        models = [item.strip() for item in models.split(",") if item.strip()]
    if models:
        variables.append(PublishedVar(
            "COMFYUI_H3_LOCAL_MODELS", "solution.minimax_h3.preflight_models",
            ",".join(models),
            "声明本机已具备的 H3 扩散权重，供外部框架做前置检查。",
        ))

    # --- 3. 物化图 + 输出节点 ---
    if materialize:
        variables.extend(_materialize_graphs(graph_dir=graph_dir, h3=h3, config=config))

    for name, value in extra.items():
        variables.append(PublishedVar(name, "--extra", value, "调用方显式指定"))

    return variables


def _materialize_graphs(
    *, graph_dir: str | None, h3: Any, config: Any
) -> list[PublishedVar]:
    """现场物化图并返回"路径 + 输出节点"两组变量。

    变量名沿用 OpenMontage 既有的那一套（``COMFYUI_MINIMAX_H3_WORKFLOW_PATH``
    与 ``_OUTPUT_NODE``）。**它们只在这里出现一次** —— 是这张表定义了对外的
    兼容契约，而不是散落在配置系统里。于是：

    * OM-Bridge 自己不需要持有"预生成文件"这个状态（少一个状态就少一类不同步）；
    * 外部框架那边读到的仍是它熟悉的名字；
    * 将来 OpenMontage 换成别的方式获取图，只需改这一张表。
    """
    registry = get_registry()
    base_dir = Path(graph_dir) if graph_dir else None
    if base_dir is None:
        configured = h3.get("graph_dir")
        base_dir = Path(configured).expanduser() if configured \
            else config.workspace / "var" / "graphs"
    if not base_dir.is_absolute():
        base_dir = config.workspace / base_dir

    placements = [
        # (形态, 变量名前缀, 需要的占位素材参数)
        ("t2v", "COMFYUI_MINIMAX_H3", {}),
        ("ref2v", "COMFYUI_MINIMAX_H3_REF2V", {"reference_image": ["__placeholder__.png"]}),
    ]

    variables: list[PublishedVar] = []
    for mode, prefix, media in placements:
        target = base_dir / f"minimax_h3_{mode}_api.json"
        try:
            graph, output_node = _build_graph(registry, mode, media, config)
        except Exception as exc:  # noqa: BLE001
            # 物化失败不该让整次发布失败：地址与就绪度声明仍然有效，
            # 而"图没生成"会通过下面缺变量体现出来，比整块回滚更容易定位。
            print(f"  ! {mode} 物化失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  · 已物化 {target}（{len(graph)} 个节点）", file=sys.stderr)

        variables.append(PublishedVar(
            f"{prefix}_WORKFLOW_PATH", f"物化自 minimax_h3.{mode}", str(target),
            f"{mode} 形态的计算图。由 OM-Bridge 现场生成，随代码一起更新。",
        ))
        variables.append(PublishedVar(
            f"{prefix}_OUTPUT_NODE", f"物化自 minimax_h3.{mode}", str(output_node),
            f"{mode} 形态的产出节点 ID。",
        ))
    return variables


def _build_graph(registry: Any, mode: str, media: dict[str, Any], config: Any):
    """用与 CLI 完全相同的路径构造图（不联网）。

    刻意复用 ``Session.prepare`` 而不是另写一段构图调用：只要有两处构图入口，
    就一定会有一处先过期，而"两处图不一致"这种问题只会以"结果不对"的形式出现。
    """
    asset_map = {
        "reference_image": MediaKind.IMAGE,
        "first_frame": MediaKind.IMAGE,
        "last_frame": MediaKind.IMAGE,
    }
    assets = [
        MediaAsset(role=role, kind=asset_map.get(role, MediaKind.IMAGE), name=name)
        for role, names in media.items()
        for name in names
    ]
    request = GenerationRequest(
        solution=f"minimax_h3.{mode}",
        prompt="OM-Bridge 占位提示词：请在提交前替换。",
        media=assets,
        validate=True,
        preflight=False,  # 物化是离线操作，不该依赖后端可达
    )
    with Session(config) as session:
        prepared = session.prepare(request)
    return prepared.job.payload, prepared.job.output_selector


# ===========================================================================
# 托管块读写
# ===========================================================================
def render_block(variables: list[PublishedVar]) -> str:
    lines = [
        BEGIN_MARKER,
        "#",
        "# 这一段由 integrations/openmontage/publish.py 整段替换，请勿手工编辑。",
        "# 撤销：python3 publish.py --target <文件> --remove",
        "#",
    ]
    for variable in variables:
        if variable.note:
            lines.append(f"# {variable.note}")
        lines.append(f"#   来源：{variable.source}")
        lines.append(f"{variable.name}={variable.value}")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def apply_block(existing: str, block: str) -> tuple[str, str]:
    """把托管块写进文本，返回 ``(新文本, 动作说明)``。

    三种情况都要照顾到，且都必须幂等：
      * 已有托管块 -> 整段替换（**不**保留旧内容：它是生成物，不是用户数据）；
      * 没有 -> 追加到末尾；
      * 文件为空/不存在 -> 只有块本身。

    块外内容一律原样保留，包括紧邻的注释与空行 —— 目标文件属于另一个项目，
    这个脚本的权限边界就是"这对标记之间"。
    """
    if BEGIN_MARKER in existing:
        start = existing.index(BEGIN_MARKER)
        end = existing.find(END_MARKER)
        if end == -1:
            # 有头无尾：上一次执行被中断了。此时**不能**猜结尾在哪，
            # 只能如实报错让人来看，否则会误删用户写的后续内容。
            raise ValueError(
                "目标文件里有托管块的开头标记但没有结尾标记（上次执行被中断？）。\n"
                "请手工删除从下面这一行开始的内容后重试：\n"
                f"  {BEGIN_MARKER}"
            )
        end += len(END_MARKER)
        # 连同结尾标记后面紧跟的那个换行一起吃掉，避免反复执行时堆积空行
        if existing[end:end + 1] == "\n":
            end += 1
        if existing[start:end] == block:
            return existing, "无变化（内容已经一致）"
        return existing[:start] + block + existing[end:], "已替换托管块"

    separator = "" if not existing or existing.endswith("\n\n") else (
        "\n" if existing.endswith("\n") else "\n\n"
    )
    return existing + separator + block, "已追加托管块"


def remove_block(existing: str) -> tuple[str, str]:
    if BEGIN_MARKER not in existing:
        return existing, "没有托管块，无需移除"
    start = existing.index(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if end == -1:
        raise ValueError("托管块缺少结尾标记，无法安全移除（请手工处理）")
    end += len(END_MARKER)
    if existing[end:end + 1] == "\n":
        end += 1
    # 连同块前面多余的空行一起去掉，让移除后的文件看起来像从没写过
    head = existing[:start].rstrip("\n")
    tail = existing[end:].lstrip("\n")
    merged = head + ("\n\n" if head and tail else "") + tail
    if merged and not merged.endswith("\n"):
        merged += "\n"
    return merged, "已移除托管块"


# ===========================================================================
# 入口
# ===========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 OM-Bridge 的后端地址、就绪度声明与物化图路径，"
                    "以托管块的形式发布到 OpenMontage 的 .env。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 publish.py --target /opt/OpenMontage/.env --dry-run\n"
               "  python3 publish.py --target /opt/OpenMontage/.env\n"
               "  python3 publish.py --target /opt/OpenMontage/.env --remove\n",
    )
    parser.add_argument("--target", required=False, help="目标 .env 文件（OpenMontage 的）")
    parser.add_argument("--env-file", dest="env_file", help="OM-Bridge 的配置文件")
    parser.add_argument("--graph-dir", dest="graph_dir",
                        help="物化图的落盘目录（默认用 OM-Bridge 配置里的 graph_dir）")
    parser.add_argument("--no-materialize", dest="materialize", action="store_false",
                        default=True,
                        help="不物化计算图，只发布地址与就绪度声明")
    parser.add_argument("--extra", action="append", metavar="名字=值",
                        help="额外发布的变量（可重复）")
    parser.add_argument("--remove", action="store_true", help="移除托管块")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="只打印将要写入的内容，不改动文件")
    args = parser.parse_args(argv)

    if not args.target:
        parser.error("需要 --target <目标 .env>")

    target = Path(args.target).expanduser()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""

    if args.remove:
        try:
            updated, action = remove_block(existing)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        if args.dry_run:
            print(updated)
            print(f"（预演：{action}）", file=sys.stderr)
            return 0
        target.write_text(updated, encoding="utf-8")
        print(f"{action}：{target}")
        return 0

    extra: dict[str, str] = {}
    for raw in args.extra or []:
        name, _, value = raw.partition("=")
        if not name or "=" not in raw:
            print(f"错误：--extra 期望 名字=值，收到 {raw!r}", file=sys.stderr)
            return 1
        extra[name.strip()] = value.strip()

    variables = build_vars(
        env_file=args.env_file,
        graph_dir=args.graph_dir,
        materialize=args.materialize,
        extra=extra,
    )
    if not variables:
        print("没有需要发布的变量 —— 检查 OM-Bridge 配置是否为空。", file=sys.stderr)
        return 1

    block = render_block(variables)

    try:
        updated, action = apply_block(existing, block)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(block)
        print(f"（预演：{action}；目标 {target}）", file=sys.stderr)
        return 0

    if not target.parent.is_dir():
        print(f"错误：目录不存在 {target.parent}", file=sys.stderr)
        return 1

    if target.is_file():
        # 目标属于另一个项目，改之前留个备份。
        # 单份、固定后缀（而不是带时间戳堆积）—— 发布是幂等的，
        # 备份也只需要"上一版"这一份。
        backup = target.with_suffix(target.suffix + ".om-bridge.bak")
        backup.write_text(existing, encoding="utf-8")
        print(f"已备份原文件：{backup}")

    target.write_text(updated, encoding="utf-8")
    print(f"{action}：{target}")
    for variable in variables:
        shown = "***" if variable.secret else variable.value
        print(f"  {variable.name}={shown}")
    print()
    print("提示：这些值只在**启动时**被读取。若 OpenMontage 正在运行，需要重启它才生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
