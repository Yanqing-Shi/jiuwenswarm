---
id: F_20261103_0545_tui-session-mode-switch-phase1
title: TUI 同一会话模式切换第一阶段
type: F
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
- jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/swarmflow.ts
- jiuwenswarm/channels/tui/frontend/src/core/commands/types.ts
- jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts
- jiuwenswarm/channels/tui/frontend/tests/run-tests.mjs
- gateway/channel_manager/tui/tui_connect.py
- gateway/app_gateway.py
- jiuwenswarm/common/schema/message.py
- jiuwenswarm/server/agent_ws_server.py
- jiuwenswarm/runtime/service.py
- jiuwenswarm/server/runtime/session/session_metadata.py
- jiuwenswarm/server/runtime/session/session_history.py
- tests/unit_tests/runtime/test_session_mode_switch.py
designs:
- D-20261101-1139-tui-session-mode-switch
---

# TUI 同一会话模式切换第一阶段

## 实施与决策

TUI `/mode` 先经 Gateway 发送 `mode.set`，由 AgentServer 调用 Runtime 的统一入口提交当前 Session 的执行模式。成功响应后才更新本地模式。请求使用完整 canonical mode，并建议携带 `expected_mode`；同目标请求幂等，陈旧期望值返回 `MODE_CONFLICT`。本进程活动执行、待答交互或其他存活进程持有运行态时返回 `SESSION_BUSY`。`/mode` 不弹出自动取消确认，也不发送中断；用户单独完成或取消任务后重试。

提交在项目、Session 和 metadata 文件锁内同步完成，仅更新 `mode` 和递增的 `mode_revision`。原 `project_id`、`project_dir`、`work_mode` 及历史目录保持不变；旧异步 metadata 快照不能回滚显式切换。聊天准入先认领运行态，再读取模式并分类，防止跨进程切换发生在读取与认领之间。`session.mode_changed` 作为独立诊断事件写入历史，不充当路由事实源。

TUI 对超时和连接中断重试一次，再通过 `mode.get` 核对权威状态。冲突直接核对，不重复覆盖。无法确认时阻止聊天发送，保留编辑器文本和附件引用；同步完成后由用户重新发送。一次模式命令固定真实 Session ID，其重试和核对不会改发到后来切入的会话，也不会污染新会话界面。AgentServer 串行处理同一 Session 的 set/get，查询等待先前切换完成启动和提交。

`/swarmflow on` 复用相同入口，在启动恢复后读取实时模式，保留已有 Team profile；配置写入失败时尝试恢复实际原模式，并明确报告未确认的回切。协议包尚未提供 MODE_SET/MODE_GET 时沿用本仓枚举兼容层，Gateway 与 AgentServer 使用同名同值成员。

## 设计偏差与范围

本次实施依据为用户确认的第一阶段范围和原项目索引决策；关联设计仍是未完成正式评审的草案。接口要求完整 canonical 值，别名由 TUI 解析。只交付 TUI 的显式路由切换，不接入 Web、自动路由插件或 Agent/Team Leader 模型上下文交接，也不修改 agent-core。模式切换不会将源执行者的模型对话自动导入目标。

## 验证与交接

候选基于上游 develop 的 `a2abceccfba89ab25a518568916152865fa28acd`，保留上游新加的非 Team 附件处理路径。Windows / Python 3.14.7 下，以下定向行为测试共 244 passed：

- `tests/unit_tests/runtime/test_session_mode_switch.py`
- `tests/unit_tests/agentserver/test_mode_cross_layer_protocol.py`
- `tests/unit_tests/server/test_session_metadata_write_options.py`
- `tests/unit_tests/agentserver/test_team_dispatch_mode.py`
- `tests/unit_tests/runtime/test_runtime_service.py`
- `tests/unit_tests/runtime/test_request.py`

TUI 用例覆盖成功、忙碌拒绝且不自动中断、超时与网络中断、冲突核对、编辑器输入和附件保留、会话切换竞态、启动 Session ID 解析及 SwarmFlow 回切。使用 Node 26.13.0，仅将 TypeScript 转译为测试所需 ES module，不运行类型检查或格式化。最终 `npm test` 全部通过，包含输入编辑、监督环境、交接端口、任务生命周期、切换命令、会话注册、用户回答、Persist、记忆、演进、cron、状态栏及工具结果套件。

未执行真实服务端连接、外部模型或交互式终端截图验证；上述证据是自动化行为测试，不代表模型上下文交接验收。运行日志、会话轨迹和其他原始运行产物不属于交付内容。
