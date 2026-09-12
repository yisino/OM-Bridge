#!/usr/bin/env bash
# =============================================================================
# OM-Bridge 卸载脚本
# =============================================================================
#
# ⚠ 会删除文件，因此默认**只做预演**（打印将要删除什么），
#   必须显式加 --yes 才真正执行。
#
# 用法：
#     ./deploy/uninstall.sh --prefix=$HOME/.local           # 预演
#     ./deploy/uninstall.sh --prefix=$HOME/.local --yes     # 执行
#
# 会被删除/移走的东西：
#   <prefix>/bin/om-bridge              安装脚本生成的启动脚本
#   <prefix>/bin/om-bridge-mcp          同上
#   <prefix>/share/om-bridge/           启动脚本与配置所在目录
#   <prefix>/venv                       **仅 venv 模式**：整个虚拟环境
#
# 配置文件的处理是**移走而不是删除**：它很可能被人工编辑过（密钥、地址），
# 而不是"重新生成一份就行"的产物。移走比删除安全，成本只是留一个备份文件。
# =============================================================================

set -euo pipefail

PREFIX="${HOME}/.local"
CONFIRM="no"
REMOVE_VENV="ask"

die() { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }

for arg in "$@"; do
  case "$arg" in
    --prefix=*)  PREFIX="${arg#*=}" ;;
    --yes|-y)    CONFIRM="yes" ;;
    --keep-venv) REMOVE_VENV="no" ;;
    --drop-venv) REMOVE_VENV="yes" ;;
    -h|--help)   sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "未知参数：$arg" ;;
  esac
done

[ -d "$PREFIX" ] || die "前缀目录不存在：${PREFIX}"

BIN_CLI="${PREFIX}/bin/om-bridge"
BIN_MCP="${PREFIX}/bin/om-bridge-mcp"
SHARE_DIR="${PREFIX}/share/om-bridge"
VENV_DIR="${PREFIX}/venv"

# ---------------------------------------------------------------------------
# 收集待处理项
# ---------------------------------------------------------------------------
targets=()
for path in "$BIN_CLI" "$BIN_MCP" "$SHARE_DIR"; do
  [ -e "$path" ] && targets+=("$path")
done

drop_venv="no"
if [ -d "$VENV_DIR" ]; then
  case "$REMOVE_VENV" in
    yes) drop_venv="yes" ;;
    no)  drop_venv="no" ;;
    ask)
      # venv 里只有依赖，没有人工编辑过的内容，删掉是可以接受的 ——
      # 但它体积可能不小，因此让调用方自己决定（--drop-venv / --keep-venv）。
      drop_venv="no"
      warn "检测到虚拟环境 ${VENV_DIR}：默认**保留**（要一并删除请加 --drop-venv）"
      ;;
  esac
fi
[ "$drop_venv" = "yes" ] && targets+=("$VENV_DIR")

if [ ${#targets[@]} -eq 0 ]; then
  info "在 ${PREFIX} 下没有找到 OM-Bridge 的安装痕迹"
  exit 0
fi

# ---------------------------------------------------------------------------
# 预演
# ---------------------------------------------------------------------------
echo "将要处理以下路径（前缀：${PREFIX}）："
for path in "${targets[@]}"; do
  printf '  · %s\n' "$path"
done

if [ -f "${SHARE_DIR}/om-bridge.env" ]; then
  echo
  echo "配置文件会被**移到备份**，不会删除："
  printf '  · %s  ->  %s.bak.%s\n' \
    "${SHARE_DIR}/om-bridge.env" "${SHARE_DIR}/om-bridge.env" "$(date +%Y%m%d%H%M%S)"
fi

if [ "$CONFIRM" != "yes" ]; then
  echo
  warn "这是预演。确认无误后加 --yes 实际执行。"
  exit 0
fi

# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
STAMP="$(date +%Y%m%d%H%M%S)"

if [ -f "${SHARE_DIR}/om-bridge.env" ]; then
  mv "${SHARE_DIR}/om-bridge.env" "${SHARE_DIR}/om-bridge.env.bak.${STAMP}"
  info "配置已备份到 ${SHARE_DIR}/om-bridge.env.bak.${STAMP}"
  # 备份里的密钥不该有全局可读权限
  chmod 600 "${SHARE_DIR}/om-bridge.env.bak.${STAMP}" || true
fi

for path in "$BIN_CLI" "$BIN_MCP"; do
  [ -e "$path" ] && rm -f "$path" && info "已删除 ${path}"
done

if [ "$drop_venv" = "yes" ]; then
  rm -rf "$VENV_DIR"
  info "已删除虚拟环境 ${VENV_DIR}"
fi

# share/om-bridge 里此时只剩备份文件与启动脚本目录，逐项清理
if [ -d "$SHARE_DIR" ]; then
  find "$SHARE_DIR" -maxdepth 1 -type f ! -name '*.bak.*' -delete 2>/dev/null || true
  rmdir "$SHARE_DIR" 2>/dev/null && info "已删除 ${SHARE_DIR}" \
    || warn "${SHARE_DIR} 非空（可能留下备份文件），未删除"
fi

echo
info "卸载完成。若曾把 ${PREFIX}/bin 或 venv/bin 写进 PATH，记得从 shell 配置里移除。"
