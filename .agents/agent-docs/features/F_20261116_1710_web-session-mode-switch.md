---
id: F_20261116_1710_web-session-mode-switch
title: Web 与 TUI 共用会话模式切换（mode.set / mode.get）
type: F
author: Yanqing-Shi
author_email: toddsyq@gmail.com
modules:
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/runtime
  - jiuwenswarm/server
  - jiuwenswarm/common
  - jiuwenswarm/channels/web/frontend
  - scripts/acceptance
files:
  - jiuwenswarm/agents/harness/team/team_manager.py
  - tests/unit_tests/runtime/test_session_mode_switch.py
  - tests/unit_tests/server/test_archive_lifecycle.py
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/runtime/service.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/channels/web/frontend/src/features/sessionMode/commitSessionMode.ts
  - jiuwenswarm/channels/web/frontend/src/features/sessionMode/inPlaceModeSwitch.ts
  - jiuwenswarm/channels/web/frontend/src/App.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/InputArea.tsx
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/channels/web/frontend/src/features/sessionMode/modeSwitchGate.ts
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/stores/chatStore.ts
  - scripts/acceptance/web_mode_switch_bench.py
designs: []
design_note: 无独立 D。本次把 TUI 已有的 mode.set / mode.get 契约（TUI 草稿会话与可靠模式切换的提交）推广到 Web 通道，方案在开发会话中与用户确认，未单独立项设计。
---

# Web 与 TUI 共用会话模式切换实施与决策归档

## 关联依据

- 设计：无独立 D，见 frontmatter 的 design_note。
- spec：未新增 S；用户文档更新 `docs/{en,zh}` 的模式系统与项目会话管理页。
- 日期：2026-11-16。
- 实际实施范围：
  - 后端 `mode.set` / `mode.get` 从仅 TUI 改为按通道集合 `MODE_SWITCH_CHANNELS = {"tui", "web"}` 开放，两端走同一实现。
  - Web 已有消息的对话可在 Agent / Team 间原地切换，始终开启，无设置开关（个人版 WebSocket；企业版与 HTTP 传输仍新建对话）。
  - 新增基准驱动脚本 `scripts/acceptance/web_mode_switch_bench.py`。
  - agent-gateway 的 Web 转发白名单加入两个方法（外部仓库，独立 PR；本仓需随后更新版本号）。
  - 未落地：Agent ↔ Team Leader 上下文交接本身（在 `feat/agent-leader-context-handoff` 分支）；TUI 前端改动不随本次提交。

## 决策归属与关系

本次是对 TUI 模式切换提交的续写：保留其 CAS、`mode_revision`、`session.mode_changed` 与跨进程运行时占用检查不变，只放宽"调用方必须是 TUI"的限制。

## 上下文与跨边界影响

- 请求链路：Web 浏览器 → agent-gateway Web 通道（`_FORWARD_REQ_METHODS` 与 `_FORWARD_NO_LOCAL_HANDLER_METHODS` 均需包含方法，否则回 `METHOD_NOT_FOUND`）→ MessageHandler 队列 → AgentServer `_answer_mode_request` → `AgentRuntime.switch_session_mode` / `get_session_mode`。TUI 走同一 MessageHandler 与后端入口。
- 权威状态：会话 `metadata.json` 的 `mode` 与 `mode_revision`；`history.jsonl` 的 `session.mode_changed` 为切换记录。客户端以服务端返回的模式为准。
- 对话请求：`normalize_request` 在 `channel_id ∈ MODE_SWITCH_CHANNELS` 且元数据含 `mode_revision` 时，只允许计划态漂移，其余路由纠正回已提交模式。没有 `mode_revision` 的 Web 会话行为不变（已证实，见 `tests/unit_tests/runtime/test_request.py` 原有用例通过）。
- 交接触发（推断，未在本次验证）：交接分支按"对话请求的模式种类与已存 owner 不同"触发，Web 原先因跨种类请求被纠正而无法触发；本次放开后，切换后的下一轮对话即会携带新模式。

## 实际实现与开发决策

