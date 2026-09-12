# =============================================================================
# OM-Bridge Makefile
# =============================================================================
# 设计取向：所有目标都**不需要**先安装本项目（走 PYTHONPATH=src），
# 因此在一台干净的机器上 clone 下来就能 `make test` / `make doctor`。
# 需要真正安装时用 `make install`（它会调 deploy/install.sh）。
# =============================================================================

SHELL := /bin/bash
PYTHON ?= python3
SRC := src
export PYTHONPATH := $(CURDIR)/$(SRC)

PROG := om-bridge
ENV_FILE ?=
ENV_ARG := $(if $(ENV_FILE),--env-file=$(ENV_FILE),)

.DEFAULT_GOAL := help
.PHONY: help install install-venv uninstall verify test test-fast lint fmt \
        doctor list describe probe graph clean distclean check docs-check

# ---------------------------------------------------------------------------
help:  ## 显示本帮助
	@printf 'OM-Bridge —— 可用目标：\n\n'
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@printf '\n变量：PYTHON=%s  ENV_FILE=%s\n' '$(PYTHON)' '$(if $(ENV_FILE),$(ENV_FILE),(未设置))'
	@printf '示例：make doctor ENV_FILE=./config/om-bridge.env\n'

# ---------------------------------------------------------------------------
# 安装 / 卸载
# ---------------------------------------------------------------------------
install:  ## 零依赖安装（不调用 pip），默认前缀 ~/.local
	./deploy/install.sh

install-venv:  ## 常规 venv 安装（pip install -e .）
	./deploy/install.sh --mode=venv

uninstall:  ## 卸载（默认预演，加 YES=1 才真删）
	./deploy/uninstall.sh --prefix=$(HOME)/.local $(if $(YES),--yes,)

verify:  ## 验证安装与连通性（SKIP_NET=1 可离线）
	./deploy/verify.sh --prefix=$(HOME)/.local $(if $(SKIP_NET),--skip-network,)

# ---------------------------------------------------------------------------
# 测试与检查
# ---------------------------------------------------------------------------
test:  ## 跑全部测试（不需要后端在线）
	$(PYTHON) -m pytest -q

test-fast:  ## 只跑不依赖网络的测试
	$(PYTHON) -m pytest -q -m 'not network'

lint:  ## ruff 静态检查
	$(PYTHON) -m ruff check $(SRC) scripts tests

fmt:  ## ruff 自动修复与格式化
	$(PYTHON) -m ruff check --fix $(SRC) scripts tests
	$(PYTHON) -m ruff format $(SRC) scripts tests

# ---------------------------------------------------------------------------
# 运行时自省（都需要能 import om_bridge，走 PYTHONPATH）
# ---------------------------------------------------------------------------
doctor:  ## 体检（配置 → 注册表 → 连通性 → 就绪度）
	$(PYTHON) -m om_bridge $(ENV_ARG) doctor

list:  ## 列出后端、方案与别名
	$(PYTHON) -m om_bridge $(ENV_ARG) list

describe:  ## 某方案/后端的完整自描述（TARGET=minimax_h3）
	$(PYTHON) -m om_bridge $(ENV_ARG) describe $(TARGET)

probe:  ## 探测后端连通性与资产盘点（需要后端在线）
	$(PYTHON) -m om_bridge $(ENV_ARG) probe

graph:  ## 物化计算图（SOLUTION=minimax_h3.t2v PROMPT="..." [OUT=/tmp/g.json]）
	@test -n "$(SOLUTION)" || { printf '需要 SOLUTION=... 例如 SOLUTION=minimax_h3.t2v\n' >&2; exit 2; }
	$(PYTHON) -m om_bridge $(ENV_ARG) graph \
		-s $(SOLUTION) $(if $(PROMPT),-p '$(PROMPT)',) $(if $(OUT),-o $(OUT),) --json

# ---------------------------------------------------------------------------
# 文档与清洁
# ---------------------------------------------------------------------------
docs-check:  ## 检查文档里的相对链接是否都指向真实文件
	@$(PYTHON) scripts/check-doc-links.py

clean:  ## 清理测试与缓存产物
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov 2>/dev/null || true

distclean: clean  ## 在 clean 基础上再删运行时目录（var/、output/）
	rm -rf var output 2>/dev/null || true
