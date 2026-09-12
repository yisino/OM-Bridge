#!/usr/bin/env bash
# =============================================================================
# 物化 H3 计算图 —— 给"只认文件路径 + 节点 ID"的外部框架使用
# =============================================================================
#
# 背景
# ----
# 有些外部框架（如 OpenMontage 的原生 H3 通道）拿不到内存里的图，
# 只接受「工作流 JSON 文件路径 + 输出节点 ID」。
#
# 旧做法是**预先存一份** JSON 并把路径写进配置。代价是：一旦构图代码改了
# （新增节点、改连线、调参数），预存的那份就悄悄过期，
# 而表现为"配置显示已就绪，跑起来却对不上"。
#
# 现在改为**按需现场物化**：这个脚本每次调用都重新构造，落盘即最新。
# 输出节点编号一并写进 manifest，外部框架读一个文件就够。
#
# 落盘目录由 CLI 自己决定（配置 solution.minimax_h3.graph_dir，
# 未配置则为 <工作区>/var/graphs）—— 这样调用方不必再解析一遍配置，
# 也就不会出现"文件写到 A、框架去 B 找"这类问题。
#
# 用法
# ----
#     ./scripts/gen-workflow.sh                       # 全部四种形态
#     ./scripts/gen-workflow.sh --modes=t2v,ref2v
#     ./scripts/gen-workflow.sh --out=/tmp/h3         # 指定目录
#     ./scripts/gen-workflow.sh --prompt-file=p.txt
#     ./scripts/gen-workflow.sh --dry-run             # 只打印，不落盘
#
# 产物
# ----
#     <目录>/minimax_h3_<模式>_api.json    各形态的计算图（API 格式）
#     <目录>/manifest.json                汇总：路径 / 输出节点 / 指纹
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODES="t2v,i2v,flf2v,ref2v"
OUT=""
PROMPT=""
DRY_RUN="no"
PLACEHOLDER="placeholder_ref.png"

die() { printf '\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '   \033[32m✔\033[0m %s\n' "$*"; }
bad()  { printf '   \033[31m✖\033[0m %s\n' "$*" >&2; }

for arg in "$@"; do
  case "$arg" in
    --out=*)         OUT="${arg#*=}" ;;
    --modes=*)       MODES="${arg#*=}" ;;
    --prompt=*)      PROMPT="${arg#*=}" ;;
    --prompt-file=*) PROMPT="$(cat "${arg#*=}")" ;;
    --placeholder=*) PLACEHOLDER="${arg#*=}" ;;
    --dry-run)       DRY_RUN="yes" ;;
    -h|--help)       sed -n '2,38p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)               die "未知参数：$arg" ;;
  esac
done

# ---------------------------------------------------------------------------
# 定位 CLI：优先已安装的，否则退回源码树（让"没装也能用"成立）
# ---------------------------------------------------------------------------
if command -v om-bridge >/dev/null 2>&1; then
  # $OM_BRIDGE 故意不加引号以便词分割 —— 源码树模式下它是
  # "python3 -m om_bridge" 这种带空格的形式。
  OM_BRIDGE="om-bridge"
elif [ -f "${PROJECT_ROOT}/src/om_bridge/__main__.py" ]; then
  OM_BRIDGE="${PYTHON:-python3} -m om_bridge"
  export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
  info "未找到已安装的 om-bridge，改用源码树：${PROJECT_ROOT}"
else
  die "找不到 om-bridge，也没有可用的源码树"
fi

# shellcheck disable=SC2086
PY_JSON="$(command -v python3 || command -v python || true)"
[ -n "$PY_JSON" ] || die "需要一个 python 来解析 CLI 的 JSON 输出"

if [ -n "$OUT" ] && [ "$OUT" != "-" ]; then
  mkdir -p "$OUT"
  OUT_ABS="$(cd "$OUT" && pwd)"
fi

if [ -z "$PROMPT" ]; then
  # 占位提示词：物化阶段关心的只是"图长什么样"。提示词全文写在 JSON 里，
  # 外部框架有需要可以替换对应节点的文本。
  PROMPT="OM-Bridge 占位提示词：请在提交前替换为实际内容。"
fi

MANIFEST_LINES=""
MANIFEST_DIR=""
FAILED=0
COUNT=0

