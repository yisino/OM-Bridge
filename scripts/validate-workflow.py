#!/usr/bin/env python3
"""校验一份**外部给的**工作流 JSON 是否能在这台后端上跑起来。

存在的理由
----------
"我这里有一份别人给的工作流，能不能跑"是一个高频且独立的需求。
它不该逼你先写一个 Solution，也不该直接丢给后端去撞 ——
后端的拒绝信息只有一个节点 ID 和一段 Python 栈，来回几轮才能定位。

这个脚本把 OM-Bridge 后端的静态校验能力单独暴露出来：读 ``/object_info``，
在本地一次性报出全部问题（节点缺失、输入名拼错、枚举越界、数值越界、
连线悬空、输出槽位越界、产出节点选错），每条都带位置与修改建议。
**零成本，且不消耗一次排队。**

用法
----
    python3 scripts/validate-workflow.py workflow.json
    python3 scripts/validate-workflow.py workflow.json --output-node 15
    python3 scripts/validate-workflow.py workflow.json --json
    python3 scripts/validate-workflow.py workflow.json --env-file config/om-bridge.env

退出码：0 = 无阻断问题；3 = 有阻断问题；2 = 后端不可达；1 = 用法错误。
（与 CLI 的退出码契约一致，便于串进现有脚本。）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从源码树直接运行，而不必先安装
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from om_bridge.core.config import load_config  # noqa: E402
from om_bridge.core.errors import OmBridgeError  # noqa: E402
from om_bridge.core.models import JobSpec, Severity, blocking_issues  # noqa: E402
from om_bridge.core.registry import get_registry  # noqa: E402

ICONS = {Severity.ERROR: "✖", Severity.WARNING: "!", Severity.INFO: "·"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验一份工作流 JSON 能否在当前后端上执行（不提交）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("workflow", help="工作流 JSON 文件（API 格式）")
    parser.add_argument("--output-node", dest="output_node",
                        help="产出成品所在节点 ID。省略时只做结构与取值校验，不检查产出节点。")
    parser.add_argument("--backend", help="指定后端（默认取配置）")
    parser.add_argument("--env-file", dest="env_file", help="显式指定配置文件")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    path = Path(args.workflow).expanduser()
    if not path.is_file():
        print(f"错误：找不到文件 {path}", file=sys.stderr)
        return 1
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"错误：不是合法 JSON：{exc}", file=sys.stderr)
        return 1

    config = load_config(env_file=args.env_file)
    registry = get_registry()

    try:
        backend = registry.create_backend(args.backend, config)
    except OmBridgeError as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"提示：{exc.hint}", file=sys.stderr)
        return exc.exit_code

    # 复用后端自己的 preflight —— 这条路径与 CLI/MCP 生成时走的是**同一个**
    # 校验器，因此这里报出来的问题与真正提交时会遇到的一模一样。
    # 另起一套校验逻辑会让"预检通过但仍被拒"变成常态。
    job = JobSpec(
        payload=graph,
        output_selector=args.output_node,
        kind="graph",
        metadata={"label": path.name},
    )
    try:
        issues = backend.preflight(job, probe=None)
    except OmBridgeError as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"提示：{exc.hint}", file=sys.stderr)
        return exc.exit_code
    finally:
        backend.close()

    blocking = blocking_issues(issues)

    if args.json:
        print(json.dumps({
            "ok": not blocking,
            "workflow": str(path),
            "output_node": args.output_node,
            "nodes": len(graph) if isinstance(graph, dict) else None,
            "blocking": len(blocking),
            "total": len(issues),
            "issues": [issue.to_dict() for issue in issues],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"文件      {path}")
        print(f"节点数    {len(graph) if isinstance(graph, dict) else '?'}")
        print(f"产出节点  {args.output_node or '(未指定，跳过该项检查)'}")
        if not issues:
            print("\n校验通过，未发现问题。")
        else:
            print(f"\n共 {len(issues)} 项问题（其中阻断 {len(blocking)} 项）：")
            for issue in issues:
                location = f" [{issue.location}]" if issue.location else ""
                print(f"  {ICONS.get(issue.severity, '?')} {issue.message}{location}")
                if issue.hint:
                    print(f"      ↳ {issue.hint}")

    return 3 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
