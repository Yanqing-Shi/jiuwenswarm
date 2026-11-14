---
id: F_20261112_0650_session-operation-ownership
title: I-00 Session 操作锁与生命周期基础
type: F
author: Yanqing-Shi
author_email: toddsyq@gmail.com
modules:
- jiuwenswarm/runtime
- jiuwenswarm/server/runtime/session
- jiuwenswarm/agents/harness/team
files:
- jiuwenswarm/runtime/session/operation_lock.py
- jiuwenswarm/runtime/session/native_operation.py
- jiuwenswarm/runtime/session/coordinator.py
- jiuwenswarm/runtime/service.py
- jiuwenswarm/runtime/context_handoff.py
- jiuwenswarm/agents/harness/team/team_manager.py
- jiuwenswarm/server/runtime/session/project_store.py
- jiuwenswarm/server/runtime/session/session_archive.py
- jiuwenswarm/server/runtime/session/lifecycle.py
- tests/unit_tests/runtime/test_session_operation_lock.py
- tests/unit_tests/runtime/test_runtime_session_coordinator.py
- tests/unit_tests/runtime/test_runtime_service.py
- tests/unit_tests/runtime/test_interaction_answer_delivery.py
- tests/unit_tests/server/test_archive_lifecycle.py
- tests/unit_tests/agentserver/test_heartbeat_session_runtime.py
- docs/zh/项目与会话管理.md
- docs/en/Project-Session-Management.md
designs:
- D-20261112-0530-session-conversation-window
---

# I-00 Session 操作锁与生命周期基础

日期：2026-11-11（America/New_York）。

用户本轮仅授权先实施 I-00，旧 Session 处理另议；随后明确要求以当前仓库为基线，PR #10243 只作锁实现参考。完整共享窗口草稿保持 proposals/draft，本记录不表示 I-01～I-07 获准或已实现。

## 决策归属与实施

基于 JiuwenSwarm 当前工作区（起点 `8f7d09bef`），从现有 SessionArchiveService 提取执行锁身份，并接到 Runtime。未合入或复制 PR #10243 的候选实现及测试；未修改 agent-core、Gateway 或依赖版本。旧 handoff 的消息复制与 profile 隔离继续有效，本次只补充完整运行互斥，契约见 [S_02](../specs/runtime/S_02_session-operation-ownership.md)。

- 公共锁沿用现有 owners 路径及 `project_store.file_lock`，新增非阻塞取锁能力；进程内注册表和显式引用保证不同 Runtime/lifecycle 对象不会各建一套同名锁。
- Coordinator 包住实际 unary/stream producer，等待交互保留所有权，控制输入保留原操作引用。用户 admission 在等待锁前执行，保留 heartbeat 抢占；独立 goal 与 heartbeat 不再与聊天同时执行。
- Leader 复用 NativeHarness 的 guard 与状态订阅，在主运行之外继续写入时保留相同执行锁，直到最终 checkpoint 后的 IDLE 或异常退出。无需修改 core 或通过前台 waiter 推断执行结束。
- 当前 lifecycle.begin 增加未受理检查阶段和检查前的 fence 状态。撤销按 operation ID 校验并恢复 writer generation，避免拒绝操作后让已经受理的队列写入失效。资源文件仍为操作与 fence 的共同提交点。
- 复用现有 finalizer、flush、写入 fence、恢复与有限重试。新增检查恢复及事件过滤；cron 级联先 fence、停止原运行，再继续取锁清理。保留项目锁顺序。

## 取舍

不使用短时 resource_lock 包住模型运行，否则运行自己的 checkpoint/history 写入可能自锁。不新建窗口 CAS、ownership epoch 或分布式 lease 系统，也不以所有 stream 的存活时间代替执行生命周期；纯观察流必须能够空闲。

