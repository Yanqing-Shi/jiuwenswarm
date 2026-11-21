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


## 2026-11-21 04:44（Asia/Singapore）：共享窗口不再写入上游的快照文件与 checkpoint

用户确认后实施（开发会话，2026-11-20）。

**起因**：分支变基到上游 `9d18e0722` 后，上游新增的上下文快照（`context_snapshot.py`）在每次模型请求前、每轮工具结果装入后保存当前窗口。
共享记录上的 Session 因此把同一个窗口写三份：core 提交共享记录；Swarm 写快照文件；随后 `session.update_state({"context": states})`
又把完整窗口放回 checkpoint（core 的 `save_contexts` 本来有意不把共享窗口放进 Session 状态）。共享 Session 恢复时只读共享记录
（`session_ops_service` 的 bound 分支），后两份从不被读取。

**实测**（本机，Swarm 配置的 SQLite 持久化 checkpointer 外加 `ContextCheckpointer`，合成消息，12 次取中位数，单次保存步骤）：

| 窗口 | 改前整步 | 其中共享记录 | 其中快照文件 | 其中 checkpoint | 改后整步 |
|---|---|---|---|---|---|
| ~10k tokens | 12.2 ms | 2.0 ms | 4.4 ms | 3.8 ms | 3.4 ms |
| ~50k tokens | 23.7 ms | 2.8 ms | 10.8 ms | 5.6 ms | 5.0 ms |
| ~150k tokens | 52.1 ms | 5.0 ms | 26.0 ms | 9.8 ms | 7.1 ms |
| ~250k tokens | 82.6 ms | 9.2 ms | 41.4 ms | 14.7 ms | 11.4 ms |

**实现**：`prepare_context_snapshot` 是显式保存（`save_context_snapshot`）与原生提交（`ContextCheckpointer._commit_bound`）共用的准备步骤。
其中若 `states` 含有绑定共享记录的窗口，则不写快照文件、把 `SNAPSHOT_KEY` 置空，只返回该 Session 的其余（非共享）context，
由调用方照常写入 checkpoint。checkpoint 提交本身保留：待答审批（中断状态）仍随它持久化。共享记录仍由 core 在 `save_contexts` 中
先于 checkpoint 提交写入，顺序未变。非共享 Session 的行为不变。

**不变的行为**：共享 Session 的恢复路径没有改动（本来就不读快照）。共享记录与 checkpoint 是两次独立写入、没有回放边界，这一点改动前后相同。

**上游用例 `test_permission_cold_snapshot.py`**：其中两个经 Runtime 建 Session 的用例在每次原生提交处读取快照，此前在本分支上只是因为
“没人读的快照”仍被写出才通过。现在这两个用例对两种存储各跑一遍（`storage` 参数：`snapshot` / `shared`）：`snapshot` 下保留原有的
快照与回放边界断言；`shared` 下改为在提交时读取共享记录，并断言没有快照引用、checkpoint 中没有窗口副本、没有快照文件。
完整的重启恢复流程在两种存储下都执行。此前本分支不覆盖“Runtime 建出的 Session 走快照路径”，现在覆盖。

**测试**：新增 `test_snapshot_step_leaves_a_shared_window_to_its_record`（改动前失败）。

验证（Linux / Python 3.12，仓库 `.venv`，本地 core `16776cbe8` 加未提交的去拷贝修复，`-o addopts='' --asyncio-mode=auto`）：
`test_context_snapshots.py`、`test_context_checkpointer.py`、`test_checkpoint_memory_budget.py`、`test_history_review_regressions.py`、
`agentserver/test_session_input_consumption.py`、`agentserver/permissions`、`runtime` 共 **3615 passed, 2 failed**；
2 项失败是 `test_shared_conversation_runtime.py` 中依赖执行顺序的 team 用例（单独运行该文件 51 passed，改动前同样失败，原因未定位）。

**未覆盖**：真实模型下的长会话；视频双工扩展（`video_duplex`）与共享 Session 并用时对 `SNAPSHOT_KEY` 的处理未核对。

## 2026-11-21 06:43（Asia/Singapore）：应用角色（NPC）会话与共享记录

用户确认后实施（开发会话，2026-11-20）。

**核对结论**：上游的应用角色有三种创建时固定的形态。单角色、以及主角色通过 `ask_<name>` 咨询各自独立成员 Session 的形态，
由 `AppRoleAdapter` 处理：不组装 DeepAgent，每次请求按 `history.jsonl` 重建模型输入，没有上下文窗口。
以 Team 模式创建的形态（`is_app_role_team`）运行在原生 Team 运行时上，没有独立的成员 Session，成员是进程内的预定义成员。
在真实的会话创建入口上探测到：改动前，单角色和咨询形态的主角色也会得到一条从不使用的空共享记录并被标记为共享
（成员 Session 没有记录）；Team 形态的主角色得到主记录。

**改动**：`initialize_main_conversation` 对“应用角色且不在原生 Team 运行时上”的 Session 返回 None，不建记录、不打共享标记；
Team 形态不变。已带有空记录的既有应用角色 Session 不迁移，该记录与标记对它们没有作用（回退时多取一次会话操作锁）。
应用角色会话的模式切换由 Web 模式切换特性拒绝（见 F_20261116_1710 同日记录）。

**测试**：

- `test_application_role_gets_a_main_record_only_in_its_native_team_form`（改动前失败）：单角色、咨询形态的主角色与成员不建记录，Team 形态建记录。
- `test_native_npc_team_keeps_the_lead_on_the_main_record_and_each_member_on_its_own`：以真实的 `build_app_role_team_spec`
  与 Team 运行入口（`_consume_stream_with_query` 加 `prepare_shared_leader`）跑一轮玩家输入：主角色用 `create_task` 给成员派一个发言任务，
  两者各自发言。断言两条 `chat.final` 都送达；主记录含玩家输入与主角色发言、不含成员发言；成员的私有窗口
  （`binding.child(kind="team_member", identity=[team, member])`）含任务内容与成员发言、不含主角色发言；模式保持 Team。
  该用例刻画既有行为，改动前同样通过。

**测试隔离的发现**：Team 运行时的 home 与团队数据库是进程级状态。`build_app_role_team_spec` 所在模块首次导入时会把 Team home
指回默认数据目录，覆盖夹具的设置；探索阶段因此把一个测试团队写进了本机默认的 `~/.jiuwenswarm`（已清除，仅限当次创建的文件）。
该用例在导入之后重新固定 Team home，并为每次运行使用独立的 Session、团队名和团队数据库（`StorageSpec`），
因为同一进程里前一次运行的团队运行时不会随用例结束而消失。本文件中另两个只在整目录运行时失败的 Team 用例尚未按此核对。

验证（Linux / Python 3.12，仓库 `.venv`，本地 core，进程环境变量 `JIUWENSWARM_DATA_DIR` / `OPENJIUWEN_HOME` 指向临时目录）：
本文件 **55 passed**；上一节的范围加 `test_app_role_adapter.py`、`test_app_role_npc_team.py`、`test_team_helpers.py`、
`test_session_ops_fork.py` 共 **3938 passed, 2 failed**，2 项失败仍是前述依赖执行顺序的 Team 用例。
