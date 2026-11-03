# 系统设计

NapCat 管理 QQ 接入，OneBot 提供传输和平台动作，AstrBot 管理事件流水线、模型、调度和插件。肥鱼助手在 AstrBot 集成点执行入口和发送边界检查。独立管理脚本负责构建、进程归属、恢复与迁移。

| 组件 | 职责 |
| --- | --- |
| gateway、permissions | 消息路由、会话边界、角色、确认、撤权和去重 |
| commands、persona | 指令帮助与意图识别、人格选择和命令回执 |
| memory、reminders | 群记忆适配与提醒时间、存储和调度接口 |
| adapters、participation、integrations | QQ 群及媒体适配、主动参与、语音和转发 |
| core | 实例身份检查 |

组件位于 [src/ffa_components](../../src/ffa_components)。[自有插件入口](../../src) 与组件分开，第三方代码按 [源码清单](../../manifests/sources.json) 组装。部署边界见 [插件约定](../protocols/PLUGIN_CONTRACT.md)。