- 会话归属：`mode_switch_channel_owns` 要求调用方通道与会话记录通道一致；未记录通道的旧会话仍归 TUI。Web 与 TUI 不能互相切换对方的会话（返回 `NOT_FOUND`）。
- 执行者固定的会话拒绝切换（`BAD_REQUEST`）：智能体组（`agent_group_name` / `team_group_identity`）、外部智能体（`session_runtime_binding`）、定时任务（`cron_id`）。该检查对 TUI 同样生效，是本次新增的行为。
- `MODE_UNKNOWN` 提示语去掉 TUI 专属的 `/mode` 字样。
- 前端 `commitSessionMode` 照搬 TUI 流程：`mode.set` → 应答丢失时重试一次 → 仍不确定用 `mode.get` 回读；`MODE_CONFLICT` 采用服务端模式；`SESSION_BUSY` 直接返回。结果以状态值返回，由 App 统一提示。
- 2026-11-17 用户决定：对话内切换始终开启，删除实验性开关 `experimental.session_mode_switch_enabled`（设置项、`config.yaml` 字段、配置读写与前端开关状态全部移除，4 个配置后端文件不再改动）。能否原地切换只看个人版 + WebSocket 传输。已写入用户 `config.yaml` 的该键不会报错：模板合并保留仅存在于用户配置的键（`common/config.py` `migrate_config_from_template`），不再有代码读取它。
- 2026-11-17 `commitSessionMode` 两处修正：
  - 没有 `code` 的失败视为结果未知，走"重试一次 → `mode.get` 回读"。AgentServer 的拒绝都带 `code`；Gateway 在 AgentServer 链路断开时只回 `ok:false` + `error`（Gateway `message_handler.py` `_build_error_out_message`），此时提交可能已生效。原先照搬 TUI 的 `AGENT_SERVER_TIMEOUT` / `SERVICE_UNAVAILABLE` 不会出现在 Web `mode.set` 上。
  - 结果按 Agent / Team 路由比较，不再要求整串相等：回读结果只差计划态，或 Gateway 走 AgentOS 路由时应答 `mode` 被投影成 `agent` / `team`，都不应提示"已在别处更改"。
- 2026-11-17 提前占用保留：`_normalize_chat_after_claim` 对 Web 自有会话的每个对话请求在分类前占用会话（与 TUI 一致）。因切换始终开启，每个 Web 会话都可能被切换，需要这一步避免跨进程切换竞争。与 develop 的差异：计划态与 Auto Harness 对话原先没有工作类型、不登记到 Runtime，现在也会登记；若另一存活 AgentServer 持有该会话，会以 `OPERATION_IN_PROGRESS` 拒绝（develop 上照常执行）。单 AgentServer 部署下无可见变化。新增 `test_chat_on_never_switched_session_keeps_its_route` 固定其余行为：未切换过的会话上，计划态、Auto Harness、Agent 对话与打开 Team 对话的模式和工作类型与 develop 相同。
- 删除 `useWebSocket.ts` 中无调用方的旧 `switchMode`（它在判断前清掉处理中状态，中断永远不会发出）。
- `expected_mode` 与目标模式都用 `chat.send` 同一个 `resolveOutgoingMode` 拼出，避免 work/code 中间段不一致。

## 与设计的偏差

