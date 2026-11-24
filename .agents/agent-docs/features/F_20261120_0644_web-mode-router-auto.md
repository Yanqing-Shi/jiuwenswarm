---
id: F_20261120_0644_web-mode-router-auto
title: Web“自动”复选框：由模式路由在每次运行前选择 Agent / Team（mode.route）
type: F
author: Yanqing-Shi
author_email: toddsyq@gmail.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/agents
  - jiuwenswarm/runtime
  - jiuwenswarm/server
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/common/mode_router_config.py
  - jiuwenswarm/common/schema/message.py
  - jiuwenswarm/server/runtime/session/mode_router.py
  - jiuwenswarm/server/runtime/session/mode_router_tasks.py
  - jiuwenswarm/server/runtime/session/session_history.py
  - jiuwenswarm/server/runtime/agent_adapter/interface.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/runtime/service.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/control/config_service.py
  - jiuwenswarm/resources/config.yaml
  - tests/unit_tests/runtime/test_session_mode_route.py
  - tests/unit_tests/runtime/test_mode_router_tasks.py
  - jiuwenswarm/channels/web/frontend/src/features/sessionMode/autoRoute.ts
  - jiuwenswarm/channels/web/frontend/src/features/sessionMode/autoRouteState.ts
  - jiuwenswarm/channels/web/frontend/src/features/sessionMode/ModeRouteConfirmDialog.tsx
  - jiuwenswarm/channels/web/frontend/src/features/sessionMode/workflowAutorun.ts
  - jiuwenswarm/channels/web/frontend/src/features/sessionMode/useWorkflowAutorun.ts
  - jiuwenswarm/channels/web/frontend/src/stores/chatStore.ts
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/InputArea.tsx
  - jiuwenswarm/channels/web/frontend/src/App.tsx
designs: []
design_note: 无独立 D。方案在开发会话中以计划形式与用户逐项确认（见“用户确认的决定”），建立在 F_20261116_1710_web-session-mode-switch 的 mode.set / mode.get 之上。
---

# Web“自动”模式路由实施与决策归档

## 关联依据

- 前置实现：`F_20261116_1710_web-session-mode-switch`（Web 的 `mode.set` / `mode.get`，前端提示词队列与 `modeSwitchPending`）。
- 日期：2026-11-20（Asia/Singapore）。
- 目标：接入一个模型形态的“模式路由”，在每次运行开始前判断提示词应在 Agent 还是 Team 模式下执行。模型权重与推理代码不进入任何仓库，以独立本地进程运行；本仓只保留一个轻量客户端。
- 实际范围：
  - 后端新增只读请求 `mode.route`（只给出答案，不提交切换）与路由服务客户端；`config.get` 增加只读项 `mode_router_enabled`。
  - Web 前端新增“自动”“提醒”两个复选框、确认弹窗，以及运行前的路由步骤。
  - agent-gateway 的 Web 转发名单加入 `mode.route`（外部仓库，独立提交）。
  - 未改动：`mode.set` / `mode.get` / `switch_session_mode` / `compare_and_set_session_mode`。

## 用户确认的决定（2026-11-19 开发会话）

- 不增加第三种模式。模式选择器旁增加复选框“自动”：未勾选时与原来一样手动切换。
- 勾选“自动”后出现第二个复选框“提醒”，默认勾选。勾选时路由每次决定切换都弹窗，接受则切换，拒绝则保持原模式，提示词都会执行；取消勾选则不询问直接切换。
- 勾选“自动”时模式选择器锁定，只显示当前模式（运行中本来也不能切换，路由的决定发生在运行开始前）。
- 两个复选框对所有对话生效，保存在浏览器。
- 路由只在对话空闲、运行即将开始时处理提示词，因为上一轮运行的结果要作为路由的输入。运行期间提交的提示词留在队列，轮到它时再询问路由。
- 给正在运行的轮次的输入不经过路由：Team 运行期间发送的消息直接交给 Leader；队列项点“立即发送”（steer）直接交给当前模式。
- 路由输入：新提示词、当前模式、work/code，以及最近若干轮对话（可配置，默认 3 轮）。
- 路由以独立本地进程运行。

## 上下文与跨边界影响

- 请求链路：浏览器 `mode.route` → Gateway Web 通道（透传）→ AgentServer `_answer_mode_request(verb="route")` → `AgentRuntime.session_mode_route_context`（归属与固定执行者校验、读取当前模式）→ `AgentRuntime.route_session_mode`（读取最近若干轮历史、调用路由服务 `POST /v1/route`）。需要切换时由浏览器随后发 `mode.set`，路径不变。
- 为什么不由服务端在收到提示词后自行切换：提示词到达 AgentServer 即登记为待处理请求（`begin_chat_request`），此后 `mode.set` 的忙碌检查会拒绝；`before_chat_request` 扩展钩子在该登记之后才触发。服务端发起切换还需要新的推送事件（Gateway 对未登记事件类型会删去 `mode` 字段）和服务端队列；确认弹窗本身也只能在浏览器完成。
- Gateway 会给不带 `session_id` 的帧分配路由用会话号，因此 AgentServer 只按 params 中是否带 `session_id` 区分已保存会话与尚未保存的新对话；新对话由客户端在 `current_mode` 中给出当前模式。
- 权威状态不变：会话 `metadata.json` 的 `mode` / `mode_revision`。`mode.route` 不写任何状态。

## 实际实现与开发决策

### 后端

