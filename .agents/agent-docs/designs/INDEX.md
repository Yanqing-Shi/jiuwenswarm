---
title: 设计文档索引
author: Yanqing-Shi
author_email: toddsyq@gmail.com
generated: true
---

<!-- 本文件由 .agents/skills/jw-design/scripts/generate_index.py 自动生成，请勿手改。重新生成：python .agents/skills/jw-design/scripts/generate_index.py -->
<!-- generated-at: 2026-11-14T15:16+08:00 -->

# 设计文档索引 (INDEX)

按模块目录分组，组内按生命周期展示；跨模块设计在各相关模块下列出。

## 模块目录

| 模块 |
|---|
| [agent-core/openjiuwen/agent_teams/harness](#module-1) |
| [agent-core/openjiuwen/core/context_engine](#module-2) |
| [agent-core/openjiuwen/core/session](#module-3) |
| [agent-core/openjiuwen/harness](#module-4) |
| [agent_teams](#module-5) |
| [agent_teams/workflow](#module-6) |
| [jiuwenswarm](#module-7) |
| [jiuwenswarm/agents/harness/common](#module-8) |
| [jiuwenswarm/channels/web/frontend](#module-9) |
| [jiuwenswarm/gateway/channel_manager/web](#module-10) |
| [jiuwenswarm/observability](#module-11) |
| [jiuwenswarm/runtime](#module-12) |
| [jiuwenswarm/server/runtime/agent_adapter](#module-13) |
| [jiuwenswarm/server/runtime/session](#module-14) |

<a id="module-1"></a>

## agent-core/openjiuwen/agent_teams/harness

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261112-0530-session-conversation-window](proposals/D-20261112-0530-session-conversation-window/design.md) | Session 共享主对话窗口 | draft | Yanqing-Shi | core | 2026-11-14 |

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261028-1704-agent-leader-context-handoff](active/D-20261028-1704-agent-leader-context-handoff/design.md) | 单智能体与团队 Leader 的最小上下文交接 | in-development | 待确认 | core | 2026-11-01 |

<a id="module-2"></a>

## agent-core/openjiuwen/core/context_engine

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261112-0530-session-conversation-window](proposals/D-20261112-0530-session-conversation-window/design.md) | Session 共享主对话窗口 | draft | Yanqing-Shi | core | 2026-11-14 |

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261028-1704-agent-leader-context-handoff](active/D-20261028-1704-agent-leader-context-handoff/design.md) | 单智能体与团队 Leader 的最小上下文交接 | in-development | 待确认 | core | 2026-11-01 |

<a id="module-3"></a>

## agent-core/openjiuwen/core/session

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261112-0530-session-conversation-window](proposals/D-20261112-0530-session-conversation-window/design.md) | Session 共享主对话窗口 | draft | Yanqing-Shi | core | 2026-11-14 |

<a id="module-4"></a>

## agent-core/openjiuwen/harness

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261112-0530-session-conversation-window](proposals/D-20261112-0530-session-conversation-window/design.md) | Session 共享主对话窗口 | draft | Yanqing-Shi | core | 2026-11-14 |

<a id="module-5"></a>

## agent_teams

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0029](proposals/SDD-0029-team-session-runtime-integration/design.md) | Team 模式接入统一 Session Runtime | draft | @zhanglongmin | core | 2026-10-09 |

<a id="module-6"></a>

## agent_teams/workflow

### 开发后 (archive)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0009](archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | shipped | @chenchunzhou | core | 2026-07-28 |
| [SDD-0010](archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | shipped | @chenchunzhou | observability | 2026-08-06 |
| [SDD-0028](archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | swarmflow verify / fork 可观测与前端呈现 | shipped | @xiajing | observability | 2026-09-29 |

<a id="module-7"></a>

## jiuwenswarm

### 开发后 (archive)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0009](archive/SDD-0009-jiuwen-tui-swarmflow-human-session-adaptation/design.md) | jiuwen TUI 适配 SwarmFlow 三算子（human / human_session / agent_session） | shipped | @chenchunzhou | core | 2026-07-28 |
| [SDD-0010](archive/SDD-0010-jiuwen-tui-nested-workflow-and-budget-observability/design.md) | jiuwen TUI 嵌套 Workflow 与 Budget 可观测性 | shipped | @chenchunzhou | observability | 2026-08-06 |
| [SDD-0028](archive/SDD-0028-swarmflow-verify-fork-observability/design.md) | swarmflow verify / fork 可观测与前端呈现 | shipped | @xiajing | observability | 2026-09-29 |

<a id="module-8"></a>

## jiuwenswarm/agents/harness/common

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261112-0530-session-conversation-window](proposals/D-20261112-0530-session-conversation-window/design.md) | Session 共享主对话窗口 | draft | Yanqing-Shi | core | 2026-11-14 |

<a id="module-9"></a>

## jiuwenswarm/channels/web/frontend

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0026](proposals/SDD-0026-trajectory-observability-web-ui/design.md) | 轨迹观测前端：单 Agent、Subagent 与 Team 多 Agent 轨迹 UI | draft | @alan_cheng | observability | 2026-09-15 |

<a id="module-10"></a>

## jiuwenswarm/gateway/channel_manager/web

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0025](proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | draft | @alan_cheng | observability | 2026-09-15 |

<a id="module-11"></a>

## jiuwenswarm/observability

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [SDD-0025](proposals/SDD-0025-trajectory-session-store-and-serving/design.md) | 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API | draft | @alan_cheng | observability | 2026-09-15 |

<a id="module-12"></a>

## jiuwenswarm/runtime

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261112-0530-session-conversation-window](proposals/D-20261112-0530-session-conversation-window/design.md) | Session 共享主对话窗口 | draft | Yanqing-Shi | core | 2026-11-14 |

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261028-1704-agent-leader-context-handoff](active/D-20261028-1704-agent-leader-context-handoff/design.md) | 单智能体与团队 Leader 的最小上下文交接 | in-development | 待确认 | core | 2026-11-01 |

<a id="module-13"></a>

## jiuwenswarm/server/runtime/agent_adapter

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261112-0530-session-conversation-window](proposals/D-20261112-0530-session-conversation-window/design.md) | Session 共享主对话窗口 | draft | Yanqing-Shi | core | 2026-11-14 |

### 开发中 (active)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261028-1704-agent-leader-context-handoff](active/D-20261028-1704-agent-leader-context-handoff/design.md) | 单智能体与团队 Leader 的最小上下文交接 | in-development | 待确认 | core | 2026-11-01 |

<a id="module-14"></a>

## jiuwenswarm/server/runtime/session

### 开发前 (proposals)

| ID | 标题 | 状态 | 作者 | 领域 | 更新日期 |
|---|---|---|---|---|---|
| [D-20261112-0530-session-conversation-window](proposals/D-20261112-0530-session-conversation-window/design.md) | Session 共享主对话窗口 | draft | Yanqing-Shi | core | 2026-11-14 |