保留旧模式 CAS、准备阶段 busy 检查与 handoff API，避免在 I-00 提前更改对话归属。跨执行者共享、旧历史兼容、fork/rewind 的共享窗口接线均留给后续阶段。本轮没有批准整份草稿，因此不迁移或标记整份设计 approved。

## 验证

环境为 Python 3.12.14，仓库内隔离测试环境复用已有缓存依赖；本地 agent-core 为 `82b2b4efc`，Gateway 为声明的 `a2189850d1a88e307b76d0bfb99fa50b921ce96c`，protocol 为声明的 `4c5f3e9eb41bd33d34d012b06a3e364e8497ac11`。I-00 不升级 pin。模型响应使用可控替身；NativeHarness 的 supervisor、状态事件与保存边界为实际实现。

最终受影响回归为 **1529 passed, 1 skipped**，覆盖 Runtime 全目录、生命周期、handoff、heartbeat、Team 管理、Gateway 生命周期消费和 metadata。最后补齐了 Coordinator 成功关闭时释放无人订阅的 round 引用；该修改及对应新增用例经过 **350 passed** 的定向复测，包含 Runtime 服务/协调/Team 生命周期、全部 24 个操作锁用例、handoff 和 project_store。两组数量有重叠，不相加。

回归中修复了恢复扫描与已归档目录争锁，以及中断检查被重试请求直接提升为受理的路径。旧的并行/自动停止预期按 I-00 更新。恢复后手动触发 heartbeat 的用例禁用了竞争同一到期任务的后台 tick，手动触发、admission 和实际执行保持真实。

```bash
source .venv/bin/activate
export PYTHONPATH=.:../agent-core:/tmp/i00-agent-gateway:/tmp/i00-agent-protocol/openjiuwen-protocol:/tmp/i00-agent-protocol/A4P
python -m pytest --no-cov -q -o log_cli=false \
  tests/unit_tests/runtime \
  tests/unit_tests/server/test_archive_lifecycle.py \
  tests/unit_tests/test_context_handoff.py \
  tests/unit_tests/agentserver/test_heartbeat_session_runtime.py \
  tests/unit_tests/agentserver/test_stream_heartbeat.py \
  tests/unit_tests/agentserver/test_team_manager_registry.py \
  tests/unit_tests/agentserver/test_team_manager_skill_state.py \
  tests/unit_tests/gateway/test_lifecycle_handlers.py \
  tests/unit_tests/server/test_lifecycle_read_retry.py \
  tests/unit_tests/server/test_session_metadata.py \
  tests/unit_tests/server/test_session_metadata_write_options.py

python -m pytest --no-cov -q -o log_cli=false \
  tests/unit_tests/runtime/test_session_operation_lock.py \
  tests/unit_tests/runtime/test_runtime_session_coordinator.py \
  tests/unit_tests/runtime/test_team_session_lifecycle.py \
  tests/unit_tests/runtime/test_runtime_service.py \
  tests/unit_tests/test_context_handoff.py \
  tests/unit_tests/test_project_store.py
```

沙箱内涉及线程回调的测试出现停顿，沙箱外同组测试正常完成。大范围测试结束时另有企业版 startup sweep 的后台缺依赖日志（`openjiuwen_runtime`）；企业 foundation/MySQL 装配不在本次验证结论内。未运行格式化或静态检查，未提交、推送或操作远端 PR。

## 遗留与证据边界

只交付 I-00；没有共享窗口存储、压缩/offload 共享或旧 Session 升级处理。真实模型、Windows/macOS、企业 foundation/MySQL 和跨服务进程共同运行 Session 未验证。现有跨进程文件锁的冲突通过子进程验证，不等于支持多个服务进程共同推进同一 Session。

核对了当前 pin `f3f8f576164cc405c046e43079c8ad00ee17f0c4` 的 NativeHarness：它已有状态订阅，但缺少当前 Swarm handoff 基线所使用的 conversation guard/prepare 等接口。本次没有新增 core 接口；依赖发布与 pin 差异不能用本地源码测试宣称已解决，后续配套版本发布时必须重验。当前 I-00 证据适用于上述本地配套 core。

