# 协作状态机与门禁

```text
RECEIVED
  → TRIAGED
  → EVIDENCE_CHECKING
      ├─ hash/file failure → NEEDS_EVIDENCE → HUMAN_REVIEW
      └─ verified → CONTRACT_RESOLVING
                       → IMPACT_COMPUTING
                       → INDEPENDENT_VALIDATION
                           ├─ disagree → DISPUTED → HUMAN_REVIEW
                           └─ agree → RECOMMENDED → HUMAN_REVIEW
                                                        ├─ PASS
                                                        ├─ BLOCK
                                                        └─ NEEDS_EVIDENCE
```

## 不可绕过规则

1. `evidence_ids` 和 `tool_run_ids` 为空的结论不能进入 `RECOMMENDED`。
2. Evidence Investigator 报告缺失或哈希不一致时，Impact Worker 不得继续计算。
3. Independent Validator 必须先锁定独立 JSON 结果，再读取主分析结论并做差异比较。
4. Agent 只能给出 `agent_recommendation`；`human_decision` 只能由 Human 角色写入。
5. `PASS` 必须具备证据完整、契约已解析、确定性复算通过、独立复核一致和 Human 签署五个印章。
6. `BLOCK` 也必须保留证据与理由，不允许用“模型认为风险高”作为唯一依据。

## Case-to-Skill 迭代

已确认 Case 不直接修改生产 Skill。Leader 仅生成 `SkillCandidate`：触发条件、失败样例、预期输出、拟议契约差异、回滚版本。随后依次经过黄金 Case 回放、旧 Case 回归、独立 Validator 对比、Owner 审批，最后形成新版本 ZIP。任何一步失败都保持旧版本。

