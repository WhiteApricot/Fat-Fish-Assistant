# P3 阶段 1 TODO

| 批次 | 任务 | 验收 |
| --- | --- | --- |
| 1 | Linux core Python 锁、固定 QQ/Shell/launcher、Ubuntu 系统前置 | WSL 校验归档、拒绝错误哈希、运行 venv 与 D-Bus/Xvfb 探针、重复执行。核对 Windows 锁与入口。 |
| 2 | ffa.py 平台选锁、固定缓存、Linux 运行目录、FFmpeg、Dashboard 凭据及插件依赖 | 干净 Linux venv 离线组装并 pip check。验证私密权限和插件顺序。Windows 安装与 DPAPI 回归。 |
| 3 | install.sh 串接系统、Python、AstrBot、插件和 QQ/NapCat | 干净 WSL 一键安装、镜像及官方备用、应用断网安装、重复执行、中断恢复、健康检查。 |
| 后续本机批次 | 统一菜单及启停、日志、备份、自动获取 NapCat Token、聊天与迁移 | Windows/Linux 独立运维，双 WebUI 回环访问，QQ 功能矩阵，双向停机及往返。 |
| 云端 | 腾讯云 Linux 实机部署 | 从零安装、SSH 隧道、重启恢复、长期运行与备份还原。 |

账号互斥及逐批发布规则见 [贡献指南](protocols/CONTRIBUTING.md)。
