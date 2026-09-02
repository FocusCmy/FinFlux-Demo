# FinFlux 公开证据驱动种子 Case

本目录把现有真实股票、期货和期权证据转成可重复的离线评测语料。它不修改行情、公告或接口原始值；每个冲突样本只改变“拟接入配置”，并以 `counterfactual_configuration=true` 显式标注。

```powershell
cd <FinFlux_demo仓库目录>
python .\evaluation\build_seed_cases.py
python .\evaluation\evaluate_seed_cases.py
```

输出位于 `data/evaluation_seed_cases_v1/`：

- `seed_cases.jsonl`：逐条 Case、原始证据定位、哈希、候选配置及预期路由；
- `manifest.json`：数量、资产类型、来源文件哈希与使用限制；
- `evaluation_report.json`：旧式 schema-only 门禁与 FinFlux Manager 路由策略的离线对照。

`single_agent_model_baseline` 和 `agentteams_end_to_end` 在报告中明确标记为 `NOT_EXECUTED`，避免用规则结果冒充模型或多 Agent 实验结果。公开语料也不等同真实金融机构生产试点。