- 开发计划原定以实验性开关控制（先定 `localStorage`，后改 config.yaml 字段）；2026-11-17 用户决定始终开启，开关整体删除。
- 计划原定模式锁属性经 `ChatPanel` 透传；实际 `InputArea` 直接按版本与传输判断，改动更少。

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 / 记录链接 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-16 后端单测 | 工作区未提交；Windows，Python 3.14；agent-gateway 以可编辑方式安装改动后的工作区 | `pytest tests/unit_tests/runtime/test_session_mode_switch.py`（原 63 项，现对 tui/web 参数化） | 152 通过 | 无 |
| 2026-11-16 后端相关套件 | 同上 | `pytest tests/unit_tests/runtime tests/unit_tests/server/test_session_metadata.py tests/unit_tests/common ...` | 除下列外全部通过 | `test_resource_kernel` 2 项、`test_filesystem_paths` 1 项、`test_jev_model_clear` 3 项为 Windows 路径 / 编码问题，与本次无关；归档测试 1 项偶发 WinError 5，重跑通过 |
| 2026-11-16 配置开关单测 | 同上 | `pytest tests/unit_tests/common/test_session_mode_switch_config.py` | 通过 | 2026-11-17 随开关一并删除 |
| 2026-11-16 Gateway 单测 | agent-gateway `1ead3bf` + 本次 4 行 | `pytest tests/unit_tests/gateway/test_app_web_handlers.py ...` | 233 通过 | 无 |
| 2026-11-16 前端单测与构建 | 同上；`npm ci` 后 | `npm run test:session-mode-commit`、`test:wire-mode`、`test:settings-panel`、`test:i18n-locales`、`node --test tests/appCollaborationSnapshot.test.mjs`、`npm run build` | 全部通过 | `test:input-area-permission-merge` 2 项、`test:team-organization` 5 项失败，变更前基线同样失败 |
| 2026-11-17 删除开关后后端套件 | 工作区未提交（含上述修正）；Windows，Python 3.14.7；agent-gateway 以可编辑方式安装 `feature/web-mode-forwarding` | `pytest tests/unit_tests/runtime/test_session_mode_switch.py tests/unit_tests/server/test_archive_lifecycle.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/test_history_replay.py tests/unit_tests/test_session_history_empty_filter.py tests/unit_tests/agentserver/test_team_helpers.py tests/unit_tests/common --no-cov -q` | 2041 通过，2 跳过，4 失败 | 失败即上文已记录的 `test_filesystem_paths` 1 项、`test_jev_model_clear` 3 项（cp1252 读取 config.yaml），与本次无关 |
| 2026-11-17 删除开关后前端单测与构建 | 同上 | `npm run test:session-mode-commit`（14）、`test:wire-mode`（22）、`test:settings-panel`（68）、`test:i18n-locales`（6）、`node --test tests/appCollaborationSnapshot.test.mjs`（10）、`node --test tests/settingsRefactor.test.mjs`（37）、`npm run build` | 全部通过 | 无 |
| 2026-11-17 提前占用与 develop 对照 | 工作区未提交；同上 | 临时探针（未入库）：Web 会话上 `agent.work.plan` / `auto_harness` 对话经 `_normalize_chat_after_claim`，以及另一存活进程持有会话时的计划态对话 | develop 工作类型为 `None`（不登记），现在登记 1 次；外部持有时抛 `LifecycleError: OPERATION_IN_PROGRESS` | 用户决定保留，见上文 |
| — 真实服务联调 | — | `make start-debug-rebuild` 后在 Web 切换、运行基准脚本、TUI 对照 | 未执行 | 需用户本地启动服务；基准脚本的轮次结束判定未经真实服务验证 |

## 验证结论

- 已证明范围：后端对 tui / web 两个通道执行同一套 CAS、忙碌、冲突、归属、未知模式修复与对话纠正逻辑；未切换过的会话上各类对话的模式与工作类型与 develop 相同；Gateway Web 通道转发两个方法；前端提交流程的成功、冲突、忙碌、重试、回读（含无 `code` 失败）、按路由比较、失败分支；仅个人版 WebSocket 允许原地切换。
- 未覆盖：浏览器中的界面行为；真实服务下的端到端切换与基准脚本；交接功能；分享图片导出与回退（rewind）对 `session.mode_changed` 记录的处理（分享导出原样带出全部历史记录）。
- 可支持的交付判断：代码层面满足约定范围，提交前仍需完成真实服务联调。

### 覆盖与资料同步

