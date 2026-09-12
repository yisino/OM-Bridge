"""注册表 —— 自动发现后端与方案，让"新增一个实现"等于"新增一个包"。

发现机制（三条路径，合并成一个注册表）
--------------------------------------
1. **内置后端**：遍历 :mod:`om_bridge.backends` 下的所有子模块，
   import 后按基类收集 :class:`~om_bridge.core.backend.Backend` 子类。
   新增一个后端 = 新建 ``backends/<name>/`` 目录，**不需要改动本文件**。

2. **内置方案**：对每个后端包，递归遍历其 ``solutions`` 子包，
   收集 :class:`~om_bridge.core.solution.Solution` 子类。
   新增一个方案 = 在 ``solutions/`` 下新建一个子包，同样无需改动核心。

3. **第三方插件**：读取 ``om_bridge.backends`` 这个 entry point 组。
   装了带该 entry point 的包（无论 pip 还是本地 editable），一样会被发现。
   这让"别人写的新后端"和"官方内置后端"在运行时毫无区别。

设计取舍
--------
* **import 失败不致命。** 某个插件写坏了不该让整个桥上不了线 ——
  记进 :attr:`Registry.errors` 并继续，``om-bridge doctor`` 会把它报出来。
  这条来自真实教训：一个可选依赖缺失曾让整条 CLI 直接不可用。

* **方案实例被当作无状态单例缓存。** 因此 :class:`Solution` 的实现必须是
  **无实例状态**的（所有可变状态要么走参数、要么走配置）。
  这条约束写进契约，是因为它同时带来了并发安全与零构造开销。

* **模式别名。** 声明了 ``modes`` 的方案会自动获得 ``<key>.<mode>`` 形式的解析键。
  于是"一个实现、四个入口"同时成立：``minimax_h3`` 是唯一实现，
  ``minimax_h3.ref2v`` 是它的一个预设入口。
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import pkgutil
from collections.abc import Iterable
from typing import Any

from .backend import Backend
from .config import Config
from .errors import UnknownBackendError, UnknownSolutionError
from .solution import Solution

BUILTIN_PACKAGE = "om_bridge.backends"
ENTRY_POINT_GROUP = "om_bridge.backends"


def _iter_subclasses(base: type, module: Any) -> Iterable[type]:
    """在给定模块里找出 ``base`` 的、定义于本模块的子类。

    只收集 ``__module__`` 等于本模块的类，避免因为 import 传递而把同一个类
    重复登记（例如方案模块 import 了另一个方案模块的情况）。
    """
    for _name, obj in vars(module).items():
        if not inspect.isclass(obj):
            continue
        if obj in (base,) or not issubclass(obj, base):
            continue
        if inspect.isabstract(obj):
            continue
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        yield obj


class Registry:
    """后端与方案的注册表。"""

    def __init__(self) -> None:
        self._backends: dict[str, type[Backend]] = {}
        self._solutions: dict[str, type[Solution]] = {}
        self._aliases: dict[str, tuple[str, dict[str, Any]]] = {}
        self._instances: dict[str, Solution] = {}
        self.errors: list[str] = []
        self.discovered = False

    # ------------------------------------------------------------------
    # 发现
    # ------------------------------------------------------------------
    def discover(self, *, force: bool = False) -> Registry:
        """执行发现。幂等，可重复调用。"""
        if self.discovered and not force:
            return self
        self._backends.clear()
        self._solutions.clear()
        self._aliases.clear()
        self._instances.clear()
        self.errors.clear()

        self._discover_builtin_backends()
        self._discover_entry_point_backends()
        self._discover_solutions()
        self._build_aliases()
        self.discovered = True
        return self

    def _discover_builtin_backends(self) -> None:
        try:
            package = importlib.import_module(BUILTIN_PACKAGE)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"无法导入 {BUILTIN_PACKAGE}: {type(exc).__name__}: {exc}")
            return
        for info in pkgutil.iter_modules(package.__path__):
            if info.name.startswith("_"):
                continue
            module_name = f"{BUILTIN_PACKAGE}.{info.name}"
            for candidate in (f"{module_name}.backend", module_name):
                try:
                    module = importlib.import_module(candidate)
                except Exception:  # noqa: BLE001
                    continue
                for cls in _iter_subclasses(Backend, module):
                    self._register_backend(cls)
                break

    def _discover_entry_point_backends(self) -> None:
        try:
            entry_points = importlib.metadata.entry_points()
            # Python 3.10/3.11 返回 dict-like，3.12+ 返回 EntryPoints；两种都兼容
            selected = (
                entry_points.select(group=ENTRY_POINT_GROUP)
                if hasattr(entry_points, "select")
                else entry_points.get(ENTRY_POINT_GROUP, [])
            )
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"读取 entry points 失败: {type(exc).__name__}: {exc}")
            return
        for entry in selected:
            try:
                loaded = entry.load()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"插件 {entry.name} 加载失败: {type(exc).__name__}: {exc}")
                continue
            if inspect.isclass(loaded) and issubclass(loaded, Backend):
                self._register_backend(loaded)
            elif inspect.ismodule(loaded):
                for cls in _iter_subclasses(Backend, loaded):
                    self._register_backend(cls)

    def _register_backend(self, cls: type[Backend]) -> None:
        name = getattr(cls, "name", "")
        if not name:
            self.errors.append(f"后端 {cls.__module__}.{cls.__name__} 未声明 name，已跳过")
            return
        existing = self._backends.get(name)
        if existing is not None and existing is not cls:
            self.errors.append(
                f"后端名 {name!r} 重复：{existing.__module__}.{existing.__name__} "
                f"与 {cls.__module__}.{cls.__name__}，保留前者"
            )
            return
        self._backends[name] = cls

    def _discover_solutions(self) -> None:
        """遍历每个后端的 ``solutions`` 子包，收集方案。"""
        for backend_name, backend_cls in list(self._backends.items()):
            package_name = f"{backend_cls.__module__.rsplit('.', 1)[0]}.solutions"
            try:
                package = importlib.import_module(package_name)
            except Exception:
                # 后端可以不带任何方案（纯执行器形态），这不是错误
                continue
            for module in self._walk_package(package):
                for cls in _iter_subclasses(Solution, module):
                    self._register_solution(cls, backend_name)

    def _walk_package(self, package: Any) -> Iterable[Any]:
        """递归 import 一个包下的所有模块，产出模块对象。"""
        yield package
        for info in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
            if info.name.rsplit(".", 1)[-1].startswith("_"):
                continue
            try:
                yield importlib.import_module(info.name)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"方案模块 {info.name} 导入失败: {type(exc).__name__}: {exc}")

    def _register_solution(self, cls: type[Solution], backend_name: str) -> None:
        key = getattr(cls, "key", "")
        if not key:
            self.errors.append(f"方案 {cls.__module__}.{cls.__name__} 未声明 key，已跳过")
            return
        existing = self._solutions.get(key)
        if existing is not None and existing is not cls:
            self.errors.append(
                f"方案键 {key!r} 重复：{existing.__module__}.{existing.__name__} "
                f"与 {cls.__module__}.{cls.__name__}，保留前者"
            )
            return
        # 方案声明与所在后端包不一致 -> 提示（不阻断，允许方案跨后端复用）
        if cls.backend_name and cls.backend_name != backend_name:
            self.errors.append(
                f"方案 {key!r} 声明 backend_name={cls.backend_name!r}，"
                f"却位于后端包 {backend_name!r} 下"
            )
        self._solutions[key] = cls

    def _build_aliases(self) -> None:
        """为带模式声明的方案生成 ``<key>.<mode>`` 别名。"""
        for key, cls in self._solutions.items():
            for mode in getattr(cls, "modes", []) or []:
                self._aliases[f"{key}.{mode}"] = (key, {"mode": mode})

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def backends(self) -> dict[str, type[Backend]]:
        self.discover()
        return dict(self._backends)

    def backend(self, name: str) -> type[Backend]:
        self.discover()
        cls = self._backends.get(name)
        if cls is None:
            raise UnknownBackendError(
                f"未知后端 {name!r}",
                hint=f"已注册的后端：{', '.join(sorted(self._backends)) or '(无)'}",
            )
        return cls

    def solutions(self, *, backend: str | None = None) -> dict[str, type[Solution]]:
        self.discover()
        if backend is None:
            return dict(self._solutions)
        return {k: v for k, v in self._solutions.items() if v.backend_name == backend}

    def solution(self, name: str) -> type[Solution]:
        """按方案键取类。不解析别名 —— 别名请用 :meth:`resolve`。"""
        self.discover()
        cls = self._solutions.get(name)
        if cls is None:
            raise UnknownSolutionError(
                f"未知方案 {name!r}",
                hint=f"已注册的方案：{', '.join(sorted(self._solutions)) or '(无)'}"
                + (f"；可用别名：{', '.join(sorted(self._aliases))}" if self._aliases else ""),
            )
        return cls

    def resolve(self, name: str) -> tuple[Solution, dict[str, Any]]:
        """解析方案名（含别名），返回 ``(方案实例, 预设参数)``。

        ``minimax_h3.ref2v`` → ``(MinimaxH3Solution 实例, {"mode": "ref2v"})``
        预设参数的优先级**低于**用户显式传入的参数，只是"替用户填好默认"。
        """
        self.discover()
        preset: dict[str, Any] = {}
        key = name
        if name not in self._solutions and name in self._aliases:
            key, preset = self._aliases[name]
        cls = self.solution(key)
        return self.instance(cls.key), dict(preset)

    def instance(self, key: str) -> Solution:
        """取得方案的单例（必须无状态，见模块文档）。"""
        cls = self.solution(key)
        cached = self._instances.get(cls.key)
        if cached is None:
            cached = cls()
            self._instances[cls.key] = cached
        return cached

    # ------------------------------------------------------------------
    # 便捷构造
    # ------------------------------------------------------------------
    def create_backend(self, name: str | None, config: Config, **options: Any) -> Backend:
        """实例化后端。``name`` 为空时用配置里的默认后端。"""
        resolved = name or config.get("global.default_backend") or "comfyui"
        cls = self.backend(resolved)
        return cls(config, **options)

    def describe(self) -> dict:
        """整座桥的自描述。``om-bridge list`` 与 MCP 的列表工具共用它。

        后端部分刻意**只读类属性、不实例化**：后端构造函数会去读配置、
        甚至可能建连接，而"列出有哪些后端"不该有这种副作用。
        """
        self.discover()
        backends = []
        for backend_name, cls in sorted(self._backends.items()):
            backends.append(
                {
                    "name": cls.name,
                    "display_name": cls.display_name or cls.name,
                    "description": cls.description,
                    "capabilities": sorted(cls.capabilities),
                    "module": f"{cls.__module__}.{cls.__name__}",
                    "solutions": sorted(
                        key for key, sol in self._solutions.items() if sol.backend_name == backend_name
                    ),
                }
            )
        solutions = []
        for cls in sorted(self._solutions.values(), key=lambda c: c.key):
            solutions.append(
                {
                    "key": cls.key,
                    "display_name": cls.display_name or cls.key,
                    "backend": cls.backend_name,
                    "kind": cls.kind,
                    "modes": list(cls.modes),
                    "default_mode": cls.default_mode,
                    "description": cls.description,
                    "docs": cls.docs,
                    "module": f"{cls.__module__}.{cls.__name__}",
                    "aliases": sorted(
                        alias for alias, (key, _preset) in self._aliases.items() if key == cls.key
                    ),
                }
            )
        return {
            "backends": backends,
            "solutions": solutions,
            "aliases": sorted(self._aliases),
            "load_errors": list(self.errors),
        }


# 进程级单例。发现是只读的、幂等的，全局缓存能避免每次调用都重新 import 一遍包。
_REGISTRY = Registry()


def get_registry() -> Registry:
    """取全局注册表（已发现）。"""
    return _REGISTRY.discover()
