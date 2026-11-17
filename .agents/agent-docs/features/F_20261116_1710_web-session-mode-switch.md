---
id: F_20261116_1710_web-session-mode-switch
title: Web 与 TUI 共用会话模式切换（mode.set / mode.get）
type: F
author: null
author_email: null
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