- 已更新：`docs/en/Modes.md`、`docs/zh/模式系统.md`、`docs/en/Project-Session-Management.md`、`docs/zh/项目与会话管理.md`。
- 待同步：agent-gateway PR 合并后更新本仓 `pyproject.toml` 与 `uv.lock` 中的版本号；在此之前 `test_mode_requests_are_forwarded_by_every_mode_switch_channel` 只能在本地联调环境通过。
- 测试归属：版本号将跟进到 Gateway `develop`，其中 `9650ef2ee`（2026-11-14，个人版 `path.get` / `path.set` 改经 AgentServer）与 A4P 路由修复使本仓 `tests/unit_tests/gateway/test_app_web_handlers.py` 中 4 个测试函数（共 6 例：`test_a4p_rpc_uses_current_websocket_route`、`test_path_set_*`）失败。逐行比对确认它们是 Gateway 仓同名测试的旧版副本，Gateway `develop` 已有覆盖相同场景的新版，故从本仓删除，与 develop 上 `227ae5260`（移除过时 Gateway 测试套件）方向一致。删除后该文件与模式切换测试在 `develop` 版 Gateway 下 449 项全部通过。

## 2026-11-18 04:37（Asia/Singapore）续写：切换与提示词按用户操作顺序（单标签页）

### 问题

服务端按到达先后处理 `mode.set` 与对话请求。用户先切换、再发送时，若对话请求先到，切换以 `SESSION_BUSY` 被拒，提示词按旧模式运行；反过来，提示词已按下但处理中标记尚未设置的窗口内（`ChatPanel.handleSendMessage` 先等待插件刷新历史），切换仍可发出。

### 用户确认的决定（2026-11-17，开发会话）

- 由 Web 前端保证顺序：同一会话在同一标签页内，不同时有切换请求与提示词在途。规则是“`mode.set` 未结束则提示词等待”。
- 等待的提示词进入现有提示词队列（`taskQueue`），不合并；切换成功后按原有规则逐条发送（上一轮结束即发送下一条）。
- 切换未成功（busy / not_applied / uncertain / failed）：队列内提示词保留并暂停队列，不以旧模式发出。
- 队列扩展到 Agent 与 Team 两种模式。Team 模式运行中提交仍直接发送给 Leader（原行为）；Team 下队列只容纳等待切换的提示词与 Agent 模式遗留项。
- 只保证单标签页内顺序。多个标签页或客户端同时操作同一会话，仍按服务端到达顺序决定；用户后续另行处理（候选方案：每个提示词携带模式版本，过期则拒绝）。
- 服务端主动推送 `session.mode_changed` 作为第二步，待用户联调本步后再实施。

### 实际实现

- `features/sessionMode/modeSwitchGate.ts`（新增，纯函数）：是否等待切换、队列在哪些模式下发送、切换结束后队列动作（committed → 发送；其余结果且队列非空 → 暂停），以及“提示词提交中”按会话计数的标记。
- `chatStore`：新增会话运行态 `modeSwitchPending`。未复用无调用方的 `switchingMode`：设置它会重置处理中状态，并使 `chat.processing_status` / `chat.interrupt_result` 被丢弃。历史重载等运行态重置不清除该标记，只由发起切换的提交清除。`claimQueuedTask`（含队列面板的“继续”）与 `claimTaskInput`（插话）在标记期间拒绝领取。
- `App.switchSessionModeInPlace`：等待 `commitSessionMode` 前设置标记，`finally` 中清除；committed / not_applied 时另外更新侧栏列表的模式（`useWorkspaceStore.patchSession`，原先侧栏保留旧模式，重新打开会话会套用旧模式）；随后按上述规则发送或暂停队列，原有提示不变。
- `App.handleSwitchMode` 与 `InputArea.handleModeSwitch`：处理中、切换未结束或提示词提交中时拒绝切换；`InputArea` 的检查放在清除已选技能之前，被拒绝的切换不再丢掉技能。`modeLocked` 包含切换未结束，模式控件期间锁定。
- `ChatPanel.handleSendMessage`：在等待插件刷新前设置“提交中”标记，在同步调用发送后（或刷新失败时）结束；发送路径在第一次 await 之前设置处理中状态，二者衔接。
- `InputArea.handleSubmit`：切换未结束时，提交加入队列并保留已选技能；已启用目标（goal）的提交直接返回并保留草稿（`command.goal` 无法排队）。
- `useWebSocket.sendMessage` 兜底：切换未结束时普通提示词入队并返回成功（覆盖 MCP 应用消息、计划执行提示等直接调用），队列项插话返回失败，避免误报“补充失败”。
- 队列两种模式化：`drainTaskQueueIfIdle` 按 Agent / Team 放行并在切换未结束时不发送；`chat.processing_status(false)` 与恢复结果处的 `currentMode === 'agent'` 前置判断删除，统一由 `drainTaskQueueIfIdle` 判断。队列卡片在 Team 下显示；单项“立即发送”（插话）按钮只在 Agent 下显示。暂停队列时的确认弹窗仍只在 Agent 下出现。
- `App.handleCancel` Team 分支：队列非空时先暂停队列，否则停止后普通的 `processing_status(false)` 会自动发出下一条。
- `App` 的 MCP 应用消息处理在切换未结束时拒绝（与队列暂停时一致）。

