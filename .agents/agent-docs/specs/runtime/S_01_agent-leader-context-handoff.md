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

# 同一 Session 的 Agent / Leader 对话归属

最近一次修订日期：2026-11-01。

范围：`jiuwenswarm/runtime/context_handoff.py` 及 AgentServer 的 DeepAdapter / Team 接入。需要配套 Core 的 conversation API；不改变 `work_mode`、项目归属或 mode → session 缓存层级。

## 有效契约

1. 同一 profile 内的 `agent.work.*` ↔ `team.work.*`、`agent.code.*` ↔ `team.code.*` normal/plan 以实际 card ID 识别对话执行者。显式三段 canonical mode 优先于 Session 中另存的 `work_mode` profile；旧式、未明确 profile 的 mode 仍受 `work_mode=code` 排除。相同执行者的 normal/plan 切换不复制消息；team.design、外部 CLI、slash command 和 attach-only 订阅不触发交接。
2. `metadata.conversation_handoff` 保存 owner（包含 code profile 标识）和 pending journal；UI mode 不决定源身份，普通 metadata 写入无权回滚此字段。源冷恢复只使用记录的 agent ID。已有另一 profile 归属的 Session 拒绝 work ↔ code 请求，不能把共享 Team 身份或 checkpoint 当作可隔离的两段历史；无归属旧会话也按最后用户请求的 profile 验证来源。
3. 仅替换目标原生 context 的消息和内存 offload 数据，保留消息 ID/metadata/完整工具配对。目标模型、权限、系统提示、工具、任务和 turn 状态保持原归属；Leader 的 `team_prompt_context` tracker 重置。
4. 跨执行者时源/目标必须静止。活动轮次、待执行输入、工具、follow-up、任务、活动/暂停 goal、人机交互、团队 pending resume、未完成任务板/成员/未读消息均拒绝交接，不自动暂停或等待。普通同模式启动仍可排队。
5. 源冻结持续到目标消息与 receipt commit 成功且 owner 更新。旧执行者的新输入会被 guard 拒绝。目标首条输入的交付保护不由后台消息解除。
6. 目标恢复在消息替换之前完成；新 Leader 在 child.pre_run 后、NativeHarness supervisor 启动前执行交接。缓存目标同样替换。无 waiter 的请求将交接生命周期移交给后台 producer。
7. 提交失败恢复目标原内存；若存储已提交但确认丢失，读取目标 receipt 收敛 owner 并重装已提交消息。journal 恢复不重新投递 query，不重放历史工具。
8. 已管理会话的冷 warmup 优先恢复原生 checkpoint，避免公共 history 过滤掉 Leader 内容。公共 history 兜底按目标 work/code profile 过滤，防止冷 Agent 吸入另一 profile 的消息。同模式普通会话继续原路径。

## 错误与边界

忙碌/并发错误返回 `chat.error`、`code`、`retryable=true`，当前 query 不交付。源身份未知、缺少/不完整快照或不可携带的文件 offload 明确失败；这些快照错误为 `retryable=false`，不能以公共 history 静默替代。

保证正常请求中 query 只由既有输入路径提交一次，以及交接自身的可恢复性。query 已消费后的跨崩溃重试仍沿用既有能力，不保证外部工具副作用恰好一次。交接锁与既有 Session runtime 一样为进程内所有权，不增加跨服务进程调度协议。

设计依据：[D-20261028-1704-agent-leader-context-handoff](../../designs/active/D-20261028-1704-agent-leader-context-handoff/design.md)。
实施与测试：[F_20261029_0001_agent-leader-context-handoff.md](../../features/F_20261029_0001_agent-leader-context-handoff.md)。
