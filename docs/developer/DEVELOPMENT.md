# 开发流程

1. 读取[路线图](../ROADMAP.md)与[文档规范](DOCUMENTATION_GUIDELINES.md)。确认当前 main 与任务范围，检查既有工作区修改。阶段性开发使用正式编号。
2. 按 `manifests/` 和 `patches/` 核对锁定依赖与宿主行为。更新补丁时同时审查上游版本、依赖完整性、升级和恢复。接入真实平台使用隔离实例和本地私有配置。
3. 修改运行行为时同步更新对应开发者文档，涉及使用方式时更新用户指南。审查数据范围、权限、重试、错误与兼容。
4. GitHub CI 在 Windows 与 Ubuntu 上使用 Python 3.12 检查语法、发行归档及离线回归。涉及原生适配器的变更还应使用固定 AstrBot 环境运行对应定向回归，需要实机时另行记录。
5. 仅文档变更检查相对链接、描述真实性、私有数据和变更范围。保留短验收结论，详细结果放本地报告。

离线回归在安装清单锁定的 `cryptography` 和 `psutil` 后执行。按终端设置 `PYTHONPATH=src`，运行 `python -m unittest discover -s tests -v`。[CI 工作流](../../.github/workflows/core-tests.yml) · [外部依赖](../DEPENDENCIES.md)