### 行为说明与取舍

- 已暂停的队列在切换成功后仍保持暂停，需用户点“继续”。
- 队列项不保存技能与输入来源：发送时使用当时已选技能，来源记为普通手动输入（原有限制）。切换等待期间选择的技能来自旧模式的选择器。
- 未覆盖的入口：`/compact`、对智能体提问的回答、A2UI 操作、Team 组织界面（本环境关闭）的后台轮次结束不触发队列发送。
- 切换到 Team 成功后立即发送的第一条队列提示词，可能早于 Team 事件流挂接（`attach_only`）发出；推断无功能影响（运行中会话的挂接本就允许），未验证。

### 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-18 变更前基线 | `local-web-mode` 工作区，基于 `54d92e149`；WSL2 Linux，Node 22.22.1 | 同下 | session-mode-commit 14/14；chat-store-streaming 10/10；input-area-permission-merge 105/107；session-input 103/104；`chatErrorSseLifecycle` 15 通过 / 5 失败 / 1 跳过 | 失败项见下行，变更前即失败 |
| 2026-11-18 前端单测 | 同上，含本次未提交改动 | `npm run test:session-mode-commit`、`test:chat-store-streaming`、`test:input-area-permission-merge`、`test:session-input`、`node --test tests/chatErrorSseLifecycle.test.mjs` | 18/18；12/12；117/119；106/107；15 通过 / 5 失败 / 1 跳过。新增 21 例全部通过 | 失败项与基线相同：企业版输入区锁定、组织 Owner 停止后禁用、MoA 状态路由各 1 例；`chatErrorSseLifecycle` 5 例为测试本身过期（`isTeamOrganizationUiEnabled is not defined` 等），前后报错一致 |
| 2026-11-18 构建 | 同上 | `node_modules/.bin/vite build` | 通过 | 未单独执行 TypeScript 类型检查（本仓约束不运行静态检查）；编辑器对 `App.tsx`、`useWebSocket.ts` 无错误诊断，其余文件未确认被分析 |
| — 真实服务联调 | — | 启动服务后：切换后立即发送；发送后立即切换；Agent 排队两条后停止再切到 Team | 未执行 | 待用户本地验证 |

新增测试：`tests/sessionModeGate.test.mjs`（纯函数 4 例）；`chatStoreStreaming` 2 例（领取拦截、无运行态时记录标记）；`inputAreaPermissionMerge` 12 例（Agent / Team 切换未结束时入队、Team 空闲直发、模式控件锁定、菜单打开期间提示词已按下则不切换、goal 保留草稿、committed 发送、四种未成功结果暂停、“提示词按下后切换即被拒绝”）；`sessionInput` 3 例（切换未结束时入队与不发送、Team 下遗留项逐条发送且无插话按钮、Team 停止暂停队列）。

## 2026-11-18 23:41（Asia/Singapore）续写：`mode.set` 必须携带 `expected_mode`

### 起因

Gateway MR openJiuwen/agent-gateway!173（GitHub openJiuwen-ai/agent-gateway#234，Web 转发 `mode.set` / `mode.get`）的检视意见：

