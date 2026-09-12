#!/usr/bin/env bash
# =============================================================================
# OM-Bridge 安装后验证
# =============================================================================
#
# 逐项验证"这套安装真的能用"，并把**离线项**与**联网项**分开：
#
#   离线项（不依赖 ComfyUI 是否可达）
#     · 包能否 import、版本号
#     · 注册表发现：有哪些后端与方案
#     · 配置分层：能否加载、有无冲突
#     · 计算图构造：5 种 H3 形态能否物化，输出节点是否符合预期
#     · MCP 协议握手与工具清单
#
#   联网项（需要 ComfyUI 可达）
#     · 连通性与能力盘点
#     · 各模式就绪度
#
# 之所以要分开：量产线上"装好了但后端没起"和"根本没装好"是两种完全不同的
# 故障，混在一起报一个"验证失败"会浪费大量时间。
#
# 用法：
#     ./deploy/verify.sh                          # 用 PATH 里的 om-bridge
#     ./deploy/verify.sh --prefix=$HOME/.local
#     ./deploy/verify.sh --env-file=/path/to/om-bridge.env
#     ./deploy/verify.sh --skip-network           # 只跑离线项
# =============================================================================

set -uo pipefail

PREFIX=""
ENV_FILE=""
SKIP_NETWORK="no"
PASS=0
FAIL=0
SKIP=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

ok()   { printf '\033[32m✔\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf '\033[31m✖\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
skip() { printf '\033[33m—\033[0m %s\n' "$*"; SKIP=$((SKIP + 1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

for arg in "$@"; do
  case "$arg" in
    --prefix=*)       PREFIX="${arg#*=}" ;;
    --env-file=*)     ENV_FILE="${arg#*=}" ;;
    --skip-network)   SKIP_NETWORK="yes" ;;
    -h|--help)        sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                printf '未知参数：%s\n' "$arg" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# 定位可执行文件
# ---------------------------------------------------------------------------
if [ -n "$PREFIX" ] && [ -x "${PREFIX}/bin/om-bridge" ]; then
  CLI="${PREFIX}/bin/om-bridge"
  MCP="${PREFIX}/bin/om-bridge-mcp"
elif command -v om-bridge >/dev/null 2>&1; then
  CLI="$(command -v om-bridge)"
  MCP="$(command -v om-bridge-mcp 2>/dev/null || echo "${CLI%-om-bridge*}om-bridge-mcp")"
elif [ -f "${PROJECT_ROOT}/src/om_bridge/__main__.py" ]; then
  # 还没安装：直接用源码树跑，让"安装前先验一遍"成为可能
  PY="${PYTHON:-python3}"
  CLI="${PY} -m om_bridge"
  MCP="${PY} -m om_bridge.interfaces.mcp"
  printf '\033[33m!\033[0m 未找到已安装的 om-bridge，改用源码树：%s\n' "$PROJECT_ROOT"
  export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
else
  printf '\033[31m错误：\033[0m找不到 om-bridge，也没有可用的源码树。用 --prefix= 指定安装前缀。\n' >&2
  exit 2
fi

[ -n "$ENV_FILE" ] && export OM_BRIDGE_ENV_FILE="$ENV_FILE"
printf 'CLI: %s\n' "$CLI"
[ -n "$ENV_FILE" ] && printf '配置: %s\n' "$ENV_FILE"

# ---------------------------------------------------------------------------
# 1. 基本可执行性
# ---------------------------------------------------------------------------
head_ "1. 基本可执行性"
if VER="$($CLI --version 2>&1)"; then
  ok "版本： ${VER}"
else
  bad "--version 失败： ${VER}"
fi

if $CLI list --json >"${WORKDIR}/list.json" 2>"${WORKDIR}/list.err"; then
  # 不依赖 jq：用 python 解析既准确又不引入依赖
  if PY_JSON="$(command -v python3 || command -v python)"; then
    RESULT="$("$PY_JSON" - "${WORKDIR}/list.json" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(len(data["backends"]), len(data["solutions"]), len(data["aliases"]), len(data["load_errors"]))
PYEOF
)"
    read -r NB NS NA NE <<<"$RESULT"
    ok "注册表：${NB} 个后端 / ${NS} 个方案 / ${NA} 个别名"
    if [ "$NE" -gt 0 ]; then
      bad "有 ${NE} 项加载问题（用 om-bridge list 查看细节）"
    else
      ok "无插件加载问题"
    fi
  else
    skip "没有可用的 python 来解析 JSON，跳过注册表统计"
  fi
