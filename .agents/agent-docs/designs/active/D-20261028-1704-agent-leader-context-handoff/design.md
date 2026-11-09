---
id: D-20261028-1704-agent-leader-context-handoff
title: 单智能体与团队 Leader 的最小上下文交接
slug: agent-leader-context-handoff
status: in-development
stage: active
reviewers: []
created: 2026-10-28
updated: 2026-11-01
domain: core
modules:
- jiuwenswarm/server/runtime/agent_adapter
- jiuwenswarm/runtime
- agent-core/openjiuwen/agent_teams/harness
- agent-core/openjiuwen/core/context_engine
tags:
- session
- context
- checkpoint
- mode-switch
scope: 同一产品 Session 内 agent 与实际团队 Leader 双向交接对话上下文
related: []
author: null
author_note: 从恢复补丁导入；原始作者无法从 Git 确认
type: D
files: []
---

# 单智能体与团队 Leader 的最小上下文交接

## 1. 目标与最小性判断

用户在同一个聊天 Session 中切换 agent/team 时，目标执行者接管上一位执行者的对话上下文，继续同一段对话。团队只交接实际 Leader 的上下文，不收集每个成员的私有历史。

首版覆盖普通 work 的 agent/team；2026-11-01 经用户确认扩展为同一 profile 内的 `agent.work.*` ↔ `team.work.*` 及 `agent.code.*` ↔ `team.code.*`，均包含 normal（常规执行）和 plan（规划）子模式。work ↔ code 不交接，也不共享其交接归属；若同一 Session 已建立另一 profile 的归属，明确拒绝跨 profile 请求。`team.design` 和外部 CLI runtime 仍不包含。交接依据是单智能体与实际 Leader 之间的执行主体变化，而不是 UI 模式名称变化。同一实际执行者进入或退出 plan 时继续使用原上下文，不触发交接，也不改变既有 plan 行为。

验收主场景：agent 收到 A，切到 team 后 Leader 知道 A 并收到 B，再切回 agent 后知道 A 和 B。目标实例是否命中缓存，不改变此行为。

在“同一时间只有一个对话执行者推进，目标接管源快照”的约束下，这是比双历史增量合并更小的方案。保留 mode → session 的缓存结构，不增加逐消息时间戳、历史去重游标或新的数据库表。复用现有 checkpoint 编解码及 commit，但不能把源 checkpoint 整体覆盖到目标 Agent。

这不是只修改 encode/decode 的补丁：它们已经负责存储。缺失的是切换时的交接、实际执行者定位，以及缓存和 checkpoint 的一致更新。

## 2. 当前行为与依据

路径相对 JiuwenSwarm 仓库；`../agent-core/` 指同级 core 仓库。

| 现有代码 | 已核实行为 | 对本方案的意义 |
| --- | --- | --- |
| `jiuwenswarm/server/runtime/agent_manager.py::_make_agent_cache_key/get_agent` | 外层按模式、子模式、项目选择实例 | 保留，不重排层级 |
| `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py::_get_or_create_session_adapter` | 命中缓存直接返回；新建分支调用历史 warmup | 交接必须覆盖缓存命中分支 |
| `jiuwenswarm/agents/harness/common/session_ops_service.py::warmup_session_context` | 把公共历史恢复到传入的 DeepAgent；已有 context 默认跳过 | 不能把它当作跨模式交接入口 |
| 同文件 `_build_context_messages_from_history` | 保留 user，随后仅接受 assistant；leader/teammate 被跳过 | 本方案直接转移原生上下文，不走此过滤器 |
| `jiuwenswarm/server/runtime/agent_adapter/team_helpers.py` | 请求交给团队 Runner，当前 query 单独传入 | 必须在首次消费 query 前给实际 Leader 导入上下文 |
| `../agent-core/openjiuwen/agent_teams/harness/team_harness.py::start` | 创建成员 child session、pre_run、启动 NativeHarness | 团队侧恢复与导入的关键边界 |
| `../agent-core/openjiuwen/core/context_engine/context/context.py::save_state/load_state` | 保存/恢复 messages、offload_messages、last_context_window_access_at | 复用消息状态格式 |
| `../agent-core/openjiuwen/core/context_engine/context_engine.py::save_contexts` | 将 context 状态写入 session | 导入后也需更新此状态 |
| `../agent-core/openjiuwen/core/session/agent.py::commit` | 调用 checkpointer.post_agent_execute | 复用目标持久化路径 |
| `../agent-core/openjiuwen/core/session/checkpointer/persistence.py::AgentStorage` | 按 session_id + agent_id 保存状态，pickle 编解码 | 保留源/目标各自的存储 key |