- 安全：任意已认证连接可对任意 `session_id` 发起 `mode.set`。会话归属已由 AgentServer 校验（`mode_switch_session_visible_to`：创建通道与用户须与请求方一致，否则 `NOT_FOUND`）；检视人建议至少要求 `expected_mode` 与当前值一致才能变更。
- 并发：两个标签页同时切换同一会话时后写覆盖前写。AgentServer 按会话串行处理 mode 请求，并在跨进程文件锁内比较后写入，后到的冲突请求得到 `MODE_CONFLICT`；但此前 `expected_mode` 可省略，省略时直接覆盖，不做比较。

### 决定与实现

- 用户决定（2026-11-18）：`mode.set` 必须携带非空字符串 `expected_mode`，缺失、`null`、非字符串或空白一律 `BAD_REQUEST`（"expected_mode is required"），在启动 Runtime 之前拒绝。改动只在 `AgentWebSocketServer._answer_mode_request`；`switch_session_mode` 与 `compare_and_set_session_mode` 不变，已存模式未知（旧会话）时仍跳过比较，可用显式切换修复。
- Web（`commitSessionMode.ts`）与 TUI（`/mode`）本来总是携带当前模式，客户端无需改动。
- `expected_mode` 是并发保护，不是访问控制：能读到模式（`mode.get`）的调用方就能给出它。访问控制仍是上面的归属校验。

### 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 | 实际结果 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| 2026-11-18 变更前 | `local-web-mode` 工作区（含未提交改动），`web-mode-env.sh` | `pytest tests/unit_tests/runtime/test_session_mode_switch.py` | 新增 3 例“缺少 / null / 空白 `expected_mode`”失败；其余 224 例通过 | — |
| 2026-11-18 变更后 | 同上 | `pytest tests/unit_tests/runtime/test_session_mode_switch.py test_session_operation_lock.py test_shared_conversation_runtime.py test_runtime_service.py` | 473 例全部通过 | 真实服务联调未执行 |

同时补充的用例（变更前即通过，记录现有行为）：两个冲突的 `mode.set` 经真实 Runtime 并发提交，一个成功、一个 `MODE_CONFLICT`，`mode_revision` 只增加 1；非规范目标模式（`team.design`、`team`、`agent.work`、`agent.work.bogus`、空串）得到 `BAD_REQUEST` 且已存模式不变；不存在的会话上 `mode.set` / `mode.get` 得到 `NOT_FOUND`。

### 兼容

Gateway 文档同步写明 `expected_mode` 必填；未包含本改动的 AgentServer 仍接受省略它的请求，两者应一起发布。

## 2026-11-19 00:57（Asia/Singapore）续写：只有归属者能切换模式，请求不再补写归属者

### 起因

Gateway MR !173 的检视继续指出两点：未记录归属者（`user_id` 为空）的会话对同通道的任意调用方都通过归属校验；`sync_session_request_metadata` 会在已有会话归属者为空时用请求方补写，一条提示词就能让已识别用户成为该会话的归属者，再通过 `mode.set` 校验。归属校验与 `expected_mode` 已在 `compare_and_set_session_mode` 的同一把锁内完成，调用方身份就是网关转发的连接身份（IAM 启用时由握手绑定到 token，见网关 MR）。

空归属者的来源：个人版 Web 不声明用户、TUI 未配置 userId 时创建的会话；少数不知道用户的补建路径（会话尚无元数据时改名、投递上下文在元数据缺失时写入）；无 `metadata.json` 的旧会话在列表中拼出的空值。会话消息服务已把空归属者视为匿名 `local` 范围。

### 决定与实现（用户确认，2026-11-19）

