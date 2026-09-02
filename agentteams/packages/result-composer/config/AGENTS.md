# 行为与权限

- 只接受包含`run_id`、`submission_id`、DataPass句柄、Human Gate状态和输出目录的结果任务。
- 仅在DataPassDraft生成后创建`preview`；仅在Human决定落盘后创建`final`。
- 按顺序调用`assemble-run-result-context`、`select-token-budget-strategy`、`compose-result-document`和`verify-result-artifact`。
- 默认策略必须是`DETERMINISTIC_TEMPLATE_ONLY`：禁止把Matrix全文、原始研报或原始金融文件重新发送给模型。
- 当前确定性路径的模型调用数和Provider Token必须为0；如果结构化字段缺失，返回`NEEDS_EVIDENCE`，不得让模型猜测。
- 可选文字润色必须由操作员显式开启，输入不超过6000字符、输出不超过350 Token；润色不得改变金融数值、准入建议、Human决定、Run血缘或哈希。
- 输出PDF、Markdown、JSON和Manifest；四个报告Skill都要记录版本、输入/输出SHA256与运行状态。
- `preview`必须显著标注“待人工签署”，不得声称生产授权；`final`必须携带Human责任人、决定时间与签署事件。
- 不修改、不移动、不删除原始证据或已有报告。哈希验证失败时返回`INVALID`并停止。
- 每个任务最多发送一条结构化终态，不发送规划、自我对话或中间进度。
