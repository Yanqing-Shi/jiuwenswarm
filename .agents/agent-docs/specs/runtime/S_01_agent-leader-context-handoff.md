---
modules:
- jiuwenswarm/runtime
- jiuwenswarm/server/runtime/agent_adapter
author: null
author_note: 从恢复补丁导入；原始作者无法从 Git 确认
title: 同一 Session 的 Agent / Leader 对话归属
id: S_01_agent-leader-context-handoff
type: S
files: []
---

# 同一 Session 的 Agent / Leader 对话归属（已撤销）

最近一次修订日期：2026-11-13。

本规约原先定义复制式交接：owner / pending journal、receipt、profile 隔离，以及 `jiuwenswarm/runtime/context_handoff.py` 的接入。
该实现未进入 develop，已按 [D-20261112-0530-session-conversation-window](../../designs/proposals/D-20261112-0530-session-conversation-window/design.md)
的用户决定移除，以上条款不再是有效契约。

当前契约的位置：

- 主对话的存储、绑定与读写：同级 agent-core 的 `.agents/agent-docs/specs/core_runtime/S_02_session-conversation-store.md`。
- 运行、模式切换、归档/删除的互斥：[S_02_session-operation-ownership](S_02_session-operation-ownership.md)。
- 没有共享记录的旧 Session 保持交接引入之前的行为（Agent 与 Team 各用各的上下文），不迁移、不交接。

实施与移除记录见 [F_20261113_0307](../../features/F_20261113_0307_shared-conversation-runtime.md)；
原实施记录 [F_20261029_0001](../../features/F_20261029_0001_agent-leader-context-handoff.md) 保留为历史依据。