当前 adapter 自带的 DeepAgent 与团队实际 Leader 不是同一对象。给 team adapter 的 `_instance` 导入消息，不能实现 Leader 交接。

## 3. 最小功能改动清单

| 编号 | 必须改变的能力 | 最小实现边界 |
| --- | --- | --- |
| M1 | 识别并串行执行模式切换 | 同一 session 的请求进入实际执行前比较执行主体；普通同模式请求保持原路径。记录上一实际主体，不能仅依赖可能已被当前请求覆盖的 UI mode |
| M2 | 定位源和目标实际执行者 | agent 对应 session adapter 的 DeepAgent；team 对应实际 Leader 的 NativeHarness/child session。使用实际 card/agent ID，不硬编码成员名或根据 mode 猜 checkpoint key |
| M3 | 导出、导入对话快照 | 复用 context.save_state/load_state 的消息结构；深复制源消息及必要 offload 数据，映射到目标 context ID，替换目标旧消息 |
| M4 | 同步内存与目标 checkpoint | 先完成目标原有恢复，再导入；更新目标 context engine 和 session context，使用现有 commit；缓存命中也执行 |
| M5 | 交接失败与重试控制 | 目标 commit 成功前不投递新 query、不宣布切换完成。保存最小交接标识，重试同次交接不重复替换快照或从旧目标反向覆盖源；正常切换只投递一次当前 query。跨崩溃请求去重边界见第 6 节 |

M1/M5 可复用已有 session 请求串行化与持久化状态载体，不另建调度器。具体挂载点需实现时核对其锁是否覆盖团队异步执行；拿到普通请求锁不等于 Leader 已停止。

已核对：`team_helpers.py::_process_team_message_stream_locked` 的 startup lock 会在流式输出前释放，不能直接作为完整交接锁；`runtime/session/coordinator.py::SessionCoordinator` 只管理内存执行状态，不能独自承担实际执行主体及交接中间态的持久化。

## 4. 时序与快照边界

```text
收到跨模式请求（当前 query 尚未进入模型）
  → 在同一 session 的交接保护下确定源主体和目标主体
  → 检查源和目标是否满足静止边界；不满足则立即返回可重试错误
  → 在交接保护下确认没有执行中/未闭合的工具调用与待自动续跑输入
  → 源 save_contexts + commit，取得独立消息快照
  → 取得或创建目标 runtime，完成其正常 checkpoint 恢复
  → 在目标消费任何输入之前导入快照
  → 目标 save_contexts + commit（含交接标识）
  → 更新实际对话所属主体
  → 将当前 query 正常投递一次
```

最低支持边界是源和目标都静止、工具结果完整。源或目标忙碌、存在执行中工具时，首版立即返回“暂不可切换”的可重试错误，保持原执行归属，不投递当前 query，不等待执行结束，也不自动暂停或取消工具。团队成员仍可能发消息唤醒 Leader，所以交接期间必须阻止 Leader 自动启动新轮次，不能只检查一次 idle。

首版不迁移运行中的任务、工具、未回答的人机交互或 pending follow-up。遇到这些情况同样立即返回可重试错误；待既有流程使源和目标满足上述静止边界后，由调用方重试。仅处于暂停状态不自动等于满足交接条件。后台继续推进两个独立对话分支不属于本方案。

## 5. 编解码如何复用

### 5.1 复用现有格式与存储

```text
源 context.save_state()
  → 独立的对话快照
  → 目标 context.load_state({目标 context ID: 快照})
  → 目标 context_engine.save_contexts(目标 session)
  → 目标 session.commit()
  → 现有 pickle serializer → SQLite KV upsert
```

内存交接不必为了复制消息先写数据库再读数据库。使用独立的消息/状态拷贝即可；需要序列化往返时复用既有受支持 codec，不新增 JSONL 转换或自制 pickle 解析。

源 runtime 已回收时，通过现有 session/checkpointer 恢复路径取得源状态，再提取 context。不要在应用层直接查询 SQLite、拼接 blob key 或调用私有 `_serialize_state`。如果现有公共接口不能无副作用地导出源上下文，新增一个范围很小的 core 导出/导入接口；避免仅为读历史重新启动整个团队。

### 5.2 仅转移哪些字段

转移 `messages`，以及其中引用所需的 `offload_messages`。保留消息内容、来源 metadata、消息 ID 和完整工具调用/结果配对。不会按业务角色丢弃成员结果；Leader 已收到的成员信息本来就在其消息中。没有收到的私有成员对话不补抓。

