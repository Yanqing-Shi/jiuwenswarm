---
modules:
- jiuwenswarm/runtime
- jiuwenswarm/server/runtime/agent_adapter
designs:
- D-20261028-1704-agent-leader-context-handoff
author: null
author_note: 从恢复补丁导入；原始作者无法从 Git 确认
title: 单智能体与 Leader 上下文交接实施记录
id: F_20261029_0001_agent-leader-context-handoff
type: F
files: []
---

# 单智能体与 Leader 上下文交接实施记录

日期：2026-10-29。状态：配套源码实现与定向回归完成；未提交/发布，真实模型验收未执行。

设计依据：[D-20261028-1704-agent-leader-context-handoff](../designs/active/D-20261028-1704-agent-leader-context-handoff/design.md)。后续相关改动以该设计及已确认澄清为依据。

## 开发安排与改动记录

| 单元 | 范围 | 进度与证据 |
| --- | --- | --- |
| 消息快照 | core context 的独立复制、校验、恢复及提交回滚 | 已实现 `context_engine/conversation.py`，支持内存 offload；拒绝不可携带的文件引用 |
| 执行边界 | 单智能体输入保护、Leader 恢复后启动前入口、忙碌拒绝 | 已实现 DeepAgent/NativeHarness guard 和 TeamHarness 的 pre-supervisor callback |
| 应用交接 | Session metadata 归属、中间态、缓存与冷恢复、请求接线 | 已实现 `runtime/context_handoff.py`，覆盖流式/非流式、缓存、无 waiter 后台启动 |
| 验证与契约 | 双向切换、失败恢复、并发回归、spec 和用户文档 | Swarm 296 项、Core 194 项通过；两侧 spec / feature 及中英文用户文档已同步 |

2026-10-30 补充：真实 TUI 上下文日志暴露了分类入口缺陷。Session 的
`work_mode=code` 会覆盖显式 `agent.work.*` / `team.work.*`，导致交接跳过。
`request_kind` 现对显式三段 work mode 按 canonical 值分类，旧式 mode 继续使用
profile gate；新增 8 个冲突模式用例，`test_context_handoff.py` 37 passed。
证据与日志解释见 [B_20261030_0330_canonical-work-mode-handoff.md](../bugfix/runtime/B_20261030_0330_canonical-work-mode-handoff.md)。

2026-11-01 扩展：用户确认 Code profile 内也执行 Agent ↔ 实际 Team Leader 的
双向消息接管，覆盖 normal/plan。复用 Core 原生快照与已有 Deep/Team 接入；
CodeAdapter 继承 DeepAdapter 输入入口，Code Team 使用同一 Team stream 接入。
`conversation_handoff.owner` 对 code 增加 `profile=code`，旧 work owner 缺省
解释为 work；单一 Session 锁拒绝 work ↔ code 跨 profile 请求，避免共享 Team
身份与 checkpoint 时隐式串历史。冷 Agent 的公共 history 兜底只读取本 profile
的事件。定向回归覆盖 Code 双向、流式/非流式入口、后台 Team、忙碌拒绝和
跨 profile 拒绝；真实模型 Code 切换仍待验收。当前验证结果见下文。

新增 Code 定向验证命令：`JIUWENSWARM_DATA_DIR=/tmp/jiuwenswarm-code-handoff-tests PYTHONPATH=../agent-core:. .venv/bin/python -m pytest tests/unit_tests/test_context_handoff.py tests/unit_tests/test_session_ops_warmup.py tests/unit_tests/server/test_session_metadata_write_options.py -q -o addopts='' --asyncio-mode=auto --tb=short -o log_cli=false`，91 passed。额外组合批次在沙箱内运行到约 90 项后停止输出，已中断，不计作通过；先前三个目标文件的独立运行完成。

## 决策与依据

- 保留 mode → session 缓存和独立 checkpoint key，只交接消息及必要 offload 数据。
- 使用已有 metadata 字段更新路径保存实际执行主体；后台旧快照写入必须保留该字段。
- startup lock 不覆盖团队后台执行，不能作为唯一的交接边界。
- 交接幂等不等同于跨崩溃 query 或外部工具恰好一次执行。
- 交接保护包含源冻结、目标 commit、metadata 归属更新及首条输入交付窗口；只有匹配的用户输入 ID 能解除 Team 的首条输入保护，后台消息不能代为确认。
- 流式 ContextVar 仅在推进内部迭代器时绑定，不能跨公开 yield 保留，允许不同任务轮流消费同一条流。
- 同模式首次启动保留原有排队；跨执行者且已有交接时立即拒绝。团队后台 producer 持有启动交接，避免无 waiter 返回提前解锁。
- 使用公开 Session/checkpointer 恢复冷源；Core 按已保存的团队存储身份检查任务板、成员和未读消息，不启动整个团队，也不在应用层读取 SQLite blob。
- Leader 模板/rail 初始化先于 host 导入；首次创建目标 context 时复用目标配置的 processor 链，避免把空处理链缓存下来而丢失压缩/观测能力。

## 实现定位

- `jiuwenswarm/runtime/context_handoff.py`：归属、journal、实际 card ID、恢复及请求生命周期。
- `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`、同目录 `team_helpers.py`：目标输入之前接入；失败通过 `chat.error` 返回。
- `jiuwenswarm/server/runtime/session/session_metadata.py`：普通与延迟写入不覆盖交接字段。
- `jiuwenswarm/agents/harness/common/session_ops_service.py`：已有交接 checkpoint 优先于公共 JSONL warmup。
- `../agent-core/openjiuwen/core/context_engine/conversation.py`：消息快照公共接口；源/目标非对话状态相互独立。
- `../agent-core/openjiuwen/harness/deep_agent.py`、`agent_teams/harness/`：执行保护、完成后的原生对话提交、Leader 启动屏障和静止检查。

