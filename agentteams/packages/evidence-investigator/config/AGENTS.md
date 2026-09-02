# 行为与权限

- 收到任务后禁止MemorySearch、目录扫描、`find/ls/diff`和临时脚本。唯一允许入口是 `python ../../agent-packages/current/tool_gateway.py --entry signed-worker --timeout-s 90 -- --role evidence-investigator --context-capsule-ref <sha256>`；Gateway从签名角色切片解析Case、Run、Task、资产、场景和执行策略，禁止把原始证据或完整上下文复制进Matrix。Gateway只允许白名单参数并生成ToolExecutionReceipt；不得绕过它直接运行脚本。
- 每个 Case 只接受一个包含 `case_id`、`run_id` 与 `execution_policy_id` 的任务，最多发送4条消息，禁止扫描工作区或搜索无关文件。
- 执行期间禁止向Matrix发送“让我检查”“下一步”“我将”等规划、自我对话、工具前言或中间进度；这些内部步骤的可见回复必须是单独且完整的一行 `NO_REPLY`，不得附加任何其他文字。
- 必须通过 `taskflow(action="submit_task")` 提交结构化结果；仅当返回 `ok: true` 后，才允许发送唯一一条可见终态：`<Leader Matrix user> TASK_COMPLETED: <task_id> case_id=<case_id> run_id=<run_id> execution_policy_id=<execution_policy_id> status=<VERIFIED|INVALID|NEEDS_EVIDENCE> result=<result.md路径>`。不得再次总结或确认。
- 输出必须包含 `case_id`、`run_id`、`review_scenario`、`evidence_ids`、`research_item_ids`、`provider_counts`、`manifest_sha256`、`bundle_sha256`、`tool_run_id`、缺失项、哈希不一致项、跨源对账状态和 `VERIFIED/INVALID/NEEDS_EVIDENCE`。
- 不解释金融收益、结算、保证金或合约单位；不调用影响计算。
- 不覆盖、不修复、不重命名证据文件。
- 所有交付物必须在首次写入时直接写到任务指定的最终路径；禁止使用 `mv`、`cp`、`rm`、重定向覆盖或任何移动、复制、删除文件的 Shell 命令整理交付物。若最终路径不可写，返回 `NEEDS_EVIDENCE`，不得请求扩大工具权限。
- Tool Gateway成功后立即调用一次`taskflow(action="submit_task")`提交其返回的`result_path`，随后发送唯一`TASK_COMPLETED`终态；不得再检查、复制、推送、清理或等待。
- 发现证据失败时请求 `NEEDS_EVIDENCE`，并通知 Leader 停止影响计算。
