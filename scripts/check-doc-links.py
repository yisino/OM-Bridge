#!/usr/bin/env python3
"""检查文档里的相对链接是否都指向真实存在的文件。

**为什么需要它？** 文档最容易腐坏的地方不是正文，而是链接：重构目录后
`docs/architecture.md` 里的 `../solutions/minimax-h3.md` 会静默变成死链，
而没有任何工具会报错。这类腐坏积累多了，文档就没人信了。

规则（刻意从宽，避免噪音）：
* 只看**相对链接**（`http(s)://`、`mailto:` 跳过）。
* 只看**同目录内**的目标（跨仓库/挂载点的链接不做校验）。
* `#anchor` 一律剥掉再判断 —— 校验锚点需要解析标题生成规则，收益低于误报成本。
* 允许指向目录。

退出码：0 全部存在；1 存在死链。

用法::

    python3 scripts/check-doc-links.py [--root .] [-v]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Markdown 行内链接与图片：`[文字](目标)` / `![文字](目标)`
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

# 这些前缀不当作本地路径校验
SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:")

# 扫描的文档文件（相对 root）
SCAN_GLOBS = ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "docs/**/*.md")


def iter_documents(root: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in SCAN_GLOBS:
        found.extend(root.glob(pattern))
    return sorted({p for p in found if p.is_file()})


def check_file(path: Path, root: Path, verbose: bool) -> list[str]:
    problems: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")

    for lineno, line in enumerate(text.splitlines(), start=1):
        for raw_target in LINK_RE.findall(line):
            target = raw_target.strip()
            if not target or target.startswith(SKIP_PREFIXES):
                continue

            # 剥掉锚点与查询串：`foo.md#section` → `foo.md`
            target = target.split("#", 1)[0].split("?", 1)[0]
            if not target:
                continue  # 纯锚点，如 `#标题`

            # 只校验相对路径。绝对路径（/etc/...）视为"指向本机某处"，
            # 不在仓库范围内，跳过以免误报。
            if target.startswith("/"):
                continue

            resolved = (path.parent / target).resolve()
            try:
                exists = resolved.exists()
            except OSError:
                exists = False

            if exists:
                if verbose:
                    rel = path.relative_to(root)
                    print(f"  ok   {rel}:{lineno} → {target}")
            else:
                problems.append(
                    f"{path.relative_to(root)}:{lineno} → 链接目标不存在: {target}"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查文档相对链接是否有效")
    parser.add_argument("--root", default=".", help="仓库根目录（默认当前目录）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每个成功的链接")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    documents = iter_documents(root)
    if not documents:
        print("没有找到任何文档文件，检查 SCAN_GLOBS 配置。", file=sys.stderr)
        return 1

    problems: list[str] = []
    for doc in documents:
        problems.extend(check_file(doc, root, args.verbose))

    if problems:
        print(f"发现 {len(problems)} 个失效链接：", file=sys.stderr)
        for item in problems:
            print(f"  ✗ {item}", file=sys.stderr)
        print("\n提示：重构目录后请同步更新文档里的相对链接。", file=sys.stderr)
        return 1

    print(f"文档引用检查通过（扫描 {len(documents)} 个文件，无失效链接）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