- `common/mode_router_config.py`：读取 `mode_router` 配置段（`url`、`timeout_seconds`、`history_runs`、`history_chars`、`query_chars`）。环境变量 `MODE_ROUTER_URL` 优先于 `url`，这样已有的、没有该配置段的用户 config.yaml 也能由启动脚本打开路由。放在 `common` 是因为 Front 的配置查询不能加载 server 侧代码。
- `server/runtime/session/mode_router.py`：`recent_runs`（经 `read_history_cursor_page` 取最近 N 条用户提示词及其后的 `chat.final`，按时间正序，超长截断；该读取会先排空待写队列，刚结束的一轮回答也在其中）与 `request_decision`（httpx，`trust_env=False`，避免代理变量截走本地请求）。失败分为 `ROUTER_DISABLED` / `ROUTER_UNAVAILABLE` / `ROUTER_BAD_ANSWER`。
- `session_metadata.switch_target_for_route`：由当前三段模式与路由答案（agent / team）拼出目标模式，保留 work/code 与计划态。
- `AgentRuntime.session_mode_route_context` / `route_session_mode`：复用 `mode_switch_session_visible_to` 与 `session_mode_switch_blocker`，不可切换的会话不会被送去路由。读取历史失败只记日志，路由仍按提示词本身判断。
- `_answer_mode_request` 的 `commit: bool` 改为 `verb`（set / get / route）。route 在会话的 mode 锁内读取当前模式，在锁外调用路由服务，慢模型不会挡住同一会话的 `mode.set`。
- 响应：`{route, mode, current_mode, switch_needed, confidence, reason}`；`mode` / `current_mode` 可直接用作随后 `mode.set` 的 `mode` / `expected_mode`。

### 前端

- `features/sessionMode/autoRoute.ts`（纯函数，可单独编译测试）：两项设置的解析与序列化、`requestModeRoute`、`afterRouteDecision`（send / confirm / switch）、`decideAutoRoute`、`isRouterFailure`。
- `features/sessionMode/autoRouteState.ts`：设置（localStorage，所有对话共用）、服务端是否配置路由、待回答的确认弹窗（按会话）、`routePromptBeforeRun`（询问路由 → 需要时确认 → 需要时经 App 登记的 `switchSessionModeInPlace` 提交切换；返回 run / hold）。
- `chatStore`：会话运行态 `modeRoutePending`（路由或用户正在为下一轮的提示词做决定；期间队列不出队、不可 steer，新提示词入队）；队列项 `routed` 标记（已询问过路由，直接发送）。
- 两个路由点：
  - 空闲时直接发送的提示词：在 `ChatPanel.handleSendMessage` 中先路由，再走原有发送路径（插件历史刷新、输入来源、话题事件均保留）。
  - 队列中的提示词：`useWebSocket.drainTaskQueueIfIdle` 在出队前对队首未路由项先路由，完成后标记 `routed` 并出队。
- `InputArea`：两个复选框（仅当服务端配置了路由、个人版 WebSocket、且当前为 Agent 或 Team 时显示）；勾选“自动”或路由进行中时模式控件锁定；路由进行中提交的提示词入队，并显示“正在选择模式…”。
- `ModeRouteConfirmDialog`：确认弹窗，Esc 等同拒绝。

## 与计划的偏差

- 计划为队列项设置 routing / confirming / done 三态，并让“自动”下所有空闲提示词都先入队。实际改为会话级 `modeRoutePending` 加队列项 `routed` 标记，空闲提示词不入队而在原发送路径内路由。原因：队列出队路径不执行插件历史刷新、不携带输入来源，若所有提示词都改走队列，这些步骤会从“偶尔缺失”变成“总是缺失”。对用户可见的行为与计划一致。
- 计划的 `switch_target_for_route(metadata, route)` 实际签名为 `(current_mode, route)`，新对话没有元数据也能复用。
- 计划写“续写 F_20261116_1710”；因属于新特性，改为本文件。
- 新增配置项 `query_chars`（计划未列），避免把超长粘贴内容整段发给本地模型。

## 行为说明与取舍

