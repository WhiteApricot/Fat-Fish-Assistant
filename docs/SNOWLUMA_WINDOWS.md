# Windows SnowLuma 官方接入

SnowLuma 是当前 Windows x64 的临时 QQ 兼容方案。FFA 使用 2.4.2 Full 官方 ZIP，单独放置。Full 已包含 Node，桌面 QQ 由腾讯官方渠道提供。 [发行锁](../manifests/snowluma/windows-x64.json) · [官方 Windows 指南](https://snowluma.github.io/en/docs/guide/deploy/windows)

## 获取程序与打开窗口

使用 `python -B ffa.py --root "<FFA 安装目录>" snowluma <操作>` 运行下表命令。FFA 安装请使用[快速开始](QUICK_START.md)中的双击入口。

| 操作 | 用途 |
| --- | --- |
| `acquire --destination "<独立 SnowLuma 目录>" --archive "<原始官方 ZIP>" --offline` | 使用用户提供的本地原始官方 ZIP，校验后解压并登记 |
| `launch` | 打开已校验目录里的官方 Launcher，WebUI 仅监听本机。重复打开会检查已有窗口和端口 |
| `webui` | 等待官方 WebUI 首页可访问，然后打开浏览器 |
| `guide` | 显示本实例的 OneBot 地址、填写项和操作顺序 |
| `copy url` / `copy token` | 复制地址或令牌到剪贴板，结果和普通日志不显示令牌 |
| `status` / `verify --timeout 60` | 查看当前分层状态，或在用户完成设置后等待有效的 OneBot 与机器人账号证据 |
| `stop-guide` | 显示官方停机步骤。切回官方 Launcher 按 Ctrl+C，根据窗口提示结束，再执行 `status` |

默认 WebUI 端口为 5099。若端口已占用，可在 `launch` 指定 `--webui-port`，后续查询复用该 Launcher 端口。独立手动启动的既有安装可用同一选项指定端口。只有经过 ZIP 校验并留有安装记录的程序由 FFA 打开 Launcher，现有独立目录可由用户双击官方入口。

## 登录和设置

1. 在官方 Launcher 按提示阅读并确认协议。初次打开官方 WebUI 时，使用官方窗口提示的登录信息。
2. 在腾讯桌面 QQ 窗口用机器人账号扫码和确认设备。在 SnowLuma 官方 WebUI 的实例页面选中该账号并点击加载。
3. 在该账号的 OneBot 设置新增反向 WebSocket 客户端。用 `copy url` 和 `copy token` 依次粘贴到 URL 和 Token 字段。
4. 角色选 Universal，消息格式选 array，重连间隔填写 5000 毫秒，关闭上报自身消息与状态指令。保存并启用连接后清理剪贴板。
5. 让 AstrBot 保持运行，然后执行 `verify`。读到反向连接、OneBot 服务、QQ 在线和绑定身份四层证据后，向 QQ 机器人发一条私聊检查实际回复。

WebUI 可打开只说明本机管理页有响应。OneBot 在线和身份匹配来自 AstrBot 现有的已鉴权连接。真实模型回答和业务聊天通过实机消息验收。 [官方反向连接说明](https://snowluma.github.io/en/docs/guide/connect-bot)

## 常见提示

| 提示 | 处理方式 |
| --- | --- |
| 校验失败 | 核对 ZIP 名称、大小和 SHA256，换用来自官方渠道且匹配的原始资产 |
| Launcher 尚在运行 | 查看旧官方窗口的协议或错误提示。停止后退出窗口，再尝试启动 |
| WebUI 未就绪或端口冲突 | 检查官方启动窗口，等待协议确认完成，必要时改用空闲回环端口 |
| 反向连接未就绪 | 核对 AstrBot 已启动、URL 端口与路径、Token、角色及保存后启用状态 |
| QQ 在线或账号未通过 | 在官方 WebUI 核对加载状态和登录账号，与 FFA 登记的机器人号码一致 |

SnowLuma 的官方配置、QQ 登录态和运行数据继续由其官方渠道管理。 [依赖与许可边界](DEPENDENCIES.md)