else
  bad "list --json 失败：$(head -c 300 "${WORKDIR}/list.err")"
fi

# ---------------------------------------------------------------------------
# 2. 配置分层
# ---------------------------------------------------------------------------
head_ "2. 配置"
if $CLI config report --json >"${WORKDIR}/cfg.json" 2>"${WORKDIR}/cfg.err"; then
  ok "配置可加载"
  if PY_JSON="$(command -v python3 || command -v python)"; then
    "$PY_JSON" - "${WORKDIR}/cfg.json" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for warning in data.get("warnings") or []:
    print("WARN " + warning)
unknown = data.get("unregistered_env") or []
print("UNREG %d" % len(unknown))
PYEOF
  fi
else
  bad "config report 失败：$(head -c 300 "${WORKDIR}/cfg.err")"
fi

# ---------------------------------------------------------------------------
# 3. 计算图构造（离线核心能力）
# ---------------------------------------------------------------------------
head_ "3. 计算图构造（离线）"
# 期望的输出节点编号来自已核对过的模板。这里断言它们，是因为
# "输出节点算错了"不会让作业失败，只会让产物收集不到 —— 一个极隐蔽的故障。
check_graph() {
  local mode="$1" expected_node="$2"; shift 2
  local out="${WORKDIR}/graph_${mode}.json"
  if $CLI graph -s "minimax_h3.${mode}" -p "verify" "$@" -o "$out" --json >"${WORKDIR}/g.out" 2>"${WORKDIR}/g.err"; then
    if grep -q "\"output_node\": \"${expected_node}\"" "${WORKDIR}/g.out"; then
      ok "minimax_h3.${mode}：输出节点 ${expected_node}"
    else
      bad "minimax_h3.${mode}：输出节点不是 ${expected_node}（$(tr -d '\n' <"${WORKDIR}/g.out" | head -c 200)）"
    fi
  else
    # --json 模式下错误在 **stdout**（结构化错误体），stderr 往往是空的；
    # 只看 stderr 会打出一句没有信息量的"物化失败："，排查无从下手。
    local detail
    detail="$(head -c 300 "${WORKDIR}/g.err")"
    [ -n "$detail" ] || detail="$(head -c 300 "${WORKDIR}/g.out")"
    bad "minimax_h3.${mode} 物化失败：${detail}"
  fi
}

# 媒体一律走 --media-remote（引用"后端素材目录里已有的文件"）：
# 物化语义只关心**图的形状**，引入本地文件就会多一个与结论无关的失败面 ——
# "ref_a.png 在验证机上不存在"会让输出节点的正确性根本没法被检验。
check_graph t2v   15
check_graph i2v   16 --media-remote first_frame=verify_first.png
check_graph flf2v 17 --media-remote first_frame=verify_first.png --media-remote last_frame=verify_last.png
check_graph ref2v 16 --media-remote reference_image=verify_ref.png
check_graph ref2v 17 --media-remote reference_image=verify_ref.png --media-remote reference_image=verify_ref2.png

# ---------------------------------------------------------------------------
# 4. 参数校验的负向路径
# ---------------------------------------------------------------------------
head_ "4. 参数校验（负向路径）"
# 校验必须能**拦住错误**。一个只会说"通过"的校验器比没有更糟。
if $CLI validate -s minimax_h3.t2v -p x --width 100 --json >"${WORKDIR}/v1.json" 2>&1; then
  if grep -q '"ok": false' "${WORKDIR}/v1.json"; then
    ok "非 32 倍数宽度被拦下"
  else
    bad "宽度 100 未被拦下（校验器失效？）"
  fi
else
  bad "validate 命令本身失败：$(head -c 300 "${WORKDIR}/v1.json")"
fi

if $CLI validate -s minimax_h3.i2v -p x --json >"${WORKDIR}/v2.json" 2>&1; then
  if grep -q 'media.missing_role' "${WORKDIR}/v2.json"; then
    ok "i2v 缺首帧被拦下"
  else
    bad "i2v 缺首帧未被拦下"
  fi
else
  bad "validate 命令本身失败：$(head -c 300 "${WORKDIR}/v2.json")"
