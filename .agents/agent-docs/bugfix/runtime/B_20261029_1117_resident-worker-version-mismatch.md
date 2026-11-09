---
modules:
- jiuwenswarm/runtime
- jiuwenswarm/server/runtime/agent_adapter
designs:
- D-20261028-1704-agent-leader-context-handoff
author: null
author_note: 从恢复补丁导入；原始作者无法从 Git 确认
title: 常驻旧 Worker 导致 AgentServer runtime failed
id: B_20261029_1117_resident-worker-version-mismatch
type: B
files: []
---

# 常驻旧 Worker 导致 AgentServer runtime failed

日期：2026-10-29（America/New_York）。状态：本地工作区服务已恢复。

## 现象与证据

用户启动聊天后收到 `AgentServer runtime failed`。`/home/yanqingshi/cluster/runtime/agent/.logs/agent_server.log`
在 11:10:56 记录 `resident Worker belongs to a different deployment or code version`。
Front 已启动，但重连的是 10 月 26 日留下的常驻 Worker（PID 18207），因此 readiness 进入 FAILED。
这是启动阶段的版本身份拒绝，尚未进入本次对话交接逻辑。

额外核实：原启动器未设置本地 Core 源码路径；venv 中旧 Core 没有
`DeepAgent.set_conversation_guard` 和 `core.context_engine.conversation`。即使只解决旧 Worker，
也还需要配套 Core 才能运行新交接代码。

## 修复与决策

- 修改工作区 `/home/yanqingshi/cluster/start-chat.sh`：将同级 `repos/agent-core` 和
  `repos/jiuwenswarm` 加入 PYTHONPATH，保持启动器、Front 和 Worker 使用配套本地源码。
  不修改发布用 pyproject/uv.lock。
- 使用已有 `stop-chat.sh` 停止这个工作区的旧常驻 Worker，再运行 `start-chat.sh`。
  恢复时前台启动器已退出，因此沿用该工作区旧 Worker 的环境；凭据只在内存中传递，未打印或另存。
- 本次恢复后的服务在后台运行，可用 `stop-chat.sh` 停止。
- 更新工作区 `CHAT.md`：修改 runtime 源码后需完整 stop/start；仅 Ctrl+C 可能保留旧 Worker。

不移除版本身份保护，不删除 Session/checkpoint，不通过绕过握手将新 Front 接到旧 Worker。

## 验证

- 先验证配套本地 Core 的 guard、export/replace API 可导入。
- 原 Worker 退出，新 Worker PID 237749；11:16:47 日志确认 `runtime worker agent_ready`。
- 11:16:59 使用 `X-Jiuwen-Connection-Role: rpc` 只读连接，返回 `readiness=AGENT_READY`、
  `heartbeat_job_ready=true`；该连接不接管已有客户端推送。
- 本次未发送聊天请求，未将服务就绪检查当作真实交接的模型验收。

启动日志：`/home/yanqingshi/cluster/runtime/agent/.logs/runtime-recovery-20261029.log`。
配套实施记录：[F_20261029_0001_agent-leader-context-handoff.md](../../features/F_20261029_0001_agent-leader-context-handoff.md)。