- 路由未启动、超时或答案不可用：提示词按当前模式执行，每个对话提示一次。会话不可路由（`BAD_REQUEST` / `NOT_FOUND` / `ROUTER_DISABLED` 等）时不提示。
- 路由决定的切换未提交（忙碌、冲突、失败）：沿用既有规则，提示词留在队列并暂停队列，不以旧模式发出；恢复队列后重新询问路由。
- 路由进行中若有其他运行开始，本次决定作废，不弹窗也不切换，该运行结束后重新询问。
- 路由进行中用户切到别的对话：提示词转入原对话的队列（已路由），由队列发送，避免发到当前显示的对话。
- 新对话的第一条提示词：路由后直接以所选模式创建会话；期间再次提交会被忽略，草稿保留。
- 多标签页：只保证单标签页内的顺序（沿用模式切换的既有约束）。
- 目标（goal）提交不经过路由（goal 无法排队，既有限制）。

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-20 后端新增用例 | 工作区未提交，基于 `11b567196`；WSL2，Python 3.12.14，`web-mode-env.sh` | `pytest tests/unit_tests/runtime/test_session_mode_route.py --no-cov -q` | 81 通过 | 无 |
| 2026-11-20 后端既有模式切换用例 | 同上 | `pytest tests/unit_tests/runtime/test_session_mode_switch.py --no-cov -q` | 253 通过 | 无 |
| 2026-11-20 后端相关套件 | 同上 | `pytest tests/unit_tests/runtime/test_session_mode_{switch,route}.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_request.py tests/unit_tests/agentserver/test_front_permission_profile.py tests/unit_tests/agentserver/test_front_phase1.py tests/unit_tests/server/test_gateway_adapter.py tests/unit_tests/test_kv_cache_affinity_settings.py tests/unit_tests/common --no-cov -q` | 1908 通过，68 跳过，6 失败 | 6 项失败在已提交代码的副本上以同一组合运行同样失败：`test_kv_cache_affinity_settings` 4 项与 `test_task_asr_config_read` 1 项单独运行通过（与运行顺序有关）；`test_req_method_contract` 1 项来自本机 Gateway 检出中的 `mcp.sources.providers` |
| 2026-11-20 Gateway 用例 | agent-gateway `cb4374cef` + 本次改动；需要比 Swarm 虚拟环境更新的 Core，运行时把本机 uv 缓存中的较新 Core 加入 PYTHONPATH | `pytest tests/unit_tests/gateway/test_web_mode_forwarding.py test_tui_mode_forwarding.py test_app_web_handlers.py` | 299 通过 | 在 Swarm 虚拟环境自带的 Core 下这些文件无法导入（缺 `openjiuwen.core.extension`），与本次改动无关 |
| 2026-11-20 前端单测 | 同上；Node 22.22.1 | `npm run test:session-mode-commit`、`test:chat-store-streaming`、`test:input-area-permission-merge`、`test:session-input`、`test:i18n-locales` | 31/31；15/15；131/133；119/120；6/6 | 失败项与变更前相同：企业版输入区锁定、组织 Owner 停止后禁用、MoA 状态路由各 1 例 |
| 2026-11-20 前端构建 | 同上 | `node_modules/.bin/vite build` | 通过 | 未执行 TypeScript 类型检查（本仓约束不运行静态检查） |
| 2026-11-20 真实服务联调（无模型调用） | Swarm 工作区 + 工作区固定的 Gateway `87ebb3415` 加本次 Gateway 改动（临时副本）；临时数据目录；占位模型密钥；路由为固定规则的替身服务 | 启动服务后运行集成工作区的 `smoke-web-mode.py`，并经 Gateway WebSocket 依次发送 `config.get`、`mode.route`、`mode.set`、停止路由后的 `mode.route` | 全部符合预期：`mode_router_enabled=true`；已保存会话与新对话均得到答案；按答案 `mode.set` 成功；缺少 query 得到 `BAD_REQUEST`；路由停止后得到 `ROUTER_UNAVAILABLE` 且模式不变 | 未在浏览器中操作界面；未接入真实路由模型；未发起模型调用，因此“路由 → 切换 → 提示词执行”的整轮未在真实服务上走通 |

新增测试：`tests/unit_tests/runtime/test_session_mode_route.py`（后端 81 例）；前端 `tests/autoRoute.test.mjs`（10 例）、`chatStoreStreaming` 3 例、`inputAreaPermissionMerge` 11 例（复选框与锁定、路由进行中入队、直接发送路径的路由 / hold / 运行中不路由 / 离开对话 / 新对话）、`sessionInput` 13 例（真实 hook 与替身 WebSocket：运行结束后才路由、切换后发送、切换未提交暂停队列、弹窗接受 / 拒绝、取消“自动”、路由失败、steer 不路由、路由期间开始新运行、路由期间移除队列项）。

## 验证结论

- 已证明范围：`mode.route` 的归属、固定执行者、新对话、历史输入、路由失败分类与不持锁调用；Gateway 透传；前端规则、队列与直接发送两条路径上的路由顺序；经真实 Gateway 与 AgentServer 的请求往返。
- 未覆盖：浏览器中的界面与弹窗；真实路由模型；带模型调用的整轮；多标签页并发。
- 可支持的交付判断：代码层面满足约定范围，提交前仍需在浏览器中用路由替身和真实模型各走一遍。

### 覆盖与资料同步

- 已更新：`docs/en/Modes.md`、`docs/zh/模式系统.md`；agent-gateway `docs/zh/reference/gateway-web-api.md` 增加 `mode.route`。
- 待同步：agent-gateway 改动合入后更新本仓 `pyproject.toml` / `uv.lock` 的版本号；在此之前 `mode.route` 只在本地联调环境可用。

## 2026-11-24 修订：由地址带入一组提示词并自动执行（autorun）

### 背景与用户确认的决定

- 需求（2026-11-24）：在本机跑一个两轮的固定流程，打开 Web 页面后自动开始，第一轮用 Team、第二轮用 Agent。两轮的模式现在写死，以后改为读取路由的输出，形式要与“自动”路由一致。
- 用户确认：由 Web 页面自己提交各轮提示词（另一方案是页面之外的脚本经 Gateway 提交，不改本仓，但不能保证页面实时显示，未采用）。
- 写死的模式不进入本仓：由集成工作区的路由替身按路由契约作答（`router-stub.py` 的 `workflow` 规则，读取工作流文件）。页面、Gateway、AgentServer 处理写死的一轮与处理真实路由的一轮完全相同；换成真实路由时本仓无需改动。

### 实现

- `features/sessionMode/workflowAutorun.ts`（纯函数）：读写地址参数 `autorun`，值为 `{"name", "prompts"}` 的 UTF-8 JSON 的 URL 安全 base64；最多 20 条，不完整或超长时整体拒绝；地址中只有提示词，没有模式。
- `features/sessionMode/useWorkflowAutorun.ts`：模块加载时读取一次参数并从地址中去掉（刷新不会再次执行）。在已连接、服务端答复了是否配置路由、当前为 Agent 或 Team 模式的新对话时开始：
  - 仅对本页面勾选“自动”、取消“提醒”（`setAutoRouteSettings(..., { remember: false })`，不写入 localStorage）；
  - 第一条提示词经 `ChatPanel.handleSendMessage` 提交，即输入区的直接发送路径；
  - 其余提示词在第一轮运行开始后加入该对话的提示词队列，由 `drainTaskQueueIfIdle` 在前一轮结束后逐条路由并发送。
  - 没有新增路由点，也没有新增请求：两处既有路由点覆盖全部各轮。
