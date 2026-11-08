# Ubuntu 24.04 x86_64 安装与使用

以服务用户登录 Ubuntu，准备 Python 系统前置所需的 sudo 权限，并确认 systemd 用户会话可用。WSL 中使用启用了 systemd 的 Ubuntu。将源码放在可长期保留的位置，安装根和缓存放在 Ubuntu 用户目录。先阅读 [外部依赖声明](../THIRD_PARTY_NOTICES.md)。

## 安装

```bash
systemctl --user show-environment >/dev/null
bash install.sh --owner 123456789 --accept-external-licenses
```

将示例 QQ 号替换为主人账号。脚本默认写入 ~/.local/share/Fat-Fish-Assistant，缓存位于 ~/.cache/Fat-Fish-Assistant。用 --root 和 --cache 指定其他目录。已安装实例需先停止再重跑。重跑会复用经校验的 venv、发布、NapCat 组装及既有数据。脚本遇到已装系统包版本与锁不一致时退出，保留系统状态。

离线安装先准备相同版本的系统前置，再把固定缓存复制到目标机，保持目录结构。缓存根放源码、Dashboard、素材及 QQ/NapCat/launcher，artifacts/ 放 Python 锁与构建输入，system/ 放固定 Ubuntu deb。应用安装用 --offline --cache <缓存路径>。系统完全离线补包还需准备 Ubuntu APT 的传递包与索引。

## 启动及管理

```bash
bash start.sh
bash start.sh status
bash start.sh menu
bash start.sh logs
bash start.sh restart
bash start.sh stop
```

脚本调用 ffa.py，服务由同一用户的 systemd 管理。status 和 health 分别显示 systemd 状态、Dashboard 是否通过鉴权、QQ 是否登录，以及 OneBot 应用层是否连接。启动时不会自动登录 QQ。若 WSL 的虚拟图形驱动导致无头 QQ 启动异常，可在启动前设置 FFA_SOFTWARE_RENDER=1，仅对该服务生效。

菜单提供打开 NapCat 和 AstrBot 页面。打开 NapCat 时自动读取受管配置的有效 Token 并交给本机浏览器。扫码后选择 connect-qq，将该机器人账号与本机 OneBot 绑定。确认另一端的同一机器人账号已下线。Windows 上的主人 QQ 可以保持运行。菜单尝试使用受限保存的初始凭据打开 AstrBot 管理界面，首次使用按页面提示改密。AstrBot 模型凭据在其管理界面中配置，安装时不使用样例密钥。

### 从 Windows 浏览器访问

WSL 默认尝试 Windows localhost 转发。用 status 查看 webui_port 和 dashboard_port。若自动转发无法使用，在 Windows PowerShell 运行下列命令，右侧替换为 status 显示的 WSL 端口。Windows 需要 Python，可用现有 Windows 肥鱼助手的 Python。

```powershell
python scripts/wsl/loopback-relay.py --distro Ubuntu --map 18775:6099 --map 18785:6185
```

保持中继窗口打开，在 WSL 使用：

```bash
bash start.sh menu --napcat-browser-port 18775 --astrbot-browser-port 18785
```

也可单独运行 open-webui napcat --browser-port 18775，或 open-webui astrbot --browser-port 18785。两个平台侧的代理监听均在 127.0.0.1。远端 Linux 的 WebUI 通过 SSH 本地端口转发使用，配置在 [运维文档](OPERATIONS.md) 中按部署环境处理。

## 故障处理

- 下载中断可重跑。已完成的缓存输入仍校验并复用，未完成 venv 只有在确认没有同一安装的活动工作进程后才归档。查看安装根的 install-journal.json、install-history/ 和 envs/ 日志。
- 哈希不一致时把报错的具体缓存文件移到隔离目录，再从可信来源补齐。已有实例和服务文件仍受安装锁保护。
- D-Bus 或 systemd 不可用时，先恢复该用户的 systemd 会话。服务端口占用时核对所属进程。脚本不会以进程名停止其他 QQ 或 AstrBot。
- 当前 Linux 锁覆盖 core。可选本地嵌入与语音需要各自的 Linux 组件锁及验收。