保持目标的系统提示词、工具注册、权限、模型、workspace、技能、DeepAgent 任务状态、turn 计数、团队 session 身份与成员状态。源的 `deepagent`、pending_follow_ups、团队全局状态不能随 blob 整体覆盖过去。

历史内已有的团队提示或框架注入消息只是历史来源内容，不提升为目标 system prompt。目标当前身份与能力仍由目标配置生成；必要时附加一个明确的模式切换说明。Leader 的团队上下文 tracker 在消息替换后应重新注入当前状态，避免其“已投递”基线让身份/成员列表永久缺失。

`last_context_window_access_at` 不作为交接游标。目标使用自身上下文统计生命周期；旧 token 统计应按已有 load/invalidation 机制失效。被压缩掉的历史不从原始 JSONL 展开，保持源当前对话表示。

若 offload 数据引用源私有文件，必须确保目标读取仍有效，或在导入时物化到目标可用的存储；不能仅复制失效路径。此兼容处理属于消息快照正确性，不是文件/记忆同步功能。

### 5.3 为什么不能直接覆盖整个 checkpoint

源和目标采用不同 agent_id，各自保存到自己的 key。整个 blob 除 context 外还含任务状态、技能状态、暂停信息和轨迹编号。整 blob 复制虽然代码可能更短，却会将 Leader 的执行状态变成单智能体的状态。

正确边界是“恢复源 → 提取对话 → 更新目标对话 → commit 目标”，而不是把源 blob 写到目标 key。

## 6. 交接标识与失败行为

建议在现有 session 状态中增加一个小字段（名称为设计示意），随目标上下文一起 commit：

```python
context_handoff = {
    "id": "<switch-request-id>",
    "source_agent_id": "<actual-source-card-id>",
    "target_agent_id": "<actual-target-card-id>",
}
```

这不是每条历史记录的同步游标，只用于识别某次上下文替换是否已经完成。实际执行主体及待处理交接使用既有 session 持久化载体记录。应定义崩溃恢复顺序：目标已 commit 而所属主体尚未更新时，凭目标标识完成交接；不得重新以旧目标为源取快照。

交接幂等与用户 query 去重是两个独立保证。首版保证交接本身可恢复、正常切换不重复投递当前 query；普通聊天请求跨崩溃的去重沿用现有能力，不新增持久化输入接收记录或通用请求恢复机制。当前尚未确认普通聊天请求具有跨崩溃的完整去重保证，不能把特定消息通道的去重能力视作所有聊天入口的保证。

例如目标已接收 B、甚至已执行工具，但响应前进程崩溃，客户端随后重发 B：交接标识只能证明快照已导入，不能证明 B 是否已消费或工具执行到哪里。本方案不承诺该窗口下 B 或工具副作用绝不重复执行；这也不允许交接逻辑主动重放历史工具调用。若以后要求更强保证，需另行设计持久化输入记录、恢复规则及工具副作用的幂等支持。

导出或导入失败：不执行目标新问题，保留源持久化快照；如果目标内存已被修改而 commit 失败，恢复目标原快照或丢弃该缓存实例再恢复。不能悄悄退回过时上下文并向用户报告切换成功。

旧会话没有交接标识时不自动认定已同步；首次真实切换从确定的上一实际执行者导出。源身份无法确定或快照不存在时明确失败，不能仅凭最后一条任意 checkpoint 的时间猜测。

## 7. 涉及文件与复用范围

| 模块 | 计划变化 |
| --- | --- |
| `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` | 在目标处理当前 query 前接入交接，覆盖缓存/新建路径；普通同模式 warmup 不变 |
| `jiuwenswarm/server/runtime/agent_adapter/team_helpers.py` | 团队启动/恢复后、Leader 自动执行及首条 query 前完成交接；读取实际 Leader |
| `jiuwenswarm/runtime/` 或已有 session lifecycle 服务 | 小型交接协调逻辑，持有 session 级交接边界和实际主体信息；优先复用现有载体 |
| `../agent-core/openjiuwen/agent_teams/harness/` | 按需暴露实际 Leader 对话导出/导入及启动前屏障；不引入应用历史文件依赖 |
| `../agent-core/openjiuwen/core/context_engine/` | 复用状态 codec；仅在现有接口不足时补充安全替换快照的公共接口 |
| checkpoint serializer、SQLite schema、`agent_manager` 缓存层级 | 保持现有机制，无计划变更 |

上述是最小能力边界，不承诺所有能力已经有可直接调用的公开 API。尤其团队恢复可能触发排队输入，需要在 core 的实际启动顺序核实屏障位置，不能在 Runner 已经开始输出后再替换消息。