- `autoRouteState.ts`：`setAutoRouteSettings` 增加 `remember` 选项；新增 `getAutoRouteAvailability` / `useAutoRouteAvailability`，区分“服务端尚未答复”与“没有路由”。
- `ChatPanel/index.tsx`：调用 `useWorkflowAutorun`。文案 4 条（中英）。

### 行为说明与取舍

- 服务端没有路由：提示并放弃，不发送任何提示词（否则各轮都在同一模式下执行，不是该流程）。
- 参数无法解析：提示，并同样从地址中去掉。
- 第一轮在 120 秒内没有开始运行：提示，其余各轮不入队。
- 当前不是新对话，或模式不是 Agent / Team：等待，直到满足条件；不提示。
- 其余各轮加入“当前显示的、正在运行的”对话的队列；第一轮开始前用户切到另一个正在运行的对话时会入错队列（未处理）。
- 每次页面加载最多执行一次。

### 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-24 前端单测 | 工作区未提交，基于 `729d4bcf8`；Node 22.22.1 | `npm run test:session-mode-commit`、`test:input-area-permission-merge`、`test:session-input`、`test:chat-store-streaming`、`test:i18n-locales` | 35/35（含新增 `tests/workflowAutorun.test.mjs` 4 例）；137/139；120/121；15/15；6/6 | 3 项失败与本次变更前相同：企业版输入区锁定、组织 Owner 停止后禁用、MoA 状态路由 |
| 2026-11-24 类型检查与构建 | 同上 | `node_modules/.bin/tsc --noEmit`、`node_modules/.bin/vite build` | 通过 | 无 |
| 2026-11-24 浏览器整轮（替身模型，autorun） | 真实 Gateway 与 AgentServer，临时数据目录；模型为本机替身服务（OpenAI 协议，固定文本）；路由为替身（`workflow` 规则）；Playwright 无头 Chromium 打开 `autorun` 地址 | 记录页面发出的请求、服务端事件、会话 `history.jsonl` 与替身模型收到的请求 | `mode.route`（新对话）→ team，`session.create` 与 `chat.send` 为 `team.work.normal`；运行结束后 `mode.route` → agent，`mode.set` team→agent 成功，`chat.send` 为 `agent.work.normal`；历史依次为 Team 提示词与 Leader 回答、`session.mode_changed`、Agent 提示词与回答；第一轮的模型请求带 Team Leader 工具（`build_team`、`spawn_teammate` 等），第二轮带 Agent 工具；地址中的 `autorun` 已去掉；localStorage 中的设置未写入 | 替身模型不调用工具，Team 未创建成员，Leader 一次作答即结束；真实模型下 Team 运行结束到 `mode.set` 可提交之间的时序未验证 |
| 2026-11-24 拒绝路径 | 同上，分别为服务端不配置路由、地址参数损坏 | 同上 | 各显示一条提示，未发出 `mode.route` / `session.create` / `chat.send` | 无 |

未覆盖：真实模型的整轮；带成员的 Team 运行；`useWorkflowAutorun` 没有独立的单元测试（由上面的浏览器整轮覆盖）。

联调中观察到、与本次变更无关：Agent 模式的回答结束后页面发出 `tts.synthesize`（`src/utils/tts.ts`），本地环境中该请求被 AgentServer 当作一条空提示词执行，历史里多出一轮空提示词和回答。未处理。

## 2026-11-24 修订：路由输入按任务组织，并保存在每个会话的任务容器中

### 背景与用户确认的决定

- 需求（2026-11-24）：路由的输入严格是此前每个任务的“用户输入 + 最终输出”再加上新提示词，按时间顺序：提示词 1、输出 1、提示词 2、……。路由不接收系统提示词和可用工具。希望有一个与会话关联的容器，在能够保存时立即保存这些输入和输出，路由时不必到上下文窗口里去找。
- 说明：此前的实现也不读取上下文窗口，而是在 `mode.route` 时倒序读取会话 `history.jsonl`，取最近 3 轮、每条截断到 2000 字符，并按位置配对（见下方“被替换的行为”）。
- 用户确认（“go”）的三项：
  1. 输入形态不变：有序列表 `history` 加新提示词 `query`，由路由自行拼接。
  2. `current_mode`、`work_mode` 和每项的 `mode` 保留，作为路由可以忽略的附加信息。
  3. 没有最终输出的任务（被停止、失败）保留提示词，其后没有输出。
- 同时确认的口径：发送此前全部任务且不截断；Team 任务的输出取 Leader 在该次运行中的最后一条最终回答；只有启动任务的提示词计入。

### 实现

