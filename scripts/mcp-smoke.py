#!/usr/bin/env python3
"""MCP 协议冒烟测试 —— 用真实子进程跑一遍 stdio 上的 JSON-RPC。

为什么必须"真实子进程 + 真实 stdio"
-----------------------------------
这个脚本的核心被测对象之一，就是**"stdout 上只有 JSON-RPC 消息"**这条硬约束。
在进程内直接调用 ``McpServer.handle`` 测不到它 —— 日志误写到 stdout、
``print`` 忘了改流向、某个第三方库往 stdout 吐了一行版本号，
这些全都会在进程内的测试里安然通过，然后在宿主机上表现为
"invalid JSON"，而那条报错不会指向真正的原因。

因此这里把服务端当黑盒启动，分别抓取 stdout 与 stderr，逐行解析 stdout，
并要求：
  * 每一行都是合法 JSON；
  * 通知（无 id）**不产生**响应；
  * 未知工具回 -32602、未知方法回 -32601、坏 JSON 回 -32700 且服务继续存活；
  * stderr 里没有 traceback。

用法
----
    python3 scripts/mcp-smoke.py
    python3 scripts/mcp-smoke.py --env-file config/om-bridge.env
    python3 scripts/mcp-smoke.py --skip-network    # 不调 om_bridge_check

退出码：0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
_PROJECT_ROOT = _SRC.parent

PASS = 0
FAIL = 0


def ok(message: str) -> None:
    global PASS
    PASS += 1
    print(f"  \033[32m✔\033[0m {message}")


def bad(message: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  \033[31m✖\033[0m {message}")


def build_transcript(*, skip_network: bool) -> list[str]:
    """构造一段真实宿主会发出的消息序列。

    顺序刻意按真实使用来：握手 → initialized 通知 → 列表 → 调用 → 错误路径。
    """
    lines: list[str] = [
        json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "1"}},
        }),
        # 通知：按协议**不该**有响应。少一个 id 就少一条响应，
        # 这里正是要验证"不会被当成请求回一个 null-id 响应"。
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "om_bridge_nonexistent_tool", "arguments": {}}}),
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "no/such/method"}),
        json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "om_bridge_list", "arguments": {}}}),
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "om_bridge_validate",
                               "arguments": {"solution": "minimax_h3.t2v",
                                             "prompt": "smoke"}}}),
    ]
    if not skip_network:
        lines.append(json.dumps({
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "om_bridge_check", "arguments": {}},
        }))
    # 故意塞一行坏 JSON：服务端必须回 -32700 并**继续存活**，
    # 而不是崩掉让宿主从此收不到任何响应。
    lines.append("this is definitely not json")
    lines.append(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP 协议冒烟测试")
    parser.add_argument("--env-file", dest="env_file", help="传给服务端的配置文件")
    parser.add_argument("--skip-network", action="store_true", help="跳过需要后端的工具调用")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env["PYTHONPATH"] = f"{_SRC}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env["PYTHONIOENCODING"] = "utf-8"
    if args.env_file:
        env["OMB_ENV_FILE"] = args.env_file
    # 进度行对 agent 无用，且会让宿主日志面板变吵 —— 但这里**不关**它，
    # 因为"进度是否泄漏到 stdout"正是被测项之一。服务端自己已经关掉了它。
    payload = "".join(line + "\n" for line in build_transcript(skip_network=args.skip_network))

    print(f"启动服务端：{sys.executable} -m om_bridge.interfaces.mcp")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "om_bridge.interfaces.mcp"],
            input=payload.encode("utf-8"),
            capture_output=True,
            env=env, cwd=str(_PROJECT_ROOT), timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        bad(f"服务端在 {args.timeout:.0f}s 内没有退出（可能卡在某个网络调用上）")
        return 1

    print(f"退出码 {proc.returncode}")
    print("\n1. stdout 是否只含合法 JSON")
    raw_lines = proc.stdout.decode("utf-8", "replace").splitlines()
    messages: list[dict] = []
    for index, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError as exc:
            bad(f"第 {index} 行不是合法 JSON：{exc} :: {line[:120]!r}")
            continue
        if not isinstance(parsed, dict):
            bad(f"第 {index} 行不是 JSON 对象")
            continue
        messages.append(parsed)
    if len(messages) == len([line for line in raw_lines if line.strip()]):
        ok(f"{len(messages)} 行全部是合法 JSON 对象")

    by_id = {message.get("id"): message for message in messages}

    print("\n2. 握手与能力声明")
    init = (by_id.get(1) or {}).get("result") or {}
    if init.get("serverInfo", {}).get("name"):
        ok(f"serverInfo.name = {init['serverInfo']['name']}")
    else:
        bad("initialize 未返回 serverInfo")
    if "tools" in (init.get("capabilities") or {}):
        ok("capabilities.tools 已声明")
    else:
        bad("capabilities.tools 未声明")

    print("\n3. 通知不产生响应")
    if None not in by_id:
        ok("notifications/initialized 没有产生响应")
    else:
        bad("通知产生了 id=null 的响应（宿主会把它当成协议错误）")

    print("\n4. 工具清单")
    tools = ((by_id.get(2) or {}).get("result") or {}).get("tools")
    if isinstance(tools, list) and tools:
        names = [tool["name"] for tool in tools]
        ok(f"{len(names)} 个工具：{', '.join(names)}")
        missing = [
            tool["name"] for tool in tools
            if not isinstance(tool.get("inputSchema"), dict)
        ]
        if missing:
            bad(f"这些工具缺少 inputSchema：{missing}")
        else:
            ok("每个工具都带 inputSchema")
    else:
        bad("tools/list 没有返回工具")

    print("\n5. 错误码契约")
    if (by_id.get(3) or {}).get("result") == {}:
        ok("ping 返回空结果")
    else:
        bad("ping 未返回空结果")
    if ((by_id.get(4) or {}).get("error") or {}).get("code") == -32602:
        ok("未知工具 -> -32602")
    else:
        bad("未知工具没有回 -32602")
    if ((by_id.get(5) or {}).get("error") or {}).get("code") == -32601:
        ok("未知方法 -> -32601")
    else:
        bad("未知方法没有回 -32601")
    parse_error_ids = [
        message.get("id") for message in messages
        if (message.get("error") or {}).get("code") == -32700
    ]
    if parse_error_ids:
        ok("坏 JSON -> -32700")
    else:
        bad("坏 JSON 没有回 -32700")

    print("\n6. 坏消息之后服务仍存活")
    if (by_id.get(9) or {}).get("result") == {}:
        ok("坏 JSON 之后的请求仍被正常响应")
    else:
        bad("坏 JSON 之后服务不再响应（宿主会认为服务已死）")

    print("\n7. 工具调用结果")
    listing = (by_id.get(6) or {}).get("result") or {}
    if listing.get("isError") is False and "content" in listing:
        ok("om_bridge_list 返回内容且未标错")
    else:
        bad("om_bridge_list 返回异常")

    validation = (by_id.get(7) or {}).get("result") or {}
    if "content" in validation:
        try:
            body = json.loads(validation["content"][0]["text"])
        except (ValueError, KeyError, IndexError):
            bad("om_bridge_validate 的 content 不是可解析的 JSON")
        else:
            ok(f"om_bridge_validate 返回结构化结论（ok={body.get('ok')}，"
               f"{body.get('total')} 项问题）")
    else:
        bad("om_bridge_validate 未返回 content")

    if not args.skip_network:
        probe = (by_id.get(8) or {}).get("result") or {}
        if "content" in probe:
            try:
                body = json.loads(probe["content"][0]["text"])
            except (ValueError, KeyError, IndexError):
                bad("om_bridge_check 的 content 不是可解析的 JSON")
            else:
                state = "可达" if body.get("reachable") else "不可达"
                ok(f"om_bridge_check：后端{state}，isError={probe.get('isError')}")
        else:
            bad("om_bridge_check 未返回 content")

    print("\n8. stderr 卫生")
    stderr_text = proc.stderr.decode("utf-8", "replace")
    if "Traceback" in stderr_text:
        bad("stderr 出现 traceback")
        print("      " + stderr_text.strip().splitlines()[-1][:200])
    else:
        ok("stderr 无 traceback")

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