IFS=',' read -r -a MODE_LIST <<<"$MODES"
for mode in "${MODE_LIST[@]}"; do
  [ -z "$mode" ] && continue
  args=(-s "minimax_h3.${mode}" -p "$PROMPT" --json)

  # 每种形态补齐**能满足构建器最低要求**的素材名。
  #
  # ⚠ 这里给的是占位名，不是真实文件：物化不校验文件存在性
  #   （图里 LoadImage 的取值是"后端 input 目录的枚举"而非路径），
  #   真正提交前会由后端静态校验拦下不存在的名字。
  #   之所以必须给，是因为 i2v/flf2v/ref2v 缺素材时构建器**拒绝构图** ——
  #   这是有意的：一张缺了首帧的 i2v 图没有任何意义，不该被物化出来。
  case "$mode" in
    i2v)   args+=(--first-image "$PLACEHOLDER") ;;
    flf2v) args+=(--first-image "$PLACEHOLDER" --last-image "$PLACEHOLDER") ;;
    ref2v) args+=(--ref-image "$PLACEHOLDER") ;;
  esac

  if [ "$OUT" = "-" ]; then
    args+=(-o -)          # 打到 stdout（人看或管道用）
  elif [ -n "$OUT" ]; then
    args+=(-o "${OUT_ABS}/minimax_h3_${mode}_api.json")
  fi
  [ "$DRY_RUN" = "yes" ] && args+=(--no-preflight)

  # shellcheck disable=SC2086
  if ! RESULT="$($OM_BRIDGE graph "${args[@]}" 2>&1)"; then
    bad "${mode}：$(printf '%s' "$RESULT" | tr '\n' ' ' | head -c 200)"
    FAILED=$((FAILED + 1))
    continue
  fi

  if [ "$OUT" = "-" ]; then
    printf '%s\n' "$RESULT"
    COUNT=$((COUNT + 1))
    continue
  fi

  PARSED="$("$PY_JSON" -c '
import json, sys
data = json.loads(sys.stdin.read())
print("%s|%s|%s|%s" % (data.get("path") or "", data.get("output_node") or "",
                       data.get("nodes") or 0, data.get("fingerprint") or ""))
' <<<"$RESULT")"
  IFS='|' read -r gpath gnode gnodes gfp <<<"$PARSED"

  ok "$(printf '%-6s 输出节点=%-3s 节点数=%-3s %s' "$mode" "$gnode" "$gnodes" "$gpath")"
  COUNT=$((COUNT + 1))
  [ -z "$MANIFEST_DIR" ] && MANIFEST_DIR="$(dirname "$gpath")"
  # manifest 只写外部框架**真正需要**的字段。把整张图的信息也塞进来
  # 会诱使调用方去解析它 —— 那条路的终点是框架对内部结构产生依赖，
  # 而我们正在消除的正是这种依赖。
  MANIFEST_LINES="${MANIFEST_LINES}    \"${mode}\": {\"path\": \"${gpath}\", \"output_node\": \"${gnode}\", \"nodes\": ${gnodes:-0}, \"fingerprint\": \"${gfp}\"},
"
done

# ---------------------------------------------------------------------------
# 写 manifest
# ---------------------------------------------------------------------------
if [ "$OUT" != "-" ] && [ "$COUNT" -gt 0 ] && [ -n "$MANIFEST_DIR" ]; then
  if [ -n "$OUT" ]; then
    MANIFEST="${OUT_ABS}/manifest.json"
  else
    # 没指定 --out 时目录由 CLI 决定，这里取第一个成功形态的所在目录
    MANIFEST="${MANIFEST_DIR}/manifest.json"
  fi
  {
    printf '{\n  "generated_by": "scripts/gen-workflow.sh",\n  "solutions": {\n'
    printf '%s' "$MANIFEST_LINES" | sed '$ s/,$//'
    printf '  }\n}\n'
  } >"$MANIFEST"
  info "manifest：${MANIFEST}"
fi

echo
if [ "$FAILED" -gt 0 ]; then
  printf '\033[31m有 %d 个形态失败。\033[0m' "$FAILED"
  echo "先看上面标 ✖ 的那一行输出的原因。"
  exit 1
fi
info "完成（${COUNT} 个形态）。外部框架只需读 manifest.json 即可拿到「路径 + 输出节点」。"
echo "   这些图是现场生成的，因此永远与当前代码一致 —— 部署时不需要预存。"