fi

if $CLI validate -s minimax_h3.t2v -p x --json >"${WORKDIR}/v3.json" 2>&1 \
   && grep -q '"ok": true' "${WORKDIR}/v3.json"; then
  ok "合法请求通过校验"
else
  bad "合法请求反而没通过：$(head -c 300 "${WORKDIR}/v3.json")"
fi

# ---------------------------------------------------------------------------
# 5. MCP 协议
# ---------------------------------------------------------------------------
head_ "5. MCP 协议（stdio）"
if MCP_LIST="$($MCP --list-tools 2>&1)"; then
  COUNT="$(printf '%s\n' "$MCP_LIST" | grep -c ':')"
  ok "工具清单可生成（${COUNT} 个）"
else
  bad "--list-tools 失败：$(printf '%s' "$MCP_LIST" | head -c 300)"
  COUNT=0
fi

# 真实握手：只要 stdout 上出现一行非 JSON，宿主就会报 invalid JSON，
# 因此这里顺带验证"stdout 只放协议消息"这条硬约束。
HANDSHAKE="$(printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | $MCP 2>"${WORKDIR}/mcp.err")"
LINE_COUNT="$(printf '%s\n' "$HANDSHAKE" | grep -c .)"
if [ "$LINE_COUNT" -eq 2 ] \
   && printf '%s' "$HANDSHAKE" | grep -q '"serverInfo"' \
   && printf '%s' "$HANDSHAKE" | grep -q '"tools"'; then
  ok "握手与 tools/list 正常（2 条响应，无多余输出）"
else
  bad "握手异常：期望 2 条响应，实际 ${LINE_COUNT} 条"
  printf '%s\n' "$HANDSHAKE" | head -c 400
fi

if [ -s "${WORKDIR}/mcp.err" ]; then
  # stderr 有内容是正常的（诊断日志），但**不能**有 traceback
  if grep -qi 'traceback' "${WORKDIR}/mcp.err"; then
    bad "MCP stderr 出现 traceback"
  else
    ok "MCP stderr 只有日志，无异常栈"
  fi
else
  ok "MCP stderr 为空"
fi

# ---------------------------------------------------------------------------
# 6. 联网探测
# ---------------------------------------------------------------------------
head_ "6. 后端连通性（联网）"
if [ "$SKIP_NETWORK" = "yes" ]; then
  skip "按要求跳过联网探测"
else
  if $CLI probe --json >"${WORKDIR}/probe.json" 2>"${WORKDIR}/probe.err"; then
    if grep -q '"reachable": true' "${WORKDIR}/probe.json"; then
      ok "后端可达"
      if PY_JSON="$(command -v python3 || command -v python)"; then
        # 先落成中间文件再逐行读取，而不是把 python 的输出直接管进 while：
        # 管道会把 while 放进子 shell，于是里面的计数（PASS/FAIL）出了循环就没了 ——
        # 表现为"明明失败了却报全部通过"，是最不该在验证脚本里出现的一类 bug。
        "$PY_JSON" - "${WORKDIR}/probe.json" >"${WORKDIR}/ready.txt" <<'PYEOF'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for mode, entry in (data.get("readiness") or {}).items():
    missing = entry.get("missing") or []
    print("%s|%s|%d" % (mode, "ready" if entry.get("ready") else "missing", len(missing)))
PYEOF
        while IFS='|' read -r mode state count; do
          [ -z "$mode" ] && continue
          if [ "$state" = "ready" ]; then
            ok "模式 ${mode}：就绪"
          else
            bad "模式 ${mode}：缺 ${count} 项"
          fi
        done <"${WORKDIR}/ready.txt"
      fi
    else
      bad "后端不可达（检查 OM_BRIDGE_COMFY_SERVER_URL 与网络/隧道）"
      head -c 400 "${WORKDIR}/probe.json"
      echo
    fi
  else
    bad "probe 命令失败：$(head -c 300 "${WORKDIR}/probe.err")"
  fi
fi

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
head_ "汇总"
printf '通过 %d 项，失败 %d 项，跳过 %d 项\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
  printf '\033[31m验证未全部通过。\033[0m先看上面标 ✖ 的项与它打印的原始输出。\n'
  exit 1
fi
printf '\033[32m全部通过。\033[0m\n'
