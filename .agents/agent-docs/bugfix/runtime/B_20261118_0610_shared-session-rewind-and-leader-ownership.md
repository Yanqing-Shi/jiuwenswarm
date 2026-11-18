---
id: B_20261118_0610_shared-session-rewind-and-leader-ownership
title: 共享会话回退写回共享记录，Leader 交还未用的会话操作
type: B
author: Yanqing-Shi
author_email: toddsyq@gmail.com
designs: []
design_note: 共享会话检视（2026-11-17）的 P1 问题修复；依据本地集成的共享会话与会话操作锁实现，无独立 D。
modules:
  - jiuwenswarm/agents/harness/common
  - jiuwenswarm/runtime/session
  - jiuwenswarm/server
files:
  - jiuwenswarm/agents/harness/common/session_ops_service.py
  - jiuwenswarm/runtime/session/conversation.py
  - jiuwenswarm/runtime/session/native_operation.py
  - jiuwenswarm/server/handlers/session.py
  - jiuwenswarm/server/agent_ws_server.py
  - tests/unit_tests/runtime/test_shared_conversation_runtime.py
---

# 共享会话回退写回共享记录，Leader 交还未用的会话操作

## 现象与影响

1. **回退后模型仍看到已撤回的消息。** `rewind_session_context` 把截断后的历史作为 `history_messages`
   传给 `create_context()`；Session 绑定共享记录时 Core 忽略该参数并重新加载原记录，保存时
   revision 未变也不写回。函数无条件返回 True，客户端得到 `rewind_context: true`。四个回退处理器
   （WebSocket 与 HTTP 各两个）都不取会话锁，回退期间的运行也能写同一记录。
2. **Leader 取得的会话操作无人释放。** Core 修复后，从 IDLE 起的 goal attempt 先过 admission；
   检查通过但 goal 已被清除时没有状态转换，`NativeOperationBinding` 只在 idle/terminated 转换时释放锁。

修复前复现（`test_shared_conversation_runtime.py`）：共享记录三轮、history 截到第一轮，
`rewind_session_context()` 返回 True，存储仍是三轮；四个处理器在锁被占用时照常截断 history。

## 关联设计与当前契约

无独立 D。依据：Core `S_02_session-conversation-store`（绑定 context 忽略 `history_messages`；会话外
的编辑需在宿主锁内显式保存）、Core agent_teams `S_18`「Host 对话归属扩展」（本次新增 `release`）。
会话操作锁的规约 `S_02_session-operation-ownership.md` 只在 `repos/jiuwenswarm` 检出中，本地集成
副本没有；移植时需把“共享会话的回退在会话锁内进行，忙则 SESSION_BUSY”补进该规约。

## 根因与修复决策

- `_apply_rewound_context`：绑定的 Session 先 `create_context(..., allow_pending_tool_calls=True)`
  装载当前记录（内容马上被替换，放宽未闭合工具调用校验），再用 `load_state` 一次替换窗口与 offload
  缓冲，由已有的 `_persist_rewound_session` 一次提交；不用 `clear_messages()`，它会先提交一个空窗口。
  没有 live Session 时，临时 Session 也带上 `load_main_binding()` 的绑定。绑定 Session 返回写回结果，
  未绑定 Session 行为不变。
- 新增 `shared_session_operation(session_id)`：共享会话时取 `acquire_operation("session", ...)`，
  否则不取锁。四个处理器把原处理体改名为 `_…_locked`，公开入口在任何改动前进入它；忙则直接回
  `SESSION_BUSY`，history 与上下文都不动（用户选择在处理器层取锁）。
- `NativeOperationBinding.release()`：harness 已不存在或处于 idle/terminated 时结束本 token 的操作；
  `bind_native_operation` 以 `release=` 传给 Core。

缓存的其他执行者不需额外处理：绑定 context 每次 `create_context` 都从记录重新加载。

## 回归证据与适用范围

本地组合：Swarm 工作区（基于 `54d92e149` 加已有 staged / unstaged 改动），Core 工作区
（`ed2754c39` 加本次 Core 修复），`web-mode-env.sh` 的 PYTHONPATH；内存与 SQLite checkpointer 各跑一遍。

- 新增 20 项（参数化后）：回退后共享记录与下一轮模型请求都不含已撤回的轮次；写回失败返回 False；
  无 live Session 也改写记录；锁辅助只对共享会话取锁；四个入口忙时回 SESSION_BUSY 且 history 字节
  不变；空闲 Leader no-op resume 后会话可取锁；admission 交还后会话可取锁。修复前 18 项失败（另 2 项
  依赖 Core 修复，在旧 Core 下复现为 `SessionOperationBusyError`）。
- `test_shared_conversation_runtime.py` 50 项通过；锁、模式切换、`test_compact_partial`、
  `test_resolve_rewind_agent`、处理器分发、TUI session ops 集成、runtime service 共 459 项通过。

未验证：没有真实模型 API key，未在真实 Web 界面走回退；未绑定共享记录的旧会话只跑了既有测试。

移植到本分支（2026-11-18，基于 `67d73fea0`，Core `0ff964b59`）：本分支没有 `ContextCheckpointer` 包装层和
`history_recovery_covered`，回退改动相应去掉这两处，测试夹具直接使用底层 checkpointer；其余改动与上文一致。
`test_shared_conversation_runtime.py`、`test_session_operation_lock.py`、`test_runtime_session_coordinator.py`、
`test_runtime_service.py` 共 293 项通过（移植前 273 项）。其他测试文件未在本分支重跑。

## 防错模式与资料同步

模式：改写共享记录的宿主操作（回退等）必须在会话操作锁内进行，并以记录写回成功作为成功条件；
`history_messages` 对绑定 Session 无效。`S_02_session-operation-ownership.md` 已补充第 7、8 条。

## 遗留与后续

- Web 前端当前没有调用 `session.rewind`；TUI `/rewind` 不读 `rewind_context`，回退写回失败时用户
  看不到提示。
- 依赖 Core 的 `set_conversation_guard(..., release=...)`；锁定的 Core 版本必须包含该修复。