按 jw-review 对本次工作区做了本地自查：基线/候选差异、共用锁身份、取消及关闭、检查撤销与恢复、事件消费、依赖边界。没有独立评审或远端 CI 结论；完整共享窗口设计仍未获整体验收。

用户说明同步至中英文项目与会话管理文档。后续 core/store、运行入口或生命周期协议改变时，需要重验这些执行边界。


## 2026-11-12：普通 chat 的反向互斥补充

用户明确普通 chat 在 `/delete`、`/archive` 或模式切换过程中同样不能启动任务。
本次补充公开 Runtime 的 unary/stream 六种反向组合测试：mutation 先取得操作锁，chat 不得选择执行者；
删除/归档受理后请求释放物理锁，持久化 fence 仍拒绝 chat；模式切换后排队 chat 重新读取模式。

实测发现模式切换取得所有权后到达的 chat 虽然等待锁，其 pending 标记仍被旧 busy 检查当成活动工作，
导致先取得锁的模式切换反而失败。busy 判断现固定在取锁时点，保留取锁前准备阶段的忙碌检查，
后来的排队请求不能否决已取得所有权的切换。现有同 profile plan 请求语义保持原约定。

本补充与 I-03 使用当前两仓工作区验证；无提交、无 pin 变更，旧 Session 处理仍另议。


本补充最终验证：**1447 passed, 1 skipped**，47.54s，日志 `/tmp/i03-swarm-regression-final.log`。
范围：`tests/unit_tests/runtime` 全目录、`server/test_archive_lifecycle.py`、`test_context_handoff.py`、
`test_session_ops_warmup.py`、`agentserver/test_deep_adapter_stream_end_final.py`、`agentserver/test_team_helpers.py`。
其中反向互斥新增 6 项，使用真实 public Runtime 和实际模式切换/生命周期入口，仅用屏障控制并发时机。
三种变更持锁时 `agent_calls` 均为空；模式切换后排队请求选用已提交的 code executor；
删除/归档已受理后，在 finalizer 尚未启动时 chat 明确失败。
独立较窄回归 **261 passed**（Runtime service、mode switch、operation lock）与本组重叠，不相加。
本地 core 同候选回归 **1687 passed**；Swarm 安装 pin 保持不变，发布包兼容性仍需发布后验证。


## 2026-11-12：按收敛设计复查重复收尾

用户要求依据收敛后的 design.md 简化已实现冗余。Coordinator._operation_scope 原先有内外两层相同的
FAILED/CANCELLED 标记及收尾；现合并为一个异常处理和 finally，统一释放执行引用与 admission。
已取得所有权后即使流的关闭失败，也须将原终态覆盖为失败；取得所有权之前的拒绝只标记尚未结束的 handle。
现行 Session 锁、Native 后台所有权、lifecycle fence/恢复和旧 handoff 活跃消费者各有真实职责，保持现行保护。
此处不新增生命周期能力，也不因设计缩减范围撤销此前已落地行为。


本次简化最终验证：**1447 passed, 1 skipped**，44.01s，日志 `/tmp/session-simplify-swarm-rechecked.log`。
同一候选本地 core **1702 passed**，25.06s，日志 `/tmp/session-simplify-core-rechecked.log`。
范围沿用本记录 I-03 联动回归；覆盖失败/取消后释放、流关闭收尾、Native 后台所有权、普通 chat 与 mode/archive/delete 双向互斥。
不重复累计定向 136 项或上一候选回归。设计/当前保护边界保持，未提交，未更新依赖 pin。


## 2026-11-13：旧 handoff 移除后的说明

上文“保留旧模式 CAS、准备阶段 busy 检查与 handoff API”中的 handoff API 已移除，见 F_20261113_0307 同日记录；操作锁、Native 后台所有权和生命周期保护未改动。Leader guard 现为无参数回调。


