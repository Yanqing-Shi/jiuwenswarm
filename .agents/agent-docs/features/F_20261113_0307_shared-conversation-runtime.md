---
id: F_20261113_0307_shared-conversation-runtime
title: I-04 共享主窗口的 Runtime 与 adapter 装配
type: F
author: Yanqing-Shi
author_email: toddsyq@gmail.com
designs:
- D-20261112-0530-session-conversation-window
modules:
- jiuwenswarm/runtime
- jiuwenswarm/server/runtime/agent_adapter
- jiuwenswarm/agents/harness/common
files:
- jiuwenswarm/runtime/session/conversation.py
- jiuwenswarm/runtime/service.py
- jiuwenswarm/runtime/session_provisioner.py
- jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
- jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
- jiuwenswarm/server/runtime/session/session_metadata.py
- jiuwenswarm/agents/harness/common/session_ops_service.py
---

# I-04 共享主窗口的 Runtime 与 adapter 装配

## 范围与决策

用户授权继续下一步 I-04，依据收敛版设计 §4.1/4.2/4.4 与 I-04。
复用当前两仓工作区及已完成的 store/binding/操作锁；不以 PR #10243 为基线，不新建锁服务或对话交接协议。
主窗口内部固定为 `(session_id, main)`，不是需要分配的第二个主身份。

新会话在既有异步创建边界建立空记录；原生 Deep Session 与 Team Session 在 pre_run 前取得同一绑定及 provider。
已存在的共享记录严格读取；metadata 的 context_storage 标识受既有字段写入保护，不能被排队旧快照回滚。
旧会话不自动从 history/checkpoint 迁移，未接通共享存储的旧路径继续原行为，处理策略仍待决定。

新共享路径绕过旧复制/owner/pending/receipt；实际 Leader 的 Native 操作所有权单独绑定，不能依赖旧 handoff。
共享 warmup 只刷新当前记录，不能用 UI history 覆盖。其他生命周期消费者属于 I-05/I-06 收尾。

本地源码联动验证不等同于发布依赖验收；本阶段不提交、不发布、不改 pin。


## 2026-11-13：移除复制式 handoff（设计 I-06）

用户检视后确认旧 handoff 提交 `8f7d09bef` 未进入 develop、无上游依赖，决定直接移除而不是保留到收尾阶段；设计 §1、§5 已同步。

移除：`jiuwenswarm/runtime/context_handoff.py` 及 `tests/unit_tests/test_context_handoff.py`；DeepAdapter 的 handoff admission 与
`ContextHandoffError` 响应；Team 流的 `team_conversation_request` / `bind_team_handoff` / `retain_team_handoff` / `team_handoff_run_context`；
`warmup_session_context` 的 `handoff_profile` 参数、`conversation_handoff` checkpoint 分支和按 profile 过滤 history；
metadata 写入对 `conversation_handoff` 字段的保护（`context_storage` 的保护保留）。guard 改为无参数。

行为：绑定共享记录的 Session 不变。没有共享记录的旧 Session 不再经过任何交接或第二把锁，回到 handoff 引入之前的行为。
Team 流中非共享 Session 不再绑定启动前钩子；`prepare_shared_leader` 与 Native 操作所有权绑定保留。

测试：只覆盖 handoff 的用例随之删除（warmup 2 项、metadata 6 项参数化、team_helpers 的隔离 fixture、deep adapter 的 admission 替身），
这四个文件恢复为 `7853595c7` 的内容；共享运行时用例去掉对旧模块的引用。

验证（本地 core 为同日移除后的工作区，`-o addopts='' --asyncio-mode=auto`）：**1666 passed, 1 skipped**，69.75s。
范围：`tests/unit_tests/runtime` 全目录、`server/test_archive_lifecycle.py`、`test_session_ops_warmup.py`、
`agentserver/test_deep_adapter_stream_end_final.py`、`agentserver/test_team_helpers.py`、`agentserver/test_heartbeat_session_runtime.py`、
`server/test_session_metadata.py`、`server/test_session_metadata_write_options.py`。
未运行格式化或静态检查，未提交、未推送，未改依赖 pin。

遗留：远端基线 `fix/tui-session-mode-switch` 当天被 rebase 并强推（现为 `884f90c26`），本分支仍在旧基线 `7853595c7` 上，尚未迁移。
设计同日确认的其余变更（取锁后再读 mode、写回时点、单一建档入口等）尚未实施。
[D-20261028-1704](../designs/active/D-20261028-1704-agent-leader-context-handoff/design.md) 仍在 active，其生命周期状态待用户决定。

## 2026-11-13：core 写回时点调整后的 Swarm 回归

core 同日改为“每次模型请求前 + 运行结束”写回共享记录，`add_messages` 不再逐条提交（见 core 的 F_20261112_1036 同日记录）。
Swarm 运行时代码无需修改：运行之外修改窗口的调用点（审批批次清理、撤回最后一条输入、warmup）本来就显式调用 `save_contexts`。
实际 Leader 某一轮运行时异常后回到 IDLE，现有的 IDLE/TERMINATED 状态订阅照常释放操作所有权。

