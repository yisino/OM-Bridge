# echo 方案 —— mock 后端的配套演示方案

> echo 不是用来生成内容的。它存在的意义是：**把"新增一个 Solution"这件事
> 变成可以照抄的、能跑的最小样例**，并让 Session 全路径在没有 GPU、
> 没有网络的环境下依然可以端到端验证。

## 定位

| | `minimax_h3` | `echo` |
|---|---|---|
| 后端 | `comfyui` | `mock` |
| 模式 | 4 个（别名 `minimax_h3.<mode>`） | 无（覆盖"单一形态"注册分支） |
| 资产需求 | 逐模式声明权重/节点 | 无（覆盖"无资产"分支） |
| 产物 | h264 mp4 + AAC 音轨 | 作业转写 JSON |
| 代码形态 | manifest / builder / solution 三分 | 单文件（规模不配三分的仪式） |

`echo` 与 H3 互补地覆盖注册表与编排层的分支，二者合起来才把
`docs/architecture.md` §3 的双层流程图全部走了一遍。

## 用法

```bash
# 离线端到端（零网络、零 GPU）
om-bridge generate -s echo -p "你好，桥" --frames 8 --backend mock

# 结构化失败路径演练（提交阶段按请求失败）
om-bridge generate -s echo -p "x" --set simulate_failure=true --backend mock
```

产物是 `var/output/mock-<指纹12位>.transcript.json`，内容含提示词、
最终参数、媒体远端名与完整作业载荷 —— `job_id` 即载荷指纹前 12 位，
**同载荷重复提交得到同一个 id**（与 ComfyUI 整图缓存同语义，
可用于验证防重复提交逻辑）。

## 参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `frames` | integer | `24` | 1–10000，转写进产物（演示类型与范围校验） |
| `case` | string | `plain` | `plain` / `upper`，upper 时提示词转大写（演示枚举） |
| `simulate_failure` | boolean | `false` | true 时 `submit` 抛 `JobRejectedError`（演示后端拒绝路径） |

媒体槽位：`--reference`（可选，可重复，最多 3 个本地图片，会被 mock
"上传"到 `var/mock-uploads/`，远端名写进转写）。

## 实现结构

单文件 `src/om_bridge/backends/mock/solutions/echo/solution.py`，约百行。
**刻意不做** manifest/builder 三分：H3 的三分是因为约束与图构造复杂到值得
分开测试；echo 的规模下三分只会让读者来回跳。分层的价值随规模出现 ——
写真实模型方案时，仍请遵循 [扩展指南](../adding-a-backend.md) §3 的三分结构。

## 对扩展作者的三条提示

1. `type` 名用**常量全名**（`integer` / `number` / `string` / `boolean`），
   写 `int`/`bool` 会落进兜底的 `str()` 分支 —— 值悄悄变成字符串，
   校验看起来全过，载荷却是错的。（echo 第一次实现就踩了。）
2. `build_job` 收到的 `resolved_params` 已填好默认值，不要自己再查默认。
3. 想测试"后端拒绝"路径，把失败条件放进**载荷**让后端在 submit 时抛
   （echo 的 `simulate_failure`），而不是在方案里提前拦截 —— 后者测不到
   Session 的 submit 错误处理。