- `server/runtime/session/mode_router_tasks.py`（新增）：
  - 容器是会话目录中与 `history.jsonl` 并列的 `mode_router_tasks.jsonl`。首行是 `head`（所属历史文件的设备号与 inode），其后每行对应一条与任务有关的历史记录，并带 `at`（来源记录在历史文件中的结束偏移）：
    - `prompt`：启动任务的用户提示词（请求号、模式、文本）；
    - `final`：主发言者的 `chat.final`（请求号、发言者 `assistant` / `leader`、文本）；
    - `turn`：开启一个轮次但不是任务的用户记录（空内容、目标、由其他会话或心跳发起），其后的回答不归于前一个任务。
  - `journal_row`：分类。给正在运行的轮次的输入不产生行：`is_supplemental_input`（补充输入 / 立即发送）、`sent_during_team_round`（见下）、带 `event_type` 的用户事件。子智能体转录、成员（`teammate`）的最终回答、其余事件记录都不进入容器。
  - `assemble_tasks`：读取时把行组装成任务。每条 `prompt` 开启一个任务；任务的输出是归属于它的最后一条 `final`：请求号与提示词相同，或者任务处于 Team 模式且发言者是 Leader。
  - `note_history_append`：由 `session_history._append_record_unfenced` 在每条记录落盘后调用（与既有的 `history_replay.note_history_append` 并列，所有 JSONL 追加都经过此处），把对应的行追加到容器。内存中按历史路径记录“容器已覆盖到的历史偏移”，与本次追加前的文件大小相等才追加；不相等说明有本进程未见到的追加，放弃并等待读取时补齐。不抛出异常。
  - `read_session_tasks`：等待排队的历史写入落盘，在历史文件锁内校验容器（`head` 与历史文件一致、覆盖偏移不超过文件大小），从已覆盖的偏移扫描历史文件尾部补齐，再组装。容器缺失、损坏或属于被替换的历史文件时从头重建。旧版单文件历史（`history.json`）没有偏移，不建容器，每次由全部记录组装。
  - 只在有路由时维护容器：环境变量 `MODE_ROUTER_URL` 已设置，或本进程已处理过路由请求（路由只写在 `config.yaml` 中时，由第一次路由请求打开，并由读取补齐）。不在历史写入线程中解析配置文件。
- `server/runtime/agent_adapter/interface.py`、`agents/harness/team/team_manager.py`：记录用户提示词时，若该会话的 Team 轮次正在进行（`TeamManager.is_round_active`，新增模块函数 `is_team_round_active`），在历史记录上标记 `sent_during_team_round: true`（`_mark_team_round_input`，用于流式与非流式两个入口）。原因见“Team 模式的记录形态”。
- `mode_router.py`：`recent_runs` 及其位置配对逻辑删除，改为 `task_history`（读取任务并展开为 `user` / `assistant` 消息；`cut_text` 的上限为 0 时不截断）。
- `runtime/service.py`：`route_session_mode` 改用 `task_history`。
- `common/mode_router_config.py`、`resources/config.yaml`：`history_runs`、`history_chars`、`query_chars` 的默认值改为 0（不限制），三者成为可选上限。
- 路由契约、`mode.route` 的请求与响应、前端、`mode.set` 均未改动。

### Team 模式的记录形态（联调中确认）

用替身模型在真实服务上发送“Team 提示词 A → 结束后 B → C → C 运行中发送 D”，历史中：

- Team 在整个生命周期内只有一条响应流：Leader 的所有 `chat.final` 都记在开启这条流的请求（A）名下，B、C 的回答也是。
- 后续提示词（B、C、D）各有自己的请求号，其 `chat.request_completed` 在送达时立即写入，而不是在该轮结束时；A 的 `chat.request_completed` 直到流关闭（例如切换模式）才写入。
- “发给已空闲 Team 的提示词”（B）与“轮次运行中发给 Leader 的消息”（D）在历史中没有任何区别。

因此不能用请求号给 Team 的回答配对，也不能用 `chat.request_completed` 判断 Team 任务是否结束。服务端在记录提示词的那一刻知道轮次是否在进行，于是在该处打标记；容器只需区分“开启任务的提示词”和“主发言者的最终回答”。最初的方案（记录 `done` / `idle` / `route` 行，用“任务仍在运行”推断并入）在这次联调中被证明会把同一 Team 的连续提示词并成一个任务，已放弃。

### 被替换的行为

| 项目 | 之前 | 现在 |
| --- | --- | --- |
| 范围 | 最近 3 轮 | 此前全部任务（可用 `history_runs` 限制） |
| 长度 | 每条截断到 2000 字符，新提示词 8000 | 完整（可用 `history_chars` / `query_chars` 限制） |
| 配对 | 按位置：回答归于文件中位于其前的提示词 | Agent 按请求号；Team 按 Leader 发言与轮次顺序 |
| 一次运行的多条最终回答 | 全部发送 | 只取最后一条 |
| 空提示词请求的回答（如页面的 `tts.synthesize`） | 被当作上一条提示词的又一条回答 | 不属于任何任务，忽略 |
| 运行中的输入 | 补充输入被当作一轮提示词 | 不计入 |
| 读取成本 | 每次倒序扫描事件记录 | 读取容器，只扫描上次之后新增的历史尾部 |

### 行为说明与取舍

- 为什么是“日志行 + 读取时组装”，而不是直接保存成对的输入 / 输出：提示词与最终回答在不同时刻落盘，逐行追加不需要回写已有内容；组装规则集中在一处，重建与增量得到相同结果。
- 为什么挂在历史写入处，而不是各个提示词入口：用户提示词在 6 处以上写入历史，最终回答另有路径；所有记录都经过同一个追加函数，在那里分类不会漏掉入口，新入口也自动覆盖。
- 容器是历史的派生物。历史文件被替换（倒回、恢复、迁移）后 inode 改变，容器在下一次读取时重建；会话删除、归档时容器随会话目录一起处理。
- `sent_during_team_round` 是历史记录上的新增字段，只在 Team 轮次进行中记录的提示词上出现。有该字段之前写入的历史中，Team 运行期间发给 Leader 的消息会被当作任务。
- Team 任务的输出是下一条提示词之前 Leader 的最后一条最终回答。Leader 在成员仍在工作时先给出一条阶段性回答，而下一个任务在最终结果之前就开始时，该任务的输出是那条阶段性回答，迟到的结果会归于后一个 Team 任务。
- 已有的用户配置文件若显式写有旧值（3 / 2000 / 8000）会继续生效，需要改为 0。集成工作区的 `runtime-mode-switch/config/config.yaml` 已随本次修改调整。

