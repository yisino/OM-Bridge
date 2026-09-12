# 连接拓扑档（profiles）

## 它们是什么

OM-Bridge 的配置查找遵循**取第一个存在的文件、不合并多个**的规则。
这意味着"连接拓扑档"是一份**完整的**配置文件，而不是一份叠加在主配置上的补丁。

于是这里只列**必须改的那几项**，其余全部取内置默认值 ——
内置默认值本身就是按 H3 的真实部署写的（权重名、栅格、步数配对），
所以一份 8 行的档就能把机器跑起来，这恰恰是这套默认值的意义。

## 怎么用

```bash
# 方式一：临时指向（推荐用于对比与排障）
OM_BRIDGE_ENV_FILE=config/profiles/through-tunnel.env om-bridge doctor

# 方式二：把内容拷进工作区根的 .env（长期使用）
cp config/profiles/through-tunnel.env .env
```

## 为什么不合并

合并多个配置文件的代价是"这个值到底哪来的"变成玄学：
排查时必须把每一层的覆盖关系在脑子里跑一遍。
用**一个**文件 + 明确的来源标注（`om-bridge config report` 会逐项标出
`override` / `env:VAR` / `file:VAR` / `default`），比多层合并好排查得多。
真需要组合时，用环境变量覆盖那一层：`OM_BRIDGE_COMFYUI_SERVER_URL=... om-bridge ...`

## 两个档的差别

| 档位 | 适用场景 | server_url |
|---|---|---|
| `same-lan.env` | 调用方与 ComfyUI 在同一局域网，且 ComfyUI 监听 `0.0.0.0` | `http://192.168.3.3:8188` |
| `through-tunnel.env` | 跨网段，或 ComfyUI 在鉴权代理之后 | `http://127.0.0.1:8188`（经 SSH 反向隧道） |

`through-tunnel.env` 对应的隧道命令（在 **ComfyUI 所在主机**上执行，保持连接）：

```bash
ssh -N -R 8188:127.0.0.1:8188 vincent@<服务器地址>
```

**为什么优先用隧道而不是"暴露到公网 + 加认证头"**：
核心客户端刻意只依赖标准库、不支持携带认证头（这是它能做到零依赖的原因之一），
所以"加认证"意味着要在中间放一个代理。隧道把鉴权与加密都交给 SSH，
两边都不用改代码，也少一个需要维护的组件。