## 8. 备选方案与非目标

| 备选 | 暂不采用的原因 |
| --- | --- |
| 重排 session/mode 缓存层级 | 不会自动转移上下文，改动面更大 |
| 每次从公共 history.jsonl 增量合并 | 需要事件转换、身份映射、去重和边界处理；本需求只需接管上一执行者 |
| 整体复制 checkpoint blob | 会覆盖目标的非对话运行状态 |
| 只在新建实例时回灌 | 无法修复 agent → team → 已缓存 agent |

不包含独立成员历史合并、共享 MEMORY.md、工具能力迁移、额外的逐消息时间戳、并行模式分支合并、work ↔ code 跨 profile 交接、`team.design` 或外部 CLI runtime 的通用迁移，也不新增跨崩溃的通用请求去重或工具副作用恰好一次执行保证。目标工具不可用时保留过去的工具调用结果作为历史，不注册源工具、不重放旧调用。

## 9. 验证计划

1. agent 收到随机 A → team Leader 准确引用 A 并收到 B → 缓存 agent 准确引用 A/B。
2. Leader 自己生成随机 D（用户输入不包含 D）→ agent 准确引用 D，排除重新推导。
3. 双向切换分别覆盖目标新建、目标缓存、源已回收、进程重启后的恢复。
4. 导入后模型确实使用新消息，下一轮 context.load_state 不会把旧 checkpoint 消息覆盖回来。
5. 正常切换时当前 query 仅投递一次；重复交接不重复替换已提交快照、不重放历史工具。query 已投递后崩溃再重试的行为遵循第 6 节限制，不作为本方案的恰好一次执行验收。
6. 目标 system prompt、工具、权限、workspace、任务状态和 turn 编号保持目标归属；Leader tracker 正确刷新。
7. 已压缩历史、完整工具配对、offload 引用可继续使用；不会复制成员私有对话。
8. 源/目标忙、工具执行中、有未回答的人机交互或 pending follow-up 时立即返回可重试错误，不自动暂停、不投递当前 query、不改变执行归属；交接期间收到成员消息、commit 失败及交接各阶段崩溃不会因交接导致双执行或静默丢上下文。query 消费后的跨崩溃重试边界见第 6 节。
9. 同模式普通请求及同一实际执行者进入/退出 plan 保持原行为；work 和 code 各自的 agent/team 双向切换覆盖 normal/plan，work ↔ code 明确拒绝；`team.design`、外部 CLI 不进入本交接路径；旧 session 无交接字段时可按定义进入首次交接。

现有测试参考：`tests/unit_tests/test_session_ops_warmup.py` 验证 warmup 的缓存跳过和刷新行为；新测试应针对上述交接行为，不把修改旧过滤器当作交接验收。本文仅是设计，未实施功能或执行这些验收测试。

## 10. 实现前核实项

- 确认实际 Leader 恢复到 ready、但尚未自动处理 inbox 的公开接入点；必要时补充启动屏障。
- 确认现有会话生命周期载体中可用于持久化实际执行主体、交接标识及恢复中间态的位置。
- 确认 offload 文件访问和目标模型的历史工具消息兼容性；无法兼容时明确报错，不静默丢弃。

这些问题决定挂载位置与失败处理，不改变已选定的“同 session、agent 与实际 Leader、消息快照双向接管”范围。

## 11. 2026-10-28 澄清确认

用户已确认以下范围与行为，后续相关实现和检视以本文为依据；实现前核实项仍按第 10 节处理。

- 忙碌时立即返回可重试错误，保持原执行归属，不自动暂停。
- 首版支持普通 work 的 agent/team，包含 normal/plan；按实际执行主体判断交接，同一实际执行者的 normal/plan 切换不触发交接，暂不包含 `team.design`。
- 保证交接可恢复及正常切换不重复投递；跨崩溃请求去重沿用现有能力，不新增工具绝不重复执行的保证。

本记录确认上述澄清结论，不表示功能已实现或验收已通过。

## 12. 2026-11-01 范围扩展确认

用户确认按相同交接语义增加 `agent.code.normal/plan` ↔ `team.code.normal/plan` 双向切换，不包含 work ↔ code 跨 profile 交接。代码配置、工具、提示词、权限和项目路径继续归目标执行者；只复制实际 Code Agent 与 Code Team Leader 的原生对话。沿用静止检查、首次输入保护、提交恢复与旧会话来源验证。Session 的同一归属锁防止 work/code 在共享 Team 身份或 checkpoint 时形成隐式跨 profile 继承。