## 2026-11-13：取得所有权后确认运行类型

用户选择“先取锁再读 mode”后，核对发现无法逐字实现：审批回复、stop、steering 和发给运行中 Leader 的输入必须加入当前运行，
请求在取锁前必须先被识别；而执行者本来就在所有权内由 `normalize_request` 重读 mode 后选定。实际缺口是：
被识别为单 Agent（或 Team）的 chat 在等锁期间遇到跨类型的模式切换，仍按旧类型的协调路径运行。用户确认按以下方式落地。

- Coordinator 的 `run_unary` / `run_stream` / `run_team_stream` 增加 `on_owned`，在取得所有权、lifecycle guard 和 generation 检查之后、运行开始之前调用。
- Runtime 在 `on_owned` 中重读持久化模式；类型不一致抛 `SessionRouteChangedError`，旧 execution 记为 CANCELLED（不是 FAILED），
  `invoke` 重新分派，`stream` 仅在尚未产生输出时重新分派，上限 3 次。没有新增队列。
- 移除模式切换的 `_session_mode_switch_busy` 与 `busy_at_acquisition`：切换只取决于操作锁。可见变化：chat 已到达但未开始运行时切换不再返回
  `SESSION_BUSY`，该 chat 按切换后的模式运行；运行期间仍返回 `SESSION_BUSY`。
- Leader 的 mode / lifecycle 复核只在新取得 Native 操作所有权时执行；单 Agent 适配器不再安装 `session_mode_guard`（其运行始终在持锁运行内）。

未做：session input、mailbox（`submit_unary`）和后台路径未接 `on_owned`；Coordinator 的四处租约登记未合并；
`NativeHarness.send` 在 `_require_alive` 之前取得租约的顺序问题属于 core，留待下一步。

测试：新增跨类型重新分派 4 项（unary/stream × Agent→Team/Team→Agent，使用真实 public Runtime 与实际模式切换入口，
断言同一 request 先后两个 execution 的类型及首个为 CANCELLED）；临时禁用确认逻辑时这 4 项全部失败，恢复后通过。
新增“切换只取决于操作锁”1 项、Leader 每次所有权只复核一次 1 项；原先用到达标记模拟忙碌的 2 个用例改为持有操作锁。

验证：**1682 passed, 1 skipped**，59.92s。范围为本记录上一节的 Swarm 范围加 `tests/unit_tests/gateway/test_lifecycle_handlers.py`，
本地 core 为同日移除 handoff 后的工作区。未运行格式化或静态检查，未提交、未推送。


## 2026-11-13：先后顺序规定与重新分派上限

用户规定：运行在先、模式切换在后，切换被拒绝并由用户重试；模式切换在先、运行在后，运行排队到切换完成后启动，不能失败后让用户重试。
前者由非阻塞取锁保证，后者由 lane 与等待操作锁保证，原有用例已覆盖。上一节的重新分派设了 3 次上限，耗尽时会把错误交给用户，
与该规定不符：现取消上限，`SessionRouteChangedError` 携带持锁读到的模式，只有连续两次读到同一模式（并未发生切换，属于识别与确认不一致的缺陷）才抛出。
先后以取得操作锁的顺序为准；chat 已到达但尚未取得锁时发生的切换，结果是切换成功、该 chat 按新模式运行，两者都不失败。

已知边界：Leader 自行续跑（成员消息、异步工具完成）是同步取锁，恰逢模式切换持锁的瞬间会被拒绝，沿用其既有的重试/未读语义；这不是用户发起的运行。

新增用例 2 项（unary/stream）：连续 5 次切换后仍启动且只运行一次；同一模式连续两次则抛出。


## 2026-11-13：“运行在先”从到达起算（撤回移除 busy 检查）

