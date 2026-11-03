---
id: S_01_tui-mode-switch
title: TUI 同一会话显式模式切换契约
type: S
author: Yanqing-Shi
author_email: toddsyq@gmail.com
modules:
  - jiuwenswarm/channels/tui/frontend
  - gateway/channel_manager/tui
  - jiuwenswarm/runtime
  - jiuwenswarm/server
  - jiuwenswarm/server/runtime/session
files:
  - jiuwenswarm/channels/tui/frontend/src/app-state.ts
  - jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mode.ts
  - jiuwenswarm/channels/tui/frontend/src/core/ws-client.ts
  - jiuwenswarm/channels/tui/frontend/src/core/commands/types.ts
  - jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts
  - gateway/channel_manager/tui/tui_connect.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/runtime/service.py
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/server/runtime/session/session_history.py
---

# TUI 同一会话显式模式切换契约

`mode.set` 是 TUI Session 切换执行模式的显式入口。请求包含当前 `session_id`、完整 canonical `mode`，以及建议携带的 `expected_mode`。仅支持 agent/team 的 work/code normal/plan 组合；`team.design` 不经此入口。成功响应包含 `previous_mode`、`mode`、`work_mode`、`project_id`、`changed`，并在返回前同步持久化。重复提交当前目标返回 `changed=false`；期望值过期返回 `MODE_CONFLICT`，会话不存在或用户不匹配返回 `NOT_FOUND`，本进程有活动执行或其他存活 AgentServer 持有运行态时返回 `SESSION_BUSY`。切换提交与 Runtime 认领共用跨进程 Session 锁。

`metadata.mode` 是当前路由的权威事实；`mode_revision` 防止旧异步快照回滚显式切换。已有 TUI 会话的聊天请求先在 Session 锁内认领运行态，再读取权威模式并分类为 Agent 或 Team 工作；认领后其他进程不得提交新模式。`work_mode`、`project_id`、`project_dir` 是创建时的项目索引归属，`mode.set` 不修改它们。除用户通过既有入口明确重绑项目外，同一 Session ID 的历史与记录继续显示在原项目下。

TUI 收到 `mode.set` 成功响应后更新本地模式；本地或 Gateway 响应超时、共享 AgentServer 的连接关闭、AgentOS 包装的请求阶段网络失败，以及 TUI 连接中断都视为提交结果未知，重试一次，仍无确定结果时调用 `mode.get` 读取当前权威模式并更新界面。TUI WebSocket 客户端保留响应错误代码；`MODE_CONFLICT` 不重试写入，直接读取 `mode.get` 并显示权威模式。AgentServer 对同一 Session 的 `mode.set` 和 `mode.get` 按到达顺序排队；`mode.get` 须等待此前的切换完成 Runtime 启动和同步提交，再读取权威模式。若读取也失败，将会话模式标为未确认，下一条聊天先同步模式且不发送聊天请求；编辑器保留原文和附件引用，用户在确认后按 Enter 重发。普通失败不自动取消正在执行的任务。

一次 TUI 模式命令的首次切换、重试和权威核对都显式携带命令发起时的 Session ID。启动会话尚未创建完成时，先解析其真实 Session ID，再固定请求目标。异步结果只在该 Session 仍为当前会话时更新界面模式、提示和未确认标记；切到另一会话后，旧命令不得修改新会话的显示或聊天准入状态。

`/swarmflow on` 在读取配置并等待启动会话创建完成后，以该会话的实时模式制定切换计划并记录失败回切目标。已处于任一 Team 模式（包括 `team.code.normal`）时不自动切换到 `team.work.normal`；状态查询也使用实时模式。若命令等待期间已切到另一会话，则停止后续模式或配置操作。

当前契约只保证路由与历史连续；不同执行者的模型上下文交接须由后续独立实现完成。
