---
id: S_02_session-operation-ownership
title: Session 运行与模式切换的操作互斥
type: S
author: Yanqing-Shi
author_email: toddsyq@gmail.com
modules:
- jiuwenswarm/runtime/session
- jiuwenswarm/agents/harness/team
files:
- jiuwenswarm/runtime/session/operation_lock.py
- jiuwenswarm/runtime/session/native_operation.py
- jiuwenswarm/runtime/session/coordinator.py
- jiuwenswarm/runtime/service.py
- jiuwenswarm/server/runtime/session/project_store.py
---

# Session 运行与模式切换的操作互斥

最近一次修订日期：2026-11-14。

本契约覆盖共享主对话方案的 I-00 基础：运行（聊天、goal、heartbeat、Team 主执行者）与模式切换共用一把 Session 操作锁。
不定义共享对话存储，不定义旧 Session 的升级处理。归档与删除不在本契约范围内，见文末“预留”。

## 执行所有权

1. 聊天、goal、heartbeat、Team 主执行者和模式切换共用 `runtime/session/operation_lock.py`。注册表按完整存储路径区分租户与资源，锁文件为 `lifecycle/operations/{kind}_{resource_id}.json.lock`。它与 `lifecycle.resource_lock` 的短时资源提交锁独立，也与归档/删除服务自己的 `lifecycle/owners/` 执行锁独立：运行持有操作锁不会让归档或删除等待。`project_store.file_lock` 为此增加了非阻塞取锁选项。
2. 模式切换非阻塞取 Session 锁；锁忙返回 `SESSION_BUSY`。聊天继续使用已有串行 lane。取锁前的 work kind 识别只用于排队或加入当前运行；Coordinator 在取得所有权、确认 Session 仍开放且属于同一代之后、运行开始之前调用宿主的 `on_owned`，Runtime 在其中重读持久化模式：单 Agent / Team 类型与识别结果不一致时抛出 `SessionRouteChangedError`，该 execution 记为 CANCELLED，`invoke` / `stream` 在未产生输出时按已提交模式重新分派，请求保持排队、不因模式切换失败，次数不设上限；只有连续两次确认读到同一模式（并未发生切换）才把该错误抛出；执行者仍在所有权内由 `normalize_request` 选定。模式切换除操作锁外保留到达判断：取得锁的时点若已有 chat 到达服务端（到达标记）、在 Runtime 排队/运行/等待交互，或 admission 显示用户、heartbeat、待答交互活动，返回 `SESSION_BUSY`，已发出的运行按原模式执行；空闲 Team 的持久响应流不算。取得锁之后才到达的运行不参与该判断，排队等待新模式。重新分派因此只覆盖没有到达标记的入口和跨进程切换。metadata CAS 保留。
3. Coordinator 通过单一异常收尾和 finally，在模型、工具、流关闭、错误/取消收尾与最终保存结束前保留所有权；等待交互的执行也保留。前台输出流结束不代表交互已经完成。独立 goal/heartbeat 与聊天使用同一互斥边界。
4. stop 不取得第二把 Session 锁。交互回复与当前 goal/Team 控制输入显式保留现有操作引用，不能等待自己持有的非重入锁。用户输入抢占 heartbeat 的既有 admission 必须发生在等待操作锁之前。
5. 实际 Leader 通过现有 conversation guard 取得或保留当前操作；mode 复核只在它新取得所有权时执行一次，持有期间的后续输入不再复核，单 Agent 适配器不安装该 guard。Leader 通过 NativeHarness 状态订阅在最终 checkpoint 之后的 IDLE/TERMINATED 释放；supervisor 异常退出也释放。后台自动输入和无人订阅的运行适用同一规则。Team round 与请求只保留同一执行锁的引用，不引入新的持久化 lease 协议。
6. 纯观察订阅与已空闲的 Team stream 不持有执行所有权；子执行者的私有窗口不申请主 Session 锁。复制式 handoff 已移除（见 S_01 的撤销说明）；实际 Leader 的操作所有权由共享绑定的启动前钩子接通，guard 不带参数。

普通 chat（unary/stream）在模式切换持有操作锁时，不能启动执行者、模型或工具；切换完成后，排队 chat 重读持久化模式。

## 预留：归档与删除

归档与删除不属于共享对话方案的范围（用户 2026-11-14 决定）。`session_archive.py`、`lifecycle.py` 及其用例保持基线内容：
归档/删除使用自己的 `lifecycle/owners/` 执行锁、持久化 fence 和后台 finalizer，可以受理运行中的 Session 并自行停止运行；
新运行在受理入口由既有的 `lifecycle.guard`（`_require_open_session`）拒绝。本方案没有改变这些行为。

为后续开发保留的接入点，目前均未实现：

- `acquire_operation(kind, resource_id)` 保留 `kind` 参数；归档/删除将来若要与运行、模式切换互斥，可在自己的入口取 `("session", id)` 或 `("project", id)`，无需改锁的实现。
- 排队运行取得所有权后的回调位置（`on_owned`）可以追加 lifecycle fence 复核。
- 实际 Leader 新取得所有权时的复核（`session_mode_guard`）目前只核对 mode。
- 共享对话记录不随删除清理：`checkpointer.release` 只清理执行者 checkpoint，删除 Session 后其主记录和成员窗口记录仍留在存储中。归档/恢复不触碰该记录。

依据：[设计草稿 I-00](../../designs/proposals/D-20261112-0530-session-conversation-window/design.md)、[实施记录](../../features/F_20261112_0650_session-operation-ownership.md)。
