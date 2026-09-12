# ADR-0003: 注册表自动发现，无中心清单

## Status

Accepted（2026-09-12）

## Context

[ADR-0001](0001-two-layer-backend-solution.md) 确立了两层扩展模型。随之而来的问题是：
**程序怎么知道有哪些实现？**

约束：

- 新增实现应当是**新增文件**，而不是**修改既有文件**。因为"修改既有文件"是
  merge 冲突与"忘了登记"的源头 —— 多人并行新增两个模型时，两个人都会去改同一个清单文件。
- 实现可能来自**本仓库之外**（第三方包）。用户不该被要求 fork 本仓库才能加一个引擎。
- 发现过程不能有副作用（不能在 import 时就去连后端）。
- 现有环境里有 1000+ 个 ComfyUI 节点类、多个模型文件，发现要足够快且不产生噪声。

## Decision

两级发现，**没有中心清单文件**：

1. **内置**：`pkgutil.walk_packages` 扫描 `om_bridge.backends` 及其下的 `solutions` 包，
   收集 `Backend` / `Solution` 的子类。
2. **第三方**：`importlib.metadata.entry_points(group="om_bridge.backends")`，
   第三方在自己的 `pyproject.toml` 里声明：

   ```toml
   [project.entry-points."om_bridge.backends"]
   myengine = "my_pkg.backend:MyBackend"
   ```

**模式别名自动派生**：若方案的 `key == "minimax_h3"` 且 `modes` 含 `ref2v`，
则 `minimax_h3.ref2v` 自动成为可解析的名字。别名无需登记。

发现是**惰性且幂等**的（`discover()` 可重复调用，`get_registry()` 返回单例）。

## Alternatives Considered

**选项 B：中心清单文件（如 `BACKENDS = [...]` 或 `registry.yaml`）。**
最直白。但每加一个实现都要改它，于是：
并行开发必然冲突；忘记登记会导致"代码在但用不了"（而且症状是运行期才暴露）；
第三方必须改本仓库。全部与本决策的约束冲突。不选。

**选项 C：装饰器注册（`@register_backend`）。**
比清单文件好（注册动作贴着实现），但要求实现在**被 import 时**才注册，
于是需要一个"把所有实现在启动时 import 一遍"的地方 —— 这就又变成了中心清单，
只是换了个位置。而且 import 时执行代码会带来副作用风险（连后端、读环境）。不选。

**选项 D：只支持 entry points，内置实现也走 entry points。**
概念最统一。但源码树直连（`PYTHONPATH=src python -m om_bridge`，即
[ADR-0002](0002-zero-runtime-dependencies.md) 的 `path` 安装模式）下**没有安装元数据**，
entry points 不存在 → 内置实现也发现不了。所以内置必须走包扫描。不选。

## Consequences

**变容易的**：
- 并行新增实现零冲突：各自新增目录，互不相干。
- 第三方集成零侵入：发一个独立 PyPI 包即可，核心完全不需要知道它的存在。
- `path` 安装模式（无 pip）也能正常发现全部内置实现。
- 不可能"忘了登记"——没有登记这个动作。

**变难的 / 代价**：
- 发现依赖**包名与目录结构的约定**（`solutions` 子包）。放错位置就静默发现不到。
  缓解：`backends/<engine>/solutions/__init__.py` 里写明这个约定；
  `om-bridge list` 是最快的"发现是否生效"验证手段。
- `walk_packages` 会 import 模块，若某个模块 import 就报错（如缺依赖），
  要有容错（不能让一个坏插件让整个注册表挂掉）。
- 报错信息需要更友好：用户写错方案名时，得告诉他"已发现的有哪些"，
  而不是只说"找不到"。

**后续要盯住的**：
- 若实现数量增长到几十个，`walk_packages` 的启动开销会变得可感。
  届时应考虑**按需发现**（用到哪个扫哪个）或加缓存，而不是回到中心清单。
- entry point 的组名 `om_bridge.backends` 一旦发布就**不能改**（会破坏所有第三方包）。
  要改必须走"同时支持新旧两个组名 + 弃用期"的迁移路径。
