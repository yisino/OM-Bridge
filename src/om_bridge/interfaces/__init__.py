"""交付面（Interfaces）—— 把同一套核心暴露给不同消费者。

这个包里的每个模块都**只做翻译**：把人/机器的输入翻成
:class:`~om_bridge.core.models.GenerationRequest`，把结果翻回该消费者
习惯的表示。它们本身不含任何生成逻辑，也不该有 —— 一旦有，
就意味着"CLI 能做的事 MCP 做不到"，而这正是原实现最大的问题。

当前提供两个交付面：

======================  ==============================================
``cli``                 ``om-bridge`` 命令。给人用，也可被任何 shell 脚本调用。
``mcp``                 ``om-bridge-mcp`` 命令。给 agent 用，JSON-RPC over stdio。
======================  ==============================================

第三个交付面是**库本身**（``import om_bridge``），不需要额外模块 ——
:class:`~om_bridge.core.session.Session` 就是 SDK 入口。

共同约定
--------
* **stdout 只放结果。** 人类可读的进度与诊断一律走 stderr。
  MCP 用 stdout 传 JSON-RPC，CLI 的 ``--json`` 也把 stdout 当数据通道，
  两者都不能容忍多余的一行文字。
* **退出码即错误分类。** CLI 用 :class:`~om_bridge.core.errors.OmBridgeError`
  自带的 ``exit_code``；MCP 把同一个异常翻成协议级错误对象。
* **新增能力只改注册表。** 新增一个后端或方案，这两个文件都不需要改 ——
  CLI 的命令行选项与 MCP 的工具 schema 都是从
  :class:`~om_bridge.core.schema.ParamSchema` 现场生成的。
"""