当前契约：[S_01_agent-leader-context-handoff.md](../specs/runtime/S_01_agent-leader-context-handoff.md)。

## 未采用的方案

不通过公共 history 重建跨模式上下文，不整体复制 checkpoint，不自动暂停忙碌执行者。

## 验证

Swarm 使用配套本地 Core 源码：`JIUWENSWARM_DATA_DIR=/tmp/jiuwenswarm-handoff-tests PYTHONPATH=../agent-core:. .venv/bin/python -m pytest <下列路径> -q -o addopts='' --asyncio-mode=auto --tb=short`。

- `tests/unit_tests/test_context_handoff.py`
- `tests/unit_tests/test_session_ops_warmup.py`
- `tests/unit_tests/server/test_session_metadata_write_options.py`
- `tests/unit_tests/agentserver/test_deep_adapter_stream_end_final.py`
- `tests/unit_tests/agentserver/test_team_helpers.py`

本批 296 passed。覆盖双向缓存交接、Leader 独有 D、实际 card 冷恢复、提交前后故障及 receipt 恢复、忙碌目标/源、并发窗口、流式跨任务消费、正常输入一次交付、模式边界、目标 processor 行为、warmup 与旧 metadata 写入保护。
既有 transport 单测的 fake Runner 没有 Leader/child Session，显式隔离交接；`test_context_handoff.py` 单独验证真实 admission 与 prepare callback 接线。

Core 从 `../agent-core` 执行：`JIUWENSWARM_DATA_DIR=/tmp/jiuwenswarm-handoff-core-tests PYTHONPATH=. ../jiuwenswarm/.venv/bin/python -m pytest <下列路径> -q -o addopts='' --asyncio-mode=auto --tb=short`。

- `tests/unit_tests/core/context_engine/test_conversation.py`
- `tests/unit_tests/agent_teams/harness`
- `tests/unit_tests/agent_teams/test_team_harness_integration.py`
- `tests/unit_tests/harness/test_deep_agent_interaction.py`
- `tests/unit_tests/harness/test_deep_agent_terminal_stream.py`
- `tests/unit_tests/harness/test_deep_agent_session_state.py`
- `tests/unit_tests/harness/test_deep_agent_stream_aclose.py`

本批 194 passed，84 条现有 Pydantic/DefaultResponse 弃用警告。实际 NativeHarness 调度循环使用测试内层执行器验证导入的 A/D 到达下一轮、B 只交付一次且新回答写入实际 PersistenceCheckpointer。冷团队与缓存团队检查验证显式 Session 隔离、成员/任务/未读消息/暂停等拒绝条件。
测试日志位于 `/tmp/handoff-swarm-tests.log`、`/tmp/handoff-core-tests.log`，不入库。
沙箱内涉及异步线程的测试出现停滞；同批在沙箱外、隔离测试数据目录中完成。未运行格式化、静态检查或真实外部模型验收。

## 检视与交付条件

按 `jw-review` 对本特性工作区差异（含新增文件）相对 HEAD 检视：Swarm 基线 `6885838d7f9e614ebad18b0cd083905264841c84`，Core 基线 `0ab5913b250d565b35bff3828dae438939cb1acd`。依据根/模块 AGENTS、批准设计 §4–6/§9、S_01、Core S_18 与 S_02，覆盖输入接线、状态归属、启动、提交/恢复、流式清理及用户文档。已修复复查发现的流式绑定跨任务、首条输入保护被后台消息释放、冷团队未检查任务板、跨上下文查询错误 session、任务终态常量错误导入和目标 processor 丢失问题；相关回归通过。尚不具备独立发布条件，原因如下。

这是两个仓库的配套源码变更。Swarm 的 `pyproject.toml` / `uv.lock` 仍锁定旧 Core；正式交付前必须先发布包含本改动的 Core 提交，再更新依赖及锁文件。当前尚无可引用的新 Core 提交，不伪造 pin，也不将本机路径写入发布配置。上述测试通过 PYTHONPATH 显式选择本地 Core，不代表旧 pin 已支持本特性。

文件系统 offload 暂不自动搬运：返回 `conversation_filesystem_offload_not_portable`，保持源归属，不执行当前 query。旧会话无法确定源身份/缺少消息快照也明确失败。没有新增跨崩溃请求去重保证。

## 工作区边界

2026-10-29 运行恢复：用户遇到 AgentServer runtime failed，确认为新 Front 重连旧常驻 Worker
触发版本身份拒绝。工作区 `start-chat.sh` 已显式加载配套本地 Core，并完整停止旧 Worker、
重启服务，RPC 返回 AGENT_READY。发布依赖 pin 仍未改变，服务就绪不代表模型交接验收完成。
详见 [B_20261029_1117_resident-worker-version-mismatch.md](../bugfix/runtime/B_20261029_1117_resident-worker-version-mismatch.md)。

开工时已有 observability 代码、测试及文档改动；本特性不修改这些文件。未提交或创建 PR。

## 2026-11-09 补丁迁入

从恢复的 `session-changes.patch` 迁入 `feat/agent-leader-context-handoff`。
Swarm 基线为 `7853595c79b8e63dbfe319c1b8f0801b7b347854`，Core 基线为
`b412128ab89362de0afe95682b113d880813c97a`。迁入时保留上游 Team 请求上下文与处理状态日志，
合并 Core 新增的 invocation/proposal 生命周期，并将 mode_matrix 引用改为独立 Gateway 包。
Core spec 和 feature 按当前文档布局定位。以上测试数字属于原补丁的历史记录；本次仅恢复和合并，
未重新运行行为测试，两个仓库当前均无 `.venv`。新基线的运行兼容性与可安装依赖仍待验证。
