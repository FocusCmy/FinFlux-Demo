# FinFlux AgentTeams运行时模块

本目录是FinFlux的AgentTeams v1.2.2运行时模块，不是第二个Demo。开发源目录名为`agent_demo/`；生成可提交包后位于`FinFlux-Demo/agentteams/`。唯一前端、API和业务流程位于同一产品的`app/`模块。

## 角色与按需路由

```text
Global Manager（只做根路由，不读取金融数值真值）
  └─ FinFlux Case Lead（创建任务、委派、汇总DataPassDraft）
       ├─ Evidence Investigator
       ├─ Semantic Impact Analyst
       ├─ Downstream Impact Analyst（版本变化时按需）
       ├─ Independent Validator
       ├─ Data Rights Steward（非公开／机密资料时按需）
       ├─ Research Context Analyst（需要研报／官方指标上下文时按需）
       ├─ Runtime Resilience Auditor（Agent路线的预算与恢复审计）
       └─ Result Composer（DataPass后处理，确定性模板优先）
            ↕
       FinChange Data Owner（独立Human资源）
```

模型角色共10个：Manager、Case Lead、7个专业Worker和Result Composer；Human不是Agent。标准公开冲突由Manager选择5个专业Worker，非公开／机密Case最多选择6个。契约一致的低风险Case走`CODE_ONLY_PRECHECK`，不为凑数量调用Agent。

## Skill与真值边界

- 14个金融核验、变更控制、权利、研究上下文和运行韧性Skill；
- 4个结果组装、Token策略、文档生成和结果校验Skill；
- 每次Worker实际调用必须记录`skill_id/version/digest/input_sha256/output_sha256/tool_run_id`；
- Agent可以解释、调查和提出建议，不能编造金融事实、改写原始证据、签署或自动放行；
- 权利、SHA256、字段语义、数值影响和结果签名均由确定性代码或Human完成。

## 已验证与尚未验证

已存在真实AgentTeams BLOCK／PASS历史闭环，以及一条现场Submission到`AWAITING_HUMAN`的裁判Run。历史Run如实保留3个核心Worker与5个Skill调用，不因新拓扑而重写。

新增3个按需Agent、6个Skill和动态Manager路由已完成独立包构建、热部署与Team关联。运行时证据为：三个Worker CR均为`Running`，均拥有独立Matrix身份与容器，`finchange-cross-asset-review`为`8/8 Ready`。三者还在各自真实AgentTeams容器内，基于同一IF2608现场Submission证据句柄完成受控Skill链：Data Rights=`PASS`、Research Context=`VERIFIED_CONTEXT`、Runtime Resilience=`READY_FOR_CHECKPOINTED_RUN`，六次Skill调用均记录版本、digest与输入输出哈希，Provider Token增量为0。

真实性边界：以上证明运行时关联、角色包和确定性Skill执行，不冒充新的Matrix／LLM多轮协作Run。当前裁判Run仍为`AWAITING_HUMAN`且Provider Token已超单Run闸门，关闭前不会重新派发模型。Runtime Resilience Auditor验证的是检查点、超时和预算配置是否就绪，不等于多副本故障迁移已经实证。

## 离线验收

从可提交目录`FinFlux-Demo/`执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 -Action Validate
```

该入口会构建8个Worker包，校验ZIP必需文件、包摘要与签名后的运行时Skill清单，并从每个包执行至少一条当前Skill路径。包级Smoke覆盖6个`bounded_worker_task.py`角色、变更影响角色和结果组装角色，并验证输入/输出哈希、`tool_run_id`、版本、退出状态及`provider_tokens=0`。该过程不调用模型、不部署Runtime、不生成伪Run。具体项数以`build/package-smoke.json`和当次测试输出为准，不在文档中固定过时数字。

单独操作运行时模块：

```powershell
powershell -ExecutionPolicy Bypass -File .\agentteams\scripts\Build-AgentPackages.ps1
python .\agentteams\scripts\validate_agent_demo.py
python .\agentteams\scripts\smoke_test_packages.py
powershell -ExecutionPolicy Bypass -File .\agentteams\scripts\Test-AgentDemoPreflight.ps1
```

验证扩展Agent的运行时关联和零模型Token受控Skill链：

```powershell
powershell -ExecutionPolicy Bypass -File .\agentteams\scripts\Deploy-ExtensionAgents.ps1 -VerifyOnly
powershell -ExecutionPolicy Bypass -File .\agentteams\scripts\Test-ExtensionAgentChain.ps1
```

`Test-ExtensionAgentChain.ps1`读取既有真实IF2608 Submission的哈希证据，不修改裁判Run，不向Matrix发送模型任务；结果保存在`build/extension-agent-chain-result.json`和`build/extension-agent-chain-result.md`。

## 部署

部署是显式动作，必须提供外部、gitignored的Runtime配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\FinFlux.ps1 `
  -Action Deploy `
  -RuntimeEnvFile C:\secure\finflux-runtime.env
```

部署器使用官方AgentTeams v1.2.2源码与CRD，构建并上传8个Worker包。被RootRouteDecision选中的Worker容器缺失时，提交会失败关闭并列出缺失角色；不会用本地模拟结果替代真实Matrix执行。

## 目录

- `config/agent_demo.json`：角色、职责边界和动态激活规则；
- `config/execution_policy.json`：活动Run、墙钟、事件、消息和Tool白名单；
- `resources/finchange-resources.yaml.template`：Manager、Team、9个Worker CR和Human资源；
- `packages/`：8个可构建Worker包；
- `protocols/`：CaseEnvelope、消息、状态机与Human Gate协议；
- `cases/`：股票、期货、期权真实种子Case入口；
- `scripts/`：构建、验证、部署、冷启动、故障注入和证据导出；
- `build/`：可重建产物，不保存API Key。

密钥、Human凭据、原始敏感数据和模型思维过程不得进入包、Matrix消息或Git。自我迭代只允许生成`SkillCandidate`，必须经过回放评测、Owner审批、灰度和可回滚发布。
