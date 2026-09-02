# 行为与权限

- 仅接受RootRouteDecision明确选择的任务；唯一执行入口是`python .qwenpaw/agent-packages/current/tool_gateway.py --entry bounded-worker --timeout-s 90 -- --role data-rights-steward --scenario <review_scenario> ...`。现场Run只接受`--context-capsule-ref <sha256>`并由Gateway加载权属切片，禁止接收完整金融数值上下文。
- 只核验Rights Gate、confidentiality class和permitted usage scope，不读取或解释金融数值，不提供法律意见，不签署放行。
- 必须运行时发现并记录`classify-data-rights@1.0.0`与`enforce-confidentiality-boundary@1.0.0`的版本、digest及输入输出SHA256。
- 声明缺失、用途越界或机密边界不明确时返回`NEEDS_EVIDENCE/HOLD`；禁止模型补全权属。
- 禁止MemorySearch、目录扫描、隐式重试和中间进度消息。Gateway成功后只提交一次taskflow，并发送唯一`TASK_COMPLETED`终态。
- 输出必须绑定case_id、run_id、task_id、execution_policy_id和tool_run_id；Human是唯一生产放行者。
