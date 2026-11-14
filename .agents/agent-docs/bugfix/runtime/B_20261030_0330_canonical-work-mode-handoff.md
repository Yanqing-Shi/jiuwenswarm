---
modules:
- jiuwenswarm/runtime
- jiuwenswarm/server/runtime/agent_adapter
designs:
- D-20261028-1704-agent-leader-context-handoff
author: null
author_note: 从恢复补丁导入；原始作者无法从 Git 确认
title: 显式 work mode 被 TUI Session code profile 阻断交接
id: B_20261030_0330_canonical-work-mode-handoff
type: B
files: []
---

# 显式 work mode 被 TUI Session code profile 阻断交接

日期：2026-10-30（Asia/Singapore）。

## 现象与证据

TUI Session `tui_1a1883fdfaf_65616a0b188a` 的 metadata 同时有
`work_mode=code`、`mode=team.work.normal`，没有 `conversation_handoff` owner。
用户提供的 context-inputs 日志在 Agent 调用中记录了 `C=17635`；之后第一次
Leader 模型输入只有原 Leader 历史和提问 `What is C`，没有该赋值。用户随后提示
查同一 Session 的 Agent 历史，后续工具结果才出现该值。日志记录模型输入，不能
单独证明具体请求参数或分类分支；metadata 是检查时的会话状态，不是每轮快照。

## 根因与修复

`prepare_chat_turn` 从 Session metadata 读取锁定的 `work_mode`，写入请求，
三段 canonical mode 则保留原有 work 语义。`request_kind` 原先先检查
`work_mode=code` 并返回，故即使 canonical mode 是 `agent.work.normal` 或
`team.work.normal` 也跳过交接。现在显式三段 work mode 优先分类；旧式 `agent`
和 `team` 在 code profile 下仍不进入交接，避免把旧式 code 请求误判为 work。

本问题在该 Session 中是确定性 gate；跨 Session 出现差异取决于保存的 profile，
不能仅由此单份日志推断为随机间歇故障。

## 验证与限制

`JIUWENSWARM_DATA_DIR=/tmp/jiuwenswarm-handoff-tests PYTHONPATH=../agent-core:. .venv/bin/python -m pytest tests/unit_tests/test_context_handoff.py -q -o addopts='' --asyncio-mode=auto --tb=short`：37 passed。
覆盖 work normal/plan 与存储 code 冲突，以及旧式/code 模式排除。尚未用真实模型
在修复后的运行服务中复验；已有 Session 的旧历史不会因分类修复自动追补。
没有 owner 的旧 Session 会先经过 `_check_legacy_owner`，可能拒绝来源不明的
跨执行者切换，或以最后一个执行者建立归属；这两种情况都不会自动合并先前分叉的
Agent 与 Leader checkpoint。