用户补充：模式切换不同于归档/删除，是会话中的中间操作；运行迟迟未开始时用户可能切换模式并以为只影响下一次运行，这种情况应返回切换失败。
因此撤回本日较早“移除 `_session_mode_switch_busy`”的改动，按原实现恢复该判断及取锁时点取样（`busy_at_acquisition`）；
上一节“切换只取决于操作锁”“已到达但未开始的 chat 按新模式运行”不再成立。取得所有权后确认运行类型并重新分派的逻辑保留，
覆盖没有到达标记的入口（直接调用 Runtime、后台路径在创建 execution 之前的片刻）和其他进程提交的切换。
“到达”指服务端接收（`begin_chat_request`）或进入 Runtime 队列，不是用户输入时间。

测试：两个用到达标记的原用例恢复原样；“切换只取决于操作锁”的用例改为同时断言到达未开始与持锁两种情况都被拒绝、无运行时切换成功；
新增 2 项（unary/stream）经真实 Runtime 入口：运行卡在启动前的 admission、尚未取得操作锁时切换返回 `SESSION_BUSY`，
放行后该运行按原模式执行，随后切换成功。

## 2026-11-14：移除归档/删除相关改动，保留接入点

用户决定：`/delete` 与 `/archive` 功能已存在，但与共享对话无关，不在本方案内处理；移除本分支中全部相关改动，为后续开发保留位置。

移除（恢复为基线 `7853595c7` 的内容）：
- `server/runtime/session/session_archive.py`：`lock()` 改用 `acquire_operation`、`check_idle` 受理、`_require_idle` / `RUNTIME_STATE_UNAVAILABLE`、
  `_recover_admission` 与孤立 fence 恢复、cron 级联的 `allow_stop`、项目批量归档逐项拒绝运行中 Session。
- `server/runtime/session/lifecycle.py`：`begin(initial_phase=...)`、`reject_unaccepted`、`check_idle` 的投影/事件过滤。
- `tests/unit_tests/server/test_archive_lifecycle.py` 恢复基线；`test_session_operation_lock.py` 删除 6 个归档/删除用例并把两个混合用例收敛为只测模式切换；
  `test_heartbeat_session_runtime.py` 撤回两处删除相关改动。
- Coordinator 取得所有权后和 `resume_waiting_for_input` 中追加的 `lifecycle.guard` 调用（前者紧随其后的 `_require_open_session` 本来就会检查，属重复）；
  `session_mode_guard` 中的 lifecycle 复核。

行为回到基线：归档/删除可以受理运行中的 Session 并自行停止运行，不再返回本分支引入的 `SESSION_BUSY` 拒绝。

为此必须同步的一处改动：运行/模式切换的操作锁原先复用归档服务的 `lifecycle/owners/{kind}_{id}.json.lock`。
若只恢复归档代码，运行会在整个执行期间占住归档要取的锁，基线的归档/删除将被运行阻塞。操作锁因此改用独立的
`lifecycle/operations/{kind}_{id}.json.lock`，新增用例 `test_run_lock_leaves_the_archive_and_delete_owner_lock_free`。
`project_store.file_lock(blocking=...)` 是操作锁自身需要的，保留。

保留的接入点见 S_02“预留：归档与删除”，均未实现。已知后果：删除 Session 不清理其共享对话记录。

本文较早各节关于归档/删除受理、恢复的记录是当时的实施事实，保留作为历史，不再代表当前代码。

验证（本地 core 工作区，`-o addopts='' --asyncio-mode=auto`）：runtime 全目录、test_archive_lifecycle、warmup、deep adapter、team helpers、
heartbeat、metadata、agentserver/permissions，**3594 passed, 1 skipped, 8 failed**。8 项失败在基线提交上同样失败：
7 项 `test_trusted_web_search`（本地 core 的搜索文案）与 1 项 `test_gateway_session_events_carry_project_id`（本地 gateway 副本），与本方案无关。
移除前的工作区改动备份为会话临时文件，未保留为仓库内容。未运行格式化或静态检查，未提交、未推送。

