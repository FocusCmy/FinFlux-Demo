# 行为与权限

- 只接受包含 `change_bundle_id`、`change_set`、`downstream_tasks`、`case_id`、`run_id` 和 `execution_policy_id` 的有界任务。
- 禁止 MemorySearch、目录扫描、开放式检索、金融数值推断和生产签署。
- 唯一允许入口是 `python .qwenpaw/agent-packages/current/tool_gateway.py --entry bounded-change --timeout-s 90 -- ...`；必须原样传入 `--change-payload-b64`。Gateway只允许白名单参数并生成ToolExecutionReceipt；不得绕过它直接运行脚本，也不得手工改写 ChangeSet 或依赖清单。
- 运行时发现并调用 `resolve-downstream-lineage@1.0.0`，在交付物中记录 Skill digest 与输入输出 SHA256。
- 依赖为空时必须输出 `UNKNOWN_IMPACT`；只能对显式依赖未命中的任务输出 `NOT_AFFECTED_BY_DECLARED_DEPENDENCIES`。
- 脚本成功后立即用 `taskflow(action="submit_task")` 提交 `result.md`；唯一可见终态为 `TASK_COMPLETED`，不得发送思维链或中间计划。
- Agent 不拥有生产准入权。发现受影响任务或未知血缘时只能建议重算、隔离或交 Human 复核。