### 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-24 新增与改写的用例 | 工作区未提交，基于 `729d4bcf8`；WSL2，Python 3.12，`web-mode-env.sh`，数据目录指向临时目录 | `pytest tests/unit_tests/runtime/test_mode_router_tasks.py tests/unit_tests/runtime/test_session_mode_route.py --no-cov -q` | 118 通过 | 无 |
| 2026-11-24 既有模式切换用例 | 同上 | `pytest tests/unit_tests/runtime/test_session_mode_switch.py --no-cov -q` | 269 通过 | 无 |
| 2026-11-24 受影响范围的用例 | 同上 | 引用提示词记录、Team 轮次、配置模板或历史追加的 70 个测试文件（`_history_user_extra`、`is_supplemental_input`、`is_round_active`、`_active_rounds`、`begin_round`、`release_round`、`resources/config.yaml`、`_deep_merge`、`mode_router`、`_append_record`、`note_history_append`、`_enqueue_history_item`、`append_history_record`） | 2802 通过，3 跳过，3 失败 | 3 项失败在已提交代码 `729d4bcf8`（临时 worktree，不含任何未提交改动）上同样失败：`test_restart_direct_expert_chat_restores_backend_and_reuses_membership`、`test_suggestion_reaches_the_pages_open_on_its_session_only`、`test_list_session_turns_includes_project_file_change_stats` |
| 2026-11-24 浏览器整轮（替身模型） | 真实 Gateway 与 AgentServer，临时数据目录；模型为本机替身服务；路由为集成工作区的替身（`workflow` 规则，日志打印收到的输入）；Playwright 无头 Chromium 打开 `autorun` 地址 | 两轮工作流结束后再经 Gateway 对同一会话发一次 `mode.route` | 路由三次收到的输入依次为：`prompt(1214)`；`prompt@team(1214)+answer@team(30)+prompt(19)`；`prompt@team(1214)+answer@team(30)+prompt@agent(19)+answer@agent(30)+prompt(13)`。容器中为 2 条 `prompt`、对应的 2 条 `final`，以及页面 `tts.synthesize` 产生的 1 条 `turn` 和其后未被采用的 `final`；容器从会话的第一条记录起随写入追加 | 替身模型不调用工具，Team 未创建成员 |
| 2026-11-24 Team 连续提示词与运行中消息 | 同上，直接经 Gateway WebSocket，不经过路由 | Team 提示词 A，结束后 B，结束后 C，C 发出 0.6 秒后发送 D | 历史中只有 D 带 `sent_during_team_round`；容器为 A、B、C 三个任务，各自的输出是其后 Leader 的最终回答，D 不在其中 | 同上 |
| 2026-11-24 较大范围回归 | 同上 | `pytest tests/unit_tests/runtime tests/unit_tests/server tests/unit_tests/agentserver` 加上其余引用 `session_history`、`history_replay`、`mode_router`、`team_manager` 的测试文件 | 17809 通过，53 跳过，33 失败 | 33 项失败在已提交代码 `729d4bcf8`（临时 worktree）上逐项重跑同样失败：真实 Worker 进程的恢复类用例 23 项（`test_inflight_stream_recovery`、`test_ipc_receive_recovery`、`test_worker_*`）、diff 服务 9 项（`test_diff_service_rewound_snapshots`、`test_redo_diff_service`、`test_turn_diff_history`）、`test_root_regressions` 1 项 |

新增测试：`tests/unit_tests/runtime/test_mode_router_tasks.py`（分类 18 例、组装 4 例、容器 7 例、Team 轮次标记 1 例，共 30 例）；`test_session_mode_route.py` 中路由输入相关用例改写为 3 例（全部任务按序完整发送、可选上限、不属于任务的记录被排除）并新增默认值用例。

未覆盖：真实模型；带成员的 Team 运行（替身模型不调用工具，Leader 一次作答即结束）；`interface.py` 两个入口上标记的调用只经真实服务联调覆盖，单元测试覆盖的是 `_mark_team_round_input` 本身；多进程同时写同一会话历史时的容器追加（依赖读取时补齐）。

## 2026-11-24 修订（二）：包含用户输入的每条提示词，每次运行只保留最后一条回答

本节替代上一节中“只有启动任务的提示词计入”的口径及相应实现；容器的存放位置、写入时机、补齐与重建方式不变。

### 用户确认的决定（2026-11-24）

- 用户输入的每条提示词都要进入路由输入，包括发入正在运行的轮次的提示词（Team 轮次运行期间发给 Leader 的消息、Agent 的“立即发送”）。不要求“提示词 + 回答”成对，可以是“提示词、提示词、回答”，只要求按时间顺序。
- 上一节“不计入的内容”中的其余各项维持不变：成员回答、无人输入的轮次（心跳、其他会话）及其回答、空提示词请求的回答、对权限询问的回答、目标（goal）。
- 回答只要“最终的那一条”：发入运行中的提示词之前的阶段性回答不要。即“C、阶段性回答、D、结果”发给路由的是“C、D、结果”。
- 发入运行中的提示词在路由输入中带附加字段 `"during_run": true`（“go”时接受的建议），用来区分“C 运行中发入 D”与“C 被停止后 D 是新任务”。

### 实现

