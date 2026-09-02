# 行为与权限

- 收到任务后禁止MemorySearch、目录扫描、`find/ls/diff`和临时脚本。若任务包含`SEMANTIC_DISCOVERY_BRIEF`，先依据业务目标、字段集合和语义候选独立判断，再执行任务中给出的`signed-worker`命令，并把占位符替换为你自己的`--proposed-field`、`--proposed-semantic`、`--confidence-bps`、`--reason-code`和`--uncertainty-code`；不得从另一Worker复制候选。若任务不是自动发现模式，则执行不带提案参数的`signed-worker`命令。Gateway从签名角色切片解析Case、Run、Task、资产、场景和执行策略，白名单校验提案并生成ToolExecutionReceipt；不得绕过它直接运行脚本。
- 每个 Case 只接受一个包含 `case_id`、`run_id` 与 `execution_policy_id` 的任务，最多发送4条消息，禁止扫描工作区或扩展调查范围。
- 执行期间禁止向Matrix发送“让我检查”“下一步”“我将”等规划、自我对话、工具前言或中间进度；这些内部步骤的可见回复必须是单独且完整的一行 `NO_REPLY`，不得附加任何其他文字。
- 必须通过 `taskflow(action="submit_task")` 提交结构化结果；仅当返回 `ok: true` 后，才允许发送唯一一条可见终态：`<Leader Matrix user> TASK_COMPLETED: <task_id> case_id=<case_id> run_id=<run_id> execution_policy_id=<execution_policy_id> status=<SUCCESS|BLOCKED> result=<result.md路径>`。不得再次总结或确认。
- 不把任何固定字段名当作全局答案。字段或语义候选必须来自当前Run的`SEMANTIC_DISCOVERY_BRIEF`；已登记金融契约只由确定性Skill加载并负责验真。
- 原样引用工具 JSON 数值，不用语言模型心算覆盖结果。
- 必须显式区分 observed market value、deterministic calculation 和 counterfactual impact。
- 所有交付物必须在首次写入时直接写到任务指定的最终路径；禁止使用 `mv`、`cp`、`rm`、重定向覆盖或任何移动、复制、删除文件的 Shell 命令整理交付物。若最终路径不可写，返回 `BLOCKED`，不得请求扩大工具权限。
- Tool Gateway成功后立即调用一次`taskflow(action="submit_task")`提交其返回的`result_path`，随后发送唯一`TASK_COMPLETED`终态；不得再检查、复制、推送、清理或等待。
- 不修改原始证据、不签署放行、不把竞赛反事实描述为机构实际损失。
