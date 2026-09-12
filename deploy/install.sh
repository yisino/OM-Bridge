#!/usr/bin/env bash
# =============================================================================
# OM-Bridge 安装脚本
# =============================================================================
#
# 两种安装模式，差别只在"依赖从哪来"：
#
#   --mode=path  （默认）零依赖安装。
#                不调用 pip、不建虚拟环境，只生成两个启动脚本，
#                通过 PYTHONPATH 直接指向源码树。
#                适用于**没有 pip 镜像的生产 GPU 主机** ——
#                "装不上依赖"不该成为"用不了"的原因。
#
#   --mode=venv  常规安装。
#                建虚拟环境并 `pip install -e .`，得到规范的 `om-bridge`
#                与 `om-bridge-mcp` 可执行文件。适用于有网络、需要长期维护的机器。
#
# 用法：
#     ./deploy/install.sh                        # 零依赖装到 ~/.local
#     ./deploy/install.sh --prefix=/opt/om-bridge
#     ./deploy/install.sh --mode=venv
#     ./deploy/install.sh --mode=venv --python=python3.13
#
# 卸载：./deploy/uninstall.sh --prefix=<同一个前缀>
# 验证：./deploy/verify.sh  --prefix=<同一个前缀>
# =============================================================================

set -euo pipefail

MODE="path"
PREFIX="${HOME}/.local"
PYTHON="${PYTHON:-python3}"
MIN_MINOR=10

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

die() { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }

for arg in "$@"; do
  case "$arg" in
    --mode=*)   MODE="${arg#*=}" ;;
    --prefix=*) PREFIX="${arg#*=}" ;;
    --python=*) PYTHON="${arg#*=}" ;;
    -h|--help)  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          die "未知参数：$arg" ;;
  esac
done

case "$MODE" in
  path|venv) ;;
  *) die "未知模式 ${MODE}（可选：path / venv）" ;;
esac

# ---------------------------------------------------------------------------
# 0. 前置检查
# ---------------------------------------------------------------------------
info "检查 Python"
command -v "$PYTHON" >/dev/null 2>&1 || die "找不到 ${PYTHON}；用 --python=<路径> 指定"

# 分两次取版本与解释器绝对路径：解释器路径可能含空格，
# 用空格分列的解析方式（一次调用打印两段）在这种机器上会静默错位。
PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
PY_EXE="$("$PYTHON" -c 'import sys; print(sys.executable)' 2>/dev/null || command -v "$PYTHON")"
PY_MAJOR="${PY_VERSION%%.*}"
PY_MINOR="${PY_VERSION##*.}"

[ "$PY_MAJOR" -ge 3 ] || die "需要 Python 3，当前是 ${PY_VERSION}"
if [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt "$MIN_MINOR" ]; then
  # 明确写出"为什么"：pkgutil.walk_packages 与 importlib.metadata 的
  # entry_points 处理在 3.10 以下行为不一致。与其在运行时踩坑，不如这里就停住。
  die "需要 Python 3.${MIN_MINOR} 及以上（当前 ${PY_VERSION}）：注册表的插件发现依赖该版本的 importlib.metadata 行为"
fi
ok "Python ${PY_VERSION} @ ${PY_EXE}"

[ -f "${PROJECT_ROOT}/pyproject.toml" ] || die "在 ${PROJECT_ROOT} 里找不到 pyproject.toml，安装脚本位置不对？"

mkdir -p "${PREFIX}/bin"

# ---------------------------------------------------------------------------
# 1a. 零依赖模式
# ---------------------------------------------------------------------------
if [ "$MODE" = "path" ]; then
  info "零依赖安装到 ${PREFIX}"

  LAUNCHER_DIR="${PREFIX}/share/om-bridge"
  mkdir -p "$LAUNCHER_DIR"

  # 启动脚本里写死绝对路径的源码目录与解释器。
  # 为什么不依赖 `pip install -e .`：那需要 setuptools + 可用的包索引，
  # 而目标机常常两者都没有。
  cat > "${PREFIX}/bin/om-bridge" <<EOF
#!/usr/bin/env bash
# OM-Bridge CLI（由 deploy/install.sh 生成的启动脚本，勿手工编辑）
export PYTHONPATH="${PROJECT_ROOT}/src\${PYTHONPATH:+:\$PYTHONPATH}"
exec "${PY_EXE}" -m om_bridge "\$@"
EOF

  cat > "${PREFIX}/bin/om-bridge-mcp" <<EOF
#!/usr/bin/env bash
# OM-Bridge MCP 服务端（stdio，由 deploy/install.sh 生成，勿手工编辑）
export PYTHONPATH="${PROJECT_ROOT}/src\${PYTHONPATH:+:\$PYTHONPATH}"
exec "${PY_EXE}" -m om_bridge.interfaces.mcp "\$@"
EOF

  chmod +x "${PREFIX}/bin/om-bridge" "${PREFIX}/bin/om-bridge-mcp"
  ok "已生成 ${PREFIX}/bin/om-bridge"
  ok "已生成 ${PREFIX}/bin/om-bridge-mcp"
  warn "该模式没有把源码复制出去：**不要移动或删除** ${PROJECT_ROOT}"

# ---------------------------------------------------------------------------
# 1b. 常规 venv 模式
# ---------------------------------------------------------------------------
else
  info "在 ${PREFIX} 建立虚拟环境"
  VENV_DIR="${PREFIX}/venv"
  [ -d "$VENV_DIR" ] || "$PY_EXE" -m venv "$VENV_DIR"
  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  python -m pip install --upgrade pip >/dev/null 2>&1 || warn "pip 自升级失败，继续"
  info "安装 om-bridge（editable）"
  python -m pip install -e "${PROJECT_ROOT}"
  ok "已安装到 ${VENV_DIR}"
  warn "可执行文件在 ${VENV_DIR}/bin，请把它加入 PATH，或用绝对路径调用"
fi

# ---------------------------------------------------------------------------
# 2. 配置
# ---------------------------------------------------------------------------
ENV_TARGET="${PREFIX}/share/om-bridge/om-bridge.env"
if [ -f "$ENV_TARGET" ]; then
  ok "配置已存在，未覆盖：${ENV_TARGET}"
else
  cp "${PROJECT_ROOT}/config/om-bridge.env.example" "$ENV_TARGET"
  chmod 600 "$ENV_TARGET"
  ok "已生成配置：${ENV_TARGET}（权限 600）"
  warn "请编辑它，至少把 OMB_COMFY_SERVER_URL 与 OMB_WORKSPACE 改成实际值"
fi

echo
info "安装完成。下一步："
cat <<EOF
  1) 编辑配置：        \$EDITOR ${ENV_TARGET}
  2) 体检：            OMB_ENV_FILE=${ENV_TARGET} ${PREFIX}/bin/om-bridge doctor
  3) 看一眼有哪些方案：${PREFIX}/bin/om-bridge list
  4) 首次生成：
       OMB_ENV_FILE=${ENV_TARGET} \\
       ${PREFIX}/bin/om-bridge generate -s minimax_h3.t2v -p "海边的灯塔，缓慢推镜"
EOF

if [ "$MODE" = "venv" ]; then
  echo
  warn "别忘了把 ${PREFIX}/venv/bin 加入 PATH：export PATH=\"${PREFIX}/venv/bin:\$PATH\""
fi
