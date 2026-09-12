#!/usr/bin/env bash
# =============================================================================
# 开发环境一键就绪
# =============================================================================
#
# 面向**改代码的人**，不是部署。做的事情：
#   1. 检查 Python 版本
#   2. 建 .venv（若不存在）
#   3. editable 安装 + 开发依赖
#   4. 跑一遍离线自检（源码树回归 + MCP 冒烟，不依赖后端是否可达）
#
# 用法：
#     ./scripts/bootstrap.sh
#     ./scripts/bootstrap.sh --no-deps      # 不装开发依赖（无网络时）
#     ./scripts/bootstrap.sh --check-only   # 只跑自检
#
# 与 deploy/install.sh 的分工：
#   deploy/install.sh  面向**运行环境**，默认零依赖（不碰 pip）。
#   scripts/bootstrap.sh 面向**开发环境**，需要 pip 与网络。
# 两者刻意分开，因为"生产机上装不上 pip"是常态，而开发机上不装测试依赖寸步难行。
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$PROJECT_ROOT"

INSTALL_DEPS="yes"
CHECK_ONLY="no"
PYTHON="${PYTHON:-python3}"

die() { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✔\033[0m %s\n' "$*"; }

for arg in "$@"; do
  case "$arg" in
    --no-deps)    INSTALL_DEPS="no" ;;
    --check-only) CHECK_ONLY="yes" ;;
    --python=*)   PYTHON="${arg#*=}" ;;
    -h|--help)    sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            die "未知参数：$arg" ;;
  esac
done

VENV="${PROJECT_ROOT}/.venv"

fatal=0
check() {
  local label="$1"; shift
  if "$@"; then ok "$label"; else printf '\033[31m✖\033[0m %s\n' "$label"; fatal=$((fatal + 1)); fi
}

echo "项目根目录：$PROJECT_ROOT"
echo

# ---------------------------------------------------------------------------
# 1. 环境准备
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = "no" ]; then
  info "检查 Python"
  command -v "$PYTHON" >/dev/null 2>&1 || die "找不到 ${PYTHON}（可用 --python= 指定）"
  PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  case "$PY_VERSION" in
    3.10|3.11|3.12|3.13|3.14) ok "Python ${PY_VERSION}" ;;
    *) die "本项目要求 Python 3.10+，当前 ${PY_VERSION}" ;;
  esac

  if [ ! -d "$VENV" ]; then
    info "创建虚拟环境 ${VENV}"
    "$PYTHON" -m venv "$VENV"
  fi
  # shellcheck disable=SC1091
  source "${VENV}/bin/activate"

  info "安装 om-bridge（editable）"
  python -m pip install --upgrade pip >/dev/null 2>&1 || true
  python -m pip install -e . >/dev/null
  ok "om-bridge 已安装（含 dev 依赖，见 pyproject.toml 的 [project.optional-dependencies]）"

  info "安装开发依赖（如果声明了）"
  if python -m pip install -e ".[dev]" >/dev/null 2>&1; then
    ok "dev 依赖已安装"
  else
    printf '\033[33m!\033[0m dev 依赖安装失败（无网络？）。自检里需要 pytest 的项会被跳过。\n'
  fi
else
  info "--check-only：跳过环境准备"
  if [ -d "$VENV" ]; then
    # shellcheck disable=SC1091
    source "${VENV}/bin/activate"
  fi
fi

# ---------------------------------------------------------------------------
# 2. 离线自检
# ---------------------------------------------------------------------------
echo
info "2. 编译检查（全部模块）"
if python -m compileall -q src >/dev/null; then
  ok "src 下所有模块语法正确"
else
  printf '\033[31m✖\033[0m 存在语法错误\n'; fatal=$((fatal + 1))
fi

info "3. 注册表与计算图构造（离线）"
if command -v om-bridge >/dev/null 2>&1; then
  if om-bridge list >/dev/null 2>&1; then
    ok "注册表可发现后端与方案"
  else
    printf '\033[31m✖\033[0m 注册表发现失败\n'; fatal=$((fatal + 1))
  fi
  # 5 种形态构造一遍，并断言输出节点编号 —— 编构造错误不会让作业失败，
  # 只会让产物收集不到，属于最隐蔽的一类缺陷。
  expect_node() {
    local mode="$1" node="$2"; shift 2
    local out
    if out="$(om-bridge graph -s "minimax_h3.${mode}" -p bootstrap "$@" --json 2>&1)" \
       && printf '%s' "$out" | grep -q "\"output_node\": \"${node}\""; then
      ok "minimax_h3.${mode} 输出节点 ${node}"
    else
      printf '\033[31m✖\033[0m minimax_h3.%s 输出节点不是 %s\n' "$mode" "$node"
      printf '      %s\n' "$(printf '%s' "$out" | tr '\n' ' ' | head -c 180)"
      fatal=$((fatal + 1))
    fi
  }
  expect_node t2v   15
  expect_node i2v   16 --first-image a.png
  expect_node flf2v 17 --first-image a.png --last-image b.png
  expect_node ref2v 16 --ref-image a.png
  expect_node ref2v 17 --ref-image a.png --ref-image b.png
else
  printf '\033[33m!\033[0m om-bridge 不在 PATH，跳过（用 source .venv/bin/activate 激活后重试）\n'
fi

info "4. pytest"
if command -v pytest >/dev/null 2>&1; then
  if [ -d tests ]; then
    if pytest -q; then ok "pytest 通过"; else printf '\033[31m✖\033[0m pytest 失败\n'; fatal=$((fatal + 1)); fi
  else
    printf '\033[33m!\033[0m 还没有 tests/ 目录，跳过\n'
  fi
else
  printf '\033[33m!\033[0m 没有 pytest，跳过（pip install -e ".[dev]"）\n'
fi

info "5. MCP 协议冒烟（离线）"
if [ -f scripts/mcp-smoke.py ]; then
  if python scripts/mcp-smoke.py --skip-network >/dev/null 2>&1; then
    ok "MCP 协议全部路径通过"
  else
    printf '\033[31m✖\033[0m MCP 冒烟测试失败，运行 python scripts/mcp-smoke.py 看细节\n'
    fatal=$((fatal + 1))
  fi
else
  printf '\033[33m!\033[0m 找不到 scripts/mcp-smoke.py\n'
fi

echo
if [ "$fatal" -gt 0 ]; then
  printf '\033[31m有 %d 项失败。\033[0m\n' "$fatal"
  exit 1
fi
printf '\033[32m开发环境就绪。\033[0m\n'
echo
echo "常用命令（先 source .venv/bin/activate）："
echo "  om-bridge list                    # 有哪些后端与方案"
echo "  om-bridge doctor                  # 连通性与就绪度（需要后端可达）"
echo "  om-bridge generate -s minimax_h3.t2v -p \"...\""
echo "  python scripts/gen-workflow.sh    # 物化计算图给外部框架用"
echo "  pytest -q                         # 跑测试"
