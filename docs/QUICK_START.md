# 克隆、安装与日常运行

正式工程仅为本仓库。安装根放在仓库外，以下示例使用 `../Fat-Fish-Assistant.local/instance`；不需要旧 feiyu 目录、旧数据库或他人的配置。Windows VM 尚待独立验收。

## 第一次安装

从仓库目录运行统一入口，按提示填写自己的主人 QQ。缺少固定解释器时自动获取，不修改系统 Python：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Fat-Fish-Assistant.ps1 -Action install -Root ..\Fat-Fish-Assistant.local\instance --components 'chat,napcat,embedding,speech' --accept-external-licenses
```

运行前须审阅 THIRD_PARTY_NOTICES 与可选资产条款。基本 Chat 可用 `--components chat`，不强制所有模型。安装器完成版本/哈希校验、独立 Python 环境、固定源码补丁和固定 WebUI、组件下载与空白实例。系统 Microsoft 运行库缺失会明确提示，安装官方系统运行库需用户操作。

上面的执行策略只作用于本次进程，不修改系统策略。先审阅并信任源码；不要为运行脚本更改机器全局策略。

QQ 官方客户端安装和账号扫码遵循其许可，由用户完成；已有 QQ 客户端可以显式选择为外部运行依赖，脚本不修改其文件。不是让用户自行寻找 AstrBot、插件、Python 包或模型：这些均由清单自动获取。不要双开同一 Bot 账号的两个 NapCat 消费者。

## 初始化连接

统一接口支持以下配置操作，`--root` 均指相同安装根；PowerShell 入口等价使用 `-Action config` 并传递后续参数。下列 Python 指安装根内 `bootstrap-python-3.12.3/tools/python.exe`，不是任意系统 Python。

```text
config initialize-chat --onebot-port 6199
config apply --file <本机私密配置.json>
config initialize-onebot --bot-id <自己的BotQQ>
```

私密配置支持 `dashboard_port`、`napcat_webui_port`、`qq_executable`、`deepseek_api_key`。密钥只通过本机私密文件或 stdin 提供，不作为命令行参数，不提交 Git。`initialize-chat` 使用环境引用加载实例私密 Key，保留独立人格与记忆设置；`initialize-onebot` 自动生成原生反向连接和匹配的随机鉴权令牌，不复制旧登录态。它们拒绝覆盖不兼容配置，不授权主人或群。

完成配置后 start。先到 NapCat 的本机 WebUI 手动扫码，再用 health 验证。WebUI 令牌位于该安装根的 `services/napcat-v4.18.41/config/webui.json`，只在本机读取。新用户不默认批准任何群；群开放必须由主人明确批准。机器人不应因初始化或安装而擅自扩大白名单。

## 日常启动

```powershell
.\Fat-Fish-Assistant.ps1 -Action start -Root ..\Fat-Fish-Assistant.local\instance
.\Fat-Fish-Assistant.ps1 -Action status -Root ..\Fat-Fish-Assistant.local\instance
.\Fat-Fish-Assistant.ps1 -Action health -Root ..\Fat-Fish-Assistant.local\instance
.\Fat-Fish-Assistant.ps1 -Action stop -Root ..\Fat-Fish-Assistant.local\instance
```

首次安装会下载和初始化，日常 start 不重新安装。重复 start 复用当前状态，不重复启动。完整停止会请求 Chat 原生终止和索引落盘，再停止模型与受控 NapCat/QQ；重启后可能需要用户再次扫码，不承诺自动登录。缺失登录时 health 返回失败而不是假通过。

日志位于安装根 `instance/logs/chat.stdout.log`、`chat.stderr.log`、模型日志和 `logs/` 下的独立维护日志。可在本机使用 `Get-Content <日志路径> -Tail 60`；不要直接上传私人日志。

维护者当前机器的确切、已经执行过的运行路径和命令保存在受限收口运行说明中。此处是参数化用户指南，不能将示例路径误写为已在任意新 Windows 上执行通过。