- `mode_router_tasks.journal_row`：带 `is_supplemental_input` 或 `sent_during_team_round` 的用户提示词不再丢弃，写成带 `during_run` 的 `prompt` 行；其中无人输入的（跨会话、心跳）或没有文本的不产生行，也不开启轮次。容器版本号升为 2，版本 1 的容器在下一次读取时由历史重建。
- `mode_router_tasks.assemble_tasks`：返回任务列表，每个任务是按记录顺序排列的条目（`user` / `assistant`，发入运行中的提示词带 `during_run`）。启动轮次的提示词开启任务；发入该轮次的提示词留在任务内原位置，其请求号计入任务；任务的回答只保留最后一条，位置是它被记录的位置，更早的回答被移除。无人输入的轮次（`turn`）之后发入的提示词单独成为一个只有该提示词的任务，该轮次的回答不保留。
- `mode_router.task_history`：按任务截取（`history_runs`）后依次展开条目，`during_run` 透传给路由。
- `sent_during_team_round` 标记保留，用途由“排除”改为“把该提示词归入正在进行的任务”。
- 路由契约：`history` 的条目不再保证 `user` / `assistant` 交替；条目新增可选字段 `during_run`。集成工作区的 `ROUTER.md` 与路由替身（日志中以 `steer` 标出）已同步。

### 行为说明与取舍

- “每次运行最后一条回答”依赖于区分“启动任务的提示词”和“发入运行中的提示词”。Agent 模式的标记（`is_supplemental_input`）在既有历史中就存在；Team 模式依赖上一节新增的 `sent_during_team_round`，没有该标记的旧 Team 历史中，运行期间发给 Leader 的消息被当作启动任务的提示词，它之前的阶段性回答会保留。
- 回答位于最后一次发入的提示词之前时（“C、回答、D”，其后该次运行不再有回答），顺序保持为记录顺序，回答不会被挪到 D 之后。
- 发入运行中的提示词本身不触发路由请求（既有约定），只在之后的路由请求中作为历史出现。

### 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-24 新增与改写的用例 | 工作区未提交，基于 `729d4bcf8`；WSL2，Python 3.12，`web-mode-env.sh`，数据目录指向临时目录 | `pytest tests/unit_tests/runtime/test_mode_router_tasks.py tests/unit_tests/runtime/test_session_mode_route.py tests/unit_tests/runtime/test_session_mode_switch.py --no-cov -q` | 391 通过（任务容器 34、路由 88、模式切换 269） | 无 |
| 2026-11-24 受影响范围的用例 | 同上 | 与上一节相同的 70 个测试文件 | 2806 通过，3 跳过，3 失败 | 3 项失败与上一节相同，在已提交代码 `729d4bcf8` 上同样失败 |
| 2026-11-24 真实服务（替身模型） | 真实 Gateway 与 AgentServer，临时数据目录；替身模型；路由替身打印收到的输入 | 经 Gateway WebSocket：Team 提示词 A，结束后 B，结束后 C，C 发出 0.6 秒后发送 D，结束后对该会话发 `mode.route`；另对上一节联调留下的同样内容的会话（版本 1 容器）发 `mode.route` | 两个会话路由收到的输入均为 `prompt@team+answer@team+prompt@team+answer@team+prompt@team+steer@team+answer@team+prompt`；旧会话的容器由版本 1 重建为版本 2 并包含 D | 这次运行中服务端在 C 与 D 之后只记录了一条 Leader 最终回答，因此“移除阶段性回答”只由单元测试覆盖；Agent 模式的“立即发送”未在真实服务上验证；未使用真实模型 |

新增与改写的用例（`test_mode_router_tasks.py`）：分类中两类发入运行中的提示词改为 `prompt` 并断言 `during_run`，新增“发入运行中但无人输入 / 无文本”2 例；组装用例改为断言完整序列，新增“发入运行中的提示词留在任务内且只保留最后一条回答”（含回答记在被发入提示词的请求号下、运行被停止、回答早于发入的提示词三种情形）；新增“旧版本容器被重建”。`test_session_mode_route.py` 的对应用例改为断言完整的 `history`（含 `during_run`）。

## 2026-11-25 修订：条目数量上限，先进先出

### 用户确认的决定（2026-11-25）

- 给容器设一个条目数量上限，“像 LRU 一样”：新的一条进入时，把最早的一条挤出去。没有“使用”这一动作，所以实际是先进先出。
- 用户没有给出数值；默认值取 20（实现时的选择，可在配置中修改，0 表示不限制）。

### 实现

- 配置项 `mode_router.history_items`（默认 20，0 为不限制）替代 `history_runs`（按任务数截取）；后者不再读取。`history_chars`、`query_chars` 不变。
- 条目指路由输入中的一项：一条提示词或一条回答。新提示词（`query`）不计入。
- `mode_router_tasks._keep_latest(rows, limit)`：把组装出的任务从最早一端裁到最多 `limit` 条，可以从任务中间切开（只剩回答，或只剩发入运行中的提示词）。同时给出仍然需要的第一行的位置：被部分保留的任务的全部行都要留下，因为任务的回答要靠启动它的提示词（请求号、模式）才能识别。
- 路由输入与容器文件使用同一上限：
  - `read_session_tasks(session_id, items=...)`（路由请求）在补齐之后裁剪，并把不再需要的行从容器文件中移除（重写文件）；
  - `note_history_append`（历史写入线程）在追加一行后同样裁剪，因此不经过路由请求时容器也不会超过上限。写入线程不解析配置文件，使用最近一次路由请求带来的上限，进程内尚无路由请求时使用默认值。
- 因此容器文件中除了路由会收到的条目，最多还多出“最早那个仍被部分保留的任务”的其余行（启动提示词、阶段性回答）。
- 被移出容器的内容仍在历史文件中。调大上限不会自动找回；删除 `mode_router_tasks.jsonl` 后，下一次路由请求按新上限从历史重建。
- 裁剪只从行的开头移除，最后一行始终保留，容器对历史的覆盖偏移（各行的 `at`）不受影响；没有任何任务、只有无人输入轮次的行时只保留最后一行。

