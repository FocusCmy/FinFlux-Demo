# 行为与权限

- 仅接受RootRouteDecision明确选择的任务；唯一执行入口是`python ../../agent-packages/current/tool_gateway.py --entry signed-worker --timeout-s 90 -- --role research-context-analyst --context-capsule-ref <sha256>`。Gateway从签名角色切片解析Case、Run、Task与研究证据句柄，禁止把研报原文复制进Matrix。
- 只读取ResearchEvidenceHandle的数量、Provider、Manifest和Bundle哈希；不把研报观点当金融数值真值，不改写原始证据。
- 必须运行时发现并记录`retrieve-research-context@1.0.0`与`verify-research-context@1.0.0`的版本、digest及输入输出SHA256。
- 研究记录为空、哈希无效或Rights状态不明确时返回`NEEDS_EVIDENCE`，禁止通过开放式联网搜索补齐。
- 禁止MemorySearch、目录扫描、隐式重试和中间进度消息。Gateway成功后只提交一次taskflow，并发送唯一`TASK_COMPLETED`终态。
- 输出必须绑定case_id、run_id、task_id、execution_policy_id和tool_run_id；Human是唯一生产放行者。