测试：`test_spawned_member_session_has_only_its_private_binding_and_restores_it` 依赖“追加即写回”，改为显式保存。

验证（本地 core 工作区，`-o addopts='' --asyncio-mode=auto`）：与上一节相同范围 **1677 passed, 1 skipped**。

本次首次把 `tests/unit_tests/agentserver/permissions` 纳入回归（1938 项），发现 24 项失败；恢复逐条写回后失败项完全相同，与本次调整无关。
在两仓基线提交（Swarm `7853595c7`、core `f152a3a42`）上同样三个文件只有 7 项失败，因此：

- 7 项 `test_trusted_web_search`：基线即失败，是本地 core 的搜索结果文案与 Swarm 期望不一致，与共享对话无关。
- 16 项 `test_permission_cold_fresh_input`（全部为 `wrapper_goal` 参数化用例）：共享对话接线引入。
  冷启动后新输入要丢弃未答复的审批批次；基线上冷 context 来自 history，不含暂停的 assistant 调用，由中断状态补入整批。
  现在冷 context 来自共享记录，已含原始调用（`call_0`），而用例只把中断状态改写为包装后的 id（`outer_call_0`），
  `discard_restored_permission_batch` 按 id 未匹配到已有消息，又追加一条 assistant 调用并只闭合新 id，原调用保持未闭合，
  下一次运行装载时报 `conversation_tool_call_unclosed`。真实流程中记录与中断状态是否会出现这种 id 不一致尚未确认。
- 1 项 `test_round_state_persistence[commit-False]`：共享对话接线引入，原因未查。

以上 17 项待用户决定处理方式，本次未修改。未运行格式化或静态检查，未提交、未推送。

## 2026-11-14：审批用例对齐、主记录单一建档入口

用户确认后实施；建档入口对应设计 §4.1 与检视意见 R7。

**上一节遗留的 17 项审批用例**：均为用例与共享记录不一致，产品代码未改。

- 16 项 `test_permission_cold_fresh_input`（`wrapper_goal` 用例）：暂停的调用就是模型自己的 assistant 消息，共享记录保存了它。
  用例原先只把中断状态改写为包装后的调用，现在同时改写 context 中对应的 assistant 消息并保存，使两者一致，与真实流程相同。
- 1 项 `test_round_state_persistence[commit-False]`：用例让执行状态提交失败后仍断言请求成功。共享记录上的 Session 在最终保存失败时
  不宣告成功（设计 C-3、§4.5），用例改为对该组合断言请求失败；中断仍保持待答、清理仍完成等其余断言不变。
  流式变体本来不断言成功，未改。

**单一建档入口**：`initialize_main_conversation(session_id, *, mode=None)` 现在自带统一判断并返回 `ConversationBinding | None`：
仅当 mode 属于原生模式，且该 Session 没有 history、没有执行者 checkpoint 时创建记录；否则返回 None，旧 Session 保持原行为。
创建是幂等的，调用方不再加锁。原先四处各自的判断条件收敛为“这里是不是 Session 的创建边界”：

| 调用点 | 保留的边界条件 | 移除 |
|---|---|---|
| `AgentRuntime.create_session` | 新建（未指定 ID 或无既有 descriptor） | `history_exists` 判断、非阻塞 Session 操作锁 |
| `_prepare_chat_turn` | chat 请求、非 attach-only、非斜杠命令、尚无 metadata | `supports_shared_mode`、`history_exists` 判断 |
| `RuntimeSessionProvisioner` | 位于新建分支内 | `supports_shared_mode` 判断、包住整个新建分支的非阻塞 Session 操作锁（可能向创建请求抛出 busy） |
| DeepAdapter `start_interaction` | 无绑定且尚无 metadata（不经 Runtime 直接驱动 adapter） | `history_exists`、checkpoint 判断 |

调用点仍是四个：Session 确实可以从这四处产生，本次没有把它们改道到同一处。provisioner 相对基线的差异降为 3 行。

**成员 Session**：core 的 `create_member_session` 去掉了 `card` 参数（见 core 的 F_20261112_1036 同日记录），对应用例同步修改；
Swarm 运行时代码不直接调用它。

测试：新增 `test_main_record_is_created_only_for_a_session_without_earlier_conversation_data`。

验证（本地 core 工作区，`-o addopts='' --asyncio-mode=auto`）：上一节范围加 `tests/unit_tests/agentserver/permissions`，
**3608 passed, 1 skipped, 7 failed**；新增用例所在文件另行运行 30 passed。7 项失败全部是 `test_trusted_web_search`，
在旧基线提交上同样失败（本地 core 的搜索结果文案与期望不一致），与共享对话无关。
未运行格式化或静态检查，未提交、未推送。

注意：本分支所基于的 `fix/tui-session-mode-switch` 是尚未合入的 PR，远端已多次 rebase（2026-11-13 晚为 `dd16c87b5`），
本分支仍在旧副本 `7853595c7` 上；上述基线对比以旧副本为准，迁移到新基线尚未进行。