- `mode_switch_session_visible_to`：调用方必须等于记录的归属者。空归属者即匿名用户，只匹配匿名调用方（个人版两边都为空，行为不变）；已识别用户访问无归属会话得到 `NOT_FOUND`。范围为所有可切换模式的会话（Web、TUI，含群聊；代理组、外部代理、定时任务会话本就不可切换）。
- `sync_session_request_metadata`：已有会话不再由请求补写归属者，归属者只在创建时记录（该函数自身的新建分支仍记录创建者）。全仓只有聊天请求同步（`runtime/request.py`）向它传 `user_id`；会话列表不按归属者过滤，其余读取方（跨会话消息、定时任务创建者兜底、断连续跑路由）都以创建时归属者为准。
- 不在本次范围：`session.switch` 与聊天请求本身不校验归属者。按用户部署 AgentServer 的 AgentOS 中由沙箱隔离兜底；多用户共享同一 AgentServer 时仍是缺口，另行立项。

### 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 | 实际结果 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| 2026-11-19 变更前 | `local-web-mode` 工作区（含未提交改动），`web-mode-env.sh` | `pytest tests/unit_tests/runtime/test_session_mode_switch.py -k "recorded_owner or ownerless or claim or still_records"` | 12 例失败（已识别用户可切换 / 读取无归属会话；提示词补写归属者），18 例通过 | — |
| 2026-11-19 变更后 | 同上 | `pytest tests/unit_tests/runtime/test_session_mode_switch.py test_session_operation_lock.py test_shared_conversation_runtime.py test_runtime_service.py` | 499 例全部通过 | 见下行全量对比 |

### 兼容

升级后，经补建路径产生的无归属会话对已识别用户不再可切换模式（`NOT_FOUND`），这类会话可在新对话中继续；已被旧逻辑补写过归属者的会话不受影响。

## 已知遗留

### 2026-11-17 04:17（Asia/Singapore）：空闲 Team 监听导致误报忙碌

- 现场证据：本地 Web 会话已记录 `is_processing=False / is_complete=True`、`team went idle` 与元数据 `status=idle`，随后切换仍提示忙碌；常驻请求继续 keepalive。现有日志未记录命中的 busy 分支，不能据此排除其他残留运行态。
- 可复现根因：Web 的 `attach_only` 订阅也登记在 Runtime 待完成请求集合中，但它从不拥有轮次。原 `team_session_has_parked_request` 要求每个请求都有已释放轮次标记，导致纯观察订阅永久阻止豁免。
- 修复：TeamManager 显式查询仍登记的只读 waiter，允许它在有或无 producer stream 时通过 parked 判定；普通请求仍要求存活 stream 和已释放轮次标记。活跃轮次、inflight 请求、Runtime 执行和交互检查保持有效，移除监听登记后不再豁免。共用该判断的归档路径也受影响，不修改 Gateway。
- 防回归约束：连接存活不能作为任务执行证据；只豁免服务端已登记的观察订阅，不能按客户端参数或持久化 `idle` 状态直接放行全部请求。
- 验证：Windows / Python 3.14.7、当前工作区与本地 Gateway 依赖，通过真实 TeamManager 登记及 Runtime 模式提交入口测试。新增 28 例覆盖 Web/TUI、默认/项目会话、纯监听、无 stream 监听、已结束轮次混合监听、待处理请求、自动化 inflight、活跃轮次及已移除监听。修复前 12 例失败、16 例通过；修复后执行 `python -m pytest tests/unit_tests/runtime/test_session_mode_switch.py tests/unit_tests/server/test_archive_lifecycle.py tests/unit_tests/runtime/test_runtime_service.py --no-cov -q -o log_cli=false`，459 例全部通过。
- 未覆盖：修复后真实浏览器联调；尚未重启用户服务。已同步中英文模式说明。文档索引同步受本地 Git 作者身份缺失影响，不推断作者或修改 Git 配置。

### 仍待处理

- 跨 work / code 类型切换时 `work_mode` 不更新（沿用 TUI 限制），基准脚本因此拒绝混合类型的序列。
- Web 的中断确认早于后端完全停止，紧接着切换可能返回 `SESSION_BUSY`；界面提示用户停止后重试，基准脚本退避重试。
- `session.switch` 与聊天请求不校验会话归属者（2026-11-19 续写的“不在本次范围”）；多用户共享同一 AgentServer 时需要单独设计。