### 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-25 新增与改写的用例 | 工作区未提交，基于 `729d4bcf8`；WSL2，Python 3.12，`web-mode-env.sh`，数据目录指向临时目录 | `pytest tests/unit_tests/runtime/test_mode_router_tasks.py tests/unit_tests/runtime/test_session_mode_route.py tests/unit_tests/runtime/test_session_mode_switch.py --no-cov -q` | 402 通过（任务容器 43、路由 90、模式切换 269） | 无 |
| 2026-11-25 受影响范围的用例 | 同上 | 与前两节相同的 70 个测试文件 | 2817 通过，3 跳过，3 失败 | 3 项失败与前两节相同，在已提交代码 `729d4bcf8` 上同样失败 |
| 2026-11-25 真实服务（替身模型） | 真实 Gateway 与 AgentServer，临时数据目录，配置 `history_items: 3`；替身模型；路由替身打印收到的输入 | 经 Gateway WebSocket：Team 提示词 A、B、C，C 运行中发送 D（7 条条目），随后对该会话发 `mode.route`；再对另一个内容相同的旧会话发 `mode.route` | 路由请求之前容器有 7 行（写入线程按默认上限 20）；路由请求之后容器剩 C、D、回答 3 行；两个会话路由收到的输入均为 `prompt@team+steer@team+answer@team+prompt` | 未使用真实模型；任务被从中间切开的情形（只剩回答）只由单元测试覆盖 |

新增用例（`test_mode_router_tasks.py`）：按上限 0、7、6、5、4、2、1 裁剪同一组行并核对结果与仍需保留的行（7 例）；写入线程单独维持上限；路由请求设定上限并裁剪容器、调大上限不找回、删除后按新上限重建。`test_session_mode_route.py`：可选上限用例改为 `history_items=3`（从任务中间切开），新增默认 20 条用例。

## 2026-11-25 修订（二）：移到 Web 模式切换的提交之上，独立分支

- 前面各节的实现与证据都在 `729d4bcf8`（Web 模式切换分支压缩之前的旧提交）之上、未提交的工作区中完成。Web 模式切换已压缩为一个提交 `79783e732`（只面向 Web，基于较新的上游 develop），对应的分支用于它自己的 PR。
- 用户确认（2026-11-25）：路由相关的全部改动放到独立分支 `feat/web-mode-router`，从 `79783e732` 开始，不进入模式切换与上下文共享两个 PR 分支。
- 做法：把旧工作区的全部改动（40 个文件）作为一个快照提交，在新分支上以三方合并方式套用到 `79783e732`。没有文本冲突。随后的调整：
  - `test_mode_route_answers_only_the_recorded_owner`：新基线上可切换模式的通道只有 Web，原来“取另一个可切换通道”的写法取不到，改为用 `feishu` 作为另一个通道（与模式切换用例一致）。
  - 本文中关于 TUI 的三处表述按新基线改写（`mode.set` 只面向 Web）。
  - 两个生成的索引在新基线上重新生成。

### 实际执行与证据（新基线）

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-25 路由与模式切换用例 | `feat/web-mode-router`，基于 `79783e732`；按上游锁文件构建的虚拟环境；数据目录指向临时目录 | `pytest tests/unit_tests/runtime/test_mode_router_tasks.py tests/unit_tests/runtime/test_session_mode_route.py tests/unit_tests/runtime/test_session_mode_switch.py --no-cov -q` | 254 通过（通道参数只有 Web，因此少于旧基线上的 402） | 无 |
| 2026-11-25 受影响范围的用例 | 同上 | 按同样的关键字在新基线上选出的 70 个测试文件 | 2680 通过，3 跳过，3 失败 | 3 项失败与旧基线上相同，在未改动的 `79783e732` 上同样失败 |
| 2026-11-25 前端 | 同上；Node 22 | `tsc --noEmit`、`vite build`、`npm run test:session-mode-commit`、`test:input-area-permission-merge`、`test:session-input`、`test:chat-store-streaming`、`test:i18n-locales` | 类型检查与构建通过；35/35、137/139、120/121、15/15、6/6 | 3 项失败与变更前相同：企业版输入区锁定、组织 Owner 停止后禁用、MoA 状态路由 |
| 2026-11-25 浏览器整轮与路由输入（替身模型） | 由新分支启动真实 Gateway 与 AgentServer，临时数据目录，`history_items: 20`；替身模型；路由替身；Playwright 无头 Chromium | 打开 `autorun` 地址跑两轮工作流，再发一次 `mode.route`；另经 WebSocket 发送 Team 提示词 A、B、C 与运行中的 D 后发 `mode.route` | 工作流：第一轮路由到 Team 并在新对话中执行，第二轮路由到 Agent、`mode.set` 成功后执行；路由收到的输入依次为 `prompt(1214)`、`prompt@team+answer@team+prompt`、`prompt@team+answer@team+prompt@agent+answer@agent+prompt`；Team 探测为 `prompt+answer+prompt+answer+prompt+steer+answer+prompt` | 未使用真实模型；替身模型不调用工具，Team 未创建成员 |

## 已知遗留

- “自动”只在 Web 提供。
- “自动”设置只保存在浏览器，不随账号或会话同步。
- 路由输入的字段取自约定的契约；真实模型若需要其他输入（例如工具调用记录），需扩展 `recent_runs` 与契约。
- 直接发送路径在路由期间若有非输入区来源的运行开始（计划执行提示、MCP 应用消息），提示词仍会发出，成为对该运行的输入。
