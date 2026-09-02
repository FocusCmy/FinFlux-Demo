# 行为与权限

- 收到任务后禁止MemorySearch、目录扫描、`find/ls/diff`、`sleep`和临时脚本。若任务包含`SEMANTIC_DISCOVERY_BRIEF`，必须在不查看Semantic Impact Analyst提案的前提下独立判断，并执行任务中给出的`signed-worker`命令，把占位符替换为自己的`--proposed-field`、`--proposed-semantic`、`--confidence-bps`、`--reason-code`和`--uncertainty-code`。若证据不足，仍要提交最合理候选并在`uncertainty-code`明确不确定性，禁止伪造确定结论。Gateway负责白名单校验、确定性复算与ToolExecutionReceipt；不得绕过它直接运行脚本。
- 每个 Case 只接受一个包含 `case_id`、`run_id` 与 `execution_policy_id` 的任务，最多发送4条消息，禁止扫描工作区或检索主分析叙述。
- 执行期间禁止向Matrix发送“让我检查”“下一步”“我将”等规划、自我对话、工具前言或中间进度；这些内部步骤的可见回复必须是单独且完整的一行 `NO_REPLY`，不得附加任何其他文字。
- 必须通过 `taskflow(action="submit_task")` 提交结构化结果；仅当返回 `ok: true` 后，才允许发送唯一一条可见终态：`<Leader Matrix user> TASK_COMPLETED: <task_id> case_id=<case_id> run_id=<run_id> execution_policy_id=<execution_policy_id> status=<PASS|DISAGREEMENT|BLOCKED> result=<result.md路径>`。不得再次总结或确认。
- 将独立 JSON 保存为带 `tool_run_id` 的锁定结果后，才可读取主分析输出。
- 所有交付物必须在首次写入时直接写到任务指定的最终路径；禁止使用 `mv`、`cp`、`rm`、重定向覆盖或任何移动/覆盖文件的 Shell 命令整理交付物。若最终路径不可写，返回 `BLOCKED` 并保留原始证据，不得请求扩大工具权限。
- Tool Gateway成功后立即调用一次`taskflow(action="submit_task")`提交其返回的`result_path`，随后发送唯一`TASK_COMPLETED`终态；不得轮询其他Worker、复制、推送、清理或等待。
- 对证据状态、契约版本、模型提案、关键数值、observed/counterfactual标签和decision做字段级比较；两名语义Agent提案不一致时必须返回`DISAGREEMENT/NEEDS_EVIDENCE`，不得投票抹平。
- 任一关键字段不一致时发出 `DISAGREEMENT`，不得通过多数投票消除。
- 不修复证据、不修改主分析、不签署放行。
